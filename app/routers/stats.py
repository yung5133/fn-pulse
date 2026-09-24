"""仪表盘统计接口。"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request

from app.core.apiwrap import guard, ok
from app.core.media_source import media_source
from app.routers.auth import require_login

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/overview")
@guard
def overview(_=Depends(require_login)):
    data = {
        **media_source.overview(),
        **media_source.library_counts(),
    }
    return ok(data)


@router.get("/trend")
@guard
def trend(days: int = 30, _=Depends(require_login)):
    data = media_source.daily_trend(days)
    # 补齐中间空缺的日期，避免前端折线图断点
    if data:
        start = datetime.strptime(data[0]["date"], "%Y-%m-%d")
        end = datetime.strptime(data[-1]["date"], "%Y-%m-%d")
        filled = {r["date"]: r for r in data}
        series = []
        cur = start
        while cur <= end:
            key = cur.strftime("%Y-%m-%d")
            row = filled.get(key) or {"date": key, "plays": 0, "hours": 0.0, "users": 0}
            row["date"] = key
            series.append(row)
            cur += timedelta(days=1)
        data = series
    return ok(data)


@router.get("/library")
@guard
def library(_=Depends(require_login)):
    return ok(media_source.library_counts())
