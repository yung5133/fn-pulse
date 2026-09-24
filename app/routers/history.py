"""播放历史接口。"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query

from app.core.apiwrap import guard, ok
from app.core.media_source import media_source
from app.routers.auth import require_login

router = APIRouter(prefix="/api/history", tags=["history"])

# 可选的时间粒度：近 N 天 / 全部
QUICK_RANGES = {"today": 1, "7d": 7, "30d": 30, "90d": 90}


@router.get("")
@guard
def history(
    user_guid: str = Query(""),
    search: str = Query(""),
    start: str = Query(""),
    end: str = Query(""),
    range_key: str = Query("", description="today / 7d / 30d / 90d"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    _=Depends(require_login),
):
    start_date, end_date = start, end
    if not start_date and range_key in QUICK_RANGES:
        days = QUICK_RANGES[range_key]
        today = datetime.now().date()
        if days == 1:
            start_date = end_date = today.strftime("%Y-%m-%d")
        else:
            start_date = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
            end_date = today.strftime("%Y-%m-%d")

    data = media_source.play_history(
        user_guid=user_guid, search=search,
        start_date=start_date, end_date=end_date,
        page=page, per_page=per_page,
    )
    data["filters"] = {
        "user_guid": user_guid, "search": search,
        "start": start_date, "end": end_date, "range_key": range_key,
    }
    return ok(data)
