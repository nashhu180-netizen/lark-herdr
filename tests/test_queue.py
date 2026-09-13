"""B14 send-queue tests: fake HerdR only, no real pane dependency."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from feishu_herdr_bridge.core import BridgeCore, Message, SendQueue
from feishu_herdr_bridge.herdr import Agent, HerdrError
from feishu_herdr_bridge.store import Store
from tests.fake_herdr import FakeHerdR, SESSION

EDGE_CAP = 5.0


class StubOutput:
    """Records capture/arm ordering; never blocks the prompt path."""

    def __init__(self, log, baseline_status="idle", arm_error=None):
        self.log = log
        self.baseline_status = baseline_status
        self.arm_error = arm_error

    def cancel_current(self, chat_id):
        self.log.append(("cancel_current", chat_id))

    def capture(self, origin, prompt):
        self.log.append(("capture", prompt))
        return SimpleNamespace(_baseline=SimpleNamespace(status=self.baseline_status))

    def arm(self, observation):
        self.log.append(("arm", observation))
        if self.arm_error is not None:
            raise self.arm_error
        return True

    def cancel(self, observation):
        self.log.append(("cancel", observation))


def scripted_agents(statuses):
    """get_agent callable walking a status script, repeating the last one."""
    calls = list(statuses)

    def get(pane_id):
        status = calls.pop(0) if calls else statuses[-1]
        if isinstance(status, Exception):
            raise status
        return Agent("workspace-a", "pane-a", "lead-a", "devin", status)

    return get


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
            edge_cap=EDGE_CAP,
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

    def events(self):
        return [(event, reason) for _ref, _rev, event, reason in self.audit]

    def test_idle_and_done_panes_send_immediately(self):
        self.bind()
        for status in ("idle", "done"):
            with self.subTest(status=status):
                self.queue._inflight.clear()  # Fresh pane: no armed edge gate.
                self.set_status(status)
                self.assertEqual(self.send("立即发送").code, "submitted")
                # Every send arms the in-flight gate; the next group message
                # queues until a busy edge is observed or the cap elapses.
                self.assertTrue(self.queue.pending("pane-a"))
                self.assertEqual(self.send("跟进的下一条").code, "queued")
                self.mnow += EDGE_CAP
                self.assertTrue(self.queue.poll())
        self.assertEqual(
            [e["text"] for e in self.fake.events("submitted")],
            ["立即发送", "跟进的下一条", "立即发送", "跟进的下一条"])
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
                self.mnow += EDGE_CAP  # Past the stale-idle window.
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
        events = self.events()
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
        self.assertIn(("closed", "queue_timeout"), self.events())

    def test_timeout_settle_stays_queued_until_persisted(self):
        """A-B14-003: no definitive receipt or dequeue before the row settles."""
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("超时落库失败").code, "queued")
        self.mnow += 601.0
        original = self.store.finish
        self.core.store.finish = lambda *a, **k: (_ for _ in ()).throw(RuntimeError())
        try:
            self.assertFalse(self.queue.poll())   # Entry kept; nothing closed.
            self.assertTrue(self.queue.pending("pane-a"))
            self.assertEqual(self.notices, [])
            self.assertEqual(self.request().status, "processing")
            self.assertIn(("poll", "settle_failed"), self.events())
        finally:
            self.core.store.finish = original
        self.assertTrue(self.queue.poll())        # Next poll settles for real.
        self.assertEqual(self.request().result_code, "queue_timeout")
        self.assertEqual(len(self.notices), 1)
        self.assertFalse(self.queue.pending("pane-a"))

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
        self.assertIn(("edge", "observed"), self.events())
        self.set_status("idle")
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")],
                         ["第一条", "第二条"])
        self.assertFalse(self.queue._queues.get("pane-a"))  # FIFO drained; gate stays armed.

    def test_stale_idle_window_never_drains_the_queue(self):
        """A-B14-001: a still-'idle' read inside the lag window is not a send go."""
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("先排队一").code, "queued")
        self.assertEqual(self.send("先排队二").code, "queued")
        self.set_status("idle")   # Stays 'idle': herdr status lags the real pane.
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")], ["先排队一"])
        self.assertFalse(self.queue.poll())  # Gate holds: second must not send yet.
        self.assertEqual(len(self.fake.events("submitted")), 1)
        self.mnow += EDGE_CAP - 0.1
        self.assertFalse(self.queue.poll())  # Still inside the unproven window.
        self.assertEqual(len(self.fake.events("submitted")), 1)
        self.mnow += 0.2                     # Cap elapsed: gate opens with a log.
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")],
                         ["先排队一", "先排队二"])
        self.assertIn(("edge", "unobserved"), self.events())

    def test_late_message_queues_while_submit_edge_is_unproven(self):
        """A-B14-001: the immediate path must not slip inside the lag window."""
        self.bind()
        self.set_status("idle")
        self.assertEqual(self.send("即时先走").code, "submitted")
        # agent_status still reads 'idle' (lag): the new message must queue.
        self.assertEqual(self.send("滞后窗口到达").code, "queued")
        self.assertEqual(len(self.fake.events("submitted")), 1)
        self.mnow += EDGE_CAP
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")],
                         ["即时先走", "滞后窗口到达"])

    def test_working_baseline_keeps_head_queued(self):
        """A-B14-002: a busy/unproven capture baseline must not be sent on."""
        self.core.output = StubOutput(self.calls, baseline_status="working")
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("基线仍忙").code, "queued")
        self.set_status("idle")
        self.assertFalse(self.queue.poll())
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertTrue(self.queue.pending("pane-a"))
        self.assertIn(("poll", "baseline_busy"), self.events())
        # The capture was cancelled and the prompt was never attempted.
        self.assertEqual([c[0] for c in self.calls],
                         ["cancel_current", "capture", "cancel"])
        self.core.output = StubOutput(self.calls)
        self.mnow += EDGE_CAP
        self.assertTrue(self.queue.poll())
        self.assertEqual(self.request().result_code, "submitted")

    def test_status_flip_after_capture_keeps_head_queued(self):
        """A-B14-002: a flip between capture and prompt is fail-closed."""
        self.core.output = StubOutput(self.calls)
        self.bind()
        adapter = self.fake.adapter()
        queue = SendQueue(self.core, get=scripted_agents(["idle", "working", "idle"]),
                          prompt=adapter.prompt, send=self._notice,
                          clock=lambda: self.mnow, interval=2.0, timeout=600.0,
                          edge_cap=EDGE_CAP,
                          audit=lambda r, v, e, s: self.audit.append((r, v, e, s)))
        self.core.send_queue = queue
        self.set_status("working")
        self.assertEqual(self.send("发送点翻转").code, "queued")
        self.set_status("idle")
        # First get: idle (gate passes); capture runs; second get: working → hold.
        self.assertFalse(queue.poll())
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertTrue(queue.pending("pane-a"))
        self.assertIn(("poll", "status_changed"), self.events())
        self.assertEqual([c[0] for c in self.calls],
                         ["cancel_current", "capture", "cancel"])
        self.assertTrue(queue.poll())  # Third get: idle → now it sends.
        self.assertEqual(self.request().result_code, "submitted")

    def test_prompt_failure_still_cancels_the_deferred_capture(self):
        """A-B14-004: every non-armed exit cancels the deferred capture."""
        self.core.output = StubOutput(self.calls)
        self.bind()
        adapter = self.fake.adapter()

        def failing_prompt(pane_id, text):
            self.calls.append(("prompt", text))
            raise HerdrError("invalid_output", uncertain=True)

        self.queue._prompt = failing_prompt
        self.set_status("working")
        self.assertEqual(self.send("发送即失败").code, "queued")
        self.set_status("idle")
        self.assertTrue(self.queue.poll())
        self.assertEqual([c[0] for c in self.calls],
                         ["cancel_current", "capture", "prompt", "cancel"])
        self.assertEqual(self.request().status, "unknown")
        self.assertEqual(self.request().result_code, "invalid_output")
        self.assertEqual(len(self.notices), 1)

    def test_arm_failure_leaves_no_live_capture(self):
        """A-B14-004: an arm() exception leaves no live capture behind."""
        self.core.output = StubOutput(self.calls, arm_error=RuntimeError("boom"))
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("arm 抛错").code, "queued")
        self.set_status("idle")
        self.assertTrue(self.queue.poll())
        self.assertEqual(self.request().result_code, "submitted")
        # arm raised; the finally path cancelled the leftover capture.
        self.assertEqual([c[0] for c in self.calls],
                         ["cancel_current", "capture", "prompt", "arm", "cancel"])

    def test_late_message_never_jumps_ahead_of_a_pending_queue(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("先到").code, "queued")
        self.set_status("idle")  # Idle before the poller runs.
        self.assertEqual(self.send("后到").code, "queued")
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")], ["先到"])
        self.mnow += EDGE_CAP  # Gate holds until the busy edge or the cap.
        self.assertTrue(self.queue.poll())
        self.assertEqual([e["text"] for e in self.fake.events("submitted")],
                         ["先到", "后到"])

    def test_private_chat_keeps_the_immediate_path(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("私聊消息", group=False).code, "submitted")
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
            [c[0] for c in self.calls],
            ["cancel_current", "capture", "prompt", "arm"])
        self.assertEqual(self.calls[1][1], "延迟捕获")
        self.assertEqual(self.calls[2][1], "延迟捕获")

    def test_poll_failure_retries_until_timeout(self):
        self.bind()
        self.set_status("working")
        self.assertEqual(self.send("轮询失败").code, "queued")
        self.fake.mode("get", "exit_error")
        self.set_status("idle")
        self.assertFalse(self.queue.poll())  # get_agent raised; entry kept.
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertTrue(self.queue.pending("pane-a"))
        self.assertIn(("poll", "get_failed"), self.events())
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
                       {"interval": float("nan")}, {"timeout": True},
                       {"edge_cap": 0}, {"edge_cap": float("inf")}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    SendQueue(self.core, get=self.fake.adapter().get_agent,
                              prompt=self.fake.adapter().prompt,
                              send=self._notice, **kwargs)


if __name__ == "__main__":
    unittest.main()
