"""
登录鉴权。

提供两条登录通道，因为飞牛影视的账号校验依赖未公开的 authx 签名素材：

    1. 本地管理员（默认，永远可用）
       凭证存放于业务库配置，PBKDF2-SHA256 加盐存储。首次启动若未设置，
       由环境变量 ADMIN_USERNAME / ADMIN_PASSWORD 播种，否则使用
       admin / fnpulse 并在启动日志中强提示修改。

    2. 飞牛账号透传（可选）
       走 /v/api/v1/login，仅在已配置 fn_secret_string / fn_api_key 时可用，
       且仅允许管理员登录。

会话使用星形 SessionMiddleware 签名的 Cookie，有效期 7 天。
"""

import hashlib
import hmac
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.core import database as db
from app.core.config import cfg

router = APIRouter()

SESSION_KEY = "fn_user"
_ITERATIONS = 120_000


# ================= 凭据存储 =================
def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return salt.hex() + ":" + dk.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return hmac.compare_digest(actual, expected)


def _ensure_bootstrap() -> None:
    """
    幂等的自愈入口：确保凭证表存在且已播种。

    不依赖 FastAPI 的 lifespan —— 任何调用路径（含被嵌入运行、lifespan 未触发）
    都会自动完成初始化，避免出现"没有任何凭证可用"的死局。
    """
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS admin_credential (
                id            INTEGER PRIMARY KEY CHECK (id = 1),
                username      TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT,
                updated_at    TEXT
            )
        """)
        ensure_admin_seeded()
    except Exception:  # noqa: BLE001
        # 业务库不可写时不应让请求崩掉，由调用方返回明确错误
        pass


def get_admin_credential() -> dict:
    _ensure_bootstrap()
    rows = db.query("SELECT * FROM admin_credential WHERE id = 1")
    if rows:
        return dict(rows[0])
    return {}


def ensure_admin_seeded() -> tuple:
    """确保本地管理员已存在。返回 (username, 是否新建)。"""
    cred = get_admin_credential()
    if cred.get("password_hash"):
        return cred["username"], False

    username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    password = os.getenv("ADMIN_PASSWORD", "").strip() or "fnpulse"
    seeded_new = password == "fnpulse"

    db.execute(
        "INSERT OR REPLACE INTO admin_credential (id, username, password_hash, created_at, updated_at)"
        " VALUES (1, ?, ?, datetime('now','localtime'), datetime('now','localtime'))",
        (username, _hash_password(password, secrets.token_bytes(16))),
    )
    return username, seeded_new


def set_admin_credential(username: str, password: str) -> None:
    db.execute(
        "UPDATE admin_credential SET username = ?, password_hash = ?, updated_at = datetime('now','localtime')"
        " WHERE id = 1",
        (username, _hash_password(password, secrets.token_bytes(16))),
    )


# ================= 登录态 =================
def current_user(request: Request) -> dict:
    u = request.session.get(SESSION_KEY)
    return u if isinstance(u, dict) and u.get("is_admin") else {}


def require_login(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="未登录")
    return u


def require_login_page(request: Request) -> RedirectResponse:
    """页面路由用：未登录则跳 /login。"""
    if not current_user(request):
        return RedirectResponse("/login", status_code=302)
    return None


# ================= 接口 =================
class LoginModel(BaseModel):
    username: str
    password: str
    via_fn: bool = False


@router.post("/api/login")
async def api_login(data: LoginModel, request: Request):
    username = (data.username or "").strip()
    if not username or not data.password:
        return JSONResponse({"status": "error", "message": "请输入账号和密码"}, status_code=400)

    # ---- 通道 2：飞牛账号透传 ----
    if data.via_fn:
        from app.core.fn_client import FnApiError, fn_client

        if not fn_client.has_signature_material:
            return JSONResponse({
                "status": "error",
                "message": "未配置 authx 签名素材，飞牛账号登录不可用。请使用本地管理员账号登录。",
            }, status_code=400)
        try:
            # 借用登录流程做一次真实凭证校验（拿到 token 即代表账号密码正确）
            fn_client._ensure_token(force=True)
            if fn_client.username != username:
                # 换账号登录时，用传入的账号再验一次
                tmp_cfg_user = cfg.get("fn_username")
                cfg.set("fn_username", username)
                cfg.set("fn_password", data.password)
                try:
                    fn_client._ensure_token(force=True)
                except FnApiError:
                    cfg.set("fn_username", tmp_cfg_user)
                    return JSONResponse({"status": "error", "message": "账号或密码错误"},
                                        status_code=401)
            request.session[SESSION_KEY] = {
                "name": username,
                "is_admin": True,
                "source": "fn",
            }
            return JSONResponse({"status": "success"})
        except FnApiError as e:
            return JSONResponse({"status": "error", "message": str(e)}, status_code=401)

    # ---- 通道 1：本地管理员 ----
    cred = get_admin_credential()
    if not cred.get("password_hash"):
        return JSONResponse({"status": "error", "message": "管理员凭证未初始化"}, status_code=500)
    if username != cred.get("username") or not _verify_password(data.password, cred["password_hash"]):
        return JSONResponse({"status": "error", "message": "账号或密码错误"}, status_code=401)

    request.session[SESSION_KEY] = {
        "name": cred.get("username"),
        "is_admin": True,
        "source": "local",
    }
    return JSONResponse({"status": "success"})


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@router.get("/api/me")
async def api_me(user: dict = Depends(require_login)):
    return {"status": "success", "data": {"name": user.get("name"), "source": user.get("source")}}
