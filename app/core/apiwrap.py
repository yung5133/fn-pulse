"""统一 API 响应封装：把数据源异常转换成前端可直接消费的结构。"""

from functools import wraps

from fastapi.responses import JSONResponse

from app.core.media_source import SourceError


def ok(data=None, **extra):
    payload = {"status": "success", "data": data}
    payload.update(extra)
    return payload


def err(message: str, code: int = 400):
    return JSONResponse({"status": "error", "message": message}, status_code=code)


def guard(fn):
    """包住数据源调用，避免 SourceError 变成 500 堆栈。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except SourceError as e:
            return err(str(e), 503)
        except Exception as e:  # noqa: BLE001
            return err(f"服务异常: {e}", 500)

    return wrapper
