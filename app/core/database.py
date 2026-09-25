"""
本项目自身的业务库（与飞牛的 trimmedia.db 物理隔离）。

注意：这里绝不放任何播放数据 —— 播放数据一律从 trimmedia.db 只读获取。
本库只存：用户备注/到期信息、媒体库扫描任务留痕、系统通知。
"""

import os
import sqlite3
import threading
import time

from app.core.config import BIZ_DB_PATH

_LOCK = threading.RLock()


def local_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_SCHEMA_READY = False


def _connect_raw() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(BIZ_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(BIZ_DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema(force: bool = False) -> None:
    """
    幂等建表。所有数据库访问都经由 connect() 触发此处，因此业务表
    不再依赖 FastAPI lifespan 的执行顺序 —— 无论谁先调用都能自愈。
    """
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with _LOCK:
        if _SCHEMA_READY and not force:
            return
        conn = _connect_raw()
        try:
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS admin_credential (
                    id            INTEGER PRIMARY KEY CHECK (id = 1),
                    username      TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at    TEXT,
                    updated_at    TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users_meta (
                    user_guid  TEXT PRIMARY KEY,
                    username   TEXT,
                    note       TEXT DEFAULT '',
                    expire_date TEXT,
                    is_hidden  INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS scan_tasks (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    library_guid TEXT,
                    library_name TEXT,
                    status       TEXT,
                    message      TEXT,
                    created_at   TEXT,
                    finished_at  TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_requests (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       TEXT NOT NULL,
                    media_type  TEXT DEFAULT 'movie',
                    season      INTEGER DEFAULT 0,
                    year        TEXT DEFAULT '',
                    note        TEXT DEFAULT '',
                    requester   TEXT NOT NULL,
                    status      INTEGER DEFAULT 0,
                    admin_note  TEXT DEFAULT '',
                    tmdb_id     INTEGER,
                    poster_path TEXT DEFAULT '',
                    overview    TEXT DEFAULT '',
                    created_at  TEXT,
                    updated_at  TEXT
                )
            """)

            # 老库无损补列（若有版本先于此结构创建过表）
            for _col, _ddl in (
                ("tmdb_id", "ALTER TABLE media_requests ADD COLUMN tmdb_id INTEGER"),
                ("poster_path", "ALTER TABLE media_requests ADD COLUMN poster_path TEXT DEFAULT ''"),
                ("overview", "ALTER TABLE media_requests ADD COLUMN overview TEXT DEFAULT ''"),
                ("mp_subscribe_id", "ALTER TABLE media_requests ADD COLUMN mp_subscribe_id INTEGER"),
                ("mp_sent_at", "ALTER TABLE media_requests ADD COLUMN mp_sent_at TEXT"),
                ("douban_id", "ALTER TABLE media_requests ADD COLUMN douban_id TEXT"),
                ("requester_verified",
                 "ALTER TABLE media_requests ADD COLUMN requester_verified INTEGER DEFAULT 0"),
            ):
                try:
                    cur.execute(_ddl)
                except sqlite3.OperationalError:
                    pass

            cur.execute("""
                CREATE TABLE IF NOT EXISTS sys_notifications (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    type       TEXT,
                    title      TEXT,
                    message    TEXT,
                    is_read    INTEGER DEFAULT 0,
                    action_url TEXT DEFAULT '',
                    created_at TEXT
                )
            """)

            conn.commit()
            _SCHEMA_READY = True
        finally:
            conn.close()


def connect() -> sqlite3.Connection:
    ensure_schema()
    return _connect_raw()


def init_db() -> None:
    """启动入口显式调用，强制确保表结构最新。"""
    ensure_schema(force=True)


# ================= 通用查询 =================
def query(sql: str, args: tuple = ()) -> list:
    with _LOCK:
        conn = connect()
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()


def execute(sql: str, args: tuple = ()) -> None:
    with _LOCK:
        conn = connect()
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()


# ================= 用户元数据 =================
def users_meta_all() -> dict:
    rows = query("SELECT * FROM users_meta")
    return {r["user_guid"]: dict(r) for r in rows}


def users_meta_get(user_guid: str) -> dict:
    rows = query("SELECT * FROM users_meta WHERE user_guid = ?", (user_guid,))
    return dict(rows[0]) if rows else {}


def users_meta_upsert(user_guid: str, username: str = "", note: str = "",
                      expire_date: str = "", is_hidden: int = 0) -> None:
    execute(
        """
        INSERT INTO users_meta (user_guid, username, note, expire_date, is_hidden, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_guid) DO UPDATE SET
            username = excluded.username,
            note = excluded.note,
            expire_date = excluded.expire_date,
            is_hidden = excluded.is_hidden,
            updated_at = excluded.updated_at
        """,
        (user_guid, username, note, expire_date or None, int(is_hidden), local_now(), local_now()),
    )


def users_meta_delete(user_guid: str) -> None:
    execute("DELETE FROM users_meta WHERE user_guid = ?", (user_guid,))


# ================= 扫描任务 =================
def scan_task_add(library_guid: str, library_name: str,
                  status: str, message: str = "") -> int:
    with _LOCK:
        conn = connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO scan_tasks (library_guid, library_name, status, message, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (library_guid, library_name, status, message, local_now()),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def scan_task_list(limit: int = 50) -> list:
    rows = query(
        "SELECT * FROM scan_tasks ORDER BY id DESC LIMIT ?", (limit,)
    )
    return [dict(r) for r in rows]


# ================= 系统通知 =================
def notify_add(ntype: str, title: str, message: str, action_url: str = "") -> None:
    execute(
        "INSERT INTO sys_notifications (type, title, message, action_url, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (ntype, title, message, action_url, local_now()),
    )


def notify_list(limit: int = 30, unread_only: bool = False) -> list:
    sql = "SELECT * FROM sys_notifications"
    if unread_only:
        sql += " WHERE is_read = 0"
    sql += " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in query(sql, (limit,))]


def notify_mark_read(notify_id: int = 0) -> None:
    if notify_id:
        execute("UPDATE sys_notifications SET is_read = 1 WHERE id = ?", (notify_id,))
    else:
        execute("UPDATE sys_notifications SET is_read = 1")
