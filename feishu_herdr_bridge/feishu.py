"""Minimal Feishu transport. Importing this module does not import/start the SDK.

SDK: lark-oapi==1.7.3. All SDK construction is explicit and injectable in tests.
No webhook server, auto-created groups, reply retry, or background task polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .core import BridgeCore, Message, Prepared, Reply
from .herdr import safe_identifier, safe_text


_LOG = logging.getLogger(__name__)
_EVENT = "im.message.receive_v1"


@dataclass(frozen=True)
class Credentials:
    app_id: str
    app_secret: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Credentials:
        app_id = env.get("FEISHU_APP_ID", "")
        secret = env.get("FEISHU_APP_SECRET", "")
        if not safe_identifier(app_id) or not secret.strip() or not safe_text(secret):
            raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
        return cls(app_id, secret)


def _get(value: object, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def parse_event(data: object, bot_open_id: str) -> Message | None:
    """Accept SDK models or equivalent dicts; never route by event_id or display name."""
    header = _get(data, "header")
    if _get(header, "event_type") != _EVENT:
        return None
    event = _get(data, "event")
    sender, incoming = _get(event, "sender"), _get(event, "message")
    user_id = _get(_get(sender, "sender_id"), "open_id")
    if _get(sender, "sender_type") != "user" or user_id == bot_open_id:
        return None
    if _get(incoming, "message_type") != "text":
        return None
    message_id, chat_id = _get(incoming, "message_id"), _get(incoming, "chat_id")
    if not all(safe_identifier(value) for value in (message_id, chat_id, user_id)):
        return None
    try:
        content = json.loads(_get(incoming, "content", ""))
    except (ValueError, TypeError):
        return None
    if not isinstance(content, dict) or not isinstance(content.get("text"), str):
        return None
    text = content["text"]
    mentioned = False
    mentions = _get(incoming, "mentions") or []
    if not isinstance(mentions, list):
        return None
    for mention in mentions:
        key = _get(mention, "key")
        if (_get(_get(mention, "id"), "open_id") == bot_open_id
                and isinstance(key, str) and re.fullmatch(r"@_user_[0-9]+", key)):
            text, count = re.subn(re.escape(key) + r"(?![A-Za-z0-9_])", "", text)
            mentioned = mentioned or count > 0
    chat_type = _get(incoming, "chat_type")
    if chat_type not in {"group", "p2p"} or (chat_type == "group" and not mentioned):
        return None
    if mentioned:
        text = text.lstrip(" ")
    stamp = _get(incoming, "create_time")
    created_at = (int(stamp) / 1000.0 if isinstance(stamp, str)
                  and re.fullmatch(r"[0-9]{1,16}", stamp) else None)
    return Message(message_id, chat_id, user_id, text, created_at)


class FeishuBridge:
    def __init__(
        self, core: BridgeCore, bot_open_id: str,
        send: Callable[[Message, Reply], None], *, thread_factory=threading.Thread,
    ) -> None:
        if not safe_identifier(bot_open_id):
            raise ValueError("bot_open_id is required for exact @ matching")
        self.core, self.bot_open_id, self.send = core, bot_open_id, send
        self._thread_factory = thread_factory

    def receive(self, data: object) -> threading.Thread | None:
        message = parse_event(data, self.bot_open_id)
        if (message is None or message.chat_id not in self.core.allowed_chats
                or message.user_id not in self.core.allowed_users):
            return None
        prepared = self.core.prepare(message)
        if isinstance(prepared, Reply):
            self._deliver(message, prepared)
            return None
        # The operation slot is already held. No executor/work queue is created.
        try:
            worker = self._thread_factory(target=self._work, args=(prepared,), daemon=True)
            worker.start()
            return worker
        except Exception:
            self._deliver(message, self.core.abandon(prepared))
            return None

    def _work(self, prepared: Prepared) -> None:
        reply = self.core.execute(prepared)
        self._deliver(prepared.message, reply)

    def _deliver(self, message: Message, reply: Reply) -> None:
        try:
            self.send(message, reply)
        except Exception:
            # Sending a receipt is independent of the persisted business result.
            _LOG.warning("receipt_failed chat=%s message=%s", message.chat_id, message.message_id)


def build_dispatcher(callback):
    import lark_oapi as lark

    return (lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(callback).build())


def build_text_request(chat_id: str, text: str):
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    return (CreateMessageRequest.builder().receive_id_type("chat_id")
            .request_body(CreateMessageRequestBody.builder().receive_id(chat_id)
                          .msg_type("text").content(json.dumps({"text": text}, ensure_ascii=False))
                          .build()).build())


class LarkTransport:
    """Use the SDK's own loop for receipts; only start() opens a connection."""

    def __init__(self, credentials: Credentials) -> None:
        import lark_oapi as lark

        self._lark = lark
        self._credentials = credentials
        self._client = (lark.Client.builder().app_id(credentials.app_id)
                        .app_secret(credentials.app_secret).timeout(10)
                        .log_level(lark.LogLevel.ERROR).build())
        self._loop = None

    def start(self, bridge: FeishuBridge) -> None:
        def callback(data):
            self._loop = asyncio.get_running_loop()
            bridge.receive(data)

        dispatcher = build_dispatcher(callback)
        websocket = self._lark.ws.Client(
            self._credentials.app_id, self._credentials.app_secret,
            event_handler=dispatcher, log_level=self._lark.LogLevel.ERROR, auto_reconnect=True,
        )
        websocket.start()

    def send(self, message: Message, reply: Reply) -> None:
        if self._loop is None or self._loop.is_closed():
            raise RuntimeError("No SDK event loop for receipt")
        # These tasks send receipts only. They never call the core or replay CLI work.
        self._loop.call_soon_threadsafe(
            lambda: self._loop.create_task(self._send_once(message, reply))
        )

    async def _send_once(self, message: Message, reply: Reply) -> None:
        try:
            request = build_text_request(message.chat_id, reply.text)
            response = await self._client.im.v1.message.acreate(request)
            if not response.success():
                raise RuntimeError("Receipt rejected")
        except Exception:
            _LOG.warning("receipt_failed chat=%s message=%s", message.chat_id, message.message_id)
