"""
FnPulse · 飞牛映迹
------------------
飞牛影视（fnOS trim.media）的数据洞察与管理面板。

端口：10307（管理后台）

数据来源：
    SQLite 引擎   只读 trimmedia.db  -> 播放统计的唯一完整数据源
    HTTP  引擎    /v/api/v1 REST     -> 媒体库列表 / 触发扫描 / 停止任务
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.core.config import PORT, SECRET_KEY, cfg
from app.core.database import init_db
from app.routers import (  # noqa: F401
    auth,
    content,
    history,
    insight,
    library,
    stats,
    system,
    users,
    views,
)
from app.routers.auth import ensure_admin_seeded


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    username, is_default = ensure_admin_seeded()

    print("\n" + "=" * 58)
    print("  FnPulse · 飞牛映迹  启动完成")
    print("-" * 58)
    print(f"  管理后台        http://0.0.0.0:{PORT}")
    print(f"  数据源模式      {cfg.mode}（sqlite 推荐）")
    print(f"  媒体数据库      {cfg.get('fn_db_path')}")
    print(f"  管理员账号      {username}")
    if is_default:
        print("  ⚠  当前使用默认密码 fnpulse，请登录后立即修改")
    print("=" * 58 + "\n")

    yield

    print("\n[系统] FnPulse 已停止。")


app = FastAPI(title="FnPulse", version="0.1.0", lifespan=lifespan)

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
app.include_router(system.router)
