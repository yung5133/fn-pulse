"""
生成静态预览快照，用于人工核对新样式（不参与运行）。

用法：python tools/make_preview.py
产出：preview/*.html —— 直接把 /static 指向仓库内的真实样式表，
      可直接在浏览器打开或在本项目预览面板里查看。
"""

import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="fnpulse_preview_")
os.environ["CONFIG_DIR"] = os.path.join(TMP, "config")
os.environ["FN_DB_PATH"] = os.path.join(TMP, "trimmedia.db")

FAKE_DB = os.path.join(TMP, "trimmedia.db")

SCHEMA = """
CREATE TABLE user (guid TEXT PRIMARY KEY, username TEXT, last_login_time INTEGER,
                   is_admin INTEGER, status INTEGER);
CREATE TABLE item (guid TEXT PRIMARY KEY, title TEXT, original_title TEXT, parent_guid TEXT,
                   overview TEXT, type TEXT, season_number INTEGER, episode_number INTEGER,
                   runtime REAL, release_date TEXT);
CREATE TABLE item_user_play (item_guid TEXT, user_guid TEXT, visible INTEGER, update_time INTEGER,
                             create_time INTEGER, ts REAL, watched INTEGER, type TEXT, resolution TEXT);
"""

ITEMS = [
    ("m1", "沙丘", "Dune", None, "沙漠史诗", "Movie", None, None, 155.0, "2021-10-22"),
    ("m2", "银翼杀手 2049", "Blade Runner 2049", None, "科幻", "Movie", None, None, 164.0, "2017-10-06"),
    ("m3", "奥本海默", "Oppenheimer", None, "传记", "Movie", None, None, 180.0, "2023-07-21"),
    ("s1", "怪奇物语", "Stranger Things", None, "小镇怪谈", "Series", None, None, None, "2016-07-15"),
    ("e1", "第一章：威尔失踪", "Chapter One", "s1", "男孩不见了", "Episode", 1, 1, 48.0, "2016-07-15"),
    ("e2", "第二章：枫树街的怪女孩", "Chapter Two", "s1", "女孩出现", "Episode", 1, 2, 55.0, "2016-07-15"),
]
USERS = [
    ("u-admin", "admin", 1, 1),
    ("u-alice", "alice", 0, 1),
    ("u-bob", "bob", 0, 1),
    ("u-carol", "carol", 0, 1),
    ("default-user-template", "tpl", 0, 1),
]


def build_db():
    conn = sqlite3.connect(FAKE_DB)
    conn.executescript(SCHEMA)
    now = int(time.time() * 1000)
    day = 86_400_000
    conn.executemany("INSERT INTO user VALUES (?,?,?,?,?)",
                     [(g, n, now - i * 1000, a, s) for i, (g, n, a, s) in enumerate(USERS)])
    conn.executemany("INSERT INTO item VALUES (?,?,?,?,?,?,?,?,?,?)", ITEMS)
    plays = []
    for d in range(45):
        base = now - d * day
        plays.append(("m1", "u-alice", 1, base, base, 5400.0, 1, "play", "2160p"))
        if d % 2 == 0:
            plays.append(("e1", "u-bob", 1, base + 3_600_000, base, 2400.0, 1, "play", "1080p"))
        if d % 3 == 0:
            plays.append(("m2", "u-carol", 1, base + 7_200_000, base, 1800.0, 0, "play", "720p"))
        if d % 5 == 0:
            plays.append(("e2", "u-alice", 1, base + 82_800_000, base, 3000.0, 1, "play", "4K"))
        if d % 7 == 0:
            plays.append(("m3", "u-admin", 1, base + 90_000_000, base, 7200.0, 1, "play", "1080p"))
    conn.executemany("INSERT INTO item_user_play VALUES (?,?,?,?,?,?,?,?,?)", plays)
    conn.commit()
    conn.close()


def main():
    build_db()
    from fastapi.testclient import TestClient

    from app.core import database as db
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    client.post("/api/login", json={"username": "admin", "password": "fnpulse"})

    # 造几条求片，让后台列表有内容
    for t, mt, s, y, req in (
        ("兰香如故", "series", 1, "2026", "alice"),
        ("沙丘：第二部", "movie", 0, "2024", "bob"),
        ("怪奇物语", "series", 5, "2022", "carol"),
        ("流浪地球 3", "movie", 0, "2027", "alice"),
    ):
        client.post("/api/requests/submit",
                    json={"title": t, "requester": req, "media_type": mt, "season": s, "year": y})
    rows = client.get("/api/requests").json()["data"]
    for i, r in enumerate(rows):
        if i == 2:
            client.post(f"/api/requests/{r['id']}/status",
                        json={"status": 2, "admin_note": "已在媒体库检测到（Series）"})
        elif i == 3:
            client.post(f"/api/requests/{r['id']}/status",
                        json={"status": 3, "admin_note": "版权原因暂不上架"})

    db.scan_task_add("lib-1", "电影", "success", "扫描完成，新增 3 个条目")

    out_dir = os.path.join(ROOT, "preview")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    pages = {
        "dashboard.html": "/",
        "history.html": "/history",
        "content.html": "/content",
        "users.html": "/users",
        "insight.html": "/insight",
        "library.html": "/library",
        "requests_admin.html": "/requests_admin",
        "settings.html": "/settings",
    }

    written = []
    for name, path in pages.items():
        resp = client.get(path)
        html = resp.text
        # 预览文件放在 preview/ 下，用相对路径指回真实样式表；
        # 去掉会发请求的 echarts CDN，改用本地兜底条形图。
        html = html.replace('href="/static/css/app.css"', 'href="../static/css/app.css"')
        html = re.sub(r'<script src="https://cdn\.jsdelivr\.net[^"]*"[^>]*></script>', "", html)
        html = html.replace("<body>", '<body data-preview="1">')
        dest = os.path.join(out_dir, name)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(html)
        written.append((name, resp.status_code))

    # 登录页与求片门户（公开页）
    for name, path in (("login.html", "/login"), ("portal.html", "/request")):
        resp = client.get(path)
        html = resp.text.replace('href="/static/css/app.css"', 'href="../static/css/app.css"')
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(html)
        written.append((name, resp.status_code))

    print("预览已生成：")
    for n, code in written:
        print(f"  preview/{n}  (HTTP {code})")
    print(f"\n可直接打开：{os.path.join(out_dir, 'dashboard.html')}")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
