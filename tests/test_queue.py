"""B14 send-queue tests: fake HerdR only, no real pane dependency."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from feishu_herdr_bridge.core import BridgeCore, Message, SendQueue
from feishu_herdr_bridge.store import Store
from tests.fake_herdr import FakeHerdR, SESSION


class StubOutput:
    """Records capture/arm ordering; never blocks the prompt path."""

    def __init__(self, log):
        self.log = log

    def cancel_current(self, chat_id):
        self.log.append(("cancel_current", chat_id))

    def capture(self, origin, prompt):
        self.log.append(("capture", prompt))
        return "observation"

    def arm(self, observation):
        self.log.append(("arm", observation))
        return True

    def cancel(self, observation):
        self.log.append(("cancel", observation))


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeHerdR(self.root)
        self.store = Store(self.root / "bridge.sqlite3")
        self.now = 100.0   # Wall-style clock for request timestamps.
        self.mnow = 0.0    # Monotonic clock for queue waits/timeouts.
        self.serial = 0
        self.lock = threading.Lock()
        self.notices = []
        self.audit = []
        self.calls = []
        self.core = BridgeCore(
            self.store, self.fake.adapter(),
            allowed_chats={"chat-a", "chat-b"}, allowed_users={"user"},
            clock=lambda: self.now, operation_lock=self.lock, bot_open_id="bot-x",
        )
        adapter = self.fake.adapter()
        prompt = adapter.prompt

        def logging_prompt(pane_id, text):
            self.calls.append(("prompt", text))
            return prompt(pane_id, text)

        self.queue = SendQueue(
            self.core, get=adapter.get_agent, prompt=logging_prompt,
            send=self._notice, clock=lambda: self.mnow, interval=2.0, timeout=600.0,
            audit=lambda ref, rev, event, reason: self.audit.append((ref, rev, event, reason)),
        )
        self.core.send_queue = self.queue

    def _notice(self, origin, text):
        self.notices.append((origin.chat_id, text))
        return "sent"

    def set_status(self, status, pane="pane-a"):
        state = self.fake.load()
        state["sessions"][SESSION]["agents"][pane]["status"] = status
        self.fake.save(state)

    def message(self, text, chat="chat-a", group=True, **kwargs):
        self.serial += 1
        return Message(
            kwargs.get("message_id", f"m-{self.serial}"), chat,
            kwargs.get("user", "user"), text, kwargs.get("stamp", self.now + 1),
            "group" if group else None,
        )

    def send(self, text, chat="chat-a", **kwargs):
        return self.core.handle(self.message(text, chat, **kwargs))

    def bind(self, chat="chat-a", suffix="a"):
        reply = self.send(f"/bind workspace-{suffix} pane-{suffix}", chat, group=False)
        self.assertEqual(reply.code, "bound", reply)
        return self.store.get_binding(chat)

    def request(self, serial=None):
        return self.store.get_request(f"m-{serial or self.serial}")

    def test_idle_and_done_panes_send_immediately(self):
        self.bind()
        for status in ("idle", "done"):
            with self.subTest(status=status):
                self.set_status(status)
                self.assertEqual(self.send("立即发送").code, "submitted")
                self.assertFalse(self.queue.pending("pane-a"))
        self.assertEqual(len(self.fake.events("submitted")), 2)
        self.assertEqual(self.notices, [])

    def test_working_and_blocked_panes_queue_without_sending(self):
        self.bind()
        for status in ("working", "blocked"):
            with self.subTest(status=status):
                sent_before = len(self.fake.events("submitted"))
                self.set_status(status)
                reply = self.send("忙时到达")
                prompt_serial = self.serial
                self.assertEqual(reply.code, "queued", reply)
                self.assertIn("已排队", reply.text)
                self.assertTrue(self.queue.pending("pane-a"))
                self.assertEqual(len(self.fake.events("submitted")), sent_before)
                self.assertEqual(self.request(prompt_serial).status, "processing")
                self.set_status("idle")
                self.assertTrue(self.queue.poll())
                self.assertEqual(self.request(prompt_serial).result_code, "submitted")
        self.assertEqual(len(self.fake.events("submitted")), 2)

    def test_queued_prompt_sends_only_after_pane_becomes_idle(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("等空闲").code, "queued")
        self.assertFalse(self.queue.poll())  # Still working: nothing consumed.
        self.assertEqual(self.fake.events("submitted"), [])
        self.set_status("idle")
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")], ["等空闲"])
        self.assertEqual(self.request().status, "done")
        self.assertEqual(self.request().result_code, "submitted")
        self.assertEqual(self.notices, [])
        events = [(event, reason) for _ref, _rev, event, reason in self.audit]
        self.assertIn(("enqueued", "busy"), events)
        self.assertIn(("closed", "submitted"), events)

    def test_queue_timeout_notifies_and_records_failure(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("永远忙").code, "queued")
        self.mnow += 599.0
        self.assertFalse(self.queue.poll())  # Not yet timed out.
        self.mnow += 2.0
        self.assertTrue(self.queue.poll())
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertEqual(self.request().status, "failed")
        self.assertEqual(self.request().result_code, "queue_timeout")
        self.assertEqual(len(self.notices), 1)
        self.assertIn("pane 持续忙", self.notices[0][1])
        self.assertFalse(self.queue.pending("pane-a"))
        events = [(event, reason) for _ref, _rev, event, reason in self.audit]
        self.assertIn(("closed", "queue_timeout"), events)

    def test_same_pane_fifo_waits_for_prior_send(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("第一条").code, "queued")
        self.assertEqual(self.send("第二条").code, "queued")
        self.set_status("idle")
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")], ["第一条"])
        self.assertTrue(self.queue.pending("pane-a"))
        self.set_status("working")  # The pane is busy with the first prompt.
        self.assertFalse(self.queue.poll())
        self.assertEqual(len(self.fake.events("submitted")), 1)
        self.set_status("idle")
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")],
                         ["第一条", "第二条"])
        self.assertFalse(self.queue.pending("pane-a"))

    def test_late_message_never_jumps_ahead_of_a_pending_queue(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("先到").code, "queued")
        self.set_status("idle")  # Idle before the poller runs.
        self.assertEqual(self.send("后到").code, "queued")
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")], ["先到"])
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")],
                         ["先到", "后到"])

    def test_private_chat_keeps_the_immediate_path(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("私聊消息", group=False).code, "submitted")
        self.assertFalse(self.queue.pending("pane-a"))
        self.assertEqual(len(self.fake.events("submitted")), 1)

    def test_binding_change_while_queued_discards_with_notice(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("排队期间换绑").code, "queued")
        prompt_serial = self.serial
        self.bind(suffix="b")  # chat-a now owns workspace-b/pane-b.
        self.set_status("idle")
        self.assertTrue(self.queue.poll())
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertEqual(self.request(prompt_serial).status, "failed")
        self.assertEqual(self.request(prompt_serial).result_code, "binding_changed")
        self.assertEqual(len(self.notices), 1)
        self.assertIn("未发送", self.notices[0][1])

    def test_deferred_send_captures_baseline_then_arms(self):
        self.core.output = StubOutput(self.calls)
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("延迟捕获").code, "queued")
        self.assertEqual(self.calls, [])  # No capture or prompt while busy.
        self.set_status("idle")
        self.assertTrue(self.queue.poll())
        self.assertEqual(
            self.calls,
            [("cancel_current", "chat-a"), ("capture", "延迟捕获"),
             ("prompt", "延迟捕获"), ("arm", "observation")],
        )

    def test_poll_failure_retries_until_timeout(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("轮询失败").code, "queued")
        self.fake.mode("get", "exit_error")
        self.set_status("idle")
        self.assertFalse(self.queue.poll())  # get_agent raised; entry kept.
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertTrue(self.queue.pending("pane-a"))
        self.assertIn(("poll", "get_failed"),
                      [(event, reason) for _r, _v, event, reason in self.audit])
        prompt_serial = self.serial
        state = self.fake.load()  # Clear the failure mode; next poll succeeds.
        state["behavior"].pop("get")
        self.fake.save(state)
        self.assertTrue(self.queue.poll())
        self.assertEqual(self.request(prompt_serial).result_code, "submitted")

    def test_stop_drops_pending_entries_and_logs_restart_loss(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("重启即丢").code, "queued")
        self.queue.stop()
        self.assertFalse(self.queue.pending("pane-a"))
        self.assertIn(("queue", 0, "lost", "restart"), self.audit)
        self.assertEqual(self.request().status, "processing")  # Recovery owns it.
        store = Store(self.store.path)
        store.recover_incomplete(self.now + 10)
        self.assertEqual(self.request().status, "unknown")
        self.assertEqual(self.request().result_code, "interrupted")
        self.assertFalse(self.store.get_binding("chat-a").valid)

    def test_invalid_queue_timing_values_are_rejected(self):
        for kwargs in ({"interval": 0}, {"interval": -1}, {"timeout": 0},
                       {"interval": float("nan")}, {"timeout": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    SendQueue(self.core, get=self.fake.adapter().get_agent,
                              prompt=self.fake.adapter().prompt,
                              send=self._notice, **kwargs)


if __name__ == "__main__":
    unittest.main()
