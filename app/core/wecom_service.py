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


# ---------------- 配置自检与报错翻译 ----------------
# 智能机器人的 Bot ID 有固定前缀（官方帮助 open.work.weixin.qq.com/help2/pc/21677）。
# 最容易踩的坑是把「自建应用」的 AgentId/Secret 填进来 —— 那样长连接能建立，
# 但订阅会被服务端拒绝，报 853000 invalid bot_id or secret。
BOT_ID_PREFIX = "aib-"

# 已核实的错误码。853000 的语义是**凭证不被接受**（不是缺少字段）——
# 官方帮助与社区排查案例一致表明「Bot ID 正确、Secret 错误」也报这个码。
ERROR_HINTS = {
    853000: (
        "Bot ID 或 Secret 不正确。请按官方路径重新获取：企业微信 → 工作台 → 智能机器人 → "
        "创建机器人 → 手动创建 → **API 模式创建**（页面底部小字入口，普通机器人没有 API 能力）→ "
        "连接方式选「使用长连接」→ 页面会生成 Bot ID 与 Secret。"
        "注意 Bot ID 以 `aib-` 开头；Secret 只显示一次，若丢失需在后台重置。"
    ),
}


def config_warning() -> str:
    """配置层面的可疑点 —— 在连接之前就能提示，不必等订阅失败。"""
    bot_id = _bot_id()
    if not bot_id:
        return ""
    if not bot_id.startswith(BOT_ID_PREFIX):
        # 自建应用的 AgentId 是纯数字，这是最常见的一种填错
        if bot_id.isdigit():
            return (f"Bot ID 看起来是**自建应用的 AgentId**（纯数字 `{bot_id[:12]}`）。"
                    f"自建应用与智能机器人是两套体系，其 AgentId / Secret 在这里无效，"
                    f"订阅会被拒绝并报 853000。请改用：工作台 → 智能机器人 → "
                    f"创建机器人 → 手动创建 → **API 模式创建** → **使用长连接**，"
                    f"生成的 Bot ID 以 `{BOT_ID_PREFIX}` 开头。")
        return (f"Bot ID 应以 `{BOT_ID_PREFIX}` 开头（当前为 `{bot_id[:12]}`）。"
                f"若填的是自建应用的凭证，订阅会被拒绝并报 853000 —— "
                f"请在「工作台 → 智能机器人 → API 模式创建」处获取。")
    if not _secret():
        return "尚未填写 Secret"
    if len(_secret()) < 16:
        return "Secret 长度异常偏短，可能复制不完整，建议重新复制"
    return ""


def explain_error(message: str) -> str:
    """把服务端错误码翻译成可行动的说明。没有命中则原样返回。"""
    text = str(message or "")
    for code, hint in ERROR_HINTS.items():
        if str(code) in text:
            return f"{text}\n\n{hint}"
    return text


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
    last_error = runtime.get("last_error", "")
    return {
        "enabled": bool(cfg.get("wecom_bot_enabled")),
        "bot_id": _bot_id(),
        "secret_set": bool(_secret()),
        "ws_url": ws_url(),
        "should_run": should_run(),
        "running": bool(runtime.get("running")),
        "state": runtime.get("state", "idle"),
        "authenticated": bool(runtime.get("authenticated")),
        "last_error": last_error,
        "last_error_hint": explain_error(last_error) if last_error else "",
        "config_warning": config_warning(),
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
                    "message": explain_error(st["last_error"]),
                    "status": st}
        time.sleep(0.2)

    st = status()
    return {"ok": False,
            "message": f"{int(wait_seconds)} 秒内未完成订阅，状态：{st['state']}。"
                       f"请确认容器能出网访问 {st['ws_url']}"
                       + (f"\n\n{st['config_warning']}" if st.get("config_warning") else ""),
            "status": st}
