"""O1 uses synthetic frames and fake time/boundaries only."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from feishu_herdr_bridge import output as out

FIXTURE = json.loads((Path(__file__).parent / "fixtures/pane_output.json").read_text(encoding="utf-8"))


def screen(kind, blocks, status="idle", input_text=""):
    lines = [FIXTURE["layouts"][kind]["header"], "BEGIN CONTENT"]
    for block in blocks:
        lines.extend([block["role"], *["| " + line for line in block["lines"]], "END"])
    lines.extend(["END CONTENT", "input> " + input_text, "state=" + status])
    return "\n".join(lines)


class ExtractionTests(unittest.TestCase):
    def test_complete_new_user_and_body_extracts_exact_unicode_answer(self):
        first = FIXTURE["rounds"][0]
        before = screen("devin", FIXTURE["history"])
        after = screen("devin", FIXTURE["history"] + [first["user"], first["assistant"]])
        result = out.extract_new_text("devin", before, after, first["prompt"])
        self.assertEqual(result.body, first["expected"])

    def test_three_explicit_synthetic_layouts_and_no_claim_of_live_recognition(self):
        first = FIXTURE["rounds"][0]
        for kind in ("codex", "claude", "devin"):
            with self.subTest(kind=kind):
                before = screen(kind, FIXTURE["history"])
                after = screen(kind, FIXTURE["history"] + [first["user"], first["assistant"]])
                self.assertEqual(out.extract_new_text(kind, before, after, first["prompt"]).body, first["expected"])
                self.assertIsNone(out.extract_new_text(kind, before, FIXTURE["layouts"][kind]["unrecognized"], first["prompt"]).body)
        self.assertEqual(FIXTURE["_meta"]["source"], "synthetic/not-live")
        self.assertEqual(FIXTURE["_meta"]["live_validation"], "pending")
        self.assertIsNone(out.extract_new_text("other", before, after, first["prompt"]).body)
        self.assertIsNone(out.extract_new_text("Devin", before, after, first["prompt"]).body)
        self.assertIsNone(out.extract_new_text("codex", before, after, first["prompt"]).body)

    def test_unchanged_chrome_and_prompt_echo_do_not_become_answer(self):
        first = FIXTURE["rounds"][0]
        before = screen("devin", FIXTURE["history"])
        cases = [before, screen("devin", FIXTURE["history"], "done", first["prompt"].replace("\n", " ")),
                 screen("devin", FIXTURE["history"] + [first["user"]]),
                 screen("devin", FIXTURE["history"] + [first["user"], {"role": "ASSISTANT", "lines": first["user"]["lines"]}])]
        for current in cases:
            with self.subTest(current=current):
                self.assertIsNone(out.extract_new_text("devin", before, current, first["prompt"]).body)

    def test_old_matching_prompt_is_not_new_and_second_round_excludes_history(self):
        first, second = FIXTURE["rounds"]
        old = FIXTURE["history"] + [first["user"], first["assistant"]]
        before = screen("devin", old)
        self.assertIsNone(out.extract_new_text("devin", before, before, first["prompt"]).body)
        # Existing users in the baseline do not count as new users in round 2.
        after = screen("devin", old + [second["user"], second["assistant"]])
        self.assertEqual(out.extract_new_text("devin", before, after, second["prompt"]).body, second["expected"])
        # Equal text with a genuinely new complete USER block is not an old echo.
        repeated = screen("devin", old + [first["user"], second["assistant"]])
        self.assertEqual(out.extract_new_text("devin", before, repeated, first["prompt"]).body, second["expected"])

    def test_two_new_users_mismatch_substrings_and_ambiguous_wrapping_are_rejected(self):
        first, second = FIXTURE["rounds"]
        before = screen("devin", FIXTURE["history"])
        additions = [
            [first["user"], first["assistant"], second["user"], second["assistant"]],
            [second["user"], first["assistant"]],
            [{"role": "USER", "lines": ["prefix " + first["prompt"].replace("\n", " ")]}, first["assistant"]],
            [{"role": "USER", "lines": [first["prompt"].replace("\n", " ")]}, first["assistant"]],
            [first["assistant"], first["user"], first["assistant"]],
        ]
        for added in additions:
            with self.subTest(added=added):
                self.assertIsNone(out.extract_new_text("devin", before, screen("devin", FIXTURE["history"] + added), first["prompt"]).body)

    def test_tool_approval_and_literal_body_markers_are_distinguished(self):
        first = FIXTURE["rounds"][0]
        before = screen("devin", FIXTURE["history"])
        blocks = FIXTURE["history"] + [first["user"], FIXTURE["tool"], FIXTURE["literal_body"]]
        body = out.extract_new_text("devin", before, screen("devin", blocks), first["prompt"]).body
        self.assertEqual(body, "\n".join(FIXTURE["literal_body"]["lines"]))
        self.assertNotIn("PRIVATE_TOOL", body)
        blocks.insert(-1, FIXTURE["approval"])
        self.assertIsNone(out.extract_new_text("devin", before, screen("devin", blocks), first["prompt"]).body)
        self.assertIsNone(out.extract_new_text("devin", before, screen("devin", FIXTURE["history"] + [first["user"], FIXTURE["tool"]]), first["prompt"]).body)

    def test_user_quote_in_assistant_prose_is_preserved_without_copying_user_block(self):
        first = FIXTURE["rounds"][0]
        answer = {"role": "ASSISTANT", "lines": ["> 检查第一项", "This is a quoted explanation."]}
        result = out.extract_new_text("devin", screen("devin", []), screen("devin", [first["user"], answer]), first["prompt"])
        self.assertEqual(result.body, "\n".join(answer["lines"]))

    def test_unique_two_body_line_scroll_anchor_is_accepted(self):
        first = FIXTURE["rounds"][0]
        before = screen("devin", FIXTURE["history"])
        current = screen("devin", [FIXTURE["history"][-1], first["user"], first["assistant"]])
        self.assertEqual(out.extract_new_text("devin", before, current, first["prompt"]).body, first["expected"])

    def test_ambiguous_short_lost_reordered_and_cleared_anchors_fail_closed(self):
        first = FIXTURE["rounds"][0]
        anchor = FIXTURE["history"][-1]
        tiny = {"role": "ASSISTANT", "lines": ["only one anchor line"]}
        cases = [
            (screen("devin", [anchor, anchor]), screen("devin", [anchor, first["user"], first["assistant"]])),
            (screen("devin", [FIXTURE["history"][0], tiny]), screen("devin", [tiny, first["user"], first["assistant"]])),
            (screen("devin", FIXTURE["history"]), screen("devin", [first["user"], first["assistant"]])),
            (screen("devin", FIXTURE["history"]), screen("devin", list(reversed(FIXTURE["history"])) + [first["user"], first["assistant"]])),
            (screen("devin", FIXTURE["history"]), screen("devin", [])),
        ]
        for before, current in cases:
            with self.subTest(current=current):
                self.assertIsNone(out.extract_new_text("devin", before, current, first["prompt"]).body)

    def test_crlf_and_known_padding_normalization_preserve_inner_unicode_spacing(self):
        first = FIXTURE["rounds"][0]
        baseline = screen("devin", FIXTURE["history"])
        padded_user = {"role": "USER", "lines": [s + "   " for s in first["user"]["lines"]]}
        current = screen("devin", FIXTURE["history"] + [padded_user, first["assistant"]]).replace("\n", "\r\n")
        result = out.extract_new_text("devin", baseline, current, first["prompt"].replace("\n", "\r\n"))
        self.assertEqual(result.body, first["expected"])
        wrong = first["prompt"].replace("  ", " ")
        self.assertIsNone(out.extract_new_text("devin", baseline, current, wrong).body)

    def test_bad_boundaries_controls_and_budget_reject_entire_frame(self):
        first = FIXTURE["rounds"][0]
        before = screen("devin", FIXTURE["history"])
        after = screen("devin", FIXTURE["history"] + [first["user"], first["assistant"]])
        invalid = [after.replace("END CONTENT", "MISSING END"), after.replace("END\nASSISTANT", "ASSISTANT", 1),
                   after.replace("ASSISTANT", "UNRECOGNIZED ROLE", 1), after + "\nforeign footer",
                   after.replace("| 第一轮", "unframed 第一轮"), "x" * (out.MAX_BYTES + 1),
                   screen("devin", [first["user"], {"role": "ASSISTANT", "lines": ["line"] * 81}])]
        invalid += [after.replace("第一轮", char + "第一轮") for char in ("\x1b[0m", "\r", "\x00", "\t", "\ud800", "\u2028")]
        for current in invalid:
            with self.subTest(current=repr(current[:50])):
                self.assertIsNone(out.extract_new_text("devin", before, current, first["prompt"]).body)
        self.assertIsNone(out.extract_new_text("devin", "x" * (out.MAX_BYTES + 1), after, first["prompt"]).body)
        self.assertIsNone(out.extract_new_text("devin", before, after, "中" * 11000).body)


class FormatTests(unittest.TestCase):
    def test_normal_payload_is_exactly_pane_label_and_body(self):
        body = FIXTURE["rounds"][0]["expected"]
        self.assertEqual(out.format_output("pane-test", body), "主控 Pane pane-test\n" + body)

    def test_codepoint_budget_and_line_budget_truncate_deterministically(self):
        for body in ("界👋é" * 1800, "line\n" * 200, "\n".join(["中" * 100] * 100)):
            with self.subTest(length=len(body)):
                wire = out.format_output("pane-test", body)
                self.assertEqual(wire, out.format_output("pane-test", body))
                self.assertLessEqual(len(wire), 3000)
                self.assertLessEqual(len(wire.split("\n")), 80)
                self.assertTrue(wire.endswith("\n" + out.TRUNCATED))
                preserved = wire[len("主控 Pane pane-test\n"):-len("\n" + out.TRUNCATED)]
                self.assertTrue(body.startswith(preserved))
                wire.encode("utf-8", errors="strict")

    def test_exact_size_is_not_truncated_and_invalid_content_is_not_sent(self):
        prefix = "主控 Pane pane-test\n"
        body = "a" * (3000 - len(prefix))
        self.assertEqual(out.format_output("pane-test", body), prefix + body)
        self.assertEqual(len(out.format_output("pane-test", "\n".join(["x"] * 79)).split("\n")), 80)
        for pane, text in (("bad\npane", "text"), ("pane", "\x1b[0m"), ("pane", ""), ("pane", "\ud800")):
            with self.subTest(pane=pane), self.assertRaises(ValueError):
                out.format_output(pane, text)


class Rig:
    def __init__(self):
        import threading
        self.now = 0.0
        self.lock = threading.Lock()
        self.bindings = {}
        self.phases = {}
        self.screens = {}
        self.states = {}
        self.calls, self.messages, self.logs, self.invalidated = [], [], [], []
        self.allowed_users = {"user"}
        self.bot = "bot"
        self.on_get = self.on_read = self.on_current = self.on_send = self.on_audit = None
        self.observer = self.make_observer()

    @staticmethod
    def binding(origin):
        return (origin.session, origin.workspace_id, origin.pane_id, origin.revision)

    def current(self, origin):
        if self.on_current:
            self.on_current(origin)
        return (self.bindings.get(origin.chat_id) == self.binding(origin)
                and origin.user_id in self.allowed_users and origin.bot_id == self.bot)

    def get(self, pane):
        self.calls.append(("get", pane))
        if self.on_get:
            self.on_get(pane)
        return self.states[pane]

    def read(self, pane):
        self.calls.append(("read", pane))
        if self.on_read:
            self.on_read(pane)
        return self.screens[pane]

    def send(self, origin, text):
        self.messages.append((origin, text))
        return self.on_send(origin, text) if self.on_send else "sent"

    def invalidate(self, origin):
        self.invalidated.append(origin)
        if self.bindings.get(origin.chat_id) == self.binding(origin):
            del self.bindings[origin.chat_id]

    def audit(self, ref, revision, event, reason):
        self.logs.append((ref, revision, event, reason))
        if self.on_audit:
            self.on_audit(event)

    def make_observer(self):
        return out.OutputObserver(current=self.current, request_phase=lambda o: self.phases.get(o, "other"),
                                  get=self.get, read=self.read, send=self.send, invalidate=self.invalidate,
                                  operation_lock=self.lock, clock=lambda: self.now, audit=self.audit)

    def origin(self, message="m1", chat="chat-a", pane="pane-a", workspace="workspace-a", kind="devin"):
        origin = out.Origin(message, chat, "user", "kpi-agg", workspace, pane, 1, "bot", kind, "group")
        self.bindings[chat] = self.binding(origin)
        self.phases[origin] = "processing"
        self.states.setdefault(pane, out.PaneState(workspace, pane, kind, "idle"))
        self.screens.setdefault(pane, screen(kind, FIXTURE["history"]))
        return origin

    def arm(self, origin, prompt=None):
        prompt = FIXTURE["rounds"][0]["prompt"] if prompt is None else prompt
        with self.lock:
            watch = self.observer.capture(origin, prompt)
            self.phases[origin] = "submitted"
            if watch is not None:
                assert self.observer.arm(watch)
            return watch

    def answer(self, origin, round_number=0, history=None):
        turn = FIXTURE["rounds"][round_number]
        history = FIXTURE["history"] if history is None else history
        self.screens[origin.pane_id] = screen(origin.kind, history + [turn["user"], turn["assistant"]])

    def tick(self, advance=0.0):
        self.now += advance
        return self.observer.tick()


class ObserverTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.rig = Rig()
        # Even swallowed boundary exceptions may not hide an attempted real I/O.
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo",
                       "subprocess.Popen", "sqlite3.connect", "threading.Thread.start"):
            guard = patch(target, side_effect=AssertionError("No real I/O or worker in O1"))
            blocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(blocked.assert_not_called)

    def test_capture_arm_and_two_stable_idle_done_samples_send_once(self):
        from dataclasses import replace
        r = self.rig
        origin = r.origin()
        watch = r.arm(origin)
        self.assertIsNotNone(watch)
        r.answer(origin)
        self.assertTrue(r.tick())
        self.assertEqual(r.messages, [])
        self.assertFalse(r.tick(1.99))
        r.states[origin.pane_id] = replace(r.states[origin.pane_id], status="done")
        self.assertTrue(r.tick(0.01))
        self.assertEqual(r.messages, [(origin, f"主控 Pane {origin.pane_id}\n" + FIXTURE["rounds"][0]["expected"])])
        self.assertEqual((watch.phase, watch.reason), ("consumed", "sent"))
        self.assertIsNone(watch._baseline)
        self.assertEqual(watch._prompt, "")
        self.assertIsNone(watch._candidate)
        for _ in range(4):
            r.tick(120)
        self.assertFalse(r.observer.arm(watch))
        self.assertEqual(len(r.messages), 1)
        self.assertEqual(set(r.calls), {("get", origin.pane_id), ("read", origin.pane_id)})

    def test_two_rounds_same_binding_have_fresh_baselines_budgets_and_two_replies(self):
        r = self.rig
        first, second = FIXTURE["rounds"]
        p1 = r.origin()
        w1 = r.arm(p1)
        r.answer(p1)
        r.tick()
        r.tick(2)
        self.assertEqual(w1.phase, "consumed")
        p2 = r.origin(message="m2")
        with r.lock:
            w2 = r.observer.capture(p2, second["prompt"])
            self.assertIsNotNone(w2)
            self.assertEqual(w2.phase, "captured")
            r.phases[p2] = "submitted"
            self.assertTrue(r.observer.arm(w2))
        self.assertEqual(w2.deadline, 122)
        self.assertEqual(w2.polls, 0)
        self.assertIsNone(w2._candidate)
        self.assertNotEqual(w1.watch_ref, w2.watch_ref)
        r.observer.cancel(w1)  # Late cleanup must not cancel the new source prompt.
        history = FIXTURE["history"] + [first["user"], first["assistant"]]
        r.answer(p2, 1, history)
        r.tick()
        self.assertEqual(len(r.messages), 1)
        r.tick(2)
        self.assertEqual(r.messages, [(p1, "主控 Pane pane-a\n" + first["expected"]),
                                     (p2, "主控 Pane pane-a\n" + second["expected"])])
        self.assertNotIn(first["expected"], r.messages[1][1])
        self.assertNotIn(first["prompt"], r.messages[1][1])
        self.assertEqual(r.binding(p1), r.binding(p2))
        for ticket, text in zip((w1, w2), (r.messages[0][1], r.messages[1][1])):
            self.assertNotIn(ticket.watch_ref, text)
            self.assertNotIn("revision", text)
        self.assertFalse(r.observer.arm(w1))
        self.assertFalse(r.observer.arm(w2))
        self.assertIsNone(r.observer.capture(p1, first["prompt"]))
        self.assertIsNone(r.observer.capture(p2, second["prompt"]))
        r.tick(130)
        self.assertEqual(len(r.messages), 2)

    def test_capture_requires_processing_and_arm_requires_submitted(self):
        r = self.rig
        origin = r.origin()
        with r.lock:
            watch = r.observer.capture(origin, FIXTURE["rounds"][0]["prompt"])
            self.assertIsNotNone(watch)
            self.assertIsNone(r.observer.capture(origin, FIXTURE["rounds"][0]["prompt"]))
            self.assertFalse(r.observer.arm(watch))  # Still processing, not confirmed.
        self.assertEqual(watch.phase, "closed")
        self.assertEqual(r.messages, [])
        before = len(r.calls)
        r.phases[origin] = "unknown"
        self.assertIsNone(r.observer.capture(origin, "task"))
        r.tick(200)
        self.assertEqual(len(r.calls), before)

    def test_capture_does_not_poll_or_set_deadline_before_submission(self):
        r = self.rig
        origin = r.origin()
        watch = r.observer.capture(origin, FIXTURE["rounds"][0]["prompt"])
        self.assertEqual(watch.phase, "captured")
        before = len(r.calls)
        self.assertFalse(r.tick(15))
        r.phases[origin] = "submitted"
        self.assertTrue(r.observer.arm(watch))
        self.assertEqual(watch.deadline, 135)
        self.assertEqual(len(r.calls), before)

    def test_working_avoids_reads_and_resets_stability(self):
        from dataclasses import replace
        r = self.rig
        origin = r.origin()
        watch = r.arm(origin)
        r.answer(origin)
        r.tick()
        r.states[origin.pane_id] = replace(r.states[origin.pane_id], status="working")
        before = len([c for c in r.calls if c[0] == "read"])
        r.tick(2)
        self.assertIsNone(watch._candidate)
        self.assertEqual(len([c for c in r.calls if c[0] == "read"]), before)
        r.states[origin.pane_id] = replace(r.states[origin.pane_id], status="idle")
        r.tick(2)
        self.assertEqual(r.messages, [])
        r.tick(2)
        self.assertEqual(len(r.messages), 1)

    def test_changed_and_empty_candidate_restart_double_sampling(self):
        r = self.rig
        origin = r.origin()
        watch = r.arm(origin)
        first = FIXTURE["rounds"][0]
        r.answer(origin)
        r.tick()
        r.screens[origin.pane_id] = screen("devin", FIXTURE["history"] + [first["user"], first["working"]])
        r.tick(2)
        self.assertEqual(r.messages, [])
        r.screens[origin.pane_id] = screen("devin", FIXTURE["history"] + [first["user"]])
        r.tick(2)
        self.assertIsNone(watch._candidate)
        r.answer(origin)
        r.tick(2)
        self.assertEqual(r.messages, [])
        r.tick(2)
        self.assertEqual(len(r.messages), 1)

    def test_deadline_unchanged_screen_and_busy_do_not_extend_lifetime(self):
        r = self.rig
        origin = r.origin()
        watch = r.arm(origin)
        r.tick()
        with r.lock:
            self.assertFalse(r.tick(121))
        self.assertEqual(watch.deadline, 120)
        before = len(r.calls)
        self.assertTrue(r.tick())
        self.assertEqual(len(r.calls), before)
        self.assertEqual(r.messages, [(origin, "主控 Pane pane-a\n" + out.NOTICE)])
        self.assertEqual(watch.reason, "sent")
        r.tick(200)
        self.assertEqual(len(r.messages), 1)

    def test_sixty_polls_max_and_zero_idle_background_work(self):
        from dataclasses import replace
        r = self.rig
        self.assertFalse(r.tick())
        self.assertEqual(r.calls, [])
        origin = r.origin()
        watch = r.arm(origin)
        r.states[origin.pane_id] = replace(r.states[origin.pane_id], status="working")
        for i in range(60):
            self.assertTrue(r.tick(0 if i == 0 else 2))
            self.assertFalse(r.tick())
        self.assertEqual(watch.polls, 60)
        before = len(r.calls)
        r.tick(2)
        self.assertEqual(len(r.calls), before)
        self.assertEqual(len(r.messages), 1)
        self.assertFalse(r.tick(120))

    def test_capacity_does_not_evict_others_and_next_round_reuses_released_slot(self):
        r = self.rig
        watches = []
        for i in range(16):
            origin = r.origin(f"m-{i}", f"chat-{i}", f"pane-{i}", f"workspace-{i}")
            watches.append(r.arm(origin))
        self.assertTrue(all(watches))
        extra = r.origin("extra", "chat-extra", "pane-extra", "workspace-extra")
        before = len(r.calls)
        self.assertIsNone(r.observer.capture(extra, "task"))
        self.assertEqual(len(r.calls), before)
        self.assertTrue(all(w.phase == "watching" for w in watches))
        r.observer.cancel(watches[0])
        self.assertIsNotNone(r.arm(extra))
        self.assertTrue(all(w.phase == "watching" for w in watches[1:]))
        # Replace one chat's pending round; other chats retain their own slots.
        new = r.origin("replacement", "chat-1", "pane-1", "workspace-1")
        self.assertIsNotNone(r.arm(new))
        self.assertEqual(watches[1].phase, "closed")
        self.assertTrue(all(w.phase == "watching" for w in watches[2:]))

    def test_two_chats_have_separate_panes_and_exact_destinations(self):
        r = self.rig
        a, b = r.origin(), r.origin("mb", "chat-b", "pane-b", "workspace-b", "claude")
        r.arm(a)
        r.arm(b)
        r.answer(a)
        r.answer(b)
        self.assertTrue(r.tick())
        self.assertTrue(r.tick())
        self.assertFalse(r.tick())
        r.tick(2)
        r.tick()
        self.assertEqual({(o.chat_id, o.pane_id) for o, _ in r.messages}, {("chat-a", "pane-a"), ("chat-b", "pane-b")})
        self.assertEqual(len(r.messages), 2)
        for origin, text in r.messages:
            self.assertTrue(text.startswith(f"主控 Pane {origin.pane_id}\n"))
        self.assertEqual(set(r.calls), {("get", "pane-a"), ("read", "pane-a"), ("get", "pane-b"), ("read", "pane-b")})

    def test_new_working_prompt_cancels_old_without_arming_a_mixed_round(self):
        from dataclasses import replace
        r = self.rig
        p1 = r.origin()
        w1 = r.arm(p1)
        p2 = r.origin(message="m2")
        r.states[p2.pane_id] = replace(r.states[p2.pane_id], status="working")
        self.assertIsNone(r.arm(p2, FIXTURE["rounds"][1]["prompt"]))
        self.assertEqual(w1.phase, "closed")
        r.answer(p1)
        r.tick(10)
        self.assertEqual(r.messages, [])
        self.assertEqual(r.invalidated, [])

    def test_cancel_and_old_handle_cleanup_never_remove_new_round(self):
        r = self.rig
        p1 = r.origin()
        w1 = r.arm(p1)
        p2 = r.origin(message="m2")
        w2 = r.arm(p2)
        r.observer.cancel(w1)
        self.assertEqual(w2.phase, "watching")
        r.observer.cancel_current("chat-a")
        self.assertEqual(w2.phase, "closed")
        r.answer(p2)
        r.tick(5)
        self.assertEqual(r.messages, [])

    def test_blocked_unknown_and_unrecognized_states_use_one_safe_notice(self):
        from dataclasses import replace
        for state in ("blocked", "unknown", "new-status"):
            with self.subTest(state=state):
                r = Rig()
                origin = r.origin()
                watch = r.arm(origin)
                r.states[origin.pane_id] = replace(r.states[origin.pane_id], status=state)
                before = len([c for c in r.calls if c[0] == "read"])
                r.tick()
                r.tick(3)
                self.assertEqual(r.messages, [(origin, "主控 Pane pane-a\n" + out.NOTICE)])
                self.assertEqual(len([c for c in r.calls if c[0] == "read"]), before)
                self.assertEqual(watch.phase, "consumed")
                self.assertEqual(r.invalidated, [])

    def test_unrecognized_or_oversize_snapshots_send_notice_not_raw_text(self):
        for raw in ("PRIVATE UNRECOGNIZED OUTPUT", "中" * 11000):
            with self.subTest(size=len(raw)):
                r = Rig()
                origin = r.origin()
                r.arm(origin)
                r.screens[origin.pane_id] = raw
                r.tick()
                r.tick(2)
                self.assertEqual(r.messages, [(origin, "主控 Pane pane-a\n" + out.NOTICE)])
                self.assertEqual(r.invalidated, [])

    def test_read_failures_or_target_mismatch_invalidate_original_only_and_never_send(self):
        from dataclasses import replace
        for failure in ("missing", "timeout", "workspace", "pane", "kind"):
            with self.subTest(failure=failure):
                r = Rig()
                origin = r.origin()
                watch = r.arm(origin)
                if failure in {"missing", "timeout"}:
                    def fail(_):
                        raise TimeoutError("PRIVATE raw error")
                    if failure == "missing":
                        r.on_get = fail
                    else:
                        r.on_read = fail
                else:
                    key = {"workspace": "workspace_id", "pane": "pane_id", "kind": "kind"}[failure]
                    r.states[origin.pane_id] = replace(r.states[origin.pane_id], **{key: "other"})
                r.tick()
                self.assertEqual(r.messages, [])
                self.assertEqual(r.invalidated, [origin])
                self.assertEqual(watch.reason, "read_failed")
                self.assertNotIn("PRIVATE", repr(r.logs))
                self.assertTrue(all(pane == origin.pane_id for _, pane in r.calls))

    def test_baseline_errors_working_and_budgets_only_disable_capture(self):
        from dataclasses import replace
        for mode in ("read_failure", "get_failure", "working", "unknown", "raw", "prompt"):
            with self.subTest(mode=mode):
                r = Rig()
                origin = r.origin()
                text = FIXTURE["rounds"][0]["prompt"]
                def fail(_):
                    raise RuntimeError("PRIVATE")
                if mode == "read_failure":
                    r.on_read = fail
                elif mode == "get_failure":
                    r.on_get = fail
                elif mode in {"working", "unknown"}:
                    r.states[origin.pane_id] = replace(r.states[origin.pane_id], status=mode)
                elif mode == "raw":
                    r.screens[origin.pane_id] = "x" * (out.MAX_BYTES + 1)
                else:
                    text = "x" * (out.MAX_BYTES + 1)
                self.assertIsNone(r.observer.capture(origin, text))
                self.assertEqual(r.invalidated, [])
                self.assertEqual(r.messages, [])
                self.assertEqual(r.phases[origin], "processing")

    def test_guard_revocation_before_read_during_read_and_before_send_discards(self):
        for point in ("before_get", "during_get", "during_read", "attempted"):
            with self.subTest(point=point):
                r = Rig()
                origin = r.origin()
                r.arm(origin)
                r.answer(origin)
                if point == "attempted":
                    r.tick()
                    r.on_audit = lambda event: r.bindings.clear() if event == "attempted" else None
                elif point == "before_get":
                    r.bindings.clear()
                elif point == "during_get":
                    r.on_get = lambda pane: r.bindings.clear()
                else:
                    r.on_read = lambda pane: r.bindings.clear()
                before = len(r.calls)
                r.tick(2)
                self.assertEqual(r.messages, [])
                self.assertEqual(r.invalidated, [])
                if point in {"before_get", "during_get"}:
                    self.assertFalse(any(action == "read" for action, _ in r.calls[before:]))

    def test_capture_rechecks_binding_after_get_without_reading_old_target(self):
        r = self.rig
        origin = r.origin()
        r.on_get = lambda pane: r.bindings.clear()
        self.assertIsNone(r.observer.capture(origin, "task"))
        self.assertEqual(r.calls, [("get", origin.pane_id)])

    def test_same_pane_new_revision_authorization_and_bot_changes_cancel(self):
        for change in ("revision", "user", "bot", "phase"):
            with self.subTest(change=change):
                r = Rig()
                origin = r.origin()
                r.arm(origin)
                if change == "revision":
                    r.bindings[origin.chat_id] = (origin.session, origin.workspace_id, origin.pane_id, 2)
                elif change == "user":
                    r.allowed_users.clear()
                elif change == "bot":
                    r.bot = "changed-bot"
                else:
                    r.phases[origin] = "unknown"
                before = len(r.calls)
                r.tick()
                self.assertEqual(len(r.calls), before)
                self.assertEqual(r.messages, [])
                self.assertEqual(r.invalidated, [])

    def test_read_failure_after_rebinding_does_not_invalidate_new_binding(self):
        r = self.rig
        origin = r.origin()
        r.arm(origin)
        new_binding = (origin.session, "new-workspace", "new-pane", 2)
        def fail(pane):
            r.bindings[origin.chat_id] = new_binding
            raise TimeoutError("uncertain read")
        r.on_read = fail
        r.tick()
        self.assertEqual(r.bindings[origin.chat_id], new_binding)
        self.assertEqual(r.invalidated, [])
        self.assertEqual(r.messages, [])

    def test_send_is_consumed_before_callback_under_operation_lock(self):
        r = self.rig
        origin = r.origin()
        watch = r.arm(origin)
        r.answer(origin)
        def sending(o, text):
            self.assertEqual(watch.phase, "consumed")
            self.assertFalse(r.lock.acquire(blocking=False))
            self.assertFalse(r.observer.tick())
            return "sent"
        r.on_send = sending
        r.tick()
        r.tick(2)
        self.assertEqual(len(r.messages), 1)
        self.assertFalse(r.lock.locked())

    def test_send_failure_unknown_or_interrupt_never_retries_but_next_prompt_can_reply(self):
        for mode in ("failed", "unknown", "exception", "interrupt", "invalid_return"):
            with self.subTest(mode=mode):
                r = Rig()
                origin = r.origin()
                watch = r.arm(origin)
                r.answer(origin)
                def sending(o, text):
                    if mode == "exception":
                        raise RuntimeError("PRIVATE")
                    if mode == "interrupt":
                        raise KeyboardInterrupt()
                    return "unrecognized" if mode == "invalid_return" else mode
                r.on_send = sending
                r.tick()
                if mode == "interrupt":
                    with self.assertRaises(KeyboardInterrupt):
                        r.tick(2)
                else:
                    r.tick(2)
                self.assertFalse(r.lock.locked())
                self.assertEqual(watch.phase, "consumed")
                self.assertIsNone(watch._baseline)
                r.tick(120)
                self.assertFalse(r.observer.arm(watch))
                self.assertEqual(len(r.messages), 1)
                self.assertEqual(r.invalidated, [])
                self.assertEqual(r.phases[origin], "submitted")
                r.on_send = None
                second = FIXTURE["rounds"][1]
                p2 = r.origin(message="m2")
                r.arm(p2, second["prompt"])
                first = FIXTURE["rounds"][0]
                r.answer(p2, 1, FIXTURE["history"] + [first["user"], first["assistant"]])
                r.tick()
                r.tick(2)
                self.assertEqual(len(r.messages), 2)
                self.assertEqual(r.messages[-1][1], "主控 Pane pane-a\n" + second["expected"])

    def test_stop_during_read_or_before_send_does_not_flush_and_fresh_instance_does_not_replay(self):
        for point in ("read", "attempted"):
            with self.subTest(point=point):
                r = Rig()
                origin = r.origin()
                r.arm(origin)
                r.answer(origin)
                if point == "read":
                    r.on_read = lambda pane: r.observer.stop()
                else:
                    r.tick()
                    r.on_audit = lambda event: r.observer.stop() if event == "attempted" else None
                r.tick(2)
                self.assertEqual(r.messages, [])
                before = len(r.calls)
                self.assertFalse(r.tick(5))
                r.on_read = r.on_audit = None
                r.observer = r.make_observer()
                self.assertFalse(r.tick())
                self.assertIsNone(r.observer.capture(origin, FIXTURE["rounds"][0]["prompt"]))
                self.assertEqual(len(r.calls), before)

    def test_raw_and_debug_values_are_not_in_payload_logs_or_terminal_repr(self):
        r = self.rig
        origin = r.origin(message="PRIVATE-MESSAGE", chat="PRIVATE-CHAT")
        watch = r.arm(origin)
        r.answer(origin)
        r.tick()
        r.tick(2)
        payload = r.messages[0][1]
        self.assertEqual(payload, "主控 Pane pane-a\n" + FIXTURE["rounds"][0]["expected"])
        for private in (origin.message_id, origin.chat_id, FIXTURE["rounds"][0]["prompt"], FIXTURE["rounds"][0]["expected"]):
            self.assertNotIn(private, repr(r.logs))
            self.assertNotIn(private, repr(watch))
        self.assertNotIn(watch.watch_ref, payload)
        self.assertNotIn("revision", payload)
        self.assertTrue(all(event in {"armed", "attempted", "closed"} for _, _, event, _ in r.logs))

    def test_non_group_nonfixed_session_unknown_kind_and_invalid_origin_never_capture(self):
        from dataclasses import replace
        r = self.rig
        origin = r.origin()
        for kwargs in ({"chat_type": "p2p"}, {"chat_type": ""}, {"session": "other-session"},
                       {"kind": "other"}, {"revision": True}, {"pane_id": "bad\npane"}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(r.observer.capture(replace(origin, **kwargs), "task"))
        self.assertEqual(r.calls, [])

    def test_long_read_crossing_deadline_cannot_emit_a_body(self):
        r = self.rig
        origin = r.origin()
        watch = r.arm(origin)
        r.answer(origin)
        r.tick()
        r.now = 119
        r.on_read = lambda pane: setattr(r, "now", 121)
        r.tick()
        self.assertEqual(r.messages, [(origin, "主控 Pane pane-a\n" + out.NOTICE)])
        self.assertEqual(watch.phase, "consumed")
        self.assertIsNone(watch._baseline)

    def test_unknown_request_ledger_or_guard_failure_does_not_use_memory_allowlist(self):
        from unittest.mock import patch
        for dependency in ("_current", "_request_phase"):
            with self.subTest(dependency=dependency):
                r = Rig()
                origin = r.origin()
                watch = r.arm(origin)
                with patch.object(r.observer, dependency, side_effect=RuntimeError("PRIVATE")):
                    before = len(r.calls)
                    r.tick()
                    self.assertEqual(len(r.calls), before)
                self.assertEqual(watch.phase, "closed")
                self.assertEqual(r.messages, [])
                self.assertEqual(r.invalidated, [])

    def test_failed_new_submission_does_not_restore_previous_round(self):
        r = self.rig
        p1 = r.origin()
        w1 = r.arm(p1)
        p2 = r.origin(message="m2")
        w2 = r.observer.capture(p2, FIXTURE["rounds"][1]["prompt"])
        self.assertEqual(w1.phase, "closed")
        r.phases[p2] = "unknown"
        self.assertFalse(r.observer.arm(w2))
        r.answer(p1)
        r.tick(5)
        self.assertEqual(r.messages, [])
        self.assertEqual(w2.phase, "closed")

    def test_stale_cleanup_of_unarmed_capture_cannot_remove_replacement(self):
        r = self.rig
        p1 = r.origin()
        old = r.observer.capture(p1, FIXTURE["rounds"][0]["prompt"])
        p2 = r.origin(message="m2")
        new = r.arm(p2)
        r.phases[p1] = "submitted"
        self.assertFalse(r.observer.arm(old))
        r.observer.cancel(old)
        self.assertEqual(new.phase, "watching")
        r.answer(p2)
        r.tick()
        r.tick(2)
        self.assertEqual([o for o, _ in r.messages], [p2])
