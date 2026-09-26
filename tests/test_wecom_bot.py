"""
企业微信智能机器人协议层 —— 离线测试。

不联网、不依赖 websocket-client：用 FakeWS 注入传输层，
直接驱动「连接 → 订阅 → 回执 → 收消息 → 发送 → 等回执」全流程。
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import wecom_bot as wb  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)[:160]))


# ==============================================================================
# FakeWS：把协议帧同步喂回客户端，使状态机可确定性推进
# ==============================================================================
class FakeWS:
    """ack_mode: 'ok' 正常回执 | 'error' 回执 errcode!=0 | None 不回执"""

    def __init__(self, url, on_open, on_message, on_error, on_close,
                 ack_mode="ok", script=None, close_immediately=False,
                 on_run=None):
        self.url = url
        self._on_open = on_open
        self._on_message = on_message
        self._on_close = on_close
        self.ack_mode = ack_mode
        self.script = list(script or [])
        self.close_immediately = close_immediately
        self.sent = []
        self.closed = False
        self._on_run = on_run

    # 客户端在 _run_once 里用关键字参数调用
    def run_forever(self, **kwargs):
        if self._on_run:
            self._on_run(self)
        if self.close_immediately:
            self._on_close(self, None, None)
            return
        self._on_open(self)
        for frame in self.script:
            self._on_message(self, frame)
        self._on_close(self, None, None)

    def send(self, data):
        payload = json.loads(data)
        self.sent.append(payload)
        req_id = ((payload.get("headers") or {}).get("req_id")) or ""
        if self.ack_mode is None:
            return
        if self.ack_mode == "ok":
            ack = {"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"}
        else:
            ack = {"headers": {"req_id": req_id}, "errcode": 40001, "errmsg": "denied"}
        self._on_message(self, json.dumps(ack, ensure_ascii=False))

    def close(self):
        self.closed = True


def make_factory(holder, **opts):
    def factory(url, on_open, on_message, on_error, on_close):
        ws = FakeWS(url, on_open, on_message, on_error, on_close, **opts)
        holder.append(ws)
        return ws
    return factory


def inbound_msg(text="我想看沙丘", sender="zhangsan", chattype="single"):
    return json.dumps({
        "cmd": wb.CMD_MSG_CALLBACK,
        "headers": {"req_id": "aibot_msg_callback-abc123"},
        "body": {
            "from": {"userid": sender},
            "chattype": chattype,
            "chatid": sender,
            "text": {"content": text},
        },
    }, ensure_ascii=False)


# ==============================================================================
# 1. 帧构造（纯函数）
# ==============================================================================
def test_builders():
    sub = wb.build_subscribe("bot-1", "sec-1")
    check("订阅帧结构",
          sub["cmd"] == "aibot_subscribe"
          and sub["body"] == {"bot_id": "bot-1", "secret": "sec-1"}
          and sub["headers"]["req_id"].startswith("aibot_subscribe"),
          json.dumps(sub, ensure_ascii=False))

    ping = wb.build_ping()
    check("心跳帧结构",
          ping["cmd"] == "ping" and ping["headers"]["req_id"].startswith("ping"),
          json.dumps(ping, ensure_ascii=False))

    send = wb.build_send_markdown("aibot_send_msg-x", "user1", "**你好**", "single")
    body = send["body"]
    check("发送帧结构（markdown/single）",
          send["cmd"] == "aibot_send_msg"
          and body["chatid"] == "user1"
          and body["chat_type"] == "single"
          and body["msgtype"] == "markdown"
          and body["markdown"]["content"] == "**你好**",
          json.dumps(send, ensure_ascii=False))

    check("req_id 前缀约定（回执匹配依赖它）",
          wb.new_req_id("aibot_send_msg").startswith("aibot_send_msg-")
          and wb.new_req_id("ping") != wb.new_req_id("ping"), "")


# ==============================================================================
# 2. 内容分块（字节安全）
# ==============================================================================
def test_split_content():
    text = "汉" * 3000  # 9000 字节
    chunks = wb.split_content(text)
    sizes = [len(c.encode("utf-8")) for c in chunks]
    check("分块按 UTF-8 字节切（中文不超限）",
          all(s <= wb.CONTENT_LIMIT_BYTES for s in sizes)
          and "".join(chunks) == text and len(chunks) == 3, str(sizes))

    exact = "a" * wb.CONTENT_LIMIT_BYTES
    check("恰好等于上限时不拆分", wb.split_content(exact) == [exact], "")

    over = "a" * (wb.CONTENT_LIMIT_BYTES + 1)
    check("超出 1 字节即拆成两块",
          [len(c) for c in wb.split_content(over)] == [wb.CONTENT_LIMIT_BYTES, 1], "")

    check("空内容返回单个空块", wb.split_content("") == [""], "")


# ==============================================================================
# 3. 帧解析（纯函数，必须容错）
# ==============================================================================
def test_parsers():
    check("非法 JSON -> None", wb.parse_frame("{not json") is None, "")
    check("非对象 JSON -> None", wb.parse_frame("[1,2]") is None, "")
    check("bytes 帧可解析",
          wb.parse_frame(json.dumps({"cmd": "ping"}).encode()) == {"cmd": "ping"}, "")

    msg = wb.parse_inbound(json.loads(inbound_msg()))
    check("用户消息归一化",
          msg and msg["kind"] == "message" and msg["sender"] == "zhangsan"
          and msg["text"] == "我想看沙丘" and msg["chat_type"] == "single", str(msg))

    at = wb.parse_inbound(json.loads(inbound_msg(text="@机器人 我想看沙丘")))
    check("@提及被剥离", at and at["text"] == "我想看沙丘", str(at))

    grp = wb.parse_inbound(json.loads(inbound_msg(chattype="group")))
    check("群聊消息带 chat_type=group（由业务层决定是否忽略）",
          grp and grp["chat_type"] == "group", str(grp))

    no_sender = json.loads(inbound_msg())
    no_sender["body"]["from"] = {}
    check("缺发送者 -> None", wb.parse_inbound(no_sender) is None, "")

    empty = json.loads(inbound_msg(text="   "))
    check("空文本 -> None", wb.parse_inbound(empty) is None, "")

    evt = wb.parse_inbound({"cmd": wb.CMD_EVENT_CALLBACK,
                            "headers": {"req_id": "aibot_event_callback-1"},
                            "body": {"event": {"eventtype": "disconnected_event"}}})
    check("事件归一化",
          evt and evt["kind"] == "event" and evt["event_type"] == "disconnected_event",
          str(evt))

    check("未知命令 -> None", wb.parse_inbound({"cmd": "unknown"}) is None, "")
    check("无 cmd 的上行回执不被当业务消息", wb.parse_inbound(
        {"headers": {"req_id": "aibot_subscribe-1"}, "errcode": 0}) is None, "")

    check("回执识别需同时有 req_id 与 errcode",
          wb.parse_ack({"headers": {"req_id": "x"}, "errcode": 0})["errcode"] == 0
          and wb.parse_ack({"headers": {"req_id": "x"}}) is None
          and wb.parse_ack({"errcode": 0}) is None, "")


# ==============================================================================
# 4. 握手：连接 -> 订阅 -> 回执 -> 认证
# ==============================================================================
def test_handshake():
    holder = []
    got = []
    client = wb.WeComBotClient(
        bot_id="bot-1", secret="sec-1",
        ws_factory=make_factory(holder, ack_mode="ok"),
        on_message=got.append, reconnect_delays=[0.01],
    )
    client.start()
    for _ in range(50):
        if client._authenticated.is_set():
            break
        time.sleep(0.02)
    check("订阅成功后进入已认证状态",
          client._authenticated.is_set() and client.state in ("subscribed", "closed"),
          client.status())
    check("首个发出帧是订阅帧",
          holder and holder[0].sent and holder[0].sent[0]["cmd"] == "aibot_subscribe",
          holder[0].sent[:1] if holder else "no ws")
    client.stop()

    # 订阅被拒：不得进入认证态，且记录错误
    holder2 = []
    bad = wb.WeComBotClient(bot_id="b", secret="s",
                            ws_factory=make_factory(holder2, ack_mode="error"),
                            reconnect_delays=[5.0])
    bad.start()
    time.sleep(0.25)
    check("订阅被拒时不认证并记录错误",
          not bad._authenticated.is_set() and "订阅失败" in bad.last_error, bad.last_error)
    bad.stop()

    check("未配置凭证时 start() 返回 False",
          wb.WeComBotClient().start() is False, "")


# ==============================================================================
# 5. 消息分发
# ==============================================================================
def test_dispatch():
    holder = []
    msgs, events = [], []
    client = wb.WeComBotClient(
        bot_id="b", secret="s",
        ws_factory=make_factory(holder, ack_mode="ok",
                                script=[inbound_msg(),
                                        json.dumps({"cmd": wb.CMD_EVENT_CALLBACK,
                                                    "headers": {"req_id": "aibot_event_callback-1"},
                                                    "body": {"event": {"eventtype": "disconnected_event"}}})]),
        on_message=msgs.append, on_event=events.append,
        reconnect_delays=[5.0],
    )
    client.start()
    for _ in range(50):
        if msgs and events:
            break
        time.sleep(0.02)
    check("用户消息分发到 on_message",
          len(msgs) == 1 and msgs[0]["text"] == "我想看沙丘", str(msgs)[:120])
    check("事件分发到 on_event",
          len(events) == 1 and events[0]["event_type"] == "disconnected_event",
          str(events)[:120])
    client.stop()


# ==============================================================================
# 6. 发送与回执
# ==============================================================================
def test_send():
    holder = []
    acks = []
    client = wb.WeComBotClient(bot_id="b", secret="s",
                               ws_factory=make_factory(holder, ack_mode="ok"),
                               on_status=acks.append, reconnect_delays=[5.0])
    client.start()
    for _ in range(50):
        if client._authenticated.is_set():
            break
        time.sleep(0.02)

    check("已认证后可发送并拿到回执",
          client.send_markdown("你好", chatid="user1") is True, client.last_error)
    sent_cmds = [p["cmd"] for p in holder[-1].sent]
    check("发送使用 aibot_send_msg", "aibot_send_msg" in sent_cmds, str(sent_cmds))
    check("缺少 chatid 时拒绝发送并报错",
          client.send_markdown("x") is False and "chatid" in client.last_error,
          client.last_error)

    long_text = "汉" * 3000  # 9000 字节 -> 3 块
    before = len([c for c in holder[-1].sent if c["cmd"] == "aibot_send_msg"])
    client.send_markdown(long_text, chatid="user1")
    after = len([c for c in holder[-1].sent if c["cmd"] == "aibot_send_msg"])
    check("超长内容自动分块发送", after - before == 3, f"本次发出 {after - before} 条")
    client.stop()

    # 回执超时
    holder2 = []
    to = wb.WeComBotClient(bot_id="b", secret="s", ack_timeout=1.0,
                           ws_factory=make_factory(holder2, ack_mode=None),
                           reconnect_delays=[5.0])
    to.start()
    time.sleep(0.05)
    to._authenticated.set()  # 绕过认证直接测发送超时
    started = time.time()
    ok = to.send_markdown("x", chatid="u")
    check("回执超时返回 False 且不挂死",
          ok is False and "超时" in to.last_error and time.time() - started < 3.0,
          to.last_error)
    to.stop()


# ==============================================================================
# 7. 重连
# ==============================================================================
def test_reconnect():
    attempts = []
    done = threading.Event()

    def on_run(_ws):
        attempts.append(time.time())
        if len(attempts) >= 3:
            done.set()

    def factory(url, on_open, on_message, on_error, on_close):
        return FakeWS(url, on_open, on_message, on_error, on_close,
                      ack_mode="ok", close_immediately=True, on_run=on_run)

    client = wb.WeComBotClient(bot_id="b", secret="s", ws_factory=factory,
                               reconnect_delays=[0.01])
    client.start()
    done.wait(timeout=3)
    client.stop()
    check("连接断开后按退避重连", len(attempts) >= 3, f"重连 {len(attempts)} 次")
    check("stop() 后线程退出", not client.is_running() and client.state == "closed",
          client.status())


# ==============================================================================
def main() -> int:
    for fn in (test_builders, test_split_content, test_parsers, test_handshake,
               test_dispatch, test_send, test_reconnect):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} 抛出异常", False, f"{type(exc).__name__}: {exc}")

    failed = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name}: {detail}")
    print(f"\n===== {len(results) - len(failed)}/{len(results)} passed, "
          f"{len(failed)} failed =====")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
