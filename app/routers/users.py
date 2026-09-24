"""用户管理接口（飞牛账号只读 + 本地元数据可写）。

重要边界：飞牛影视的账号体系由 trimmedia.db 承载，且不支持安全的 API 写操作，
因此本项目**只提供只读展示 + 本地元数据管理**（备注 / 到期日 / 是否计入统计），
不做账号增改删。这是对数据安全的保守选择，而非功能缺失。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core import database as db
from app.core.apiwrap import guard, ok
from app.core.database import users_meta_upsert
from app.core.media_source import media_source
from app.routers.auth import require_login

router = APIRouter(prefix="/api/users", tags=["users"])


class MetaModel(BaseModel):
    user_guid: str
    username: str = ""
    note: str = ""
    expire_date: str = ""
    is_hidden: bool = False


@router.get("")
@guard
def users(_=Depends(require_login)):
    """用户列表：飞牛账号信息 + 播放活跃度 + 本地元数据。"""
    meta = db.users_meta_all()
    activity = {r["guid"]: r for r in media_source.user_activity(limit=500)}
    rows = media_source.users()

    out = []
    for r in rows:
        m = meta.get(r["guid"], {})
        a = activity.get(r["guid"], {})
        out.append({
            **r,
            "plays": a.get("plays", 0),
            "hours": a.get("hours", 0.0),
            "last_play": a.get("last_play", ""),
            "note": m.get("note", ""),
            "expire_date": m.get("expire_date", "") or "",
            "is_hidden": bool(m.get("is_hidden")),
        })
    return ok(out)


@router.post("/meta")
@guard
def save_meta(data: MetaModel, _=Depends(require_login)):
    users_meta_upsert(
        user_guid=data.user_guid,
        username=data.username,
        note=data.note,
        expire_date=data.expire_date,
        is_hidden=1 if data.is_hidden else 0,
    )
    # is_hidden 同步到统计过滤列表，一处修改两处生效
    from app.core.config import cfg

    hidden = set(cfg.get("hidden_users") or [])
    if data.is_hidden:
        hidden.add(data.user_guid)
    else:
        hidden.discard(data.user_guid)
    cfg.set("hidden_users", sorted(hidden))
    return ok({"user_guid": data.user_guid})
