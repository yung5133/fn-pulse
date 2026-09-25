"""MoviePilot 连接与搜索（管理端）。"""

from fastapi import APIRouter, Depends, Query

from app.core.apiwrap import ok
from app.core.moviepilot_client import MoviePilotError, moviepilot_client
from app.routers.auth import require_login

router = APIRouter(prefix="/api/moviepilot", tags=["moviepilot"])


@router.get("/config")
def mp_config(_=Depends(require_login)):
    return ok({
        "configured": moviepilot_client.is_configured(),
        "host": moviepilot_client.host,
    })


@router.get("/search")
def mp_search(query: str = Query(""), _=Depends(require_login)):
    """在 MoviePilot 里搜媒体，供「下发 MoviePilot」时挑选。"""
    query = (query or "").strip()
    if not query:
        return ok([], configured=moviepilot_client.is_configured(),
                  message="请输入搜索关键词")
    if not moviepilot_client.is_configured():
        return ok([], configured=False,
                  message="未配置 MoviePilot 地址或账号，请在系统设置里填写")
    try:
        rows = moviepilot_client.search_media(query)
    except MoviePilotError as e:
        return ok([], configured=True, message=str(e))
    return ok(rows, configured=True,
              message="" if rows else "MoviePilot 没有搜到匹配条目")


@router.post("/test")
def mp_test(_=Depends(require_login)):
    ok_flag, msg = moviepilot_client.test_connection()
    return ok({"ok": ok_flag, "message": msg})
