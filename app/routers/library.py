"""
媒体库管理接口 —— 依赖飞牛 REST 引擎（签名密钥已内置）。

设计说明：库列表与扫描触发是 REST 能覆盖的部分；切勿由此推断
"HTTP 引擎也能查播放统计"，它做不到。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core import database as db
from app.core.apiwrap import ok
from app.core.fn_client import FnApiError, fn_client
from app.routers.auth import require_login

router = APIRouter(prefix="/api/library", tags=["library"])


class ScanModel(BaseModel):
    guid: str
    name: str = ""
    dir_list: list = []


def _unavailable(message: str):
    return {"status": "error", "message": message, "available": False}


@router.get("/list")
def library_list(_=Depends(require_login)):
    try:
        return ok(fn_client.library_list(), available=True)
    except FnApiError as e:
        return ok([], available=False, message=str(e))
    except Exception as e:  # noqa: BLE001
        return ok([], available=False, message=f"未知异常: {e}")


@router.post("/scan")
def library_scan(data: ScanModel, _=Depends(require_login)):
    success, msg = fn_client.library_scan(data.guid, data.dir_list or None)
    db.scan_task_add(
        data.guid, data.name,
        "success" if success else "error",
        msg,
    )
    return ok({"ok": success}, message=msg)


@router.post("/scan/stop")
def scan_stop(data: ScanModel, _=Depends(require_login)):
    success, msg = fn_client.task_stop(data.guid)
    return ok({"ok": success}, message=msg)


@router.get("/tasks")
def scan_tasks(limit: int = 50, _=Depends(require_login)):
    return ok(db.scan_task_list(limit))
