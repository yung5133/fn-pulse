"""
企业微信机器人的运行时装配层。

职责边界：
    wecom_bot.py      协议层（长连接、订阅、心跳、帧收发）—— 不感知业务与配置
    本模块             按 cfg 启停客户端、缓存状态与近期日志、提供测试入口
    路由层（下一步）   命令解析与求片业务

把启停逻辑集中在这里，是为了让「保存设置后立即生效」这件事只有一处实现：
system.py 保存到相关键后调一次 apply_config() 即可，不必关心连接细节。
"""

import threading
import time
from typing import Any, Dict, List, Optional

from app.core.config import cfg
from app.core.wecom_bot import DEFAULT_WS_URL, WeComBotClient

_lock = threading.RLock()
_client: Optional[WeComBotClient] = None
_recent: List[str] = []
_MAX_LOG_LINES = 60


def _log_line(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _lock:
        _recent.append(line)
        del _recent[:-_MAX_LOG_LINES]
    print(f"[企微机器人] {msg}")


# ---------------- 配置读取 ----------------
def _bot_id() -> str:
    return str(cfg.get("wecom_bot_id") or "").strip()


def _secret() -> str:
    return str(cfg.get("wecom_bot_secret") or "").strip()


def ws_url() -> str:
    return str(cfg.get("wecom_ws_url") or "").strip() or DEFAULT_WS_URL


def should_run() -> bool:
    """启用且凭证齐全才跑 —— 避免开了开关却没填凭证导致无意义重连。"""
    return bool(cfg.get("wecom_bot_enabled")) and bool(_bot_id()) and bool(_secret())


# ---------------- 客户端构造（测试可替换） ----------------
def _make_client() -> WeComBotClient:
    return WeComBotClient(
        bot_id=_bot_id(),
        secret=_secret(),
        ws_url=ws_url(),
        on_message=handle_message,
        on_status=lambda state: _log_line(f"连接状态：{state}"),
        logger=_log_line,
    )


# ---------------- 业务入口（下一步接入求片路由） ----------------
HELP_TEXT = (
    "**FnPulse 求片助手**\n\n"
    "连接已就绪。求片命令正在开发中，敬请期待。\n\n"
    "目前可用：\n"
    "- `帮助` —— 显示这条说明\n"
)


def handle_message(msg: Dict[str, Any]) -> None:
    """
    收到用户消息时的入口。

    当前只实现「帮助」以保证链路可端到端验证；求片命令（搜索→选片→提交）
    与身份绑定将在下一步接入，届时复用 requests 路由里已有的搜索与提交逻辑。
    """
    text = (msg.get("text") or "").strip()
    sender = msg.get("sender") or "?"
    _log_line(f"收到 {sender}：{text}")

    lowered = text.lower()
    if lowered in ("帮助", "help", "菜单", "?", "？"):
        reply = HELP_TEXT
    else:
        reply = f"暂不支持该指令。发送 `帮助` 查看当前可用功能。\n\n> 收到：{text}"

    client = _client
    if client is None or not client.is_authenticated():
        _log_line("连接未就绪，回复未发送")
        return
    ok = client.send_markdown(reply, chatid=msg.get("chatid") or sender,
                              chat_type=msg.get("chat_type") or "single")
    _log_line("回复已发送" if ok else f"回复失败：{client.last_error}")


# ---------------- 生命周期 ----------------
def stop() -> None:
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        client.stop()
        _log_line("已停止")


def apply_config() -> Dict[str, Any]:
    """
    按当前配置启停机器人。配置变更后调用即可即时生效。
    返回最新状态，便于接口直接回显。
    """
    global _client
    with _lock:
        old, _client = _client, None
    if old is not None:
        old.stop()

    if not should_run():
        reason = ("未启用" if not cfg.get("wecom_bot_enabled")
                  else "缺少 bot_id 或 secret")
        _log_line(f"未启动（{reason}）")
        return status()

    client = _make_client()
    with _lock:
        _client = client
    if client.start():
        _log_line("正在连接…")
    else:
        _log_line(f"启动失败：{client.last_error}")
    return status()


def status() -> Dict[str, Any]:
    with _lock:
        client = _client
        recent = list(_recent[-12:])
    runtime = client.status() if client is not None else {}
    return {
        "enabled": bool(cfg.get("wecom_bot_enabled")),
        "bot_id": _bot_id(),
        "secret_set": bool(_secret()),
        "ws_url": ws_url(),
        "should_run": should_run(),
        "running": bool(runtime.get("running")),
        "state": runtime.get("state", "idle"),
        "authenticated": bool(runtime.get("authenticated")),
        "last_error": runtime.get("last_error", ""),
        "connected_at": runtime.get("connected_at"),
        "log": recent,
    }


def test(wait_seconds: float = 8.0) -> Dict[str, Any]:
    """
    测试连接：按当前配置重启并等待订阅结果。
    供设置页「测试连接」按钮使用 —— 成败都给可行动的说明。
    """
    if not cfg.get("wecom_bot_enabled"):
        apply_config()
        return {"ok": False, "message": "未启用机器人（请勾选启用并保存）",
                "status": status()}
    if not (_bot_id() and _secret()):
        apply_config()
        return {"ok": False, "message": "缺少 bot_id 或 secret", "status": status()}

    apply_config()
    deadline = time.time() + max(1.0, wait_seconds)
    while time.time() < deadline:
        st = status()
        if st["authenticated"]:
            return {"ok": True, "message": "连接成功，已完成订阅，可以收发消息", "status": st}
        if st.get("last_error"):
            return {"ok": False,
                    "message": f"{st['last_error']}（请确认容器能访问 {st['ws_url']}，"
                               f"以及 bot_id / secret 是否正确）",
                    "status": st}
        time.sleep(0.2)

    st = status()
    return {"ok": False,
            "message": f"{int(wait_seconds)} 秒内未完成订阅，状态：{st['state']}。"
                       f"请确认容器能出网访问 {st['ws_url']}",
            "status": st}
