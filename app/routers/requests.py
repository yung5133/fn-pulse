"""
求片系统。

门户鉴权（portal_auth_mode）三档，默认 fn：
    fn        必须用**飞牛影视账号登录**，提交人取自会话，不再自报（推荐）
    passcode  只需提交口令（request_passcode），提交人自报
    none      完全开放自报（仅内网/测试用）

fn 档之所以可行：飞牛 REST 的 authx 签名密钥已内置，且
/v/api/v2|v1 的登录接口可以用任意用户自己的账号密码校验，
不需要管理员token —— 与 emby-pulse 用 /Users/AuthenticateByName 同理。

安全边界：求片门户运行在独立端口（默认 10208）上的独立 ASGI 引擎，
路径白名单之外一律 404，无法越权触达后台接口。
"""

import time
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core import database as db
from app.core.apiwrap import ok
from app.core.config import cfg
from app.routers.auth import require_login

router = APIRouter(prefix="/api/requests", tags=["requests"])

# 门户会话在 session 中的键名。与后台的 fn_user 并存于同一 cookie，
# 互不覆盖 —— 同浏览器既是管理员又在门户登录时两者都能保持。
PORTAL_SESSION_KEY = "portal_user"

# 0 待处理 / 1 已下载 / 2 已入库 / 3 已拒绝
STATUS_TEXT = {0: "待处理", 1: "已下载", 2: "已入库", 3: "已拒绝"}
STATUS_BADGE = {0: "badge-warn", 1: "badge-info", 2: "badge-ok", 3: "badge-err"}

AUTH_MODES = ("fn", "passcode", "none")


def auth_mode() -> str:
    m = str(cfg.get("portal_auth_mode", "fn")).lower()
    return m if m in AUTH_MODES else "fn"


def portal_user(request: Request) -> dict:
    """取门户登录态（与后台管理员登录态分开存放）。"""
    u = request.session.get(PORTAL_SESSION_KEY)
    return u if isinstance(u, dict) and u.get("name") else {}


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
        "douban_id": d.get("douban_id") or "",
        "requester_verified": bool(d.get("requester_verified")),
        "poster_url": d.get("poster_path") or "",   # 列名沿用 poster_path，存的是完整 URL
        "overview": d.get("overview") or "",
        "mp_subscribe_id": d.get("mp_subscribe_id"),
        "mp_sent_at": d.get("mp_sent_at") or "",
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
def portal_config(request: Request):
    """门户端读取：是否开放、鉴权档位、是否需要口令、能否用 TMDB 搜索。"""
    mode = auth_mode()
    return ok({
        "enabled": bool(cfg.get("request_enabled", True)),
        "auth_mode": mode,
        "need_passcode": mode == "passcode" and bool(cfg.get("request_passcode", "")),
        "search_source": _search_source(),
        "tmdb_enabled": bool(cfg.get("tmdb_api_key", "")),
        "logged_in": bool(portal_user(request)),
    })


# ================= 门户登录（飞牛账号校验） =================
class PortalLoginModel(BaseModel):
    username: str
    password: str


@router.post("/portal_login")
def portal_login(data: PortalLoginModel, request: Request):
    """
    用飞牛影视账号登录求片门户。

    校验在独立实例上发起，不会影响后台持有的管理员 token。
    """
    if auth_mode() != "fn":
        return JSONResponse(
            {"status": "error", "message": "当前门户未启用飞牛账号登录"}, status_code=400
        )
    username = (data.username or "").strip()
    if not username or not data.password:
        return JSONResponse(
            {"status": "error", "message": "请输入飞牛影视账号和密码"}, status_code=400
        )

    from app.core.fn_client import fn_client

    ok_flag, msg = fn_client.verify_credentials(username, data.password)
    if not ok_flag:
        return JSONResponse({"status": "error", "message": msg}, status_code=401)

    request.session[PORTAL_SESSION_KEY] = {"name": username}
    return ok({"username": username})


@router.post("/portal_logout")
def portal_logout(request: Request):
    request.session.pop(PORTAL_SESSION_KEY, None)
    return ok({"logged_out": True})


@router.get("/me")
def portal_me(request: Request):
    u = portal_user(request)
    return ok({
        "logged_in": bool(u),
        "username": u.get("name", ""),
        "auth_mode": auth_mode(),
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
def submit(data: SubmitModel, request: Request):
    if not cfg.get("request_enabled", True):
        return {"status": "error", "message": "求片通道当前已关闭"}

    mode = auth_mode()
    verified = 0
    requester = (data.requester or "").strip()

    if mode == "fn":
        # 登录态为准，忽略前端自报的用户名 —— 这是该档位的核心价值
        u = portal_user(request)
        if not u:
            return JSONResponse(
                {"status": "error", "message": "请先登录飞牛影视账号"}, status_code=401
            )
        requester = u["name"]
        verified = 1
    elif mode == "passcode":
        want = (cfg.get("request_passcode") or "").strip()
        if want and data.passcode != want:
            return {"status": "error", "message": "提交口令不正确"}

    if not requester:
        return JSONResponse(
            {"status": "error", "message": "缺少提交人"}, status_code=400
        )

    media_type = data.media_type if data.media_type in ("movie", "series", "episode") else "movie"
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    # 外部 ID 分列保存：MP 下发时 tmdb 走 tmdbid，豆瓣走 doubanid
    tmdb_id = data.external_id if data.external_source == "tmdb" else None
    douban_id = str(data.external_id) if data.external_source == "douban" and data.external_id else None

    db.execute(
        """INSERT INTO media_requests
           (title, media_type, season, year, note, requester, requester_verified,
            status, admin_note, tmdb_id, douban_id, poster_path, overview,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?, ?, ?, ?, ?)""",
        (data.title.strip(), media_type, max(0, int(data.season or 0)),
         (data.year or "").strip()[:16], (data.note or "").strip()[:500],
         requester, verified,
         tmdb_id, douban_id, (data.poster_url or "").strip()[:400],
         (data.overview or "").strip()[:600],
         now, now),
    )
    return ok({"title": data.title.strip(), "requester": requester, "verified": bool(verified)})


@router.get("/mine")
def mine(request: Request, requester: str = ""):
    """
    查看自己提交过的请求。

    fn 档位下强制以会话身份为准，不允许通过参数窥探他人记录。
    """
    if auth_mode() == "fn":
        u = portal_user(request)
        requester = u.get("name", "") if u else ""
    else:
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


# ================= MoviePilot 下发 =================
class DispatchModel(BaseModel):
    """下发到 MoviePilot 的参数。media 外部信息由前端选中后回传，避免二次搜索。"""
    media_type: str = "movie"
    external_source: str = ""      # tmdb / douban / other
    external_id: Optional[int] = None
    title: str = ""
    year: str = ""
    season: int = 0
    poster_url: str = ""
    overview: str = ""


@router.post("/{request_id}/dispatch")
def dispatch(request_id: int, data: DispatchModel, _=Depends(require_login)):
    """
    把求片下发为 MoviePilot 订阅。之后 MP 负责搜索下载整理，
    FnPulse 的「检测入库闭环」负责把状态推进到已入库。

    搜索策略：
        * external_source == tmdb 且带 external_id -> 直接用，不搜索
        * 否则按标题在 MP 里搜，用类型 + 年份收敛；
          命中多条时返回 needs_choice 交由前端让管理员挑选
    """
    from app.core.moviepilot_client import MoviePilotError, moviepilot_client

    rows = db.query("SELECT * FROM media_requests WHERE id = ?", (request_id,))
    if not rows:
        return {"status": "error", "message": f"求片 #{request_id} 不存在"}
    req = dict(rows[0])

    if not moviepilot_client.is_configured():
        return {"status": "error",
                "message": "未配置 MoviePilot。请在系统设置里填写地址与账号。"}

    title = (data.title or req.get("title") or "").strip()
    year = (data.year or req.get("year") or "").strip()
    media_type = data.media_type or "movie"
    season = max(0, int(data.season or req.get("season") or 0))
    douban_id = str(req.get("douban_id") or "").strip()

    chosen = None
    if data.external_source == "tmdb" and data.external_id:
        chosen = {
            "media_type": media_type, "title": title, "year": year,
            "external_source": "tmdb", "external_id": data.external_id,
            "poster_url": data.poster_url, "overview": data.overview,
        }
    elif douban_id:
        # 豆瓣来源直接带 doubanid 下发，跳过 MP 搜索 —— MP 原生支持豆瓣订阅。
        # 华语新剧/冷门剧在 MP 的元数据源里常常搜不到，按标题搜索是死路。
        chosen = {
            "media_type": media_type, "title": title, "year": year,
            "external_source": "douban", "douban_id": douban_id,
            "poster_url": data.poster_url or req.get("poster_path") or "",
            "overview": data.overview or req.get("overview") or "",
        }
    else:
        try:
            candidates = moviepilot_client.search_media(title)
        except MoviePilotError as e:
            return {"status": "error", "message": str(e)}

        want_type = "movie" if media_type == "movie" else "series"
        filtered = [x for x in candidates if x.get("media_type") == want_type]
        if year:
            strict = [x for x in filtered if str(x.get("year") or "") == year]
            filtered = strict or filtered
        if len(filtered) == 1:
            chosen = filtered[0]
        elif len(filtered) > 1:
            return ok({
                "needs_choice": True,
                "candidates": filtered[:8],
                "message": f"在 MoviePilot 中找到 {len(filtered)} 个同名条目，请选择",
            })
        else:
            return {"status": "error",
                    "message": (f"MoviePilot 按「{title}」未搜到条目"
                                f"（接口返回 {len(candidates)} 条）。"
                                f"MP 的搜索依赖其元数据源，新剧/冷门内容可能未收录；"
                                f"建议在 MP 中手动搜索确认，或让用户从豆瓣选片重新提交"
                                f"（豆瓣来源可按 ID 直接下发）。")}

    payload = moviepilot_client.build_subscribe_payload(chosen, season=season)
    try:
        ok_flag, msg, sub_id = moviepilot_client.add_subscribe(payload)
    except MoviePilotError as e:
        return {"status": "error", "message": str(e)}

    if not ok_flag:
        return {"status": "error", "message": f"MoviePilot 拒绝订阅：{msg}"}

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    note = f"已下发 MoviePilot 订阅{f' #{sub_id}' if sub_id else ''} · {now}"
    db.execute(
        """UPDATE media_requests
           SET status = 1, admin_note = ?, mp_subscribe_id = ?, mp_sent_at = ?, updated_at = ?
           WHERE id = ?""",
        (note, sub_id, now, now, request_id),
    )
    return ok({
        "id": request_id,
        "mp_subscribe_id": sub_id,
        "chosen": chosen,
        "message": note,
    })


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
