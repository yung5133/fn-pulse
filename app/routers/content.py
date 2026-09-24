"""内容风云榜接口。"""

from fastapi import APIRouter, Depends, Query

from app.core.apiwrap import guard, ok
from app.core.media_source import media_source
from app.routers.auth import require_login

router = APIRouter(prefix="/api/content", tags=["content"])


@router.get("/top")
@guard
def top_items(
    type: str = Query("all", description="all / movie / series / episode"),
    order: str = Query("plays", description="plays / duration"),
    user_guid: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    _=Depends(require_login),
):
    if type not in ("all", "movie", "series", "episode"):
        type = "all"
    if order not in ("plays", "duration"):
        order = "plays"
    return ok(media_source.top_items(type, order, user_guid, limit))


@router.get("/item/{item_guid}")
@guard
def item_detail(item_guid: str, _=Depends(require_login)):
    data = media_source.item_detail(item_guid)
    if not data:
        return ok(None, found=False)
    return ok(data, found=True)
