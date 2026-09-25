"""页面路由。所有管理页面统一在此注册。"""

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import APP_VERSION, PORT, USER_PORT, cfg
from app.routers.auth import current_user

router = APIRouter()

# 资源目录一律基于包位置解析，避免受进程工作目录影响。
# __file__ = <项目根>/app/routers/views.py -> 连退三级得到 <项目根>
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PKG_STATIC = os.path.join(BASE_DIR, "static")
PKG_TEMPLATES = os.path.join(BASE_DIR, "templates")
templates = Jinja2Templates(directory=PKG_TEMPLATES)

NAV = [
    {"key": "dashboard", "path": "/", "label": "仪表盘", "icon": "◧"},
    {"key": "history", "path": "/history", "label": "播放历史", "icon": "▤"},
    {"key": "content", "path": "/content", "label": "风云榜", "icon": "★"},
    {"key": "users", "path": "/users", "label": "用户中心", "icon": "◍"},
    {"key": "insight", "path": "/insight", "label": "数据洞察", "icon": "◉"},
    {"key": "library", "path": "/library", "label": "媒体库", "icon": "▒"},
    {"key": "requests", "path": "/requests_admin", "label": "求片管理", "icon": "◆"},
    {"key": "settings", "path": "/settings", "label": "系统设置", "icon": "⚙"},
]


def _render(request: Request, template: str, active: str, extra: dict | None = None):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ctx = {
        "request": request,
        "nav": NAV,
        "active": active,
        "version": APP_VERSION,
        "port": PORT,
        "user": user,
        "fn_url": cfg.public_url(),
        "engine": {"mode": cfg.mode},
        "engine_mode": cfg.mode,
    }
    if extra:
        ctx.update(extra)
    # fastapi >= 0.141 起 TemplateResponse 必须使用具名参数，旧的位置参数写法已被移除
    return templates.TemplateResponse(request=request, name=template, context=ctx)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=302)
    from app.core.fn_client import fn_client

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "version": APP_VERSION,
            "fn_login_available": fn_client.has_signature_material,
        },
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return _render(request, "index.html", "dashboard")


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    return _render(request, "history.html", "history")


@router.get("/content", response_class=HTMLResponse)
async def content_page(request: Request):
    return _render(request, "content.html", "content")


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    return _render(request, "users.html", "users")


@router.get("/insight", response_class=HTMLResponse)
async def insight_page(request: Request):
    return _render(request, "insight.html", "insight")


@router.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    return _render(request, "library.html", "library")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return _render(request, "settings.html", "settings")


@router.get("/request", response_class=HTMLResponse)
async def request_portal_page(request: Request):
    """
    用户求片门户页。刻意不校验管理员登录 —— 它主要运行在隔离端口 10208 上，
    由 main.py 的 portal_app 白名单负责边界。
    """
    return templates.TemplateResponse(
        request=request,
        name="request.html",
        context={"request": request, "version": APP_VERSION, "port": USER_PORT},
    )


@router.get("/requests_admin", response_class=HTMLResponse)
async def requests_admin_page(request: Request):
    return _render(request, "requests_admin.html", "requests")
