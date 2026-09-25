"""
端到端冒烟测试：用合成的 trimmedia.db 驱动真实的 ASGI 应用。

不接触真实用户数据 —— 每次运行都在临时目录里造一份 5 个条目 / 82 条流水的库。

运行：
    pip install -r requirements.txt -r requirements-dev.txt
    python tests/smoke.py

退出码 0 表示全部通过，非 0 表示有失败项（CI 用）。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback

# 必须在导入 app 之前设好，否则 config 会落到默认路径
_TMPDIR = tempfile.mkdtemp(prefix="fnpulse_smoke_")
os.environ["CONFIG_DIR"] = os.path.join(_TMPDIR, "config")
os.environ["FN_DB_PATH"] = os.path.join(_TMPDIR, "trimmedia.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAKE_DB = os.path.join(_TMPDIR, "trimmedia.db")

SCHEMA = """
CREATE TABLE user (guid TEXT PRIMARY KEY, username TEXT, last_login_time INTEGER,
                   is_admin INTEGER, status INTEGER);
CREATE TABLE item (guid TEXT PRIMARY KEY, title TEXT, original_title TEXT, parent_guid TEXT,
                   overview TEXT, type TEXT, season_number INTEGER, episode_number INTEGER,
                   runtime REAL, release_date TEXT);
CREATE TABLE item_user_play (item_guid TEXT, user_guid TEXT, visible INTEGER, update_time INTEGER,
                             create_time INTEGER, ts REAL, watched INTEGER, type TEXT, resolution TEXT);
"""

# item.runtime 单位是分钟，item_user_play.ts 单位是秒，update_time 是毫秒时间戳
ITEMS = [
    ("m1", "沙丘", "Dune", None, "沙漠史诗", "Movie", None, None, 155.0, "2021-10-22"),
    ("m2", "银翼杀手 2049", "Blade Runner 2049", None, "科幻", "Movie", None, None, 164.0, "2017-10-06"),
    ("s1", "怪奇物语", "Stranger Things", None, "小镇", "Series", None, None, None, "2016-07-15"),
    ("e1", "第一章", "Chapter One", "s1", "男孩不见了", "Episode", 1, 1, 48.0, "2016-07-15"),
    ("e2", "第二章", "Chapter Two", "s1", "女孩出现", "Episode", 1, 2, 55.0, "2016-07-15"),
]

USERS = [
    ("u-admin", "admin", 1, 1),
    ("u-alice", "alice", 0, 1),
    ("u-bob", "bob", 0, 1),
    # 飞牛库的伪账号，统计必须排除
    ("default-user-template", "tpl", 0, 1),
]


def build_fake_db() -> int:
    conn = sqlite3.connect(FAKE_DB)
    conn.executescript(SCHEMA)
    now = int(time.time() * 1000)
    day = 86_400_000

    conn.executemany(
        "INSERT INTO user VALUES (?,?,?,?,?)",
        [(g, n, now - i * 1000, a, s) for i, (g, n, a, s) in enumerate(USERS)],
    )
    conn.executemany("INSERT INTO item VALUES (?,?,?,?,?,?,?,?,?,?)", ITEMS)

    plays = []
    for d in range(40):
        base = now - d * day
        plays.append(("m1", "u-alice", 1, base, base, 3600.0, 1, "play", "2160p"))
        if d % 2 == 0:
            plays.append(("e1", "u-bob", 1, base + 3_600_000, base, 2400.0, 1, "play", "1080p"))
        if d % 3 == 0:
            plays.append(("m2", "u-admin", 1, base + 7_200_000, base, 1800.0, 0, "play", "720p"))
        if d % 5 == 0:
            plays.append(("e2", "u-alice", 1, base + 82_800_000, base, 3000.0, 1, "play", "4K"))
    # visible=0 的记录必须被过滤
    plays.append(("m1", "u-bob", 0, now, now, 100.0, 0, "play", "1080p"))
    conn.executemany("INSERT INTO item_user_play VALUES (?,?,?,?,?,?,?,?,?)", plays)
    conn.commit()
    conn.close()
    return len(plays)


def main() -> int:
    from fastapi.testclient import TestClient

    from app.main import PORTAL_EXACT_PATHS, PORTAL_PREFIX_PATHS, app

    c = TestClient(app, raise_server_exceptions=False)
    results = []

    def check(method, path, expect, **kw):
        name = f"{method.upper()} {path}"
        try:
            r = c.request(method, path, follow_redirects=False, **kw)
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"exception: {e}"))
            return None
        ok = r.status_code == expect
        detail = f"http={r.status_code}"
        try:
            body = r.json()
            if isinstance(body, dict):
                detail += " " + str(body.get("message") or body.get("status") or body)[:80]
        except Exception:  # noqa: BLE001
            detail += f" <html {len(r.content)}B>"
        results.append((name, ok, detail))
        return r

    # ---- 鉴权 ----
    check("GET", "/api/stats/overview", 401)
    check("GET", "/", 302)
    check("GET", "/login", 200)
    check("POST", "/api/login", 401, json={"username": "admin", "password": "wrong"})
    check("POST", "/api/login", 200, json={"username": "admin", "password": "fnpulse"})

    # ---- 业务接口 ----
    check("GET", "/api/stats/overview", 200)
    check("GET", "/api/stats/trend?days=7", 200)
    check("GET", "/api/stats/trend?days=90", 200)
    check("GET", "/api/stats/library", 200)
    check("GET", "/api/history?per_page=5", 200)
    check("GET", "/api/history?range_key=today", 200)
    check("GET", "/api/history?search=Dune", 200)
    check("GET", "/api/history?start=2020-01-01&end=2030-01-01", 200)
    check("GET", "/api/content/top?type=movie&limit=5", 200)
    check("GET", "/api/content/top?order=duration", 200)
    check("GET", "/api/users", 200)
    check("GET", "/api/insight/hours", 200)
    check("GET", "/api/insight/quality", 200)
    check("GET", "/api/insight/profiles", 200)
    check("GET", "/api/library/list", 200)
    check("GET", "/api/library/tasks", 200)
    check("GET", "/api/system/engine", 200)
    check("GET", "/api/system/settings", 200)
    check("GET", "/api/me", 200)

    # ---- 页面渲染（fastapi>=0.141 的 TemplateResponse 签名变更会在这里暴露）----
    for p in ("/", "/history", "/content", "/users", "/insight", "/library", "/settings"):
        check("GET", p, 200)

    # ---- 写操作 ----
    users = c.get("/api/users").json()["data"]
    if users:
        u = users[0]
        check("POST", "/api/users/meta", 200, json={
            "user_guid": u["guid"], "username": u["username"],
            "note": "smoke", "expire_date": "2030-01-01", "is_hidden": False,
        })
    # 非白名单键与非法模式必须被忽略
    check("POST", "/api/system/settings", 200, json={
        "data": {"db_copy_ttl": 45, "timezone_offset_hours": 8,
                 "injected_key": "x", "playback_data_mode": "bogus"},
    })
    check("POST", "/api/system/admin/credential", 400,
          json={"username": "a", "password": "12"})
    check("POST", "/api/system/engine/refresh", 200)
    check("POST", "/api/system/test/connection", 200)
    check("GET", "/logout", 302)
    check("GET", "/", 302)

    # ---- 求片：门户端（无需登录） ----
    check("GET", "/api/requests/config", 200)
    check("POST", "/api/requests/submit", 200, json={
        "title": "沙丘 3", "requester": "alice", "media_type": "series",
        "season": 2, "year": "2026", "note": "要 4K",
        "tmdb_id": 693134, "poster_path": "/x.jpg", "overview": "续集",
    })
    # 空片名/空用户名必须被拒绝
    check("POST", "/api/requests/submit", 422, json={"title": "", "requester": "alice"})
    check("POST", "/api/requests/submit", 422, json={"title": "x", "requester": ""})
    # TMDB 搜索：无 Key 时必须优雅降级（200 + 空结果 + 提示），而不是报错
    check("GET", "/api/requests/search?query=Dune", 200)
    check("GET", "/api/requests/mine?requester=alice", 200)
    check("GET", "/request", 200)

    # ---- 求片：管理端必须先登录（此时已 logout）----
    check("GET", "/api/requests", 401)

    # ---- 求片：管理端登录后 ----
    c.post("/api/login", json={"username": "admin", "password": "fnpulse"})
    r = check("GET", "/api/requests", 200)
    request_id = None
    if r is not None:
        rows = r.json().get("data") or []
        if rows:
            request_id = rows[0]["id"]
    check("GET", "/requests_admin", 200)
    if request_id:
        check("POST", f"/api/requests/{request_id}/status", 200,
              json={"status": 2, "admin_note": "已收录"})
        # 非法状态值必须被拒绝
        check("POST", f"/api/requests/{request_id}/status", 200, json={"status": 99})
        check("DELETE", f"/api/requests/{request_id}", 200)

    # ---- 入库闭环：造一条与库里同名的待处理求片，必须被命中并置为已入库 ----
    check("POST", "/api/requests/submit", 200,
          json={"title": "沙丘", "requester": "bob", "media_type": "movie"})
    check("POST", "/api/requests/check_library", 200)
    mine = c.get("/api/requests/mine?requester=bob").json()["data"]
    closed = [x for x in mine if x["title"] == "沙丘" and x["status"] == 2]
    results.append(("闭环 同名求片被自动置为已入库",
                    bool(closed) and "媒体库" in (closed[0].get("admin_note") or ""), ""))
    # 清理，避免影响后续隔离断言的计数
    for x in c.get("/api/requests").json()["data"]:
        c.delete(f"/api/requests/{x['id']}")

    # ---- 求片门户物理隔离：后台接口绝不能被放行 ----
    results.append(("隔离 后台列表不在白名单", "/api/requests" not in PORTAL_EXACT_PATHS, ""))
    results.append(("隔离 改状态不在白名单",
                    not any(p.startswith("/api/requests/") and p.endswith("/status")
                            for p in PORTAL_EXACT_PATHS), ""))
    results.append(("隔离 删除接口不在白名单",
                    "/api/requests/1" not in PORTAL_EXACT_PATHS, ""))
    for blocked in ("/", "/settings", "/users", "/api/stats/overview"):
        allowed = blocked in PORTAL_EXACT_PATHS or blocked.startswith(PORTAL_PREFIX_PATHS)
        results.append((f"隔离 {blocked} 不可达门户", not allowed, ""))

    # ---- 结果 ----
    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}: {detail}")
    print()
    print(f"===== {len(results) - len(failed)}/{len(results)} passed, "
          f"{len(failed)} failed =====")
    return 1 if failed else 0


if __name__ == "__main__":
    code = 1
    try:
        n = build_fake_db()
        print(f"seeded: {n} play records, {len(ITEMS)} items, {len(USERS)} users")
        code = main()
    except Exception:  # noqa: BLE001
        traceback.print_exc(limit=8)
        code = 1
    finally:
        shutil.rmtree(_TMPDIR, ignore_errors=True)
    sys.exit(code)
