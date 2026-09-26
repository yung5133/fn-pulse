"""
企业微信智能机器人 —— WebSocket 协议层。

为什么不用 HTTP 回调
--------------------
企业微信有两套完全不同的接入机制：
    自建应用（旧）  消息靠「API 接收消息」HTTP 回调，回调 URL 必须公网 HTTPS 可达
    智能机器人（新） 靠 WebSocket 长连接，**只需能出网**，无需公网 IP / 域名 / 内网穿透

本模块实现后者。协议以 MoviePilot 的 `app/modules/wechat/wechatbot.py`
（实测可用实现）为准，非官方文档逆向：

    连接    wss://openws.work.weixin.qq.com
    订阅    {"cmd":"aibot_subscribe","headers":{"req_id":...},"body":{"bot_id","secret"}}
    心跳    {"cmd":"ping","headers":{"req_id":...}}        每 30s
    下行    {"cmd":"aibot_msg_callback",  ...}              用户消息
            {"cmd":"aibot_event_callback",...}              事件（如旧连接被踢）
    上行    {"cmd":"aibot_send_msg","headers":{"req_id":...},
             "body":{"chatid","chat_type","msgtype":"markdown","markdown":{"content"}}}
    回执    服务端回一条带同样 req_id 的帧，errcode == 0 表示成功

设计约定
--------
* 帧构造与解析全部是**纯函数** —— 可离线单测，CI 不依赖企微可用性。
* 传输层可注入（`ws_factory`）—— 测试用 FakeWS 即可驱动完整状态机。
* `websocket-client` 懒加载 —— 未安装该依赖时模块仍可 import 与测试。
"""

import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

CMD_SUBSCRIBE = "aibot_subscribe"
CMD_SEND = "aibot_send_msg"
CMD_PING = "ping"
CMD_MSG_CALLBACK = "aibot_msg_callback"
CMD_EVENT_CALLBACK = "aibot_event_callback"

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

# 单条消息的字节上限。与 MP 一致按 4000 字节切分，避免超长被服务端拒收。
CONTENT_LIMIT_BYTES = 4000

# 重连退避（秒）。末位持续使用，不无限增长。
DEFAULT_RECONNECT_DELAYS = (1, 2, 5, 10, 30, 60)


# ==============================================================================
# 纯函数：帧构造
# ==============================================================================
def new_req_id(cmd: str) -> str:
    """
    生成请求 ID。

    约定：**必须以 cmd 开头** —— 服务端回执里会带回同一个 req_id，
    而订阅回执的识别方式是 `str(req_id).startswith("aibot_subscribe")`。
    这是从 MP 实现观察到的约定，ID 尾部随机即可。
    """
    return f"{cmd}-{uuid.uuid4().hex[:10]}"


def build_subscribe(bot_id: str, secret: str, req_id: Optional[str] = None) -> Dict[str, Any]:
    req_id = req_id or new_req_id(CMD_SUBSCRIBE)
    return {
        "cmd": CMD_SUBSCRIBE,
        "headers": {"req_id": req_id},
        "body": {"bot_id": bot_id, "secret": secret},
    }


def build_ping(req_id: Optional[str] = None) -> Dict[str, Any]:
    return {"cmd": CMD_PING, "headers": {"req_id": req_id or new_req_id(CMD_PING)}}


def build_send_markdown(req_id: str, chatid: str, content: str,
                        chat_type: str = "single") -> Dict[str, Any]:
    return {
        "cmd": CMD_SEND,
        "headers": {"req_id": req_id},
        "body": {
            "chatid": chatid,
            "chat_type": chat_type,
            "msgtype": "markdown",
            "markdown": {"content": content},
        },
    }


def split_content(text: str, limit: int = CONTENT_LIMIT_BYTES) -> List[str]:
    """
    按 UTF-8 字节数切分文本。

    必须按字节而非字符计数：一个中文字符占 3 字节，
    按字符切会在 4000 字符时产出约 12000 字节，仍会被服务端拒收。
    同时保证不把多字节字符切成两半（逐字符累加，天然安全）。
    """
    if text is None:
        return []
    if not text:
        return [""]
    chunks: List[str] = []
    buf: List[str] = []
    size = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if size + n > limit and buf:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(ch)
        size += n
    if buf:
        chunks.append("".join(buf))
    return chunks or [""]


# ==============================================================================
# 纯函数：帧解析
# ==============================================================================
def parse_frame(raw: Any) -> Optional[Dict[str, Any]]:
    """把原始帧解析成 dict；非 JSON / 非对象一律返回 None（不抛异常）。"""
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw if isinstance(raw, dict) else None


def frame_req_id(frame: Dict[str, Any]) -> str:
    return str(((frame.get("headers") or {}).get("req_id")) or "")


def extract_text(body: Dict[str, Any]) -> str:
    """
    从消息体里取文本。字段形态以 MP 的解析为准，这里做宽容匹配：
    text.content / markdown.content / 直接的 text 字符串。
    """
    body = body or {}
    for key in ("text", "markdown"):
        node = body.get(key)
        if isinstance(node, dict) and isinstance(node.get("content"), str):
            return node["content"]
        if isinstance(node, str):
            return node
    for key in ("content", "msg"):
        if isinstance(body.get(key), str):
            return body[key]
    return ""


def strip_mentions(text: str) -> str:
    """去掉 @某人 前缀（群里 @机器人 是常见触发方式）。"""
    if not text:
        return ""
    out = []
    for token in text.split():
        if token.startswith("@") and len(token) > 1:
            continue
        out.append(token)
    return " ".join(out).strip()


def parse_inbound(frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    把下行帧归一化为统一结构，供业务层消费。

    返回 None 表示「不是需要业务处理的消息」（回执帧、未知命令、
    缺发送者、空消息等），调用方据此跳过。
    """
    if not isinstance(frame, dict):
        return None
    cmd = frame.get("cmd")
    body = frame.get("body") or {}
    if not isinstance(body, dict):
        body = {}

    if cmd == CMD_MSG_CALLBACK:
        sender = str(((body.get("from") or {}).get("userid")) or "").strip()
        if not sender:
            return None
        text = strip_mentions(extract_text(body))
        chat_type = str(body.get("chattype") or body.get("chat_type") or "single")
        if not text:
            return None
        return {
            "kind": "message",
            "sender": sender,
            "text": text,
            "chat_type": chat_type,
            "chatid": str(body.get("chatid") or sender),
            "message_id": str(body.get("msgid") or body.get("message_id") or ""),
            "req_id": frame_req_id(frame),
            "raw": frame,
        }

    if cmd == CMD_EVENT_CALLBACK:
        event = body.get("event") or {}
        if not isinstance(event, dict):
            event = {}
        return {
            "kind": "event",
            "event_type": str(event.get("eventtype") or ""),
            "req_id": frame_req_id(frame),
            "raw": frame,
        }

    return None


def parse_ack(frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    识别回执帧：带 req_id 且含 errcode 才算回执。
    服务端的订阅回执不带 cmd（MP 的注释：「subscribe 回执走这里」）。
    """
    if not isinstance(frame, dict):
        return None
    req_id = frame_req_id(frame)
    if not req_id:
        return None
    errcode = frame.get("errcode")
    if errcode is None:
        return None
    return {
        "req_id": req_id,
        "errcode": int(errcode),
        "errmsg": str(frame.get("errmsg") or ""),
    }


# ==============================================================================
# 默认传输层（懒加载 websocket-client）
# ==============================================================================
def _default_ws_factory(url: str, on_open, on_message, on_error, on_close):
    import websocket  # 懒加载：未安装该依赖时协议层仍可 import

    return websocket.WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )


# ==============================================================================
# 客户端：连接 / 订阅 / 心跳 / 重连 / 收发
# ==============================================================================
class WeComBotClient:
    """
    企业微信智能机器人的长连接客户端。

    on_message(msg: dict) / on_event(msg: dict) 由业务层注入；
    on_status(状态字符串) 可选，用于把连接状态暴露给管理界面。

    线程模型：`start()` 起一个守护线程跑连接循环，`stop()` 会优雅退出。
    读写分锁：`_send_lock` 串行化发送，`_acks_lock` 保护待回执表。
    """

    def __init__(self,
                 bot_id: str = "",
                 secret: str = "",
                 ws_url: str = DEFAULT_WS_URL,
                 on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 on_status: Optional[Callable[[str], None]] = None,
                 heartbeat_interval: float = 30.0,
                 ack_timeout: float = 10.0,
                 reconnect_delays: Iterable[float] = DEFAULT_RECONNECT_DELAYS,
                 ws_factory: Optional[Callable] = None,
                 logger: Optional[Callable[[str], None]] = None):
        self.bot_id = (bot_id or "").strip()
        self.secret = (secret or "").strip()
        self.ws_url = ws_url or DEFAULT_WS_URL
        self._on_message = on_message
        self._on_event = on_event
        self._on_status = on_status
        self._heartbeat_interval = max(5.0, float(heartbeat_interval))
        self.ack_timeout = max(1.0, float(ack_timeout))
        self._reconnect_delays = list(reconnect_delays) or list(DEFAULT_RECONNECT_DELAYS)
        self._ws_factory = ws_factory or _default_ws_factory
        self._log = logger or (lambda _msg: None)

        self._ws = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._authenticated = threading.Event()
        self._send_lock = threading.RLock()
        self._acks_lock = threading.RLock()
        self._pending_acks: Dict[str, Dict[str, Any]] = {}

        self.state = "idle"          # idle / connecting / subscribed / closed
        self.last_error = ""
        self.connected_at: Optional[float] = None

    # ---------------- 配置 ----------------
    def is_configured(self) -> bool:
        return bool(self.bot_id and self.secret)

    # ---------------- 生命周期 ----------------
    def start(self) -> bool:
        if not self.is_configured():
            self.last_error = "未配置 bot_id / secret"
            self._set_state("idle")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="wecom-bot", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._authenticated.clear()
        self._set_state("closed")

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop_event.is_set())

    def is_authenticated(self) -> bool:
        """是否已完成订阅认证（可收发消息）。"""
        return self._authenticated.is_set()

    def status(self) -> Dict[str, Any]:
        return {
            "configured": self.is_configured(),
            "running": self.is_running(),
            "state": self.state,
            "authenticated": self._authenticated.is_set(),
            "connected_at": self.connected_at,
            "last_error": self.last_error,
            "ws_url": self.ws_url,
        }

    # ---------------- 连接循环 ----------------
    def _set_state(self, state: str) -> None:
        self.state = state
        if self._on_status:
            try:
                self._on_status(state)
            except Exception:  # noqa: BLE001
                pass

    def _run_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                self._run_once()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"连接异常：{self.last_error}")
            if self._stop_event.is_set():
                break
            delay = self._reconnect_delays[min(attempt, len(self._reconnect_delays) - 1)]
            attempt += 1
            self._set_state("reconnecting")
            self._log(f"{delay}s 后重连（第 {attempt} 次）")
            if self._stop_event.wait(delay):
                break
        self._set_state("closed")

    def _run_once(self) -> None:
        self._set_state("connecting")
        ws = self._ws_factory(
            self.ws_url,
            self._make_handler("open"),
            self._make_handler("message"),
            self._make_handler("error"),
            self._make_handler("close"),
        )
        self._ws = ws
        # websocket-client 的心跳由我们自己做，关掉它自带的 ping
        ws.run_forever(ping_interval=None, ping_timeout=None, skip_utf8_validation=True)

    def _make_handler(self, kind: str) -> Callable:
        if kind == "open":
            return lambda *a, **k: self._on_open(*a, **k)
        if kind == "message":
            return lambda *a, **k: self._on_raw_message(*a, **k)
        if kind == "error":
            return lambda *a, **k: self._on_error(*a, **k)
        return lambda *a, **k: self._on_close(*a, **k)

    # ---------------- 事件回调 ----------------
    def _on_open(self, ws, *args, **kwargs) -> None:
        self.connected_at = time.time()
        self.last_error = ""
        self._set_state("open")
        self._log("已连接，发送订阅")
        payload = build_subscribe(self.bot_id, self.secret)
        with self._acks_lock:
            self._subscribe_req_id = payload["headers"]["req_id"]
        self._send_raw(payload)
        self._start_heartbeat()

    def _on_error(self, ws, error, *args, **kwargs) -> None:
        self.last_error = str(error)
        self._authenticated.clear()
        self._log(f"连接错误：{error}")

    def _on_close(self, ws, *args, **kwargs) -> None:
        self._authenticated.clear()
        self._set_state("closed")
        self._log("连接已关闭")

    def _on_raw_message(self, ws, message, *args, **kwargs) -> None:
        self._handle_frame(message)

    def _start_heartbeat(self) -> None:
        def beat():
            while not self._stop_event.is_set():
                if self._authenticated.wait(timeout=self._heartbeat_interval):
                    if self._stop_event.wait(self._heartbeat_interval):
                        return
                    try:
                        self._send_raw(build_ping())
                    except Exception:  # noqa: BLE001
                        return
                else:
                    return  # 一直没认证成功，交给重连逻辑
        threading.Thread(target=beat, name="wecom-bot-heartbeat", daemon=True).start()

    # ---------------- 帧处理 ----------------
    def _handle_frame(self, raw: Any) -> Optional[Dict[str, Any]]:
        """
        处理一条下行帧。返回归一化后的业务消息（若有），便于测试直接断言。
        顺序：先消回执（订阅成功会 set authenticated），再走业务分发。
        """
        frame = parse_frame(raw)
        if frame is None:
            self._log("收到无法解析的帧")
            return None

        ack = parse_ack(frame)
        if ack is not None:
            self._resolve_ack(ack)
            if ack["req_id"].startswith(CMD_SUBSCRIBE):
                if ack["errcode"] == 0:
                    self._authenticated.set()
                    self._set_state("subscribed")
                    self._log("订阅成功")
                else:
                    self._authenticated.clear()
                    self.last_error = f"订阅失败：{ack['errmsg']} ({ack['errcode']})"
                    self._log(self.last_error)
                return None

        msg = parse_inbound(frame)
        if msg is None:
            return None

        if msg["kind"] == "event":
            if self._on_event:
                self._on_event(msg)
        elif self._on_message:
            self._on_message(msg)
        return msg

    def _resolve_ack(self, ack: Dict[str, Any]) -> None:
        with self._acks_lock:
            pending = self._pending_acks.get(ack["req_id"])
        if pending is not None:
            pending["payload"] = ack
            pending["event"].set()

    # ---------------- 发送 ----------------
    def _send_raw(self, payload: Dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None:
            return False
        data = json.dumps(payload, ensure_ascii=False)
        with self._send_lock:
            try:
                ws.send(data)
                return True
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"发送失败：{exc}"
                self._log(self.last_error)
                return False

    def send_with_ack(self, payload: Dict[str, Any],
                      wait_authenticated: bool = True) -> bool:
        """
        发送并等待服务端回执。返回是否成功（errcode == 0）。
        订阅前调用会先等认证就绪，超时即失败。
        """
        req_id = frame_req_id(payload)
        if not req_id:
            self.last_error = "缺少 req_id"
            return False
        if wait_authenticated and not self._authenticated.wait(timeout=self.ack_timeout):
            self.last_error = "未完成订阅认证，无法发送"
            return False

        pending = {"event": threading.Event(), "payload": None}
        with self._acks_lock:
            self._pending_acks[req_id] = pending
        try:
            if not self._send_raw(payload):
                return False
            if not pending["event"].wait(timeout=self.ack_timeout):
                self.last_error = f"等待回执超时：{req_id}"
                self._log(self.last_error)
                return False
            ack = pending["payload"] or {}
            if ack.get("errcode") != 0:
                self.last_error = f"发送被拒：{ack.get('errmsg')} ({ack.get('errcode')})"
                self._log(self.last_error)
                return False
            return True
        finally:
            with self._acks_lock:
                self._pending_acks.pop(req_id, None)

    def send_markdown(self, content: str, chatid: Optional[str] = None,
                      chat_type: str = "single") -> bool:
        """
        发 Markdown 消息。超长自动按字节分块，任一块失败即整体失败。
        """
        target = chatid or ""
        if not target:
            self.last_error = "缺少 chatid"
            return False
        if not content:
            return False
        ok_all = True
        for chunk in split_content(content):
            if not self.send_with_ack(build_send_markdown(new_req_id(CMD_SEND),
                                                          target, chunk, chat_type)):
                ok_all = False
                break
        return ok_all
