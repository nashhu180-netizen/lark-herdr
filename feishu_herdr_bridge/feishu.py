"""Minimal Feishu transport. Importing this module does not import/start the SDK.

SDK: lark-oapi==1.7.3. All SDK construction is explicit and injectable in tests.
Only confirmed group creation is supported; no webhook, retry, or task polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping
from uuid import UUID

from .core import (
    BridgeCore, GroupCreateError, GroupCreateInput, GroupCreateResult, Message, Prepared, Reply,
)
from .herdr import safe_identifier, safe_text
from .output import MAX_CHARS, MAX_LINES, Origin


_LOG = logging.getLogger(__name__)
_EVENT = "im.message.receive_v1"
_GROUP_CREATE_LOCK = threading.Lock()


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
    return Message(message_id, chat_id, user_id, text, created_at, chat_type)


class FeishuBridge:
    def __init__(
        self, core: BridgeCore, bot_open_id: str,
        send: Callable[[Message, Reply], None], *, thread_factory=threading.Thread,
    ) -> None:
        if not safe_identifier(bot_open_id) or (core.bot_open_id is not None and core.bot_open_id != bot_open_id):
            raise ValueError("bot_open_id is required for exact @ matching")
        self.core, self.bot_open_id, self.send = core, bot_open_id, send
        self._thread_factory = thread_factory

    def receive(self, data: object) -> threading.Thread | None:
        message = parse_event(data, self.bot_open_id)
        if (message is None or (self.core.bot_open_id is not None and self.core.bot_open_id != self.bot_open_id)
                or not self.core.is_authorized(message)):
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
            _LOG.warning("receipt_failed")


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


def build_group_request(value: GroupCreateInput):
    from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody
    from lark_oapi.core.enum import AccessTokenType

    if (not isinstance(value, GroupCreateInput) or not safe_identifier(value.requested_by)
            or not safe_text(value.group_name) or not 1 <= len(value.group_name) <= 60
            or any(c in value.group_name for c in ("\n", "\u2028", "\u2029"))
            or str(UUID(value.create_uuid)) != value.create_uuid):
        raise ValueError("Invalid confirmed group input")
    body = (CreateChatRequestBody.builder().name(value.group_name)
            .description(f"HerdR kpi-agg; G-{value.create_uuid}")
            .owner_id(value.requested_by).user_id_list([value.requested_by])
            .chat_mode("group").chat_type("private").external(False).build())
    request = (CreateChatRequest.builder().user_id_type("open_id").uuid(value.create_uuid)
               .request_body(body).build())
    # Never select user identity, even if other SDK clients use user tokens.
    request.token_types = {AccessTokenType.TENANT}
    return request


def decode_group_response(content: bytes, status: int, owner: str) -> GroupCreateResult:
    """Validate raw primitive types; retain a known resource before other checks."""
    try:
        envelope = json.loads(content)
    except (ValueError, TypeError):
        return GroupCreateResult()
    data = envelope.get("data") if isinstance(envelope, dict) else None
    if not isinstance(data, dict):
        return GroupCreateResult()
    chat_id = data.get("chat_id")
    known = chat_id if safe_identifier(chat_id) else None
    verified = (known is not None and type(status) is int and 200 <= status < 300
                and type(envelope.get("code")) is int and envelope["code"] == 0
                and data.get("owner_id") == owner and data.get("owner_id_type") == "open_id"
                and data.get("chat_mode") == "group" and data.get("chat_type") == "private"
                and data.get("external") is False)
    return GroupCreateResult(known, verified)


class LarkTransport:
    """SDK event loop for receipts; a confirmed worker performs group creation."""

    def __init__(self, credentials: Credentials) -> None:
        from importlib.metadata import version

        if version("lark-oapi") != "1.7.3":
            raise ValueError("The pinned SDK version is required")
        import lark_oapi as lark

        self._lark = lark
        self._credentials = credentials
        self._client = (lark.Client.builder().app_id(credentials.app_id)
                        .app_secret(credentials.app_secret).timeout(10)
                        .log_level(lark.LogLevel.ERROR).build())
        self._loop = None
        self.is_stopping: Callable[[], bool] = lambda: False

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


    def create_group(self, value: GroupCreateInput) -> GroupCreateResult:
        """Use SDK create/models with a request-local, single-send HTTP boundary.

        The pinned SDK does not expose redirect or pre-send hooks. Its chat
        resource's Transport name is scoped below to this request only. Other
        requests delegate unchanged; token and message modules are untouched.
        The nonblocking lock prevents overlapping replacements. No SDK files or
        global requests functions are changed, and the name is always restored.
        """
        if self.is_stopping() or not _GROUP_CREATE_LOCK.acquire(blocking=False):
            raise GroupCreateError(uncertain=False)
        sent = False
        raw = None
        try:
            import requests
            from lark_oapi.api.im.v1.resource import chat as chat_module
            from lark_oapi.core.http.transport import _build_header
            from lark_oapi.core.model import RawResponse
            from lark_oapi.core.enum import AccessTokenType

            request = build_group_request(value)
            original = chat_module.Transport
            stopping = self.is_stopping

            class SingleSend(original):
                @staticmethod
                def execute(config, incoming, option=None):
                    nonlocal sent, raw
                    if incoming is not request:
                        return original.execute(config, incoming, option)
                    if sent or stopping():
                        raise GroupCreateError(uncertain=sent)
                    token = getattr(option, "tenant_access_token", None)
                    if incoming.token_types != {AccessTokenType.TENANT} or not safe_identifier(token):
                        raise GroupCreateError(uncertain=False)
                    headers = _build_header(incoming, option, config)
                    body = self._lark.JSON.marshal(incoming.body).encode("utf-8")
                    # No redirect can turn one SDK invocation into a second POST.
                    # A fresh Session has zero adapter retries; make that explicit.
                    with requests.Session() as http:
                        http.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
                        if stopping():
                            raise GroupCreateError(uncertain=False)
                        sent = True
                        response = http.request(
                            "POST", "https://open.feishu.cn/open-apis/im/v1/chats",
                            headers=headers, params=incoming.queries, data=body,
                            timeout=(1.0, 2.0), allow_redirects=False,
                        )
                        raw = RawResponse()
                        raw.status_code, raw.headers, raw.content = (
                            response.status_code, dict(response.headers), response.content,
                        )
                    return raw

            chat_module.Transport = SingleSend
            try:
                response = self._client.im.v1.chat.create(request)
            finally:
                chat_module.Transport = original
            result = (decode_group_response(raw.content, raw.status_code, value.requested_by)
                      if raw is not None else GroupCreateResult())
            return GroupCreateResult(result.created_chat_id,
                                     result.verified and response.success() and not stopping())
        except Exception:
            # SDK model decoding may fail after the raw response contained an ID.
            result = (decode_group_response(raw.content, raw.status_code, value.requested_by)
                      if raw is not None else GroupCreateResult())
            raise GroupCreateError(created_chat_id=result.created_chat_id, uncertain=sent) from None
        finally:
            _GROUP_CREATE_LOCK.release()

    def send_output_once(self, origin: Origin, text: str) -> str:
        """Wait for one guarded send; never use the fire-and-forget receipt path."""
        if (self.is_stopping() or origin.session != "kpi-agg" or origin.chat_type != "group"
                or not safe_identifier(origin.chat_id) or not safe_text(text) or not text.strip()
                or len(text) > MAX_CHARS or len(text.split("\n")) > MAX_LINES):
            return "failed"
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return "failed"
        try:
            if asyncio.get_running_loop() is loop:
                return "failed"  # This blocking API belongs only to the observer thread.
        except RuntimeError:
            pass
        deadline = time.monotonic() + 3.0
        coroutine = self._output_post(origin, text, deadline)
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            return future.result(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            return "unknown"  # No raw exception, replay, or failure receipt.
        finally:
            if future is None:
                coroutine.close()
            elif not future.done():
                future.cancel()  # A late-starting coroutine also checks the absolute deadline.

    async def _output_post(self, origin: Origin, text: str, deadline: float) -> str:
        if self.is_stopping() or time.monotonic() >= deadline:
            return "failed"
        import httpx  # Already a dependency of the pinned SDK.
        from lark_oapi.core.enum import AccessTokenType

        # SDK acreate performs synchronous token verification. Avoid that path
        # here: token acquisition AND message delivery share a cancellable budget.
        # Reuse the real SDK text model/serializer, not its retry/redirect path.
        async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
            request = build_text_request(origin.chat_id, text)
            request.token_types = {AccessTokenType.TENANT}
            body = json.loads(self._lark.JSON.marshal(request.body))
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0),
                                         follow_redirects=False, timeout=3.0) as client:
                if self.is_stopping() or time.monotonic() >= deadline:
                    return "failed"
                token_response = await client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self._credentials.app_id, "app_secret": self._credentials.app_secret},
                )
                token_data = token_response.json()
                if (token_response.status_code != 200 or not isinstance(token_data, dict)
                        or type(token_data.get("code")) is not int or token_data["code"] != 0
                        or not safe_identifier(token_data.get("tenant_access_token"))):
                    return "failed"  # No message POST was attempted.
                if self.is_stopping() or time.monotonic() >= deadline:
                    return "failed"
                response = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    params={"receive_id_type": "chat_id"}, json=body,
                    headers={"Authorization": "Bearer " + token_data["tenant_access_token"]},
                )
                data = response.json()
                if (self.is_stopping() or not 200 <= response.status_code < 300
                        or not isinstance(data, dict) or type(data.get("code")) is not int
                        or data["code"] != 0 or not isinstance(data.get("data"), dict)
                        or data["data"].get("chat_id") != origin.chat_id
                        or not safe_identifier(data["data"].get("message_id"))):
                    return "unknown"
                return "sent"

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
            _LOG.warning("receipt_failed")
