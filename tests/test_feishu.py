"""Offline SDK and event tests. No test may contact Feishu or a real HerdR."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from feishu_herdr_bridge.core import BridgeCore, Message, Reply
from feishu_herdr_bridge.feishu import Credentials, FeishuBridge, LarkTransport, parse_event
from feishu_herdr_bridge.store import Store
from tests.fake_herdr import FakeHerdR


BOT = "ou-offline-bot"


def event(text, message_id="m-1", chat="chat-a", user="user", group=False):
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1", "event_id": "evt-" + message_id},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": user}},
            "message": {"message_id": message_id, "chat_id": chat, "chat_type": "group" if group else "p2p",
                        "message_type": "text", "create_time": "101000",
                        "content": json.dumps({"text": ("@_user_1 " if group else "") + text}),
                        "mentions": [{"key": "@_user_1", "id": {"open_id": BOT}}] if group else []},
        },
    }


class FeishuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite3")
        self.fake = FakeHerdR(self.root)
        self.core = BridgeCore(self.store, self.fake.adapter(), allowed_users={"user"},
                               allowed_chats={"chat-a", "chat-b"}, clock=lambda: 100.0,
                               operation_lock=threading.Lock())
        self.sent = []
        self.bridge = FeishuBridge(self.core, BOT, lambda message, reply: self.sent.append((message, reply)))
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex"):
            guard = patch(target, side_effect=AssertionError("Network is forbidden in tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def dispatch(self, data):
        worker = self.bridge.receive(data)
        if worker:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        return self.sent[-1][1] if self.sent else None

    def test_exact_bot_mention_and_millisecond_timestamp(self):
        data = event("中文\nsecond line", group=True)
        message = parse_event(data, BOT)
        self.assertEqual(message.text, "中文\nsecond line")
        self.assertEqual(message.created_at, 101.0)
        self.assertEqual(message.message_id, "m-1")
        data["event"]["message"]["mentions"][0]["id"]["open_id"] = "another-bot"
        self.assertIsNone(parse_event(data, BOT))
        data["event"]["message"]["mentions"][0]["id"]["open_id"] = BOT
        data["event"]["message"]["content"] = json.dumps({"text": "@_user_10 not our mention"})
        self.assertIsNone(parse_event(data, BOT))

    def test_ignored_events_do_not_reply_or_touch_storage_or_cli(self):
        cases = []
        for key, value in (("message_type", "image"), ("content", "invalid JSON"), ("chat_id", "not-allowed")):
            data = event("hello")
            data["event"]["message"][key] = value
            cases.append(data)
        for user in ("stranger", BOT):
            cases.append(event("hello", user=user))
        data = event("hello")
        data["event"]["sender"]["sender_type"] = "app"
        cases.append(data)
        data = event("hello")
        data["header"]["event_type"] = "im.message.updated_v1"
        cases.append(data)
        data = event("hello", group=True)
        data["event"]["message"]["mentions"] = []
        cases.append(data)
        for data in cases:
            self.assertIsNone(self.bridge.receive(data))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fake.events(), [])
        self.assertIsNone(self.store.get_request("m-1"))

    def test_two_chats_use_original_fake_cli_panes_and_no_replay(self):
        self.assertEqual(self.dispatch(event("/bind workspace-a pane-a", "bind-a", group=True)).code, "bound")
        self.assertEqual(self.dispatch(event("/bind workspace-b pane-b", "bind-b", chat="chat-b")).code, "bound")
        first = event("A-task", "task-a", group=True)
        self.assertEqual(self.dispatch(first).code, "submitted")
        self.assertEqual(self.dispatch(event("B-task", "task-b", chat="chat-b")).code, "submitted")
        first["header"]["event_id"] = "different-redelivery-event-id"
        self.assertEqual(self.dispatch(first).code, "duplicate")
        self.assertEqual([(r["pane_id"], r["text"]) for r in self.fake.events("submitted")],
                         [("pane-a", "A-task"), ("pane-b", "B-task")])
        self.assertIn("screen-A", self.dispatch(event("/read", "read-a")).text)
        self.assertIn("screen-B", self.dispatch(event("/read", "read-b", chat="chat-b")).text)

    def test_unknown_command_and_bad_time_never_submit(self):
        self.dispatch(event("/bind workspace-a pane-a", "bind"))
        self.assertEqual(self.dispatch(event("/clear", "clear")).code, "unknown_command")
        data = event("task", "bad-time")
        data["event"]["message"]["create_time"] = "unparseable"
        self.assertEqual(self.dispatch(data).code, "invalid_time")
        data = event("late", "late")
        data["event"]["message"]["create_time"] = "99000"
        self.assertEqual(self.dispatch(data).code, "stale_message")
        self.assertEqual(self.fake.events("submitted"), [])

    def test_slot_is_acquired_before_worker_start_and_busy_is_not_queued(self):
        workers = []

        class HeldWorker:
            def __init__(self, *, target, args, daemon):
                self.target, self.args = target, args
                workers.append(self)

            def start(self):
                pass  # Deliberately hold the accepted operation before execution.

        self.bridge = FeishuBridge(self.core, BOT, lambda m, r: self.sent.append((m, r)), thread_factory=HeldWorker)
        self.bridge.receive(event("/agents", "first"))
        self.assertEqual(self.fake.events(), [])
        self.assertEqual(self.dispatch(event("/agents", "second")).code, "busy")
        self.assertEqual(len(workers), 1)
        workers[0].target(*workers[0].args)
        calls = len(self.fake.events())
        self.assertEqual(self.dispatch(event("/agents", "second")).code, "duplicate")
        self.assertEqual(len(self.fake.events()), calls)

    def test_worker_start_failure_is_recorded_and_releases_slot(self):
        with patch.object(self.bridge, "_thread_factory", side_effect=RuntimeError("no thread")):
            self.assertEqual(self.dispatch(event("/agents", "failed-start")).code, "worker_unavailable")
        self.assertEqual(self.dispatch(event("/agents", "failed-start")).code, "duplicate")
        self.assertEqual(self.dispatch(event("/agents", "fresh")).code, "agents")

    def test_receipt_failure_does_not_redo_prompt_after_reconnect(self):
        self.dispatch(event("/bind workspace-a pane-a", "bind"))
        data = event("once", "task")
        self.bridge.send = lambda m, r: (_ for _ in ()).throw(RuntimeError("PRIVATE-ERROR"))
        with self.assertLogs("feishu_herdr_bridge.feishu", level="WARNING") as logs:
            self.dispatch(data)
        self.assertNotIn("PRIVATE-ERROR", "\n".join(logs.output))
        self.assertEqual(self.store.get_request("task").status, "done")
        self.bridge = FeishuBridge(self.core, BOT, lambda m, r: self.sent.append((m, r)))
        self.assertEqual(self.dispatch(data).code, "duplicate")
        self.assertEqual(len(self.fake.events("submitted")), 1)

    def test_full_feishu_creation_confirmation_path_uses_static_cli_only(self):
        from tests.test_creation import StaticCLI

        cli = StaticCLI()
        core = BridgeCore(self.store, cli.adapter(), allowed_users={"user"}, allowed_chats={"chat-a"},
                          projects={"demo": self.root}, clock=lambda: 100.0, operation_lock=threading.Lock())
        self.bridge = FeishuBridge(core, BOT, lambda m, r: self.sent.append((m, r)))
        proposal = self.dispatch(event("/new demo claude", "new", group=True))
        self.assertEqual(proposal.code, "creation_pending")
        self.assertEqual(cli.created, [])
        code = proposal.text.split("/confirm ", 1)[1].split("。", 1)[0]
        data = event("/confirm " + code, "confirm", group=True)
        self.assertEqual(self.dispatch(data).code, "created")
        self.assertEqual(self.dispatch(data).code, "duplicate")
        self.assertEqual(len(cli.created), 1)
        self.assertEqual(self.dispatch(event("new task", "task", group=True)).code, "submitted")
        self.assertEqual(cli.submitted, [(self.store.get_binding("chat-a").pane_id, "new task")])

    def test_config_and_missing_credentials_are_offline_and_do_not_start_sdk(self):
        from feishu_herdr_bridge.__main__ import load_config, main

        config = self.root / "config.json"
        raw = {"herdr_executable": "/offline/herdr", "herdr_session": "explicit", "bot_open_id": BOT,
               "allowed_users": ["user"], "allowed_chats": ["chat-a"], "projects": {"demo": str(self.root)}}
        config.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(load_config(config).session, "explicit")
        with patch("feishu_herdr_bridge.__main__.LarkTransport") as transport, redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as error:
                main(["--config", str(config)], env={})
        self.assertEqual(error.exception.code, 2)
        transport.assert_not_called()
        for key, value in (("herdr_executable", "relative"), ("herdr_session", ""),
                           ("allowed_chats", []), ("write_timeout", float("nan")),
                           ("app_secret", "DO-NOT-STORE")):
            invalid = {**raw, key: value}
            config.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(config)
        credentials = Credentials.from_env({"FEISHU_APP_ID": "offline-app", "FEISHU_APP_SECRET": "DO-NOT-LOG"})
        self.assertNotIn("DO-NOT-LOG", repr(credentials))

    def test_help_imports_no_sdk_and_opens_no_network(self):
        script = """
import sys

def guard(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Network forbidden')
sys.addaudithook(guard)
from feishu_herdr_bridge.__main__ import main
assert 'lark_oapi' not in sys.modules
try:
    main(['--help'], env={})
except SystemExit as error:
    assert error.code == 0
assert 'lark_oapi' not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_receipt_is_attempted_once_and_exception_is_redacted(self):
        transport = LarkTransport.__new__(LarkTransport)
        send = AsyncMock(side_effect=RuntimeError("PRIVATE-TOKEN"))
        transport._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(acreate=send))))
        message = Message("m", "chat", "user", "PRIVATE-PROMPT", 1.0)
        with patch("feishu_herdr_bridge.feishu.build_text_request", return_value=object()), \
                self.assertLogs("feishu_herdr_bridge.feishu", level="WARNING") as logs:
            await transport._send_once(message, Reply("done", "submitted", "receipt"))
        self.assertEqual(send.await_count, 1)
        self.assertNotIn("PRIVATE", "\n".join(logs.output))


@unittest.skipUnless(importlib.util.find_spec("lark_oapi"), "Install pinned SDK to run its offline contract test")
class InstalledSDKTests(unittest.TestCase):
    def test_pinned_sdk_import_dispatch_and_request_models_without_credentials_or_network(self):
        script = """
import json, sys
from importlib.metadata import version

def guard(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Network forbidden')
sys.addaudithook(guard)
import lark_oapi
assert version('lark-oapi') == '1.7.3'
from feishu_herdr_bridge.feishu import build_dispatcher, build_text_request, parse_event
from tests.test_feishu import event, BOT
received = []
dispatcher = build_dispatcher(received.append)
dispatcher._do_without_validation(json.dumps(event('hello', group=True)).encode())
assert len(received) == 1
assert parse_event(received[0], BOT).text == 'hello'
request = build_text_request('chat-a', 'reply')
assert request.body.receive_id == 'chat-a'
assert request.body.msg_type == 'text'
assert json.loads(request.body.content) == {'text': 'reply'}
from unittest.mock import Mock, patch
from feishu_herdr_bridge.feishu import Credentials, LarkTransport
transport = LarkTransport(Credentials('offline-app', 'offline-placeholder'))
with patch('lark_oapi.ws.Client') as websocket:
    transport.start(Mock())
    websocket.return_value.start.assert_called_once_with()
    assert websocket.call_args.kwargs['auto_reconnect'] is True
"""
        env = {k: v for k, v in os.environ.items() if k not in {"FEISHU_APP_ID", "FEISHU_APP_SECRET"}}
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
