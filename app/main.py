"""
FnPulse · 飞牛映迹
------------------
飞牛影视（fnOS trim.media）的数据洞察与管理面板。

双端口（物理隔离，形态与 emby-pulse 一致）:
    10207  管理员后台
    10208  用户求片门户 —— 独立 ASGI 引擎，路径白名单之外一律 404，
           无法越权触达后台页面与后台接口。两者均可通过 PORT / USER_PORT 覆盖。

数据来源：
    SQLite 引擎   只读 trimmedia.db  -> 播放统计的唯一完整数据源
    HTTP  引擎    /v/api/v1 REST     -> 媒体库列表 / 触发扫描 / 停止任务
"""

import asyncio
import socket
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import APP_VERSION, PORT, SECRET_KEY, USER_PORT, cfg
from app.core.database import init_db
from app.routers import (  # noqa: F401
    auth,
    content,
    history,
    insight,
    library,
    moviepilot,
    requests as requests_router,
    stats,
    system,
    users,
    views,
)
from app.routers.auth import ensure_admin_seeded

# ==============================================================================
# 用户求片门户：独立 ASGI 引擎，铁血白名单
# ==============================================================================
# 为什么不能用 startswith("/api/requests")：那会同时放行管理端的
# /api/requests/{id}/status 与 DELETE。这里逐个显式放行，只给门户必要的最小面。
PORTAL_EXACT_PATHS = {
    "/request",
    "/favicon.ico",
    "/api/requests/config",
    "/api/requests/submit",
    "/api/requests/search",
    "/api/requests/mine",
    # 门户登录相关：只放行登录/登出/查自己，后台的管理接口仍被隔离
    "/api/requests/portal_login",
    "/api/requests/portal_logout",
    "/api/requests/me",
}
PORTAL_PREFIX_PATHS = ("/static",)


async def portal_app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        return

    if scope["type"] == "http":
        path = scope.get("path", "")

        # 门户根路径直达求片页
        if path == "/":
            scope["path"] = "/request"
            scope["raw_path"] = b"/request"

        allowed = (scope["path"] in PORTAL_EXACT_PATHS
                   or scope["path"].startswith(PORTAL_PREFIX_PATHS))
        if not allowed:
            await send({
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"text/html; charset=utf-8")],
            })
            await send({
                "type": "http.response.body",
                "body": "<h1>404 Not Found</h1>"
                        "<p>求片门户已被物理隔离，后台管理界面不可从该端口访问。</p>"
                        .encode("utf-8"),
            })
            return

    await app(scope, receive, send)


def start_portal_server():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("0.0.0.0", USER_PORT))
        sock.listen(100)
    except OSError as e:
        print(f"[警告] 求片门户端口 {USER_PORT} 绑定失败（可能被占用）：{e}")
        return

    import uvicorn

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server = uvicorn.Server(uvicorn.Config(app=portal_app, log_level="error"))
    server.install_signal_handlers = lambda: None
    try:
        loop.run_until_complete(server.serve(sockets=[sock]))
    except BaseException:  # noqa: BLE001
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    username, is_default = ensure_admin_seeded()
    threading.Thread(target=start_portal_server, daemon=True).start()

    print("\n" + "=" * 58)
    print("  FnPulse · 飞牛映迹  启动完成")
    print("-" * 58)
    print(f"  管理后台        http://0.0.0.0:{PORT}")
    print(f"  求片门户        http://0.0.0.0:{USER_PORT}")
    print(f"  数据源模式      {cfg.mode}（sqlite 推荐）")
    print(f"  媒体数据库      {cfg.get('fn_db_path')}")
    print(f"  管理员账号      {username}")
    if is_default:
        print("  ⚠  当前使用默认密码 fnpulse，请登录后立即修改")
    print("=" * 58 + "\n")

    yield

    print("\n[系统] FnPulse 已停止。")


app = FastAPI(title="FnPulse", version=APP_VERSION, lifespan=lifespan)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * 7)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# 静态资源目录同样基于包位置解析
app.mount("/static", StaticFiles(directory=views.PKG_STATIC), name="static")

app.include_router(views.router)
app.include_router(auth.router)
app.include_router(stats.router)
app.include_router(history.router)
app.include_router(content.router)
app.include_router(users.router)
app.include_router(insight.router)
app.include_router(library.router)
app.include_router(requests_router.router)
app.include_router(moviepilot.router)
app.include_router(system.router)
