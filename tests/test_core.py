from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import threading
import unittest
from pathlib import Path

from feishu_herdr_bridge.core import BridgeCore, Message
from feishu_herdr_bridge.herdr import run_command
from feishu_herdr_bridge.store import Store
from tests.fake_herdr import FakeHerdR, SESSION


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeHerdR(self.root)
        self.store = Store(self.root / "bridge.sqlite3")
        self.now = 100.0
        self.serial = 0
        self.lock = threading.Lock()
        self.core = self.make_core()

    def make_core(self, adapter=None):
        return BridgeCore(
            self.store, adapter or self.fake.adapter(),
            allowed_chats={"chat-a", "chat-b"}, allowed_users={"user"},
            clock=lambda: self.now, operation_lock=self.lock,
        )

    def message(self, text, chat="chat-a", **kwargs):
        self.serial += 1
        return Message(
            kwargs.get("message_id", f"m-{self.serial}"), chat,
            kwargs.get("user", "user"), text, kwargs.get("stamp", self.now + 1),
        )

    def send(self, text, chat="chat-a", **kwargs):
        return self.core.handle(self.message(text, chat, **kwargs))

    def bind(self, chat="chat-a", suffix="a"):
        reply = self.send(f"/bind workspace-{suffix} pane-{suffix}", chat)
        self.assertEqual(reply.code, "bound", reply)
        return self.store.get_binding(chat)

    def test_duplicate_message_is_submitted_once(self):
        self.bind()
        message = self.message("只执行一次")
        first = self.core.handle(message)
        second = self.core.handle(message)
        self.assertEqual(len(self.fake.events("submitted")), 1)
        self.assertEqual(first.code, "submitted")
        self.assertEqual(second.code, "duplicate")
        self.assertEqual(self.store.get_request(message.message_id).status, "done")

    def test_two_chats_route_only_to_their_original_panes(self):
        self.bind()
        self.bind("chat-b", "b")
        self.assertEqual(self.send("任务 A").code, "submitted")
        self.assertEqual(self.send("任务 B", "chat-b").code, "submitted")
        self.assertEqual(
            [(e["session"], e["workspace_id"], e["pane_id"], e["text"])
             for e in self.fake.events("submitted")],
            [(SESSION, "workspace-a", "pane-a", "任务 A"),
             (SESSION, "workspace-b", "pane-b", "任务 B")],
        )
        self.assertIn("screen-A", self.send("/read").text)
        self.assertNotIn("screen-B", self.send("/read").text)
        self.assertIn("screen-B", self.send("/read", "chat-b").text)

    def test_workspace_has_only_one_chat_owner(self):
        old = self.bind("chat-b", "b")
        self.bind()
        reply = self.send("/bind workspace-a pane-a", "chat-b")
        self.assertEqual(reply.code, "workspace_occupied")
        self.assertEqual(self.store.get_binding("chat-b"), old)

    def test_unbound_message_does_not_discover_a_default_target(self):
        self.assertEqual(self.send("hello").code, "unbound")
        self.assertEqual(self.fake.events(), [])

    def test_unauthorized_sources_do_not_touch_cli_or_requests(self):
        for kwargs in ({"user": "stranger"}, {"chat": "private"}):
            message = self.message("/agents", **kwargs)
            self.assertEqual(self.core.handle(message).code, "forbidden")
            self.assertIsNone(self.store.get_request(message.message_id))
        self.assertEqual(self.fake.events(), [])

    def test_duplicate_id_cannot_disclose_another_chats_result(self):
        self.bind()
        original = self.message("private task", message_id="shared-id")
        self.core.handle(original)
        forged = self.message("/read", chat="chat-b", message_id="shared-id")
        reply = self.core.handle(forged)
        self.assertEqual(reply.code, "message_conflict")
        self.assertNotIn("workspace-a", reply.text)
        self.assertNotIn("private task", reply.text)
        self.assertEqual(len(self.fake.events("submitted")), 1)

    def test_unknown_commands_and_control_characters_are_not_forwarded(self):
        self.bind()
        before = len(self.fake.events())
        for text in ("/clear", "/new project codex", "/read extra", "\x1b[31m", "a\x00b", "a\x7fb", "\u009b"):
            with self.subTest(text=repr(text)):
                self.assertEqual(self.send(text).status, "failed")
        self.assertEqual(len(self.fake.events()), before)

    def test_unicode_multiline_body_is_preserved_as_one_argument(self):
        self.bind()
        body = "检查中文与空格\n第二行 $(touch SHOULD_NOT_EXIST); 'quoted'"
        self.assertEqual(self.send(body).code, "submitted")
        self.assertEqual(self.fake.events("submitted")[0]["text"], body)
        prompt = [e for e in self.fake.events("call") if e["argv"][1] == "prompt"][0]
        self.assertEqual(prompt["argv"], ["agent", "prompt", "pane-a", body])

    def test_working_devin_exact_queue_is_confirmed_once(self):
        state = self.fake.load()
        agent = state["sessions"][SESSION]["agents"]["pane-a"]
        agent.update(kind="devin", status="working")
        rule_top = "─" * 40 + " (bypass permissions on) ─"
        rule_bottom = "─" * 46
        status = "SWE-2 Max" + " " * 24 + "Context: 120k / 262k tokens (46%)"
        prompt = "测试自动回复"
        state["sessions"][SESSION]["screens"]["pane-a"] = "\n".join([
            " ⏺ Existing work", "", "⠀⢸ Running tools · 1m 0s (esc twice to interrupt)",
            "── 1 queued " + "─" * 20 + " ↑ edit · ↵ send now ──", "○ " + prompt,
            rule_top, "❭ Press Enter to send queued messages now", rule_bottom, status,
            "3 subagents · ↓ select", "",
        ])
        self.fake.save(state)
        self.bind()
        self.assertEqual(self.send(prompt).code, "submitted")
        self.assertEqual([(e["pane_id"], e["key"]) for e in self.fake.events("key")],
                         [("pane-a", "enter")])

    def test_leading_hyphen_is_rejected_until_live_verified(self):
        self.bind()
        self.assertEqual(self.send("--wait").code, "leading_hyphen_unverified")
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertTrue(self.store.get_binding("chat-a").valid)

    def test_stale_and_unparseable_timestamps_do_not_submit(self):
        self.bind()
        for stamp in (99.0, None, float("nan"), float("inf"), True, "bad"):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.send("late", stamp=stamp).status, "failed")
        self.assertEqual(self.fake.events("submitted"), [])

    def test_rebinding_does_not_redirect_old_or_delayed_messages(self):
        self.bind()
        old = self.message("old task")
        self.core.handle(old)
        delayed = self.message("not yet delivered")
        self.now = 200.0
        self.bind(suffix="b")
        self.assertEqual(self.core.handle(old).code, "duplicate")
        self.assertEqual(self.core.handle(delayed).code, "stale_message")
        self.assertEqual(self.send("new task").code, "submitted")
        self.assertEqual([e["pane_id"] for e in self.fake.events("submitted")], ["pane-a", "pane-b"])

    def test_rebinding_while_get_runs_does_not_retarget_the_old_request(self):
        original = self.bind()

        def rebind_during_get(command, timeout):
            if list(command[-3:]) == ["agent", "get", "pane-a"]:
                self.store.bind(
                    chat_id="chat-a", session=SESSION, workspace_id="workspace-b",
                    pane_id="pane-b", agent_name="lead", user_id="user", now=200,
                    expected_revision=original.revision,
                )
            return run_command(command, timeout)

        self.core = self.make_core(self.fake.adapter(runner=rebind_during_get))
        reply = self.send("must not move")
        self.assertEqual(reply.code, "binding_changed")
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertTrue(self.store.get_binding("chat-a").valid)
        self.assertEqual(self.store.get_binding("chat-a").pane_id, "pane-b")

    def test_wrong_workspace_reply_invalidates_without_prompt(self):
        self.bind()
        self.fake.mode("get", "wrong_workspace")
        self.assertEqual(self.send("hello").code, "workspace_mismatch")
        self.assertFalse(self.store.get_binding("chat-a").valid)
        self.assertEqual(self.fake.events("submitted"), [])

    def test_failed_bind_keeps_the_previous_binding(self):
        old = self.bind()
        self.assertEqual(self.send("/bind workspace-a pane-b").code, "workspace_mismatch")
        self.assertEqual(self.store.get_binding("chat-a"), old)

    def test_missing_target_invalidates_without_any_bare_input(self):
        self.bind()
        state = self.fake.load()
        del state["sessions"][SESSION]["agents"]["pane-a"]
        self.fake.save(state)
        self.assertEqual(self.send("do not send to shell").code, "target_missing")
        self.assertFalse(self.store.get_binding("chat-a").valid)
        self.assertEqual(self.send("again").code, "binding_invalid")
        self.assertFalse(any(e["argv"][1] == "prompt" for e in self.fake.events("call")))

    def test_prompt_validates_again_if_target_disappears_after_get(self):
        self.bind()
        self.fake.mode("prompt", "target_gone")
        self.assertEqual(self.send("no fallback").code, "target_missing")
        self.assertFalse(self.store.get_binding("chat-a").valid)
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertTrue(all(e["argv"][0] == "agent" for e in self.fake.events("call")))

    def test_blocked_invalidates_and_never_sends_confirmation_keys(self):
        self.bind()
        self.fake.mode("prompt", "blocked")
        self.assertEqual(self.send("task").code, "blocked")
        self.assertFalse(self.store.get_binding("chat-a").valid)
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertTrue(all(e["argv"][1] in {"get", "prompt"} for e in self.fake.events("call")))

    def test_after_submission_timeout_is_unknown_and_not_retried(self):
        self.bind()
        self.fake.mode("prompt", "timeout_after")
        self.core = self.make_core(self.fake.adapter(write_timeout=0.3))
        message = self.message("possibly accepted")
        reply = self.core.handle(message)
        self.assertEqual(reply.status, "unknown")
        self.assertEqual(reply.code, "timeout")
        self.assertFalse(self.store.get_binding("chat-a").valid)
        self.assertEqual(self.core.handle(message).code, "duplicate")
        self.assertEqual(len(self.fake.events("submitted")), 1)
        self.assertEqual(self.store.get_request(message.message_id).status, "unknown")

    def test_unparseable_submit_response_is_unknown_without_replay(self):
        self.bind()
        self.fake.mode("prompt", "malformed_after")
        message = self.message("one try")
        self.assertEqual(self.core.handle(message).status, "unknown")
        self.core.handle(message)
        self.assertEqual(len(self.fake.events("submitted")), 1)
        self.assertFalse(self.store.get_binding("chat-a").valid)

    def test_busy_message_is_not_queued_or_replayed(self):
        self.bind()
        message = self.message("try now")
        self.lock.acquire()
        try:
            self.assertEqual(self.core.handle(message).code, "busy")
        finally:
            self.lock.release()
        self.assertEqual(self.core.handle(message).code, "duplicate")
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertEqual(self.send("new message").code, "submitted")

    def test_read_is_capped_and_sensitive_content_not_persisted(self):
        self.bind()
        secret = "SENSITIVE-SCREEN-CONTENT"
        state = self.fake.load()
        state["sessions"][SESSION]["screens"]["pane-a"] = (secret + "x" * 50 + "\n") * 90
        self.fake.save(state)
        reply = self.send("/read")
        self.assertEqual(reply.code, "read")
        self.assertIn("输出已截断", reply.text)
        self.assertLess(len(reply.text), 3100)
        self.send("SENSITIVE-PROMPT-CONTENT")
        with closing(sqlite3.connect(self.store.path)) as db:
            dump = "\n".join(db.iterdump())
        self.assertNotIn(secret, dump)
        self.assertNotIn("SENSITIVE-PROMPT-CONTENT", dump)

    def test_read_failure_invalidates_binding(self):
        self.bind()
        self.fake.mode("read", "exit_error")
        self.assertEqual(self.send("/read").status, "failed")
        self.assertFalse(self.store.get_binding("chat-a").valid)

    def test_restart_preserves_binding_but_does_not_replay_processing(self):
        snapshot = self.bind()
        message = self.message("interrupted")
        self.store.claim(message_id=message.message_id, chat_id="chat-a", user_id="user",
                         action="prompt", snapshot=snapshot, now=self.now)
        self.store = Store(self.store.path)
        self.core = self.make_core()
        self.assertEqual(self.store.get_request(message.message_id).status, "unknown")
        self.assertFalse(self.store.get_binding("chat-a").valid)
        self.assertEqual(self.core.handle(message).code, "duplicate")
        self.assertEqual(self.fake.events("submitted"), [])
        self.bind()
        self.assertEqual(self.send("after manual check").code, "submitted")

    def test_config_session_change_does_not_use_the_old_binding(self):
        self.bind()
        self.core = self.make_core(self.fake.adapter(session="different-session"))
        before = len(self.fake.events())
        self.assertEqual(self.send("do not move sessions").code, "session_mismatch")
        self.assertEqual(len(self.fake.events()), before)
        self.assertFalse(self.store.get_binding("chat-a").valid)

    def test_agents_and_binding_display_are_target_aware(self):
        self.bind()
        text = self.send("/agents").text
        self.assertIn("Workspace（2）：", text)
        self.assertIn("Pane/Agent（2）：", text)
        self.assertIn("- workspace-a | Project Alpha", text)
        self.assertIn("  pane-a | Tab: 主控 | Agent: lead-a", text)
        self.assertIn("workspace-b | 项目乙", text)
        self.assertEqual(text.count("workspace-a | Project Alpha"), 2)
        self.assertIn("本会话", text)
        self.assertIn("未绑定", text)
        self.assertIn("workspace-a / pane-a", self.send("/bind").text)

    def test_same_pane_replacement_does_not_require_an_agent_fingerprint(self):
        self.bind()
        state = self.fake.load()
        agent = state["sessions"][SESSION]["agents"]["pane-a"]
        agent["name"] = None
        agent["kind"] = "claude"
        self.fake.save(state)
        self.assertEqual(self.send("still this pane").code, "submitted")
        self.assertEqual(self.fake.events("submitted")[0]["pane_id"], "pane-a")
        self.assertTrue(self.store.get_binding("chat-a").valid)

    def test_read_line_limit_is_applied_even_if_fake_returns_more(self):
        self.bind()
        state = self.fake.load()
        state["sessions"][SESSION]["screens"]["pane-a"] = "line\n" * 90
        self.fake.save(state)
        reply = self.send("/read")
        self.assertEqual(reply.text.splitlines().count("line"), 80)
        self.assertIn("输出已截断", reply.text)

    def test_lost_reply_and_reopened_store_do_not_repeat_a_completed_prompt(self):
        self.bind()
        message = self.message("completed before disconnect")
        self.assertEqual(self.core.handle(message).code, "submitted")
        self.store = Store(self.store.path)
        self.core = self.make_core()
        self.assertEqual(self.core.handle(message).code, "duplicate")
        self.assertEqual(len(self.fake.events("submitted")), 1)
        self.assertTrue(self.store.get_binding("chat-a").valid)

    def test_prepare_claims_slot_without_cli_and_abandon_releases_it(self):
        from feishu_herdr_bridge.core import Prepared

        self.bind()
        before = len(self.fake.events())
        message = self.message("not started")
        prepared = self.core.prepare(message)
        self.assertIsInstance(prepared, Prepared)
        self.assertTrue(self.lock.locked())
        self.assertEqual(len(self.fake.events()), before)
        self.assertEqual(self.send("other request").code, "busy")
        self.assertEqual(self.core.abandon(prepared).code, "worker_unavailable")
        self.assertFalse(self.lock.locked())
        self.assertEqual(self.core.handle(message).code, "duplicate")
        self.assertEqual(self.fake.events("submitted"), [])

    def test_prepared_message_rechecks_binding_when_worker_executes(self):
        self.bind()
        prepared = self.core.prepare(self.message("do not retarget"))
        old = self.store.get_binding("chat-a")
        self.store.bind(chat_id="chat-a", session=SESSION, workspace_id="workspace-b", pane_id="pane-b",
                        agent_name="other", user_id="user", now=self.now + 10,
                        expected_revision=old.revision)
        self.assertEqual(self.core.execute(prepared).code, "binding_changed")
        self.assertFalse(self.lock.locked())
        self.assertEqual(self.fake.events("submitted"), [])

    def test_legacy_message_constructor_and_unconfigured_group_gate(self):
        message = self.message('/agents')
        self.assertIsNone(message.chat_type)
        self.assertTrue(self.core.is_authorized(message))
        self.assertEqual(self.core.handle(message).code, 'agents')
        before = len(self.fake.events())
        self.assertEqual(self.send('/group-new private').code, 'forbidden')
        self.assertEqual(self.send('/confirm g-' + 'a' * 16).code, 'forbidden')
        self.assertEqual(len(self.fake.events()), before)

    def test_revoked_user_is_rechecked_before_prepared_work_executes(self):
        self.bind()
        prepared = self.core.prepare(self.message('no longer authorized'))
        self.core.allowed_users = frozenset()
        self.assertEqual(self.core.execute(prepared).code, 'forbidden')
        self.assertEqual(self.fake.events('submitted'), [])
        self.assertFalse(self.lock.locked())


    def test_help_names_exactly_the_three_creation_kinds(self):
        expected = ("/agents | /bind | /bind <workspace_id> <pane_id> | /read | "
                    "/new <项目别名> <codex或claude或devin> | /group-new <群名> | /confirm <确认码> | /cancel")
        self.assertEqual(self.send("/unsupported").text,
                         f"支持：{expected}。其他斜杠命令不会透传。")

    def test_devin_proposal_is_pending_without_cli_or_binding_changes(self):
        from unittest.mock import call, patch

        self.core.projects = {"demo": self.root}
        old = self.bind()
        before = len(self.fake.events())
        with patch("feishu_herdr_bridge.core.secrets.token_hex",
                   side_effect=["01234567", "0123456789ab"]) as random_hex:
            message = self.message("/new demo devin")
            reply = self.core.handle(message)
        self.assertEqual(reply.code, "creation_pending")
        self.assertEqual(random_hex.call_args_list, [call(4), call(6)])
        creation = self.store.find_creation("01234567", "chat-a", "user")
        self.assertEqual((creation.agent_kind, creation.status), ("devin", "pending"))
        self.assertEqual(creation.agent_name, "fb-devin-0123456789ab")
        self.assertRegex(creation.agent_name, r"^fb-devin-[0-9a-f]{12}$")
        self.assertEqual(creation.expires_at, self.now + 300)
        self.assertIn("Agent：devin / fb-devin-0123456789ab", reply.text)
        self.assertEqual(self.core.handle(message).code, "duplicate")
        self.assertEqual(self.store.get_binding("chat-a"), old)
        self.assertEqual(len(self.fake.events()), before)

    def test_new_rejects_case_variants_fourth_kinds_and_extra_arguments(self):
        self.core.projects = {"demo": self.root}
        for kind in ("Devin", "DEVIN", "Codex", "CLAUDE", "shell", "other"):
            with self.subTest(kind=kind):
                self.assertEqual(self.send(f"/new demo {kind}").code, "invalid_agent")
        self.assertEqual(self.send("/new demo devin --flag").code, "unknown_command")
        self.assertEqual(self.send("/new not-allowed devin").code, "invalid_project")
        self.assertEqual(self.send(f"/new {self.root} devin").code, "invalid_project")
        self.assertEqual(self.fake.events(), [])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM create_requests").fetchone()[0], 0)

    def test_optional_capture_failure_does_not_fail_or_replay_prompt(self):
        from dataclasses import replace
        from unittest.mock import Mock
        from feishu_herdr_bridge.output import OutputObserver

        self.bind()
        self.core.bot_open_id = "fixture-bot"
        self.core.output = Mock(spec=OutputObserver)
        self.core.output.capture.side_effect = RuntimeError("PRIVATE hook error")
        message = replace(self.message("normal task"), chat_type="group")
        reply = self.core.handle(message)
        self.assertEqual((reply.status, reply.code), ("done", "submitted"))
        self.assertNotIn("PRIVATE", reply.text)
        self.assertEqual(self.core.handle(message).code, "duplicate")
        self.assertEqual(len(self.fake.events("submitted")), 1)
        self.assertTrue(self.store.get_binding("chat-a").valid)
        self.core.output.arm.assert_not_called()

    def test_manual_read_cancels_observation_without_changing_cancel_command(self):
        from unittest.mock import Mock
        from feishu_herdr_bridge.output import OutputObserver

        self.bind()
        self.core.output = Mock(spec=OutputObserver)
        self.assertEqual(self.send("/cancel").code, "nothing_to_cancel")
        self.core.output.cancel_current.assert_not_called()
        reply = self.send("/read")
        self.assertEqual(reply.code, "read")
        self.assertIn("screen-A", reply.text)
        self.core.output.cancel_current.assert_called_once_with("chat-a")
