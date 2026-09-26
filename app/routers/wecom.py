"""企业微信机器人：状态、测试与手动重连（管理端）。"""

from fastapi import APIRouter, Depends

from app.core import wecom_service
from app.core.apiwrap import ok
from app.routers.auth import require_login

router = APIRouter(prefix="/api/wecom", tags=["wecom"])


@router.get("/status")
def bot_status(_=Depends(require_login)):
    return ok(wecom_service.status())


@router.post("/test")
def bot_test(_=Depends(require_login)):
    """按当前配置重启连接并等待订阅结果（最长约 8 秒）。"""
    return ok(wecom_service.test())


@router.post("/restart")
def bot_restart(_=Depends(require_login)):
    """手动重连 —— 网络恢复后不必重启容器。"""
    st = wecom_service.apply_config()
    return ok(st, message="已按当前配置重新连接" if st["should_run"] else "未启用或凭证不全")
