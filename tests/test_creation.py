"""Creation tests use an injected CLI runner and schema-derived, non-live fixtures."""

from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from feishu_herdr_bridge.core import BridgeCore, Message
from feishu_herdr_bridge.herdr import HerdrAdapter, HerdrError, decode_protocol22, session_command
from feishu_herdr_bridge.store import Store


FIXTURE = Path(__file__).parent / "fixtures/herdr_0_9_0.json"


class StaticCLI:
    """In-process fake CLI boundary; never executes the configured executable."""

    def __init__(self):
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.agents = {a["pane_id"]: a for a in copy.deepcopy(self.fixture["list"]["result"]["agents"])}
        self.calls = []
        self.created = []
        self.started = []
        self.submitted = []
        self.modes = {}
        self.session = "configured-session"

    def adapter(self, **kwargs):
        return HerdrAdapter(self.session, command_builder=session_command("/offline/herdr"),
                            decoder=decode_protocol22, runner=self.run, **kwargs)

    def run(self, command, timeout):
        command = list(command)
        assert command[:3] == ["/offline/herdr", "--session", self.session]
        args = command[3:]
        action = "create" if args[:2] == ["workspace", "create"] else args[1]
        self.calls.append((action, args))
        mode = self.modes.get(action)
        if mode == "error":
            return subprocess.CompletedProcess(command, 1, json.dumps(self.fixture["error"]), "")
        if action == "list":
            assert args == ["agent", "list"]
            result = {"type": "agent_list", "agents": list(self.agents.values())}
        elif action == "get":
            assert len(args) == 3
            agent = copy.deepcopy(self.agents[args[2]])
            if mode == "wrong_workspace":
                agent["workspace_id"] = "other-workspace"
            result = {"type": "agent_info", "agent": agent}
        elif action == "create":
            assert len(args) == 7 and args[2] == "--cwd" and args[4] == "--label" and args[6] == "--no-focus"
            number = len(self.created) + 1
            w, t, p = f"workspace-new-{number}", f"tab-new-{number}", f"pane-new-{number}"
            self.created.append((w, t, p))
            result = {"type": "workspace_created", "workspace": {"workspace_id": w},
                      "tab": {"tab_id": t, "workspace_id": w},
                      "root_pane": {"pane_id": p, "workspace_id": w, "tab_id": t}}
        elif action == "start":
            assert len(args) == 9 and args[3] == "--kind" and args[5] == "--pane" and args[7] == "--timeout"
            name, kind, pane = args[2], args[4], args[6]
            w, t, p = next(item for item in self.created if item[2] == pane)
            agent = copy.deepcopy(self.fixture["start"]["result"]["agent"])
            agent.update(workspace_id=w, tab_id=t, pane_id=p, name=name, agent=kind, display_agent=kind)
            if mode == "wrong_kind":
                agent.update(agent="unknown", display_agent="unknown")
            self.agents[p] = agent
            self.started.append((name, kind, p))
            result = {"type": "agent_started", "agent": agent, "argv": [kind]}
        elif action == "prompt":
            assert len(args) == 4
            self.submitted.append((args[2], args[3]))
            result = {"type": "agent_prompted"}
        elif action == "read":
            return subprocess.CompletedProcess(command, 0, "screen-" + args[2], "")
        else:
            raise AssertionError("Unexpected CLI action")
        if mode == "timeout_after":
            raise subprocess.TimeoutExpired(command, timeout)
        output = "{" if mode == "malformed_after" else json.dumps({"id": "static-call", "result": result})
        return subprocess.CompletedProcess(command, 0, output, "")


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite3")
        self.cli = StaticCLI()
        self.adapter = self.cli.adapter()
        self.now = 100.0
        self.serial = 0
        self.core = self.make_core()

    def make_core(self):
        return BridgeCore(self.store, self.adapter, projects={"demo": self.root},
                          allowed_users={"user", "other"}, allowed_chats={"chat-a", "chat-b"},
                          clock=lambda: self.now, operation_lock=threading.Lock())

    def message(self, text, chat="chat-a", user="user"):
        self.serial += 1
        return Message(f"m-{self.serial}", chat, user, text, self.now + 1)

    def send(self, text, **kwargs):
        return self.core.handle(self.message(text, **kwargs))

    def propose(self, kind="codex", chat="chat-a"):
        message = self.message(f"/new demo {kind}", chat)
        reply = self.core.handle(message)
        self.assertEqual(reply.code, "creation_pending", reply)
        code = reply.text.split("/confirm ", 1)[1].split("。", 1)[0]
        return self.store.find_creation(code, chat, "user")

    def confirm(self, creation, **kwargs):
        return self.send(f"/confirm {creation.confirmation_code}", **kwargs)

    def old_binding(self):
        return self.store.bind(chat_id="chat-a", session=self.cli.session, workspace_id="workspace-a",
                               pane_id="pane-a", agent_name="lead-a", user_id="user", now=self.now,
                               expected_revision=None)

    def test_new_is_a_proposal_with_no_cli_or_binding_side_effect(self):
        old = self.old_binding()
        creation = self.propose()
        self.assertEqual(creation.expires_at, 400.0)
        self.assertRegex(creation.agent_name, r"^[a-z][a-z0-9_-]{0,31}$")
        self.assertEqual(creation.status, "pending")
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.store.get_binding("chat-a"), old)
        self.assertEqual(self.store.get_request(creation.request_id).herdr_session, self.cli.session)

    def test_both_kinds_create_then_start_then_verify_then_bind(self):
        for kind in ("codex", "claude"):
            with self.subTest(kind=kind):
                creation = self.propose(kind)
                self.assertEqual(self.confirm(creation).code, "created")
                binding = self.store.get_binding("chat-a")
                self.assertEqual(binding.pane_id, self.cli.created[-1][2])
                self.assertEqual(binding.workspace_id, self.cli.created[-1][0])
                self.assertEqual(binding.herdr_session, self.cli.session)
                self.assertEqual(self.cli.started[-1][1], kind)
                self.assertEqual([a for a, _ in self.cli.calls[-4:]], ["list", "create", "start", "get"])
                self.assertEqual(self.store.find_creation(creation.confirmation_code, "chat-a", "user").status, "done")
        self.assertEqual(len(self.cli.created), 2)
        self.assertEqual(self.store.get_binding("chat-a").revision, 2)

    def test_invalid_cross_user_cross_chat_and_expired_confirm_never_write(self):
        creation = self.propose()
        self.assertEqual(self.send("/confirm no-such-code").status, "failed")
        self.assertEqual(self.confirm(creation, user="other").code, "invalid_confirmation")
        self.assertEqual(self.confirm(creation, chat="chat-b").code, "invalid_confirmation")
        self.now = creation.expires_at
        self.assertEqual(self.confirm(creation).code, "confirmation_expired")
        self.assertEqual(self.cli.calls, [])

    def test_duplicate_message_and_repeated_confirmation_do_not_repeat_writes(self):
        creation = self.propose()
        message = self.message(f"/confirm {creation.confirmation_code}")
        self.assertEqual(self.core.handle(message).code, "created")
        self.assertEqual(self.core.handle(message).code, "duplicate")
        self.assertEqual(self.confirm(creation).code, "confirmation_used")
        self.assertEqual((len(self.cli.created), len(self.cli.started)), (1, 1))

    def test_new_supersedes_pending_and_cancel_is_chat_local(self):
        old = self.propose()
        current = self.propose("claude")
        self.assertEqual(self.confirm(old).code, "confirmation_used")
        self.assertEqual(self.send("/cancel", chat="chat-b").code, "nothing_to_cancel")
        self.assertEqual(self.send("/cancel").code, "cancelled")
        self.assertEqual(self.confirm(current).code, "confirmation_used")
        self.assertEqual(self.cli.calls, [])

    def test_changed_binding_or_session_prevents_creation(self):
        creation = self.propose()
        self.old_binding()
        self.assertEqual(self.confirm(creation).code, "binding_changed")
        self.assertEqual(self.cli.calls, [])
        other = self.propose(chat="chat-b")
        self.adapter = HerdrAdapter("another-session")
        self.core = self.make_core()
        self.assertEqual(self.confirm(other, chat="chat-b").code, "session_changed")
        self.assertEqual(self.cli.calls, [])

    def test_arbitrary_paths_kinds_and_changed_project_are_rejected(self):
        for command in ("/new /tmp codex", "/new ../demo codex", "/new demo shell"):
            self.assertEqual(self.send(command).status, "failed")
        creation = self.propose()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.core.projects["demo"] = elsewhere
        self.assertEqual(self.confirm(creation).code, "project_changed")
        self.assertEqual(self.cli.calls, [])

    def test_one_name_regeneration_then_no_more(self):
        occupied, available = "fb-codex-000000000000", "fb-codex-111111111111"
        self.cli.agents["pane-a"]["name"] = occupied
        with patch("feishu_herdr_bridge.core.agent_name", side_effect=[occupied, available]) as names:
            creation = self.propose()
            self.assertEqual(self.confirm(creation).code, "created")
            self.assertEqual(names.call_count, 2)
        self.assertEqual(self.cli.started[0][0], available)
        self.assertEqual(self.store.get_binding("chat-a").agent_name, available)

    def test_second_name_conflict_does_not_create_workspace(self):
        occupied = "fb-codex-000000000000"
        self.cli.agents["pane-a"]["name"] = occupied
        with patch("feishu_herdr_bridge.core.agent_name", return_value=occupied) as names:
            creation = self.propose()
            self.assertEqual(self.confirm(creation).code, "name_conflict")
            self.assertEqual(names.call_count, 2)
        self.assertEqual(self.cli.created, [])

    def test_known_start_conflict_keeps_resource_and_old_binding(self):
        old = self.old_binding()
        creation = self.propose()
        # Normalized synthetic rejection; exact live error codes remain pending.
        with patch.object(self.adapter, "start_agent", side_effect=HerdrError("name_conflict")) as start:
            reply = self.confirm(creation)
        self.assertEqual(reply.status, "failed")
        self.assertEqual(start.call_count, 1)
        self.assertIn(self.cli.created[0][0], reply.text)
        self.assertEqual(self.store.get_binding("chat-a"), old)
        self.assertEqual(self.confirm(creation).code, "confirmation_used")

    def test_start_error_preserves_ids_and_does_not_replace_old_binding(self):
        old = self.old_binding()
        creation = self.propose()
        self.cli.modes["start"] = "error"
        reply = self.confirm(creation)
        self.assertEqual(reply.status, "unknown")
        saved = self.store.find_creation(creation.confirmation_code, "chat-a", "user")
        self.assertEqual((saved.workspace_id, saved.tab_id, saved.pane_id), self.cli.created[0])
        self.assertEqual(self.store.get_binding("chat-a"), old)
        self.assertEqual(self.confirm(creation).code, "confirmation_used")
        self.assertEqual(len(self.cli.created), 1)

    def test_unknown_create_never_retries_or_starts_an_agent(self):
        creation = self.propose()
        self.cli.modes["create"] = "timeout_after"
        self.assertEqual(self.confirm(creation).status, "unknown")
        self.assertEqual(self.confirm(creation).code, "confirmation_used")
        self.assertEqual(len(self.cli.created), 1)
        self.assertEqual(self.cli.started, [])
        self.assertIsNone(self.store.get_binding("chat-a"))

    def test_start_success_still_requires_get_and_type_verification(self):
        old = self.old_binding()
        creation = self.propose()
        self.cli.modes["start"] = "wrong_kind"
        self.assertEqual(self.confirm(creation).code, "start_unverified")
        self.assertEqual(self.store.get_binding("chat-a"), old)
        self.assertEqual(len(self.cli.started), 1)

    def test_restart_marks_partial_creation_unknown_without_replay(self):
        creation = self.propose()
        self.store.begin_creation(creation, self.cli.session, self.now)
        self.store.save_created_workspace(creation.request_id, "w-partial", "t-partial", "p-partial", self.now)
        self.core = self.make_core()
        self.assertEqual(self.confirm(creation).code, "confirmation_used")
        saved = self.store.find_creation(creation.confirmation_code, "chat-a", "user")
        self.assertEqual(saved.status, "unknown")
        self.assertEqual(saved.workspace_id, "w-partial")
        self.assertEqual(self.cli.calls, [])

    def test_binding_race_rolls_back_completion_and_preserves_resource(self):
        old = self.old_binding()
        creation = self.propose()
        complete = self.store.complete_creation

        def occupied(request_id, session, now):
            w, _, p = self.cli.created[-1]
            self.store.bind(chat_id="chat-b", session=session, workspace_id=w, pane_id=p,
                            agent_name="other", user_id="other", now=now, expected_revision=None)
            return complete(request_id, session, now)

        with patch.object(self.store, "complete_creation", side_effect=occupied):
            reply = self.confirm(creation)
        self.assertEqual(reply.status, "failed")
        self.assertEqual(self.store.get_binding("chat-a"), old)
        self.assertEqual(len(self.cli.created), 1)

    def test_unused_missing_project_does_not_block_an_allowed_project(self):
        self.core.projects["unused"] = self.root / "does-not-exist"
        creation = self.propose()
        self.assertEqual(self.confirm(creation).code, "created")

    def test_uncertain_start_is_not_repeated_and_preserves_old_binding(self):
        old = self.old_binding()
        creation = self.propose()
        self.cli.modes["start"] = "timeout_after"
        self.assertEqual(self.confirm(creation).status, "unknown")
        self.assertEqual(self.confirm(creation).code, "confirmation_used")
        self.assertEqual((len(self.cli.created), len(self.cli.started)), (1, 1))
        self.assertEqual(self.store.get_binding("chat-a"), old)
