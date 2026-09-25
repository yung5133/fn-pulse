"""
求片系统。

设计约束（与 emby-pulse 的关键差异）
------------------------------------
emby-pulse 的求片门户要求用户"用 Emby 账号登录"，因为它能通过
`/Users/AuthenticateByName` 校验密码。飞牛影视做不到这一点：
    * REST 登录需要官方未公开的 authx 签名素材；
    * trimmedia.db 里的口令是自家哈希，格式未确认，不宜依赖。
因此本项目采用**自报身份 + 可选口令**的模式：用户填写飞牛用户名与想看的片名，
管理员在后台审核。定位是"点片箱"，而非强身份系统。

安全边界：求片门户运行在独立端口（默认 10208）上的独立 ASGI 引擎，
路径白名单之外一律 404，无法越权触达后台接口 —— 与 emby-pulse 的物理隔离一致。
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core import database as db
from app.core.apiwrap import ok
from app.core.config import cfg
from app.routers.auth import require_login

router = APIRouter(prefix="/api/requests", tags=["requests"])

# 0 待处理 / 1 已下载 / 2 已入库 / 3 已拒绝
STATUS_TEXT = {0: "待处理", 1: "已下载", 2: "已入库", 3: "已拒绝"}
STATUS_BADGE = {0: "badge-warn", 1: "badge-info", 2: "badge-ok", 3: "badge-err"}


def _row(r) -> dict:
    # db.query 返回的是 sqlite3.Row，它没有 .get()，必须先转成 dict
    d = dict(r)
    status = d.get("status") or 0
    return {
        "id": d["id"],
        "title": d["title"],
        "media_type": d["media_type"],
        "season": d["season"] or 0,
        "year": d["year"] or "",
        "note": d["note"] or "",
        "requester": d["requester"],
        "status": status,
        "status_text": STATUS_TEXT.get(status, "未知"),
        "status_badge": STATUS_BADGE.get(status, "badge"),
        "admin_note": d["admin_note"] or "",
        "tmdb_id": d.get("tmdb_id"),
        "poster_url": d.get("poster_path") or "",   # 列名沿用 poster_path，存的是完整 URL
        "overview": d.get("overview") or "",
        "created_at": d["created_at"],
        "updated_at": d["updated_at"],
    }


# ================= 门户端（无需登录） =================
class SubmitModel(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    requester: str = Field(..., min_length=1, max_length=64)
    media_type: str = "movie"
    season: int = 0
    year: str = ""
    note: str = ""
    passcode: str = ""
    # 来自选片搜索的元数据；手填片名时三者皆空
    external_source: str = ""      # douban / tmdb
    external_id: Optional[int] = None
    poster_url: str = ""           # 海报完整 URL
    overview: str = ""


@router.get("/config")
def portal_config():
    """门户端读取：是否开放、是否需要口令、能否用 TMDB 搜索。"""
    return ok({
        "enabled": bool(cfg.get("request_enabled", True)),
        "need_passcode": bool(cfg.get("request_passcode", "")),
        "search_source": _search_source(),
        "tmdb_enabled": bool(cfg.get("tmdb_api_key", "")),
    })


def _search_source() -> str:
    """选片搜索源。豆瓣无需任何 Key（默认），TMDB 需要自行申请。"""
    src = str(cfg.get("search_source", "douban")).lower()
    return src if src in ("douban", "tmdb") else "douban"


# 统一 UA：豆瓣对无 UA / 爬虫 UA 的请求会拒绝或返回验证页
_DOUBAN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@router.get("/search")
def search(query: str = ""):
    """
    门户端搜索选片，用于"从海报墙选片"。

    数据源由 search_source 决定：
        douban（默认）  movie.douban.com/j/subject_suggest —— 无需任何 Key，
                        国内访问也无墙；缺点是联想接口不给剧情简介与评分
        tmdb            api.themoviedb.org —— 信息更全，但需要 API Key 且国内需代理

    两条路径都失败时返回明确提示而非报错 —— 手填片名的旧路径始终可用。
    """
    query = (query or "").strip()
    if not query:
        return ok([])

    if _search_source() == "tmdb":
        items, message = _search_tmdb(query)
        source = "tmdb"
    else:
        items, message = _search_douban(query)
        source = "douban"

    if message and not items:
        return ok([], search_source=source, message=message)
    return ok(items, search_source=source, message=message or "")


def _search_tmdb(query: str):
    key = cfg.get("tmdb_api_key", "")
    if not key:
        return [], "已切换为 TMDB 搜索但未配置 API Key。可在后台「系统设置」填写，或把搜索源改回豆瓣。"

    proxy = cfg.get("proxy_url", "")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        import requests as _requests
        resp = _requests.get(
            "https://api.themoviedb.org/3/search/multi",
            params={"api_key": key, "query": query, "language": "zh-CN", "page": 1},
            proxies=proxies, timeout=12,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        return [], f"TMDB 查询失败：{e}"

    out = []
    for item in (payload.get("results") or [])[:18]:
        # 只收电影与剧集；person 结果丢弃
        mtype = item.get("media_type")
        if mtype not in ("movie", "tv"):
            continue
        path = item.get("poster_path") or ""
        out.append({
            "external_source": "tmdb",
            "external_id": item.get("id"),
            "media_type": "movie" if mtype == "movie" else "series",
            "title": item.get("title") or item.get("name") or "",
            "year": (item.get("release_date") or item.get("first_air_date") or "")[:4],
            "poster_url": f"https://image.tmdb.org/t/p/w342{path}" if path else "",
            "overview": (item.get("overview") or "")[:300],
            "rating": item.get("vote_average") or 0,
        })
    return out, ""


def _parse_douban_suggest(payload) -> list:
    """
    解析 /j/subject_suggest 的返回。独立成纯函数便于离线测试，
    不发网络请求 —— CI 不依赖豆瓣可用性。
    """
    out = []
    for item in (payload or [])[:18]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        sub_type = (item.get("sub_type") or "").lower()
        img = (item.get("img") or "").strip()
        # 豆瓣返回的是 s_ratio_poster 小图，换成更大的 m 尺寸
        if "s_ratio_poster" in img:
            img = img.replace("s_ratio_poster", "m_ratio_poster")
        out.append({
            "external_source": "douban",
            "external_id": item.get("id"),
            "media_type": "series" if sub_type == "tv" else "movie",
            "title": title,
            "year": str(item.get("year") or ""),
            "poster_url": img,
            "overview": "",      # 联想接口不给简介
            "rating": 0,
        })
    return out


def _search_douban(query: str):
    try:
        import requests as _requests
        resp = _requests.get(
            "https://movie.douban.com/j/subject_suggest",
            params={"q": query, "_sync": 1},
            headers={"User-Agent": _DOUBAN_UA, "Referer": "https://movie.douban.com/"},
            timeout=12,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        return [], f"豆瓣查询失败：{e}"

    items = _parse_douban_suggest(payload)
    if not items:
        return [], "豆瓣没有匹配结果，可直接手填片名。"
    return items, ""


@router.post("/submit")
def submit(data: SubmitModel):
    if not cfg.get("request_enabled", True):
        return {"status": "error", "message": "求片通道当前已关闭"}

    want = (cfg.get("request_passcode") or "").strip()
    if want and data.passcode != want:
        return {"status": "error", "message": "提交口令不正确"}

    media_type = data.media_type if data.media_type in ("movie", "series", "episode") else "movie"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    # tmdb_id 列沿用为"外部条目 ID"，豆瓣条目不写该列（避免语义混淆）
    external_id = data.external_id if data.external_source == "tmdb" else None

    db.execute(
        """INSERT INTO media_requests
           (title, media_type, season, year, note, requester, status, admin_note,
            tmdb_id, poster_path, overview, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, '', ?, ?, ?, ?, ?)""",
        (data.title.strip(), media_type, max(0, int(data.season or 0)),
         (data.year or "").strip()[:16], (data.note or "").strip()[:500],
         data.requester.strip(),
         external_id, (data.poster_url or "").strip()[:400],
         (data.overview or "").strip()[:600],
         now, now),
    )
    return ok({"title": data.title.strip()})


@router.get("/mine")
def mine(requester: str = ""):
    """按用户名查看自己提交过的请求。"""
    requester = (requester or "").strip()
    if not requester:
        return ok([])
    rows = db.query(
        "SELECT * FROM media_requests WHERE requester = ? ORDER BY id DESC LIMIT 50",
        (requester,),
    )
    return ok([_row(r) for r in rows])


# ================= 管理端 =================
@router.get("")
def admin_list(status: Optional[int] = None, limit: int = 100, _=Depends(require_login)):
    sql = "SELECT * FROM media_requests"
    args: tuple = ()
    if status is not None:
        sql += " WHERE status = ?"
        args = (status,)
    sql += " ORDER BY id DESC LIMIT ?"
    args = args + (max(1, min(limit, 500)),)
    rows = db.query(sql, args)
    return ok([_row(r) for r in rows])


class StatusModel(BaseModel):
    status: int
    admin_note: str = ""


@router.post("/{request_id}/status")
def set_status(request_id: int, data: StatusModel, _=Depends(require_login)):
    if data.status not in STATUS_TEXT:
        return {"status": "error", "message": "状态值非法"}
    db.execute(
        "UPDATE media_requests SET status = ?, admin_note = ?, updated_at = ? WHERE id = ?",
        (data.status, (data.admin_note or "")[:500],
         time.strftime("%Y-%m-%d %H:%M:%S"), request_id),
    )
    return ok({"id": request_id, "status_text": STATUS_TEXT[data.status]})


@router.delete("/{request_id}")
def delete_request(request_id: int, _=Depends(require_login)):
    db.execute("DELETE FROM media_requests WHERE id = ?", (request_id,))
    return ok({"id": request_id})


# ================= 入库闭环 =================
@router.post("/check_library")
def check_library(_=Depends(require_login)):
    """
    入库自动闭环：把「待处理/已下载」的求片与飞牛媒体库比对，
    库里已存在的自动置为「已入库」。等价于 emby-pulse 的入库闭环，
    差别在于这里靠直读 trimmedia.db 比对，而不是等 Emby 的 webhook。
    """
    from app.core.media_source import SourceError, media_source

    pending = db.query(
        "SELECT * FROM media_requests WHERE status IN (0, 1) ORDER BY id DESC"
    )
    if not pending:
        return ok({"matched": 0, "scanned": 0})

    try:
        library = media_source.library_titles()
    except SourceError as e:
        return {"status": "error", "message": str(e)}

    matched = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for r in pending:
        d = dict(r)
        title = (d.get("title") or "").strip().lower()
        if not title:
            continue
        hit = library.get(title)
        if not hit:
            continue
        db.execute(
            "UPDATE media_requests SET status = ?, admin_note = ?, updated_at = ? WHERE id = ?",
            (2, f"已在媒体库检测到（{hit}）", now, d["id"]),
        )
        matched += 1
    return ok({"matched": matched, "scanned": len(pending)})
