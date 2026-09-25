"""
数据源双擎：SQLite 直读（主） / HTTP REST（辅）。

与 Emby 版的关键差异
--------------------
EmbyPulse 的 "API 模式" 依赖 Emby 官方 Playback Reporting 插件暴露的
`submit_custom_query` 任意 SQL 穿透接口，因此远端也能拿到完整播放流水。
飞牛影视没有等价能力，其 REST 只覆盖媒体库/任务管理，**播放明细无法穿透**。
故本项目的引擎职责被重新划分：

    SQLite 引擎   直接只读 trimmedia.db —— 播放统计的唯一完整数据源
    HTTP  引擎    仅用于媒体库列表、触发扫描、停止任务等管理动作

未知项处理原则：飞牛库结构随版本演进，凡未能确认的列（如媒体库归属、
软删标记）一律通过 PRAGMA 运行期探测后决定是否启用，绝不硬编码猜测列名。
"""

import os
import shutil
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import cfg

# ---- 飞牛 known schema ----
TBL_USER = "user"
TBL_ITEM = "item"
TBL_PLAY = "item_user_play"

# 每种类型给出多个候选值，兼容不同版本的命名差异
MOVIE_TYPES = ("movie", "film", "Movie")
SERIES_TYPES = ("series", "tvshow", "show", "Series")
EPISODE_TYPES = ("episode", "Episode")

EXCLUDED_USER_GUID = "default-user-template"


class SourceError(Exception):
    """数据源不可用或查询失败。"""


class _SqliteEngine:
    """
    对 trimmedia.db 的安全只读访问。

    为什么必须先复制：飞牛影视服务持续持有该库（通常为 WAL 模式），
    直接连接会与其它进程争锁。做法是把它**原子复制**成一个快照副本，
    再以 `mode=ro` 打开副本；副本带 TTL，到期后下一次查询自动重建。
    这与社区 fntv-record-view 的做法一致，实测可靠。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._copy_at = 0.0
        self._cols_cache: Dict[str, List[str]] = {}

    # ---------- 路径 ----------
    @property
    def src_path(self) -> str:
        return str(cfg.get("fn_db_path", "/fn-data/trimmedia.db"))

    @property
    def snapshot_path(self) -> str:
        base = os.path.dirname(self.src_path) or "."
        # 快照落到项目自己的 config 目录，避免污染只读挂载点
        from app.core.config import CONFIG_DIR
        base = CONFIG_DIR
        return os.path.join(base, "trimmedia_snapshot.db")

    @property
    def ttl(self) -> int:
        try:
            return max(5, int(cfg.get("db_copy_ttl", 60)))
        except (TypeError, ValueError):
            return 60

    # ---------- 快照 ----------
    def _make_snapshot(self) -> None:
        src = self.src_path
        dst = self.snapshot_path
        if not os.path.exists(src):
            raise SourceError(
                f"找不到飞牛影视数据库：{src}。"
                f"请确认已把 /usr/local/apps/@appdata/trim.media/database 挂载进来，"
                f"并在设置页校正 fn_db_path。"
            )

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".building"
        # 1) 主库文件
        shutil.copy2(src, tmp)
        # 2) WAL / SHM 一并带上，否则最多丢一个 checkpoint 周期的数据
        wal, shm = src + "-wal", src + "-shm"
        if os.path.exists(wal):
            try:
                shutil.copy2(wal, tmp + "-wal")
                if os.path.exists(shm):
                    shutil.copy2(shm, tmp + "-shm")
            except OSError:
                pass
        # 3) 原子替换
        os.replace(tmp, dst)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(tmp + suffix):
                os.replace(tmp + suffix, dst + suffix)
        self._copy_at = time.time()

    def _ensure_snapshot(self, force: bool = False) -> None:
        with self._lock:
            expired = (time.time() - self._copy_at) > self.ttl
            if force or expired or not os.path.exists(self.snapshot_path):
                self._make_snapshot()

    def refresh(self) -> None:
        with self._lock:
            self._make_snapshot()

    # ---------- schema 探测 ----------
    def _columns(self, table: str) -> List[str]:
        if table in self._cols_cache:
            return self._cols_cache[table]
        rows = self.query(f"PRAGMA table_info({table})")
        cols = [r["name"] for r in rows]
        self._cols_cache[table] = cols
        return cols

    def has_column(self, table: str, column: str) -> bool:
        try:
            return column in self._columns(table)
        except SourceError:
            return False

    # ---------- 执行 ----------
    def connect(self) -> sqlite3.Connection:
        self._ensure_snapshot()
        conn = sqlite3.connect(
            f"file:{self.snapshot_path}?mode=ro", uri=True, timeout=15, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        return conn

    def query(self, sql: str, args: Tuple = ()) -> List[sqlite3.Row]:
        conn = self.connect()
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:
            # 首次可能因为快照过期/结构变化失败，重建一次再试
            try:
                conn.close()
                self._ensure_snapshot(force=True)
                conn = self.connect()
                return conn.execute(sql, args).fetchall()
            except sqlite3.OperationalError:
                raise SourceError(f"SQL 执行失败：{e}")
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class MediaSource:
    """对上层暴露与媒体服务器无关的统一数据视图。"""

    def __init__(self):
        self.sqlite = _SqliteEngine()

    # ================= 入库闭环用的标题索引 =================
    def library_titles(self) -> Dict[str, str]:
        """
        返回 {小写标题: 展示类型} 的索引，供求片「入库闭环」比对。
        只取真实媒体条目（电影/剧集/单集），体积可控。
        """
        rows = self.sqlite.query(
            f"SELECT LOWER(TRIM(title)) AS t, type AS ty FROM {TBL_ITEM} "
            f"WHERE TRIM(COALESCE(title,'')) <> ''"
        )
        idx: Dict[str, str] = {}
        for r in rows:
            key = (r["t"] or "").strip().lower()
            if key and key not in idx:
                idx[key] = str(r["ty"] or "未知")
        return idx

    # ================= 引擎诊断 =================
    @property
    def mode(self) -> str:
        return cfg.mode

    def engine_info(self) -> Dict[str, Any]:
        """返回引擎可用性诊断，供设置页与首页展示。"""
        info: Dict[str, Any] = {
            "mode": self.mode,
            "sqlite_db_path": cfg.get("fn_db_path", ""),
            "sqlite_ok": False,
            "sqlite_error": "",
            "sqlite_snapshot_at": "",
            "http_ok": False,
            "http_error": "",
        }

        # SQLite
        try:
            path = self.sqlite.src_path
            if not os.path.exists(path):
                info["sqlite_error"] = f"数据库文件不存在：{path}"
            else:
                self.sqlite._ensure_snapshot()
                size = os.path.getsize(path)
                mtime = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))
                )
                info["sqlite_ok"] = True
                info["sqlite_size_mb"] = round(size / 1024 / 1024, 2)
                info["sqlite_mtime"] = mtime
                info["sqlite_snapshot_at"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(self.sqlite._copy_at)
                )
                tbl = self.sqlite.query(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
                    (TBL_USER, TBL_ITEM, TBL_PLAY),
                )
                found = {r["name"] for r in tbl}
                missing = {TBL_USER, TBL_ITEM, TBL_PLAY} - found
                if missing:
                    info["sqlite_ok"] = False
                    info["sqlite_error"] = f"缺少数据表：{', '.join(sorted(missing))}"
        except Exception as e:  # noqa: BLE001
            info["sqlite_error"] = str(e)

        # HTTP
        try:
            from app.core.fn_client import fn_client
            ok, msg = fn_client.test_connection()
            info["http_ok"] = ok
            info["http_error"] = "" if ok else msg
        except Exception as e:  # noqa: BLE001
            info["http_error"] = str(e)

        return info

    # ================= 过滤条件 =================
    def _tz_expr(self, column: str) -> str:
        """把毫秒时间戳列转成 'YYYY-MM-DD HH:MM:SS' 的本地时间文本。"""
        offset = int(cfg.get("timezone_offset_hours", 8) or 8)
        sign = "+" if offset >= 0 else "-"
        return (
            f"datetime({column}/1000, 'unixepoch', "
            f"'{sign}{abs(offset)} hours')"
        )

    def _date_expr(self, column: str) -> str:
        offset = int(cfg.get("timezone_offset_hours", 8) or 8)
        sign = "+" if offset >= 0 else "-"
        return f"date({column}/1000, 'unixepoch', '{sign}{abs(offset)} hours')"

    @staticmethod
    def _hidden_filter(alias: str = "u") -> Tuple[str, list]:
        hidden = cfg.get("hidden_users") or []
        hidden = [h for h in hidden if h]
        if not hidden:
            return "", []
        ph = ",".join("?" * len(hidden))
        return f" AND {alias}.guid NOT IN ({ph})", hidden

    # ================= 用户 =================
    def users(self) -> List[Dict[str, Any]]:
        where_hidden, params = self._hidden_filter("u")
        sql = f"""
            SELECT u.guid, u.username, u.last_login_time, u.is_admin, u.status
            FROM {TBL_USER} u
            WHERE 1=1 {where_hidden}
              AND u.guid <> ?
            ORDER BY u.username
        """
        rows = self.sqlite.query(sql, tuple(params) + (EXCLUDED_USER_GUID,))
        out = []
        for r in rows:
            out.append({
                "guid": r["guid"],
                "username": r["username"],
                "last_login": self._fmt_ms(r["last_login_time"]),
                "last_login_ms": r["last_login_time"],
                "is_admin": bool(r["is_admin"]),
                "status": r["status"],
            })
        return out

    # ================= 时间边界（统一由 Python 按配置时区计算，不依赖容器 TZ） =================
    @staticmethod
    def _offset_seconds() -> int:
        return int(cfg.get("timezone_offset_hours", 8) or 8) * 3600

    @classmethod
    def _local_start_of_day_ms(cls, days_ago: int = 0) -> int:
        """返回本地时区第 N 天前的零点毫秒时间戳。"""
        epoch_ms = int(time.time() * 1000)
        local_ms = epoch_ms + cls._offset_seconds() * 1000
        start = local_ms - (local_ms % 86_400_000) - days_ago * 86_400_000
        return start - cls._offset_seconds() * 1000

    @staticmethod
    def _fmt_ms(ms) -> str:
        if not ms:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ms) / 1000))

    # ================= 概览 =================
    def library_counts(self) -> Dict[str, Any]:
        """媒体条目按类型分布。返回值同时给出原始分布，便于核对类型命名。"""
        rows = self.sqlite.query(
            f"SELECT type AS t, COUNT(*) AS c FROM {TBL_ITEM} GROUP BY type ORDER BY c DESC"
        )
        dist = [{"type": r["t"], "count": r["c"]} for r in rows]

        def total(candidates: Tuple[str, ...]) -> int:
            # 用小写比较，避免不同版本大小写差异导致漏统计
            lowered = {c.lower() for c in candidates}
            return sum(r["count"] for r in dist if str(r["type"] or "").lower() in lowered)

        return {
            "movie": total(MOVIE_TYPES),
            "series": total(SERIES_TYPES),
            "episode": total(EPISODE_TYPES),
            "total": sum(r["count"] for r in dist),
            "distribution": dist,
        }

    def overview(self) -> Dict[str, Any]:
        hidden_where, hidden = self._hidden_filter("u")
        base = f"""
            FROM {TBL_PLAY} p
            JOIN {TBL_USER} u ON p.user_guid = u.guid
            JOIN {TBL_ITEM} i ON p.item_guid = i.guid
            WHERE COALESCE(p.visible, 1) = 1 {hidden_where}
              AND u.guid <> ?
        """
        args = tuple(hidden) + (EXCLUDED_USER_GUID,)

        def scalar(expr: str, extra_args: Tuple = ()) -> Any:
            rows = self.sqlite.query(f"SELECT {expr} AS v {base}", args + extra_args)
            return rows[0]["v"] if rows else 0

        today_start = self._local_start_of_day_ms(0)
        return {
            "total_plays": scalar("COUNT(*)") or 0,
            # iup.ts 单位秒 -> 转小时
            "total_hours": round((scalar("COALESCE(SUM(p.ts),0)") or 0) / 3600, 1),
            "active_users": scalar("COUNT(DISTINCT p.user_guid)") or 0,
            "today_plays": scalar(
                "SUM(CASE WHEN p.update_time >= ? THEN 1 ELSE 0 END)", (today_start,)
            ) or 0,
            "latest_play": self._fmt_ms(
                self._scalar_raw(
                    f"SELECT MAX(p.update_time) AS v {base}", args
                )
            ),
        }

    def _scalar_raw(self, sql: str, args: Tuple) -> Any:
        rows = self.sqlite.query(sql, args)
        return rows[0]["v"] if rows else None

    # ================= 趋势 =================
    def daily_trend(self, days: int = 30) -> List[Dict[str, Any]]:
        days = max(1, min(days, 365))
        start_ms = self._local_start_of_day_ms(days - 1)
        hidden_where, hidden = self._hidden_filter("u")
        date_expr = self._date_expr("p.update_time")
        sql = f"""
            SELECT {date_expr} AS d,
                   COUNT(*) AS plays,
                   COALESCE(SUM(p.ts), 0) AS seconds,
                   COUNT(DISTINCT p.user_guid) AS users
            FROM {TBL_PLAY} p
            JOIN {TBL_USER} u ON p.user_guid = u.guid
            JOIN {TBL_ITEM} i ON p.item_guid = i.guid
            WHERE COALESCE(p.visible,1) = 1 {hidden_where}
              AND u.guid <> ?
              AND p.update_time >= ?
            GROUP BY d
            ORDER BY d
        """
        rows = self.sqlite.query(sql, tuple(hidden) + (EXCLUDED_USER_GUID, start_ms))
        return [
            {
                "date": r["d"],
                "plays": r["plays"],
                "hours": round((r["seconds"] or 0) / 3600, 2),
                "users": r["users"],
            }
            for r in rows
        ]

    # ================= 排行 =================
    def top_items(self, item_type: str = "all", order_by: str = "plays",
                  user_guid: str = "", limit: int = 20) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, 100))
        hidden_where, hidden = self._hidden_filter("u")
        args: List[Any] = list(hidden) + [EXCLUDED_USER_GUID]

        type_filter = ""
        mapping = {
            "movie": MOVIE_TYPES,
            "series": SERIES_TYPES,
            "episode": EPISODE_TYPES,
        }.get(item_type)
        if mapping:
            ph = ",".join("?" * len(mapping))
            type_filter = f" AND LOWER(i.type) IN ({ph})"
            args.extend([m.lower() for m in mapping])

        user_filter = ""
        if user_guid:
            user_filter = " AND p.user_guid = ?"
            args.append(user_guid)

        order_sql = "COUNT(*) DESC" if order_by != "duration" else "COALESCE(SUM(p.ts),0) DESC"

        sql = f"""
            SELECT i.guid AS item_guid, i.title, i.original_title, i.type AS item_type,
                   i.season_number, i.episode_number, i.runtime, i.release_date,
                   COUNT(*) AS plays,
                   COALESCE(SUM(p.ts),0) AS seconds,
                   MAX(p.update_time) AS last_play
            FROM {TBL_PLAY} p
            JOIN {TBL_USER} u ON p.user_guid = u.guid
            JOIN {TBL_ITEM} i ON p.item_guid = i.guid
            WHERE COALESCE(p.visible,1) = 1 {hidden_where}
              AND u.guid <> ? {type_filter} {user_filter}
            GROUP BY i.guid
            ORDER BY {order_sql}
            LIMIT ?
        """
        rows = self.sqlite.query(sql, tuple(args) + (limit,))
        return [
            {
                "item_guid": r["item_guid"],
                "title": r["title"],
                "original_title": r["original_title"],
                "item_type": r["item_type"],
                "season_number": r["season_number"],
                "episode_number": r["episode_number"],
                "release_date": r["release_date"],
                "plays": r["plays"],
                "hours": round((r["seconds"] or 0) / 3600, 2),
                "last_play": self._fmt_ms(r["last_play"]),
            }
            for r in rows
        ]

    def user_activity(self, limit: int = 20) -> List[Dict[str, Any]]:
        hidden_where, hidden = self._hidden_filter("u")
        sql = f"""
            SELECT u.guid, u.username, u.last_login_time,
                   COUNT(p.item_guid) AS plays,
                   COALESCE(SUM(p.ts),0) AS seconds,
                   MAX(p.update_time) AS last_play
            FROM {TBL_USER} u
            LEFT JOIN {TBL_PLAY} p
                   ON p.user_guid = u.guid AND COALESCE(p.visible,1) = 1
            WHERE u.guid <> ? {hidden_where}
            GROUP BY u.guid, u.username
            ORDER BY plays DESC, seconds DESC
            LIMIT ?
        """
        rows = self.sqlite.query(sql, (EXCLUDED_USER_GUID,) + tuple(hidden) + (limit,))
        return [
            {
                "guid": r["guid"],
                "username": r["username"],
                "plays": r["plays"] or 0,
                "hours": round((r["seconds"] or 0) / 3600, 1),
                "last_play": self._fmt_ms(r["last_play"]),
                "last_play_ms": r["last_play"] or 0,
                "last_login": self._fmt_ms(r["last_login_time"]),
            }
            for r in rows
        ]

    # ================= 播放历史 =================
    def play_history(self, user_guid: str = "", search: str = "",
                     start_date: str = "", end_date: str = "",
                     page: int = 1, per_page: int = 25) -> Dict[str, Any]:
        page = max(1, page)
        per_page = max(1, min(per_page, 200))
        offset = (page - 1) * per_page

        hidden_where, hidden = self._hidden_filter("u")
        args: List[Any] = list(hidden) + [EXCLUDED_USER_GUID]
        extra = ""

        if user_guid:
            extra += " AND p.user_guid = ?"
            args.append(user_guid)

        if search:
            extra += " AND (i.title LIKE ? OR i.original_title LIKE ?)"
            args.extend([f"%{search}%", f"%{search}%"])

        if start_date:
            extra += f" AND {self._date_expr('p.update_time')} >= date(?)"
            args.append(start_date)
        if end_date:
            extra += f" AND {self._date_expr('p.update_time')} <= date(?)"
            args.append(end_date)

        base = f"""
            FROM {TBL_PLAY} p
            JOIN {TBL_USER} u ON p.user_guid = u.guid
            JOIN {TBL_ITEM} i ON p.item_guid = i.guid
            WHERE COALESCE(p.visible,1) = 1 {hidden_where}
              AND u.guid <> ? {extra}
        """
        total_rows = self.sqlite.query(
            f"SELECT COUNT(*) AS c {base}", tuple(args)
        )
        total = total_rows[0]["c"] if total_rows else 0

        rows = self.sqlite.query(
            f"""
            SELECT p.item_guid, p.user_guid, p.ts AS position, p.watched,
                   p.create_time, p.update_time, p.resolution,
                   u.username,
                   i.title, i.original_title, i.overview, i.type AS item_type,
                   i.season_number, i.episode_number, i.parent_guid,
                   i.runtime, i.release_date
            {base}
            ORDER BY p.update_time DESC
            LIMIT ? OFFSET ?
            """,
            tuple(args) + (per_page, offset),
        )

        items = []
        for r in rows:
            runtime_sec = (r["runtime"] or 0) * 60  # item.runtime 单位为分钟
            pos = r["position"] or 0
            progress = round(min(100.0, pos / runtime_sec * 100), 1) if runtime_sec else 0.0
            items.append({
                "item_guid": r["item_guid"],
                "user_guid": r["user_guid"],
                "username": r["username"],
                "title": r["title"],
                "original_title": r["original_title"],
                "overview": (r["overview"] or "")[:220],
                "item_type": r["item_type"],
                "resolution": r["resolution"],
                "season_number": r["season_number"],
                "episode_number": r["episode_number"],
                "position": pos,
                "position_text": self._hms(pos),
                "runtime": runtime_sec,
                "runtime_text": self._hms(runtime_sec),
                "progress": progress,
                "watched": bool(r["watched"]),
                "create_time": self._fmt_ms(r["create_time"]),
                "update_time": self._fmt_ms(r["update_time"]),
                "release_date": r["release_date"],
            })

        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
            "data": items,
        }

    @staticmethod
    def _hms(seconds) -> str:
        s = int(seconds or 0)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"

    # ================= 洞察 =================
    def hour_distribution(self) -> List[Dict[str, Any]]:
        """按本地小时统计播放次数，用于分析用户作息。"""
        hidden_where, hidden = self._hidden_filter("u")
        offset = int(cfg.get("timezone_offset_hours", 8) or 8)
        sign = "+" if offset >= 0 else "-"
        sql = f"""
            SELECT CAST(strftime('%H', p.update_time/1000, 'unixepoch',
                                 '{sign}{abs(offset)} hours') AS INTEGER) AS h,
                   COUNT(*) AS plays
            FROM {TBL_PLAY} p
            JOIN {TBL_USER} u ON p.user_guid = u.guid
            JOIN {TBL_ITEM} i ON p.item_guid = i.guid
            WHERE COALESCE(p.visible,1) = 1 {hidden_where}
              AND u.guid <> ?
            GROUP BY h
            ORDER BY h
        """
        rows = self.sqlite.query(sql, tuple(hidden) + (EXCLUDED_USER_GUID,))
        buckets = {r["h"]: r["plays"] for r in rows}
        return [{"hour": h, "plays": buckets.get(h, 0)} for h in range(24)]

    def quality_audit(self) -> Dict[str, Any]:
        """按分辨率/播放设备类型的分布。resolution 字段可能为空，需容忍。"""
        hidden_where, hidden = self._hidden_filter("u")
        sql = f"""
            SELECT COALESCE(NULLIF(p.resolution,''), '未知') AS res,
                   COUNT(*) AS plays
            FROM {TBL_PLAY} p
            JOIN {TBL_USER} u ON p.user_guid = u.guid
            JOIN {TBL_ITEM} i ON p.item_guid = i.guid
            WHERE COALESCE(p.visible,1) = 1 {hidden_where}
              AND u.guid <> ?
            GROUP BY res
            ORDER BY plays DESC
        """
        rows = self.sqlite.query(sql, tuple(hidden) + (EXCLUDED_USER_GUID,))
        dist = [{"resolution": r["res"], "plays": r["plays"]} for r in rows]
        return {"distribution": dist, "total": sum(d["plays"] for d in dist)}

    # ================= 单个媒体的观看情况 =================
    def item_detail(self, item_guid: str) -> Optional[Dict[str, Any]]:
        rows = self.sqlite.query(
            f"""
            SELECT i.guid, i.title, i.original_title, i.overview, i.type AS item_type,
                   i.season_number, i.episode_number, i.runtime, i.release_date,
                   i.parent_guid
            FROM {TBL_ITEM} i WHERE i.guid = ?
            """,
            (item_guid,),
        )
        if not rows:
            return None
        r = rows[0]
        plays = self.sqlite.query(
            f"""
            SELECT u.username, p.ts AS position, p.watched, p.update_time, p.resolution
            FROM {TBL_PLAY} p
            JOIN {TBL_USER} u ON p.user_guid = u.guid
            WHERE p.item_guid = ? AND COALESCE(p.visible,1) = 1
            ORDER BY p.update_time DESC LIMIT 20
            """,
            (item_guid,),
        )
        return {
            "guid": r["guid"],
            "title": r["title"],
            "original_title": r["original_title"],
            "overview": r["overview"],
            "item_type": r["item_type"],
            "season_number": r["season_number"],
            "episode_number": r["episode_number"],
            "release_date": r["release_date"],
            "runtime_minutes": r["runtime"],
            "plays": [
                {
                    "username": p["username"],
                    "position_text": self._hms(p["position"]),
                    "watched": bool(p["watched"]),
                    "update_time": self._fmt_ms(p["update_time"]),
                    "resolution": p["resolution"],
                }
                for p in plays
            ],
        }


media_source = MediaSource()
