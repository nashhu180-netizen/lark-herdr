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
        self.assertEqual(message.chat_type, "group")
        self.assertEqual(parse_event(event("private"), BOT).chat_type, "p2p")
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

    def test_devin_creation_from_done_dynamic_group_uses_existing_entry_without_sdk(self):
        import re
        from unittest.mock import Mock
        from feishu_herdr_bridge.core import GroupCreateResult
        from tests.test_creation import StaticCLI

        cli = StaticCLI()
        cli.session = "kpi-agg"
        creator = Mock(side_effect=[GroupCreateResult("fixture-dynamic-a", True),
                                    GroupCreateResult("fixture-dynamic-b", True)])
        self.core = BridgeCore(self.store, cli.adapter(), allowed_users={"user"}, allowed_chats={"chat-a"},
                               management_chat_id="chat-a", admin_users={"user"}, bot_open_id=BOT,
                               create_group=creator, projects={"demo": self.root},
                               clock=lambda: 100.0, operation_lock=threading.Lock())
        self.bridge = FeishuBridge(self.core, BOT, lambda m, r: self.sent.append((m, r)))
        sequence = 0

        def send(text, chat="chat-a"):
            nonlocal sequence
            sequence += 1
            return self.dispatch(event(text, f"devin-entry-{sequence}", chat=chat, group=True))

        self.assertEqual(send("/bind workspace-a pane-a").code, "bound")
        management_binding = self.store.get_binding("chat-a")
        for chat in ("fixture-dynamic-a", "fixture-dynamic-b"):
            proposal = send("/group-new synthetic task group")
            self.assertEqual(proposal.code, "group_pending")
            code = re.search(r"g-[0-9a-f]{16}", proposal.text).group()
            self.assertEqual(send("/confirm " + code).code, "group_created")
            self.assertNotIn(chat, self.core.allowed_chats)
            self.assertIsNone(self.store.get_binding(chat))
            self.assertEqual(send("/agents", chat).code, "agents")
        before = len(cli.calls)
        proposal = send("/new demo devin", "fixture-dynamic-a")
        self.assertEqual(proposal.code, "creation_pending")
        self.assertEqual(len(cli.calls), before)
        self.assertIsNone(self.store.get_binding("fixture-dynamic-a"))
        code = re.search(r"/confirm ([0-9a-f]{8})", proposal.text).group(1)
        confirmation = event("/confirm " + code, "devin-entry-confirm", chat="fixture-dynamic-a", group=True)
        self.assertEqual(self.dispatch(confirmation).code, "created")
        binding = self.store.get_binding("fixture-dynamic-a")
        self.assertEqual((binding.herdr_session, binding.workspace_id, binding.pane_id),
                         ("kpi-agg", cli.created[0][0], cli.created[0][2]))
        self.assertEqual(cli.started, [(binding.agent_name, "devin", binding.pane_id)])
        self.assertRegex(binding.agent_name, r"^fb-devin-[0-9a-f]{12}$")
        self.assertEqual(self.dispatch(confirmation).code, "duplicate")
        self.assertEqual(send("/confirm " + code, "fixture-dynamic-a").code, "confirmation_used")
        self.assertEqual(send(f"/bind {binding.workspace_id} {binding.pane_id}", "fixture-dynamic-b").code,
                         "workspace_occupied")
        self.assertEqual(send("only the new fake Pane", "fixture-dynamic-a").code, "submitted")
        self.assertEqual(cli.submitted, [(binding.pane_id, "only the new fake Pane")])
        self.assertIn(binding.pane_id, send("/read", "fixture-dynamic-a").text)
        self.assertIn("| devin |", send("/agents", "fixture-dynamic-a").text)
        self.assertEqual(send("/group-new forbidden here", "fixture-dynamic-a").code, "forbidden")
        self.assertEqual(self.store.get_binding("chat-a"), management_binding)
        self.assertEqual(creator.call_count, 2)
        self.assertEqual(len(cli.created), 1)
        self.assertTrue(self.store.group_allowed("fixture-dynamic-a", "kpi-agg", BOT))
        self.assertTrue(self.store.group_allowed("fixture-dynamic-b", "kpi-agg", BOT))

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


class GroupResponseTests(unittest.TestCase):
    def test_raw_fields_are_strict_and_known_id_survives_invalid_other_fields(self):
        import copy
        from feishu_herdr_bridge.feishu import decode_group_response

        fixture = json.loads((Path(__file__).parent / 'fixtures/feishu_group_create.json').read_text())
        original = fixture['create_response']
        owner = fixture['owner_id']
        self.assertTrue(decode_group_response(json.dumps(original).encode(), 200, owner).verified)
        for key, invalid in (('owner_id', 'wrong'), ('owner_id_type', 'user_id'), ('chat_mode', 'topic'),
                             ('chat_type', 'public'), ('external', True), ('external', 'false'), ('external', 0)):
            for missing in (False, True):
                with self.subTest(key=key, missing=missing, invalid=invalid):
                    payload = copy.deepcopy(original)
                    if missing:
                        payload['data'].pop(key)
                    else:
                        payload['data'][key] = invalid
                    result = decode_group_response(json.dumps(payload).encode(), 200, owner)
                    self.assertFalse(result.verified)
                    self.assertEqual(result.created_chat_id, original['data']['chat_id'])
        for status in (301, 307, 400, 429, 500, 503):
            self.assertFalse(decode_group_response(json.dumps(original).encode(), status, owner).verified)
        for code in (None, False, '0', 99991663):
            payload = {**original, 'code': code}
            self.assertFalse(decode_group_response(json.dumps(payload).encode(), 200, owner).verified)
        for content in (b'{', b'null', b'[]', b'{"code":0}', b'{"data":[]}'):
            result = decode_group_response(content, 200, owner)
            self.assertFalse(result.verified)
            self.assertIsNone(result.created_chat_id)
        for chat_id in (None, '', 'bad id', 123):
            payload = copy.deepcopy(original)
            payload['data']['chat_id'] = chat_id
            self.assertIsNone(decode_group_response(json.dumps(payload).encode(), 200, owner).created_chat_id)

    def test_receipt_failure_never_logs_identifiers_or_body(self):
        core = SimpleNamespace(bot_open_id=BOT)
        bridge = FeishuBridge(core, BOT, lambda m, r: (_ for _ in ()).throw(RuntimeError('RAW-SECRET')))
        message = Message('RAW-MESSAGE-ID', 'RAW-CHAT-ID', 'RAW-USER-ID', '/confirm g-PRIVATE', 1.0)
        with self.assertLogs('feishu_herdr_bridge.feishu', level='WARNING') as logs:
            bridge._deliver(message, Reply('unknown', 'group_unknown', 'PRIVATE-GROUP-NAME'))
        self.assertEqual(logs.output, ['WARNING:feishu_herdr_bridge.feishu:receipt_failed'])


class GroupHTTPFixture:
    """Real pinned SDK + real requests serialization; intercept only socket-facing send.

    No mock of chat.create, token verification, or SDK request/response models.
    A fake token cache forces the real tenant-token HTTP path on every call.
    """
    def __init__(self, case):
        import copy
        from importlib.metadata import version
        from urllib.parse import urlsplit
        from unittest.mock import Mock

        case.assertIsNotNone(importlib.util.find_spec('lark_oapi'), 'Install lark-oapi==1.7.3; do not skip SDK tests')
        case.assertEqual(version('lark-oapi'), '1.7.3')
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/feishu_group_create.json').read_text())
        self.owner = self.fixture['owner_id']
        self.bot = self.fixture['bot_open_id']
        self.payload = copy.deepcopy(self.fixture['create_response'])
        self.token_payload = copy.deepcopy(self.fixture['tenant_response'])
        self.status = 200
        self.error = None
        self.token_error = None
        self.raw_body = None
        self.after_token = lambda: None
        self.after_create = lambda: None
        self.calls = []
        self.posts = []
        self.responses = []
        self.stopped = False
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.getaddrinfo'):
            guard = patch(target, side_effect=AssertionError('Unexpected real network'))
            blocked = guard.start()
            case.addCleanup(guard.stop)
            case.addCleanup(blocked.assert_not_called)
        import requests
        from lark_oapi.core.token.manager import TokenManager

        cache = patch.object(TokenManager, 'cache', Mock(get=lambda key: None, set=lambda *args: None))
        cache.start()
        case.addCleanup(cache.stop)

        def send(adapter, prepared, **kwargs):
            self.calls.append(prepared)
            case.assertEqual(prepared.method, 'POST')
            case.assertEqual(adapter.max_retries.total, 0)
            path = urlsplit(prepared.url).path
            if path == '/open-apis/auth/v3/tenant_access_token/internal':
                body = json.loads(prepared.body)
                case.assertEqual(body, {'app_id': self.fixture['app_id'], 'app_secret': self.fixture['app_secret']})
                if self.token_error is not None:
                    raise self.token_error
                self.after_token()
                status, payload, raw_body = 200, self.token_payload, None
            elif path == '/open-apis/im/v1/chats':
                self.posts.append(prepared)
                case.assertEqual(prepared.headers['Authorization'], 'Bearer ' + self.fixture['tenant_response']['tenant_access_token'])
                if self.error is not None:
                    raise self.error
                self.after_create()
                status, payload, raw_body = self.status, self.payload, self.raw_body
            else:
                raise AssertionError('Unexpected endpoint')
            response = requests.Response()
            response.status_code = status
            response._content = raw_body if raw_body is not None else json.dumps(payload).encode('utf-8')
            response.headers = {'Content-Type': 'application/json', 'Location': 'https://open.feishu.cn/open-apis/im/v1/chats'}
            response.request, response.url = prepared, prepared.url
            self.responses.append(response)
            return response

        # The application deliberately catches transport exceptions. Assert HTTP
        # invariants again during cleanup so it cannot swallow a test failure.
        violations = []
        def checked_send(*args, **kwargs):
            try:
                return send(*args, **kwargs)
            except AssertionError as error:
                violations.append(str(error))
                raise
        case.addCleanup(lambda: case.assertEqual(violations, []))
        boundary = patch('requests.adapters.HTTPAdapter.send', new=checked_send)
        boundary.start()
        case.addCleanup(boundary.stop)
        self.transport = LarkTransport(Credentials(self.fixture['app_id'], self.fixture['app_secret']))
        self.transport.is_stopping = lambda: self.stopped

    def core(self, root):
        from tests.test_creation import StaticCLI

        self.cli = StaticCLI()
        self.cli.session = 'kpi-agg'
        self.store = Store(root / 'sdk-groups.sqlite3')
        return BridgeCore(self.store, self.cli.adapter(), allowed_users={self.owner}, allowed_chats={'management'},
                          management_chat_id='management', admin_users={self.owner}, bot_open_id=self.bot,
                          create_group=self.transport.create_group, projects={'demo': root},
                          clock=lambda: 100.0, operation_lock=threading.Lock())


class GroupSDKTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wire = GroupHTTPFixture(self)
        self.core = self.wire.core(self.root)
        self.sequence = 0

    def message(self, text):
        self.sequence += 1
        return Message(f'sdk-message-{self.sequence}', 'management', self.wire.owner, text, 101.0, 'group')

    def proposal(self):
        import re
        reply = self.core.handle(self.message('/group-new SDK test'))
        self.assertEqual(reply.code, 'group_pending')
        code = re.search(r'g-[0-9a-f]{16}', reply.text).group()
        return self.core.store.find_group(code, 'management', self.wire.owner, 'kpi-agg', self.wire.bot)

    def confirm(self, group):
        return self.core.handle(self.message('/confirm ' + group.confirmation_code))

    def test_real_models_tenant_body_uuid_and_exactly_one_post_per_confirmation(self):
        from urllib.parse import parse_qs, urlsplit
        from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody
        from lark_oapi.core.enum import AccessTokenType
        from feishu_herdr_bridge.core import GroupCreateInput
        from feishu_herdr_bridge.feishu import build_group_request

        group = self.proposal()
        request = build_group_request(GroupCreateInput(group.group_name, group.requested_by, group.create_uuid))
        self.assertIsInstance(request, CreateChatRequest)
        self.assertIsInstance(request.body, CreateChatRequestBody)
        self.assertEqual(request.token_types, {AccessTokenType.TENANT})
        self.assertEqual(self.wire.calls, [])
        self.assertEqual(self.confirm(group).code, 'group_created')
        self.assertEqual(self.confirm(group).code, 'group_created')
        self.assertEqual(len(self.wire.posts), 1)
        prepared = self.wire.posts[0]
        self.assertEqual(urlsplit(prepared.url).path, '/open-apis/im/v1/chats')
        self.assertEqual(parse_qs(urlsplit(prepared.url).query), {'user_id_type': ['open_id'], 'uuid': [group.create_uuid]})
        self.assertEqual(json.loads(prepared.body), {
            'name': group.group_name, 'description': f'HerdR kpi-agg; G-{group.create_uuid}',
            'owner_id': self.wire.owner, 'user_id_list': [self.wire.owner],
            'chat_mode': 'group', 'chat_type': 'private', 'external': False,
        })
        self.assertEqual(self.wire.cli.calls, [])
        self.assertEqual(self.wire.fixture['_meta']['source'], 'schema-derived/static-not-live')

    def test_http_failures_redirects_and_invalid_json_never_repeat_post(self):
        import requests
        scenarios = [(status, None, None) for status in (301, 302, 307, 308, 400, 401, 403, 429, 500, 503)]
        scenarios += [(200, error, None) for error in (requests.Timeout('SECRET'), requests.ConnectionError('SECRET'))]
        scenarios += [(200, None, b'{'), (200, None, b'null'), (200, None, b'{"code":99991663,"msg":"SECRET"}')]
        for status, error, content in scenarios:
            with self.subTest(status=status, error=type(error).__name__, content=content):
                group = self.proposal()
                self.wire.status, self.wire.error, self.wire.raw_body = status, error, content
                before = len(self.wire.posts)
                first = self.confirm(group)
                second = self.confirm(group)
                self.assertEqual((first.status, second.status), ('unknown', 'unknown'))
                self.assertEqual(len(self.wire.posts), before + 1)
                self.assertNotIn('SECRET', first.text)
                self.assertNotIn(self.wire.fixture['create_response']['data']['chat_id'], first.text)

    def test_every_invalid_success_field_preserves_resource_without_enabling(self):
        import copy
        for key in ('owner_id', 'owner_id_type', 'chat_mode', 'chat_type', 'external'):
            with self.subTest(key=key):
                self.wire.payload = copy.deepcopy(self.wire.fixture['create_response'])
                self.wire.payload['data']['chat_id'] = 'oc_fixture_partial_' + key
                self.wire.payload['data'].pop(key)
                group = self.proposal()
                self.assertEqual(self.confirm(group).status, 'unknown')
                saved = self.core.store.find_group(group.confirmation_code, 'management', self.wire.owner, 'kpi-agg', self.wire.bot)
                self.assertEqual(saved.created_chat_id, self.wire.payload['data']['chat_id'])
                self.assertFalse(self.core.is_authorized(Message('other', saved.created_chat_id, self.wire.owner, '/agents', 101, 'group')))
                before = len(self.wire.posts)
                self.assertEqual(self.confirm(group).status, 'unknown')
                self.assertEqual(len(self.wire.posts), before)

    def test_sdk_decode_error_preserves_raw_resource_and_restores_transport(self):
        from lark_oapi.core import JSON
        from lark_oapi.api.im.v1 import CreateChatResponse
        from lark_oapi.api.im.v1.resource import chat as chat_module

        original_transport = chat_module.Transport
        original_decode = JSON.unmarshal
        def broken_decode(text, cls, *args, **kwargs):
            if cls is CreateChatResponse:
                raise ValueError('PRIVATE RAW RESPONSE')
            return original_decode(text, cls, *args, **kwargs)
        group = self.proposal()
        with patch.object(JSON, 'unmarshal', side_effect=broken_decode):
            reply = self.confirm(group)
        self.assertEqual(reply.status, 'unknown')
        self.assertNotIn('PRIVATE', reply.text)
        saved = self.core.store.find_group(group.confirmation_code, 'management', self.wire.owner, 'kpi-agg', self.wire.bot)
        self.assertEqual(saved.created_chat_id, self.wire.payload['data']['chat_id'])
        self.assertIs(chat_module.Transport, original_transport)
        self.assertEqual(len(self.wire.posts), 1)

    def test_stopping_before_send_including_token_fetch_never_starts_post(self):
        for during_token in (False, True):
            with self.subTest(during_token=during_token):
                self.wire.stopped = not during_token
                if during_token:
                    self.wire.after_token = lambda: setattr(self.wire, 'stopped', True)
                group = self.proposal()
                self.assertEqual(self.confirm(group).status, 'failed')
                self.wire.stopped = False
                self.assertEqual(self.confirm(group).status, 'failed')
        self.assertEqual(self.wire.posts, [])

    def test_inflight_stop_preserves_known_resource_as_unknown(self):
        group = self.proposal()
        self.wire.after_create = lambda: setattr(self.wire, 'stopped', True)
        self.assertEqual(self.confirm(group).status, 'unknown')
        self.wire.stopped = False
        self.assertEqual(self.confirm(group).status, 'unknown')
        self.assertEqual(len(self.wire.posts), 1)
        saved = self.core.store.find_group(group.confirmation_code, 'management', self.wire.owner, 'kpi-agg', self.wire.bot)
        self.assertEqual((saved.status, saved.created_chat_id), ('unknown', self.wire.payload['data']['chat_id']))

    def test_token_error_or_empty_token_does_not_fall_back_to_user_identity(self):
        import requests
        self.wire.token_error = requests.Timeout('PRIVATE AUTH ERROR')
        group = self.proposal()
        self.assertEqual(self.confirm(group).status, 'failed')
        self.wire.token_error = None
        self.wire.token_payload['tenant_access_token'] = ''
        other = self.proposal()
        self.assertEqual(self.confirm(other).status, 'failed')
        self.assertEqual(self.wire.posts, [])

    def test_raw_id_name_code_and_secret_never_enter_receipt_failure_log(self):
        group = self.proposal()
        self.wire.payload['msg'] = 'SECRET ' + group.confirmation_code + ' ' + group.group_name
        self.wire.status = 500
        records = []
        class Capture(__import__('logging').Handler):
            def emit(self, record):
                records.append(record.getMessage())
        logger = __import__('logging').getLogger()
        handler = Capture()
        logger.addHandler(handler)
        try:
            reply = self.confirm(group)
            bridge = FeishuBridge(self.core, self.wire.bot,
                                  lambda m, r: (_ for _ in ()).throw(RuntimeError('RAW-SECRET')))
            bridge._deliver(self.message('/confirm ' + group.confirmation_code), reply)
        finally:
            logger.removeHandler(handler)
        log = '\n'.join(records)
        for sensitive in (group.confirmation_code, group.group_name, group.requested_by,
                          group.source_chat_id, group.request_id, 'SECRET', self.wire.payload['data']['chat_id'],
                          self.wire.fixture['app_secret'], self.wire.fixture['tenant_response']['tenant_access_token']):
            self.assertNotIn(sensitive, log)
        self.assertNotIn('SECRET', reply.text)
        self.assertEqual(len(self.wire.posts), 1)


class OutputHTTPTests(unittest.TestCase):
    """Pinned SDK models and the real HTTPX send boundary; never live network."""

    def setUp(self):
        from importlib.metadata import version
        self.assertIsNotNone(importlib.util.find_spec("lark_oapi"), "Install lark-oapi==1.7.3; do not skip this test")
        self.assertEqual(version("lark-oapi"), "1.7.3")
        import httpx
        from tests.test_output import CoreRig
        from feishu_herdr_bridge import output

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rig = CoreRig(self.temp.name)
        self.transport = LarkTransport(Credentials("fixture-app", "fixture-secret"))
        self.posts, self.auth, self.models = [], [], []
        self.status = 200
        self.failure = None
        self.payload = None
        self.stopped = False
        self.after_token = self.after_message = lambda: None
        self.transport.is_stopping = lambda: self.stopped
        self.rig.observer = output.connect_output(self.rig.core, self.rig.herdr,
                                                  self.transport.send_output_once, clock=lambda: self.rig.now)
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("Real network is forbidden"))
            blocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(blocked.assert_not_called)
        from feishu_herdr_bridge.feishu import build_text_request
        def model(*args):
            request = build_text_request(*args)
            self.models.append(request)
            return request
        guarded_model = patch("feishu_herdr_bridge.feishu.build_text_request", side_effect=model)
        guarded_model.start()
        self.addCleanup(guarded_model.stop)
        transport_factory = patch("httpx.AsyncHTTPTransport", wraps=httpx.AsyncHTTPTransport)
        self.transports = transport_factory.start()
        self.addCleanup(transport_factory.stop)
        violations = []
        async def boundary(transport, request):
            try:
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.url.host, "open.feishu.cn")
                if request.url.path.endswith("/tenant_access_token/internal"):
                    self.auth.append(request)
                    self.assertEqual(json.loads(request.content), {"app_id": "fixture-app", "app_secret": "fixture-secret"})
                    self.after_token()
                    return httpx.Response(200, json={"code": 0, "tenant_access_token": "fixture-tenant-token"}, request=request)
                self.assertEqual(request.url.path, "/open-apis/im/v1/messages")
                self.assertEqual(dict(request.url.params), {"receive_id_type": "chat_id"})
                self.assertEqual(request.headers["Authorization"], "Bearer fixture-tenant-token")
                self.posts.append(request)
                if self.failure is not None:
                    if callable(self.failure):
                        await self.failure()
                    else:
                        raise self.failure
                self.after_message()
                body = json.loads(request.content)
                data = self.payload or {"code": 0, "data": {"chat_id": body["receive_id"], "message_id": "fixture-message"}}
                if isinstance(data, bytes):
                    return httpx.Response(self.status, content=data, request=request)
                return httpx.Response(self.status, json=data, request=request,
                                      headers={"Location": "https://open.feishu.cn/open-apis/im/v1/messages"})
            except AssertionError as error:
                violations.append(str(error))
                raise
        network = patch("httpx.AsyncHTTPTransport.handle_async_request", new=boundary)
        network.start()
        self.addCleanup(network.stop)
        self.addCleanup(lambda: self.assertEqual(violations, []))
        # A real SDK WebSocket supplies this loop in production. This test starts
        # only a local event loop; no SDK WebSocket or external connection starts.
        import asyncio
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        def run_loop():
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
        worker = threading.Thread(target=run_loop, daemon=True)
        worker.start()
        self.assertTrue(ready.wait(2))
        self.transport._loop = loop
        def close_loop():
            loop.call_soon_threadsafe(loop.stop)
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.addCleanup(close_loop)

    def complete_round(self, index=0, message_id="source", history=None):
        from tests.test_output import FIXTURE, screen
        r = self.rig
        r.screens["pane-a"] = screen("devin", FIXTURE["history"] if history is None else history)
        self.assertEqual(r.send(FIXTURE["rounds"][index]["prompt"], message_id=message_id).code, "submitted")
        r.answer(index, history=history)
        r.tick()
        r.tick(2)

    def test_two_turns_use_real_models_tenant_identity_and_two_message_posts(self):
        from tests.test_output import FIXTURE
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        from lark_oapi.core.enum import AccessTokenType

        first, second = FIXTURE["rounds"]
        self.complete_round(message_id="p1")
        self.complete_round(1, "p2", FIXTURE["history"] + [first["user"], first["assistant"]])
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(len(self.auth), 2)
        self.assertEqual([json.loads(json.loads(p.content)["content"])["text"] for p in self.posts],
                         ["主控 Pane pane-a\n" + first["expected"], "主控 Pane pane-a\n" + second["expected"]])
        for request in self.models:
            self.assertIsInstance(request, CreateMessageRequest)
            self.assertIsInstance(request.body, CreateMessageRequestBody)
            self.assertEqual(request.token_types, {AccessTokenType.TENANT})
            self.assertEqual(request.body.receive_id, "chat-a")
        self.assertTrue(all(c.kwargs == {"retries": 0} for c in self.transports.call_args_list))
        self.assertEqual(self.rig.send(first["prompt"], message_id="p1").code, "duplicate")
        self.assertEqual(self.rig.send(second["prompt"], message_id="p2").code, "duplicate")
        self.rig.tick(200)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(len(self.rig.prompts), 2)
        self.assertTrue(all(self.rig.store.get_request(key).status == "done" for key in ("p1", "p2")))

    def test_http_errors_redirects_disconnect_and_bad_response_do_not_retry(self):
        import httpx
        cases = [(code, None, None) for code in (301, 302, 307, 308, 403, 429, 500, 503)]
        cases += [(200, httpx.ReadTimeout("PRIVATE"), None), (200, httpx.ConnectError("PRIVATE"), None),
                  (200, None, b'{'), (200, None, {"code": 0, "data": {"message_id": "fixture"}}),
                  (200, None, {"code": 0, "data": {"chat_id": "wrong-chat", "message_id": "fixture"}})]
        for index, (status, error, payload) in enumerate(cases):
            with self.subTest(index=index):
                self.status, self.failure, self.payload = status, error, payload
                before = len(self.posts)
                source = f"fault-{index}"
                self.complete_round(message_id=source)
                self.rig.tick(130)
                self.assertEqual(self.rig.send("duplicate", message_id=source).code, "duplicate")
                self.assertEqual(len(self.posts), before + 1)
                self.assertTrue(self.rig.store.get_binding("chat-a").valid)
                self.assertEqual(self.rig.store.get_request(source).status, "done")
                self.assertEqual(self.rig.observer._active, {})

    def test_stop_after_auth_skips_message_and_inflight_stop_is_unknown(self):
        self.after_token = lambda: setattr(self, "stopped", True)
        self.complete_round(message_id="auth-stop")
        self.assertEqual(len(self.auth), 1)
        self.assertEqual(self.posts, [])
        self.stopped = False
        self.after_token = lambda: None
        self.after_message = lambda: setattr(self, "stopped", True)
        self.complete_round(message_id="inflight-stop")
        self.assertEqual(len(self.posts), 1)
        self.stopped = False
        self.assertEqual(self.rig.send("duplicate", message_id="inflight-stop").code, "duplicate")
        self.rig.tick(130)
        self.assertEqual(len(self.posts), 1)

    def test_total_timeout_cancels_pending_http_without_background_retry(self):
        import asyncio
        import time
        cancelled = threading.Event()
        async def held():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.failure = held
        start = time.monotonic()
        self.complete_round(message_id="timeout")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 4.5)
        self.assertTrue(cancelled.wait(1))
        self.assertEqual(len(self.posts), 1)
        self.rig.tick(130)
        self.assertEqual(self.rig.send("duplicate", message_id="timeout").code, "duplicate")
        self.assertEqual(len(self.posts), 1)

    def test_private_payloads_and_exceptions_never_enter_audit_or_failure_reply(self):
        import httpx
        self.failure = httpx.ReadError("PRIVATE-token PRIVATE-chat PRIVATE-prompt")
        with self.assertLogs("feishu_herdr_bridge.output", level="INFO") as logs:
            self.complete_round(message_id="PRIVATE-source")
        text = "\n".join(logs.output)
        for secret in ("PRIVATE", "fixture-secret", "fixture-tenant-token", "chat-a", "pane-a"):
            self.assertNotIn(secret, text)
        self.assertEqual(len(self.rig.receipts), 1)  # Original submission only; no failure echo.
        self.assertEqual(self.rig.receipts[0][1].code, "submitted")


class OutputSendGuardTests(unittest.TestCase):
    def test_stopped_or_invalid_destination_never_enters_http_or_sdk(self):
        from dataclasses import replace
        from feishu_herdr_bridge.output import Origin
        transport = LarkTransport.__new__(LarkTransport)
        transport.is_stopping = lambda: True
        origin = Origin("source", "chat", "user", "kpi-agg", "workspace", "pane", 1, "bot", "devin", "group")
        with patch.object(transport, "_output_post") as http:
            self.assertEqual(transport.send_output_once(origin, "text"), "failed")
            transport.is_stopping = lambda: False
            for bad in (replace(origin, session="other"), replace(origin, chat_type="p2p")):
                self.assertEqual(transport.send_output_once(bad, "text"), "failed")
            self.assertEqual(transport.send_output_once(origin, "x" * 3001), "failed")
            self.assertEqual(transport.send_output_once(origin, "\x1b[31m"), "failed")
            http.assert_not_called()

    def test_expired_send_waiting_for_sdk_loop_is_cancelled_without_running_http(self):
        import asyncio
        import time
        from feishu_herdr_bridge.output import Origin
        loop = asyncio.new_event_loop()
        entered, release = threading.Event(), threading.Event()
        ran = []
        transport = LarkTransport.__new__(LarkTransport)
        transport.is_stopping = lambda: False
        transport._loop = loop
        origin = Origin("source", "chat", "user", "kpi-agg", "workspace", "pane", 1, "bot", "devin", "group")
        def blocked_callback():
            entered.set()
            release.wait(5)
        def run_loop():
            asyncio.set_event_loop(loop)
            loop.call_soon(blocked_callback)
            loop.run_forever()
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.close()
        original_post = transport._output_post
        errors = []
        async def http(*args):
            try:
                result = await original_post(*args)
                ran.append(result)
                return result
            except BaseException as error:
                errors.append(type(error).__name__)
                raise
        async def drained():
            pass
        worker = threading.Thread(target=run_loop, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with patch.object(transport, "_output_post", new=http), \
                    patch("feishu_herdr_bridge.feishu.build_text_request") as build:
                start = time.monotonic()
                self.assertEqual(transport.send_output_once(origin, "text"), "unknown")
                self.assertLess(time.monotonic() - start, 4.5)
                release.set()
                asyncio.run_coroutine_threadsafe(drained(), loop).result(timeout=2)
                # Cancellation can race task startup. The real method must
                # reject an expired budget before even building an SDK request.
                self.assertTrue(all(result == "failed" for result in ran))
                self.assertEqual(errors, [])
                build.assert_not_called()
        finally:
            release.set()
            loop.call_soon_threadsafe(loop.stop)
            worker.join(3)
            self.assertFalse(worker.is_alive())
