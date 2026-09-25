"""系统设置与引擎诊断。"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.apiwrap import ok
from app.core.config import DEFAULT_CONFIG, PORT, cfg
from app.core.fn_client import fn_client
from app.core.media_source import SourceError, media_source
from app.routers.auth import (
    ensure_admin_seeded,
    get_admin_credential,
    require_login,
    set_admin_credential,
)

router = APIRouter(prefix="/api/system", tags=["system"])

# 允许通过接口修改的配置键（白名单，避免任意写盘）
WRITABLE_KEYS = {
    "fn_host", "fn_username", "fn_password", "fn_app_name",
    "fn_api_key", "fn_api_secret", "fn_public_url",
    "playback_data_mode", "fn_db_path", "db_copy_ttl",
    "hidden_users", "timezone_offset_hours",
    "tmdb_api_key", "proxy_url", "webhook_token",
    "request_enabled", "portal_auth_mode", "request_passcode", "search_source",
    "mp_host", "mp_username", "mp_password", "mp_token",
}


@router.get("/settings")
def get_settings(_=Depends(require_login)):
    data = {k: cfg.get(k) for k in WRITABLE_KEYS}
    cred = get_admin_credential()
    data["admin_username"] = cred.get("username", "")
    return ok(data)


class SettingsModel(BaseModel):
    data: dict


@router.post("/settings")
def save_settings(body: SettingsModel, _=Depends(require_login)):
    incoming: dict = body.data or {}
    updates: dict[str, Any] = {}
    for k, v in incoming.items():
        if k not in WRITABLE_KEYS:
            continue
        if k == "playback_data_mode":
            v = str(v).lower()
            if v not in ("sqlite", "api"):
                continue
        if k in ("db_copy_ttl", "timezone_offset_hours"):
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        if k == "hidden_users":
            if not isinstance(v, list):
                continue
            v = [str(x) for x in v if str(x).strip()]
        if k == "request_enabled":
            v = bool(v)
        if k == "request_passcode":
            v = str(v).strip()
        if k == "portal_auth_mode":
            v = str(v).lower()
            if v not in ("fn", "passcode", "none"):
                continue
        if k == "search_source":
            v = str(v).lower()
            if v not in ("douban", "tmdb"):
                continue
        updates[k] = v

    if updates:
        cfg.update_many(updates)
        # 数据库源切换后强制重建快照
        if "fn_db_path" in updates or "db_copy_ttl" in updates:
            media_source.sqlite._cols_cache.clear()

    return ok({"updated": sorted(updates.keys())})


class AdminCredentialModel(BaseModel):
    username: str
    password: str


@router.post("/admin/credential")
def update_admin_credential(body: AdminCredentialModel, _=Depends(require_login)):
    if not body.username.strip() or len(body.password) < 6:
        return JSONResponse(
            {"status": "error", "message": "用户名不能为空，且密码至少 6 位"}, status_code=400
        )
    set_admin_credential(body.username.strip(), body.password)
    return ok({"username": body.username.strip()})


@router.get("/engine")
def engine_info(_=Depends(require_login)):
    """双擎健康诊断。"""
    return ok(media_source.engine_info(), port=PORT)


@router.post("/engine/refresh")
def refresh_snapshot(_=Depends(require_login)):
    """强制重建 SQLite 快照（数据看起来滞后时用）。"""
    try:
        media_source.sqlite.refresh()
        return ok({"message": "快照已重建"})
    except SourceError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=503)


@router.post("/test/connection")
def test_connection(_=Depends(require_login)):
    """分别测试 HTTP 引擎与 SQLite 引擎。"""
    result = {"http": None, "sqlite": None}
    ok_http, msg = fn_client.test_connection()
    result["http"] = {"ok": ok_http, "message": msg}

    try:
        rows = media_source.sqlite.query(
            "SELECT COUNT(*) AS c FROM item_user_play"
        )
        result["sqlite"] = {"ok": True, "message": f"可读取播放记录 {rows[0]['c']} 条"}
    except Exception as e:  # noqa: BLE001
        result["sqlite"] = {"ok": False, "message": str(e)}

    return ok(result)
