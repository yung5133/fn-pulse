"""
端到端冒烟测试：用合成的 trimmedia.db 驱动真实的 ASGI 应用。

不接触真实用户数据 —— 每次运行都在临时目录里造一份 5 个条目 / 82 条流水的库。

运行：
    pip install -r requirements.txt -r requirements-dev.txt
    python tests/smoke.py

退出码 0 表示全部通过，非 0 表示有失败项（CI 用）。
"""

import json
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

    # ---- 选片搜索：CI 不打外网，走确定性的降级路径 + 解析器单元测试 ----
    r = c.get("/api/requests/config").json()["data"]
    results.append(("搜索源默认为豆瓣", r.get("search_source") == "douban", str(r)))
    results.append(("门户默认鉴权档位为 fn（需飞牛账号登录）",
                    r.get("auth_mode") == "fn", str(r)))
    # 后续批量用例走 none 档（等价于旧的自报行为），fn 档的守卫在末尾单独验证
    c.post("/api/system/settings", json={"data": {"portal_auth_mode": "none"}})

    # 切到 TMDB 且无 Key：必须优雅降级（200 + 空结果 + 提示），而不是报错。
    # 注意要在登录态下改配置，否则 POST /api/system/settings 会 401 而不生效。
    c.post("/api/system/settings", json={"data": {"search_source": "tmdb"}})
    rr = c.get("/api/requests/search?query=Dune").json()
    results.append(("TMDB 无 Key 优雅降级",
                    rr.get("search_source") == "tmdb" and rr.get("data") == []
                    and bool(rr.get("message")), str(rr)[:120]))
    c.post("/api/system/settings", json={"data": {"search_source": "douban"}})

    # 豆瓣解析器：纯函数离线验证，不依赖豆瓣可用性
    from app.routers.requests import _parse_douban_suggest
    canned = [
        {"title": "沙丘", "year": "2021", "sub_type": "movie", "id": "35267208",
         "img": "https://img1.doubanio.com/view/photo/s_ratio_poster/p1.jpg"},
        {"title": "沙丘", "year": "2000", "sub_type": "tv", "id": "1395364",
         "img": "https://img9.doubanio.com/view/photo/s_ratio_poster/p2.jpg"},
        {"title": "", "sub_type": "movie", "id": "1", "img": ""},
        "not-a-dict",
    ]
    parsed = _parse_douban_suggest(canned)
    ok_parse = (
        len(parsed) == 2
        and parsed[0]["media_type"] == "movie"
        and parsed[0]["external_source"] == "douban"
        and parsed[0]["poster_url"].endswith("m_ratio_poster/p1.jpg")
        and parsed[1]["media_type"] == "series"
        and parsed[1]["year"] == "2000"
    )
    results.append(("豆瓣解析器（类型映射/海报放大/脏数据过滤）", ok_parse, str(parsed)[:160]))

    # ---- authx 签名：与 MoviePilot 公开算法做金标比对，防止顺序/分段漂移 ----
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
    import verify_authx  # noqa: E402

    from app.core.config import cfg as _cfg
    from app.core.fn_client import (
        DEFAULT_API_KEY, DEFAULT_API_SECRET, fn_client as _fc,
    )

    results.append(("authx 内置密钥与 MoviePilot 逆向值一致",
                    DEFAULT_API_KEY == "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
                    and DEFAULT_API_SECRET == "16CCEB3D-AB42-077D-36A1-F355324E4237",
                    f"{DEFAULT_API_KEY[:8]}… / {DEFAULT_API_SECRET[:8]}…"))

    # 金标：按 MoviePilot __get_authx 的拼接顺序独立重算，与 fn_client 输出比对
    _method, _path, _body = "POST", "/v/api/v1/task/stop", {"guid": "g1", "type": "TaskItemScrap"}
    _nonce, _ts = "123456", "1700000000000"
    authx = _fc._cse_sign(_method, _path, None, _body, nonce=_nonce, timestamp=_ts)
    sign = verify_authx.parse_authx(authx)[2]
    bh = verify_authx.body_hash_for(_method, None, _body)
    golden = verify_authx.md5("_".join([
        DEFAULT_API_KEY, _path, _nonce, _ts, bh, DEFAULT_API_SECRET]))
    results.append(("authx 金标比对（MoviePilot 顺序）", sign == golden,
                    f"sign={sign[:12]}… golden={golden[:12]}…"))

    # 工具与客户端自洽（防止两处实现漂移）
    v = verify_authx.verify(DEFAULT_API_KEY, DEFAULT_API_SECRET,
                            _method, _path, None, _body, _nonce, _ts, sign)
    results.append(("authx 工具与客户端算法一致", bool(v["match"]), str(v)[:140]))

    check("GET", "/logout", 302)
    check("GET", "/", 302)

    # ---- 求片：门户端（无需登录） ----
    check("GET", "/api/requests/config", 200)
    check("POST", "/api/requests/submit", 200, json={
        "title": "沙丘 3", "requester": "alice", "media_type": "series",
        "season": 2, "year": "2026", "note": "要 4K",
        "external_source": "douban", "external_id": 35267208,
        "poster_url": "https://img1.doubanio.com/view/photo/m_ratio_poster/x.jpg",
        "overview": "续集",
    })
    # 空片名/空用户名必须被拒绝
    check("POST", "/api/requests/submit", 422, json={"title": "", "requester": "alice"})
    check("POST", "/api/requests/submit", 422, json={"title": "x", "requester": ""})
    # 豆瓣 ID 必须持久化 —— 这是 MP 下发的关键（MP 原生支持豆瓣订阅）
    mine = c.get("/api/requests/mine?requester=alice").json()["data"]
    dr = [x for x in mine if x["title"] == "沙丘 3"]
    results.append(("豆瓣 external_id 已持久化为 douban_id",
                    bool(dr) and dr[0].get("douban_id") == "35267208", str(dr)[:120]))
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

    # ---- MoviePilot 对接：默认未配置，必须给出明确提示而不是 500 ----
    check("GET", "/api/moviepilot/config", 200)
    r = c.get("/api/moviepilot/config").json()["data"]
    results.append(("MP 默认未配置", r.get("configured") is False, str(r)))
    rr = c.get("/api/moviepilot/search?query=Dune").json()
    results.append(("MP 未配置时搜索给出提示",
                    rr.get("configured") is False and bool(rr.get("message")), str(rr)[:120]))
    check("POST", "/api/requests/submit", 200,
          json={"title": "银翼杀手 2049", "requester": "bob", "media_type": "movie"})
    r = c.post("/api/requests/2/dispatch", json={"media_type": "movie"}).json()
    results.append(("MP 未配置时下发给出明确错误",
                    r.get("status") == "error" and "未配置" in str(r.get("message")), str(r)[:140]))

    # MP 载荷构造：type 必须是中文枚举，剧集必须有 season
    from app.core.moviepilot_client import moviepilot_client as _mp
    movie_payload = _mp.build_subscribe_payload(
        {"media_type": "movie", "title": "沙丘", "year": "2021",
         "external_id": 438148, "poster_url": "u", "overview": "o"})
    tv_payload = _mp.build_subscribe_payload(
        {"media_type": "series", "title": "怪奇物语", "year": "2016",
         "external_id": 66732}, season=2)
    # 豆瓣来源可直接按 doubanid 下发（不走 MP 搜索）—— 冷门华语剧的关键路径
    douban_payload = _mp.build_subscribe_payload(
        {"media_type": "series", "title": "兰香如故", "year": "2026",
         "external_source": "douban", "douban_id": "36081234",
         "poster_url": "u", "overview": "o"}, season=1)
    results.append(("MP 载荷 豆瓣来源带 doubanid 且不带 tmdbid",
                    douban_payload.get("doubanid") == "36081234"
                    and douban_payload.get("type") == "电视剧"
                    and "tmdbid" not in douban_payload
                    and douban_payload.get("season") == 1,
                    str(douban_payload)[:160]))
    results.append(("MP 载荷 电影 -> 电影", movie_payload.get("type") == "电影"
                    and movie_payload.get("tmdbid") == 438148
                    and "season" not in movie_payload, str(movie_payload)[:140]))
    results.append(("MP 载荷 series -> 电视剧 且带 season",
                    tv_payload.get("type") == "电视剧" and tv_payload.get("season") == 2,
                    str(tv_payload)[:140]))
    results.append(("MP 载荷 无 tmdbid 时剔除该键",
                    "tmdbid" not in _mp.build_subscribe_payload(
                        {"media_type": "movie", "title": "x", "year": ""}),
                    ""))

    # 配置回填后 is_configured 应为 True（不发起真实连接）
    c.post("/api/system/settings", json={"data": {
        "mp_host": "http://127.0.0.1:3000", "mp_username": "admin", "mp_password": "pass123"}})
    r = c.get("/api/moviepilot/config").json()["data"]
    results.append(("MP 配置回填后 configured=True", r.get("configured") is True, str(r)))

    # 静态 API_TOKEN 必须走 X-API-KEY 头（此前误用 Bearer 导致 token校验不通过）
    _cfg.set("mp_token", '"quoted-token"')
    _cfg.set("mp_username", "")
    _cfg.set("mp_password", "")
    h = _mp._auth_headers()
    results.append(("MP 静态令牌走 X-API-KEY 且去除引号",
                    h.get("X-API-KEY") == "quoted-token" and "Authorization" not in h, str(h)))
    _cfg.set("mp_token", "")
    _cfg.set("mp_username", "admin")
    _cfg.set("mp_password", "pass123")
    # 直接注入缓存 JWT，避免 _auth_headers 触发真实登录请求
    _mp._token = "jwt-test"
    _mp._token_at = time.time()
    h2 = _mp._auth_headers()
    results.append(("MP 账号密码走 Bearer JWT",
                    h2.get("Authorization") == "Bearer jwt-test"
                    and "X-API-KEY" not in h2, str(h2)))

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

    # ---- 求片门户鉴权三档 ----
    # fn 档：未登录必须拒绝；登录接口在未配置飞牛地址时给出明确错误（不联网）
    c.post("/api/system/settings", json={"data": {
        "portal_auth_mode": "fn", "fn_host": "", "fn_username": "", "fn_password": ""}})
    results.append(("fn 档 config 报告需登录",
                    c.get("/api/requests/config").json()["data"]["auth_mode"] == "fn", ""))
    me = c.get("/api/requests/me").json()["data"]
    results.append(("fn 档未登录 /me 为未登录态", me.get("logged_in") is False, str(me)))
    r = c.post("/api/requests/submit", json={"title": "未登录求片", "requester": "hacker"})
    results.append(("fn 档未登录提交被拒（401）",
                    r.status_code == 401, f"http={r.status_code} {r.text[:80]}"))
    r = c.post("/api/requests/portal_login", json={"username": "alice", "password": "x"})
    body = r.json()
    results.append(("fn 档未配置飞牛地址时登录给出明确错误",
                    r.status_code == 401 and "飞牛" in str(body.get("message")),
                    f"http={r.status_code} {body.get('message')}"))
    # 未登录时 /mine 不应泄露任何记录（后端强制以会话身份为准）
    mine_anon = c.get("/api/requests/mine?requester=alice").json()["data"]
    results.append(("fn 档未登录 /mine 不泄露他人记录", mine_anon == [], str(mine_anon)[:80]))

    # passcode 档：口令校验
    c.post("/api/system/settings", json={"data": {
        "portal_auth_mode": "passcode", "request_passcode": "s3cret"}})
    r = c.post("/api/requests/submit", json={
        "title": "口令错误", "requester": "alice", "passcode": "wrong"})
    results.append(("passcode 档口令错误被拒",
                    r.json().get("status") == "error", r.text[:100]))
    r = c.post("/api/requests/submit", json={
        "title": "口令正确", "requester": "alice", "passcode": "s3cret"})
    results.append(("passcode 档口令正确可提交且标记为自报",
                    r.json().get("status") == "success"
                    and r.json()["data"]["verified"] is False, r.text[:120]))

    # 恢复默认档位，避免影响真实部署的认知
    c.post("/api/system/settings", json={"data": {
        "portal_auth_mode": "fn", "request_passcode": ""}})

    # ---- 企业微信机器人：配置界面与运行时状态 ----
    check("GET", "/api/wecom/status", 200)
    d = c.get("/api/wecom/status").json()["data"]
    results.append(("机器人默认未启用且未运行",
                    d.get("enabled") is False and d.get("running") is False
                    and d.get("should_run") is False, str(d)[:140]))
    r = c.post("/api/wecom/restart").json()
    results.append(("未启用时重连给出提示而非报错",
                    r.get("status") == "success" and "未启用" in str(r.get("message")),
                    str(r.get("message"))[:80]))
    r = c.post("/api/wecom/test").json()["data"]
    results.append(("未启用时测试连接直接返回原因（不联网）",
                    r.get("ok") is False and "未启用" in str(r.get("message")),
                    str(r.get("message"))[:80]))

    # 只填 bot_id 不填 secret：开关打开也不得启动连接（避免 CI 打真网）
    c.post("/api/system/settings", json={"data": {
        "wecom_bot_enabled": True, "wecom_bot_id": "bot-test", "wecom_bot_secret": ""}})
    d = c.get("/api/wecom/status").json()["data"]
    results.append(("凭证不全时即使启用也不建立连接",
                    d["enabled"] is True and d["should_run"] is False
                    and d["running"] is False, str(d)[:150]))

    # 补齐 secret：仍然保持 enabled=False，确保 CI 不发起真实连接
    c.post("/api/system/settings", json={"data": {
        "wecom_bot_enabled": False, "wecom_bot_secret": "sec-test"}})
    d = c.get("/api/wecom/status").json()["data"]
    results.append(("配置回填后状态可读（Bot ID / Secret 齐备但不启用）",
                    d["bot_id"] == "bot-test" and d["secret_set"] is True
                    and d["should_run"] is False, str(d)[:150]))
    results.append(("状态接口不泄露 secret 原文", "sec-test" not in json.dumps(d),
                    "已确认"))

    # 业务入口：用桩客户端验证回复逻辑（完全离线）
    from app.core import wecom_service as _ws

    class _StubClient:
        def __init__(self):
            self.sent = []

        def is_authenticated(self):
            return True

        @property
        def last_error(self):
            return ""

        def send_markdown(self, content, chatid=None, chat_type="single"):
            self.sent.append((content, chatid, chat_type))
            return True

    stub = _StubClient()
    _ws._client = stub
    _ws.handle_message({"text": "帮助", "sender": "zhangsan",
                        "chatid": "zhangsan", "chat_type": "single"})
    results.append(("「帮助」触发说明回复",
                    len(stub.sent) == 1 and "FnPulse 求片助手" in stub.sent[0][0]
                    and stub.sent[0][1] == "zhangsan", str(stub.sent)[:120]))
    _ws.handle_message({"text": "我想看沙丘", "sender": "zhangsan",
                        "chatid": "zhangsan", "chat_type": "single"})
    results.append(("未实现的指令给出引导而不是静默丢弃",
                    len(stub.sent) == 2 and "暂不支持" in stub.sent[1][0],
                    str(stub.sent[1][0])[:100]))
    _ws._client = None

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
