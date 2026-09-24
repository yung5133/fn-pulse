"""数据洞察接口：作息分布、画质结构、用户画像。"""

import time
from datetime import datetime

from fastapi import APIRouter, Depends

from app.core.apiwrap import guard, ok
from app.core.database import users_meta_all
from app.core.media_source import media_source
from app.routers.auth import require_login

router = APIRouter(prefix="/api/insight", tags=["insight"])


@router.get("/hours")
@guard
def hours(_=Depends(require_login)):
    return ok(media_source.hour_distribution())


@router.get("/quality")
@guard
def quality(_=Depends(require_login)):
    return ok(media_source.quality_audit())


def _badges(activity: dict) -> list:
    """依据播放行为生成趣味勋章。规则简单透明，便于后续扩展。"""
    badges = []
    plays = activity.get("plays") or 0
    hours = activity.get("hours") or 0.0
    last_ts = activity.get("last_play_ms") or 0

    if plays == 0:
        badges.append({"name": "尚未开张", "desc": "还没有任何观影记录", "icon": "🌱"})
        return badges
    if plays >= 100:
        badges.append({"name": "骨灰级影迷", "desc": f"累计播放 {plays} 次", "icon": "🏆"})
    elif plays >= 30:
        badges.append({"name": "资深观众", "desc": f"累计播放 {plays} 次", "icon": "🎖️"})
    if hours >= 100:
        badges.append({"name": "百小时俱乐部", "desc": f"累计观看 {hours} 小时", "icon": "⏳"})
    if last_ts:
        days = (int(time.time() * 1000) - last_ts) / 86400_000
        if days <= 1:
            badges.append({"name": "日更打卡", "desc": "最近 24 小时有观影", "icon": "🔥"})
        elif days >= 90:
            badges.append({"name": "失踪人口", "desc": f"已 {int(days)} 天未见", "icon": "👻"})
    return badges


@router.get("/profiles")
@guard
def profiles(_=Depends(require_login)):
    """用户画像：活跃数据 + 勋章 + 本地备注。"""
    meta = users_meta_all()
    rows = media_source.user_activity(limit=200)
    out = []
    for r in rows:
        m = meta.get(r["guid"], {})
        expire = m.get("expire_date") or ""
        days_left = None
        if expire:
            try:
                days_left = (datetime.strptime(expire, "%Y-%m-%d").date()
                             - datetime.now().date()).days
            except ValueError:
                days_left = None
        out.append({
            **r,
            "note": m.get("note", ""),
            "expire_date": expire,
            "days_left": days_left,
            "is_hidden": bool(m.get("is_hidden")),
            "badges": _badges(r),
        })
    return ok(out)
