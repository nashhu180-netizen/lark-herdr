"""O1: passive, bounded extraction and a manually driven observation kernel.

Import starts no SDK, database, CLI, thread, timer, prompt, or file I/O.
Two screen grammars are recognized, both fail-closed: the deliberately labelled
synthetic/not-live grammar in pane_output.json, and - for kind "devin" only - the
real Devin CLI visible-text layout sampled from the authorized disposable pane
(transcript rows over a fixed input chrome). All other layouts fail closed.

capture/arm/cancel are called under the existing foreground operation lock;
tick takes that same lock nonblockingly. connect_output supplies authoritative
binding/authorization and request-phase checks. Runtime owns one observation
thread; get/read and send remain injected, bounded, single-operation boundaries.
"""

from __future__ import annotations

import math
import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable


KINDS = frozenset({"codex", "claude", "devin"})
MAX_BYTES = 32 * 1024
MAX_LINES = 80
MAX_CHARS = 3000
CAPACITY = 16
LIFETIME = 120.0
INTERVAL = 2.0
MAX_POLLS = 60
# Consecutive unverifiable samples tolerated per watch; a live TUI may emit a
# torn frame mid-redraw or a shifted overlap mid-scroll, but a persistently
# foreign or unattributable layout still stops fast. ambiguous_prompt and
# attention stay terminal: a different user block or an approval card is
# evidence of external input, not redraw noise.
TRANSIENT_POLLS = 5
TRANSIENT_REASONS = frozenset({"unrecognized", "ambiguous_overlap", "partial_block"})
NOTICE = "本次自动回传已停止，可用 /read 查看当前画面"
TRUNCATED = "[已截断；可用 /read 查看当前画面]"


@dataclass(frozen=True, repr=False)
class Extraction:
    body: str | None
    reason: str


@dataclass(frozen=True, repr=False)
class _Block:
    role: str
    start: int
    end: int
    text: str


@dataclass(frozen=True, repr=False)
class _Frame:
    rows: tuple[str, ...]
    blocks: tuple[_Block, ...]
    status: str
    real: bool = False


# Real Devin CLI chrome markers (sampled visible layout; see tests for fixtures).
# The status bar cycles between several tails; all are exact literals observed
# on real panes (Issue #28 samples plus verbatim sightings on o24 panes).
_DEVIN_STATUS = re.compile(
    r"\S(?:.*?\S)?\s+(?:Context: [0-9.]+[kKmM]? / [0-9.]+[kKmM]? tokens \([0-9]+%\)"
    r"|Press alt\+t to cycle thinking levels"
    r"|See usage and cost: /session-stats"
    r"|Press Ctrl\+L to clear the screen, Ctrl\+Shift\+L to redraw)")
_DEVIN_RULE = re.compile(r"─+")
_DEVIN_RULE_TOP = re.compile(r"─+( \([^()]*\) ─+)?")
_DEVIN_SPINNER = re.compile(r"\(esc (?:(?:twice|again) )?to interrupt[^()]*\)"
                            r"(?:\s[^()]*\([^()]*\))?(\s[^()]*)?$")
_DEVIN_ACTIVITY = re.compile(
    r"(?:(?:[1-9][0-9]* subagents?)(?: · [1-9][0-9]* shells?)?"
    r"|[1-9][0-9]* shells?) · ↓ select"
)
_DEVIN_QUEUE_RULE = re.compile(r"── 1 queued ─+ ↑ edit · ↵ send now ──")
_DEVIN_QUEUE_INPUT = "❭ Press Enter to send queued messages now"
_DEVIN_TOOL_HEAD = re.compile(r" [○◐◔◑◕⏺] ")
_DEVIN_USER_CONT = re.compile(r"  \S")
_DEVIN_TOOL_BODY = ("│", " │", " └")
# Did-you-know tips are cycling UI hints, not transcript content. The exact
# header line is dropped only at a provable UI boundary — it must sit at the
# transcript start or right after a blank row — and at most two deeper-
# indented continuation lines directly under it go with it (real samples all
# carry exactly one). Anything past that boundary is kept: indented assistant
# body (code blocks, lists) is a legal transcript shape and must never be
# silently eaten (text is not enumerated).
_DEVIN_DID_YOU_KNOW = " ✱ Did you know"
_DEVIN_TIP_CONT_MAX = 2


def _devin_frame(lines: list[str]) -> _Frame | None:
    """Strictly parse the sampled real Devin CLI layout; any deviation fails closed.

    Chrome: transcript rows, optional working spinner, a rule, one '❭ ' input
    line, a rule, then the model/context status bar as the last row.
    """
    chrome_end = len(lines)
    if lines and _DEVIN_ACTIVITY.fullmatch(lines[-1]) is not None:
        chrome_end -= 1
    if (chrome_end < 5 or _DEVIN_STATUS.fullmatch(lines[chrome_end - 1]) is None
            or _DEVIN_RULE.fullmatch(lines[chrome_end - 2]) is None
            or not lines[chrome_end - 3].startswith("❭")
            or _DEVIN_RULE_TOP.fullmatch(lines[chrome_end - 4]) is None):
        return None
    end, status = chrome_end - 4, "idle"
    if lines[chrome_end - 3] == _DEVIN_QUEUE_INPUT:
        if (end < 3 or _DEVIN_SPINNER.search(lines[end - 3]) is None
                or _DEVIN_QUEUE_RULE.fullmatch(lines[end - 2]) is None
                or not lines[end - 1].startswith("○ ") or not lines[end - 1][2:].strip()):
            return None
        status, end = "working", end - 3
    elif end > 0 and _DEVIN_SPINNER.search(lines[end - 1]):
        status, end = "working", end - 1
    if lines[chrome_end - 3] == "❭ Guide Devin while it works":
        status = "working"  # Working placeholder; same signal the agent detector uses.
    transcript = lines[:end]

    def blank(row: str) -> bool:
        return not row.strip()

    while transcript and blank(transcript[-1]):
        transcript.pop()  # Pre-chrome blank rows are spacing, not transcript.
    i = 0
    while i < len(transcript):
        if (transcript[i] == _DEVIN_DID_YOU_KNOW
                and (i == 0 or blank(transcript[i - 1]))):
            del transcript[i]
            taken = 0
            while (i < len(transcript) and taken < _DEVIN_TIP_CONT_MAX
                   and transcript[i].startswith("  ") and transcript[i].strip()):
                del transcript[i]
                taken += 1
        else:
            i += 1
    blocks: list[_Block] = []
    i, n = 0, len(transcript)

    def tail(start: int, stop: int) -> int:
        while stop > start and blank(transcript[stop - 1]):
            stop -= 1
        return stop

    while i < n:
        line = transcript[i]
        if blank(line):
            i += 1
            continue
        if line.startswith("❭"):
            j = i + 1
            while j < n and (blank(transcript[j]) or _DEVIN_USER_CONT.match(transcript[j])):
                j += 1
            end_i = tail(i + 1, j)
            echo = "".join((transcript[i][1:] + "".join(transcript[i + 1:end_i])).split())
            blocks.append(_Block("USER", i, end_i, echo))
        elif _DEVIN_TOOL_HEAD.match(line) or line.startswith(_DEVIN_TOOL_BODY):
            # A scrolled window may open inside a tool block, header off-screen.
            j = i + 1
            while j < n and (blank(transcript[j]) or transcript[j].startswith(_DEVIN_TOOL_BODY)):
                j += 1
            blocks.append(_Block("TOOL", i, tail(i + 1, j), ""))
        elif line.startswith(" "):
            j = i + 1
            while j < n and (blank(transcript[j])
                             or (transcript[j].startswith(" ")
                                 and not _DEVIN_TOOL_HEAD.match(transcript[j])
                                 and not transcript[j].startswith(_DEVIN_TOOL_BODY))):
                j += 1
            end_i = tail(i + 1, j)
            text = "\n".join(transcript[k][1:].rstrip() if transcript[k] else ""
                             for k in range(i, end_i)).strip("\n")
            blocks.append(_Block("ASSISTANT", i, end_i, text))
        else:
            return None
        i = j
    return _Frame(tuple("| " + row.rstrip() for row in transcript), tuple(blocks), status, True)



def _text(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        if len(value.encode("utf-8")) > MAX_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    value = value.replace("\r\n", "\n")
    if any(c != "\n" and unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} for c in value):
        return None
    return value


def _prompt(value: str) -> str | None:
    value = _text(value)
    if value is None or not value.strip():
        return None
    return "\n".join(line.rstrip(" ") for line in value.split("\n"))


def _frame(kind: str, raw: str) -> _Frame | None:
    text = _text(raw)
    if not isinstance(kind, str) or kind not in KINDS or text is None:
        return None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # One read-command trailing newline, not a transcript line.
    if not 5 <= len(lines) <= MAX_LINES:
        return None
    if lines[:2] != [f"[synthetic/not-live:{kind}]", "BEGIN CONTENT"]:
        return _devin_frame(lines) if kind == "devin" else None
    if (lines[-3] != "END CONTENT" or not lines[-2].startswith("input> ")
            or lines[-1] not in {"state=idle", "state=done", "state=working", "state=blocked", "state=unknown"}):
        return None
    rows = tuple(lines[2:-3])
    blocks: list[_Block] = []
    i = 0
    while i < len(rows):
        start, role = i, rows[i]
        if role not in {"USER", "ASSISTANT", "TOOL", "APPROVAL"}:
            return None
        i += 1
        content = []
        while i < len(rows) and rows[i].startswith("| "):
            content.append(rows[i][2:].rstrip(" "))
            i += 1
        if i == len(rows) or rows[i] != "END":
            return None
        blocks.append(_Block(role, start, i + 1, "\n".join(content)))
        i += 1
    # Normalize only known body-line right padding; structural markers stay exact.
    normalized = tuple("| " + row[2:].rstrip(" ") if row.startswith("| ") else row for row in rows)
    return _Frame(normalized, tuple(blocks), lines[-1][6:])


def _occurrences(rows: tuple[str, ...], anchor: tuple[str, ...]) -> int:
    return sum(rows[i:i + len(anchor)] == anchor for i in range(len(rows) - len(anchor) + 1))


def _extract(before: _Frame, after: _Frame, prompt: str) -> Extraction:
    if after.status not in {"idle", "done"}:
        return Extraction(None, "not_ready")
    if before.real is not after.real:
        return Extraction(None, "unrecognized")
    old, new = before.rows, after.rows
    if old == new:
        return Extraction(None, "unchanged")
    if new[:len(old)] == old:
        boundary = len(old)
    else:
        boundary = next((k for k in range(min(len(old), len(new)), 0, -1)
                         if old[-k:] == new[:k]), 0)
        anchor = new[:boundary]
        if (boundary == 0 or sum(row.startswith("| ") and bool(row[2:].strip()) for row in anchor) < 2
                or _occurrences(old, anchor) != 1 or _occurrences(new, anchor) != 1):
            return Extraction(None, "ambiguous_overlap")
    if any(block.start < boundary < block.end for block in after.blocks):
        return Extraction(None, "partial_block")
    added = [block for block in after.blocks if block.start >= boundary]
    if not added:
        return Extraction(None, "unchanged")
    users = [block for block in added if block.role == "USER"]
    if not users:
        return Extraction(None, "waiting_for_prompt")
    # Real Devin echoes hard-wrap and reflow paragraphs; compare whitespace-free.
    want = "".join(prompt.split()) if after.real else prompt
    mine = added.index(users[0])
    # A working baseline may have in-flight output legitimately precede the
    # guidance echo; only an idle/done baseline must echo as the first new block.
    if (len(users) != 1 or (before.status != "working" and mine != 0)
            or users[0].text != want):
        return Extraction(None, "ambiguous_prompt")
    if any(block.role == "APPROVAL" for block in added):
        return Extraction(None, "attention")
    body = "\n\n".join(block.text for block in added[mine + 1:]
                       if block.role == "ASSISTANT" and block.text.strip())
    if not body or body == prompt:
        return Extraction(None, "echo_only")
    return Extraction(body, "candidate")


def extract_new_text(kind: str, baseline: str, current: str, prompt: str) -> Extraction:
    """No generic screen diff fallback; source labels are not live compatibility."""
    before, after, expected = _frame(kind, baseline), _frame(kind, current), _prompt(prompt)
    if before is None or after is None or expected is None or before.status not in {"idle", "done", "working"}:
        return Extraction(None, "unrecognized")
    return _extract(before, after, expected)


def _identifier(value: str) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 256 and _text(value) == value
            and not value.startswith("-") and not any(c.isspace() for c in value))


def format_output(pane_id: str, body: str) -> str:
    """Preserve code points/line order; header and optional tail count in limits."""
    if not _identifier(pane_id) or not isinstance(body, str) or _text(body) != body or not body.strip():
        raise ValueError("Invalid output")
    prefix = f"主控 Pane {pane_id}\n"
    full = prefix + body
    if len(full) <= MAX_CHARS and len(full.split("\n")) <= MAX_LINES:
        return full
    budget = MAX_CHARS - len(prefix) - len("\n" + TRUNCATED)
    content = "\n".join(body.split("\n")[:MAX_LINES - 2])[:budget]
    return prefix + content + "\n" + TRUNCATED


@dataclass(frozen=True, repr=False)
class Origin:
    message_id: str
    chat_id: str
    user_id: str
    session: str
    workspace_id: str
    pane_id: str
    revision: int
    bot_id: str
    kind: str
    chat_type: str


@dataclass(frozen=True, repr=False)
class PaneState:
    workspace_id: str
    pane_id: str
    kind: str
    status: str


@dataclass(eq=False, repr=False)
class Observation:
    origin: Origin
    watch_ref: str = field(default_factory=lambda: secrets.token_hex(8))
    phase: str = "captured"
    reason: str = ""
    deadline: float = 0.0
    next_due: float = 0.0
    polls: int = 0
    _baseline: _Frame | None = None
    _prompt: str = ""
    _candidate: str | None = None
    _candidate_at: float = 0.0
    _transient: int = 0


class OutputObserver:
    """One active record per chat; no worker/DB/network is created.

    Call capture immediately before the already-authorized prompt under the
    shared operation lock, then arm only after done/submitted was persisted.
    Always cancel the capture if submission or bookkeeping fails. The injected
    request_phase must consult the original message and complete frozen target:
    'processing' permits capture, 'submitted' permits arm/tick. All other values
    deny. This reuses message deduplication, without an unbounded tombstone set.

    current must check valid binding, full original revision/target, original
    user/chat authorization and bot identity. invalidate must condition its write
    on that same old snapshot. get/read accept only the frozen Pane ID. They must
    already have <=2s timeouts; send must be a bounded <=3s single-send operation.
    Tests can drive tick explicitly; runtime may run the interruptible loop.
    """

    def __init__(self, *, current: Callable[[Origin], bool],
                 request_phase: Callable[[Origin], str], get: Callable[[str], PaneState],
                 read: Callable[[str], str], send: Callable[[Origin, str], str],
                 invalidate: Callable[[Origin], None], operation_lock=None,
                 clock: Callable[[], float] = time.monotonic,
                 audit: Callable[[str, int, str, str], None] | None = None,
                 stopping: Callable[[], bool] = lambda: False) -> None:
        self._current, self._request_phase = current, request_phase
        self._get, self._read, self._send, self._invalidate = get, read, send, invalidate
        self._lock = operation_lock if operation_lock is not None else threading.Lock()
        self._clock, self._audit = clock, audit
        self._active: dict[str, Observation] = {}
        self._stopped = False
        self._external_stopping = stopping
        self._wake = threading.Event()

    def _stopping(self) -> bool:
        try:
            return self._stopped or bool(self._external_stopping())
        except Exception:
            return True

    def _allowed(self, origin: Origin, phase: str) -> bool:
        try:
            return (not self._stopping() and self._current(origin) is True
                    and self._request_phase(origin) == phase)
        except Exception:
            return False

    @staticmethod
    def _valid(origin: Origin) -> bool:
        return (isinstance(origin, Origin) and origin.session == "kpi-agg" and isinstance(origin.kind, str) and origin.kind in KINDS
                and origin.chat_type == "group" and type(origin.revision) is int and origin.revision > 0
                and all(_identifier(v) for v in (origin.message_id, origin.chat_id, origin.user_id,
                                                origin.workspace_id, origin.pane_id, origin.bot_id)))

    @staticmethod
    def _matches(origin: Origin, state: PaneState) -> bool:
        return ((state.workspace_id, state.pane_id, state.kind)
                == (origin.workspace_id, origin.pane_id, origin.kind))

    def _emit(self, watch: Observation, event: str, reason: str) -> None:
        if self._audit is not None:
            try:
                self._audit(watch.watch_ref, watch.origin.revision, event, reason)
            except Exception:
                pass  # Never leak exception text or retry an operation to log it.

    def _decline(self, origin: Origin, reason: str) -> None:
        """One fixed-code audit event per refused capture; the refusal never changes."""
        try:
            revision = (origin.revision if isinstance(origin, Origin)
                        and type(origin.revision) is int else 0)
            if self._audit is not None:
                self._audit("capture", revision, "declined", reason)
        except Exception:
            pass  # Never leak exception text or retry an operation to log it.

    def _close(self, watch: Observation, reason: str) -> None:
        if self._active.get(watch.origin.chat_id) is watch:
            del self._active[watch.origin.chat_id]
        if watch.phase != "consumed":
            watch.phase = "closed"
        watch.reason = reason
        watch._baseline, watch._prompt, watch._candidate = None, "", None
        self._emit(watch, "closed", reason)
        self._wake.set()

    def cancel(self, watch: Observation) -> None:
        # A stale completion/cancellation must not remove a newer round.
        if watch.phase in {"captured", "watching"}:
            self._close(watch, "cancelled")

    def cancel_current(self, chat_id: str) -> None:
        """Foreground only, under the shared lock (e.g. immediately before /read)."""
        watch = self._active.get(chat_id)
        if watch is not None:
            self.cancel(watch)

    def request_stop(self) -> None:
        # Safe while a foreground operation owns the lock; do not touch records.
        self._stopped = True
        self._wake.set()

    def stop(self) -> None:
        self.request_stop()
        for watch in tuple(self._active.values()):
            self._close(watch, "stopped")

    def capture(self, origin: Origin, prompt: str) -> Observation | None:
        """Best effort only: failure must not change whether the prompt is sent."""
        if not self._valid(origin):
            return self._decline(origin, "invalid_origin")
        if not self._allowed(origin, "processing"):
            return self._decline(origin, "not_allowed")
        old = self._active.get(origin.chat_id)
        if old is not None and old.origin.message_id == origin.message_id:
            return self._decline(origin, "same_message")
        self.cancel_current(origin.chat_id)
        expected = _prompt(prompt)
        if len(self._active) >= CAPACITY:
            return self._decline(origin, "capacity")
        if expected is None:
            return self._decline(origin, "prompt_invalid")
        try:
            state = self._get(origin.pane_id)
        except Exception:
            return self._decline(origin, "get_failed")
        try:
            if not self._matches(origin, state):
                return self._decline(origin, "kind_mismatch")
            if state.status not in {"idle", "done", "working"}:
                return self._decline(origin, "status_other")
            if not self._allowed(origin, "processing"):
                return self._decline(origin, "not_allowed")
            before = None
            attempts = 2 if origin.kind == "devin" and state.status == "working" else 1
            for _ in range(attempts):
                try:
                    raw = self._read(origin.pane_id)
                except Exception:
                    return self._decline(origin, "read_failed")
                before = _frame(origin.kind, raw)
                if before is not None and before.status in {"idle", "done", "working"}:
                    break
                if not self._allowed(origin, "processing"):
                    return self._decline(origin, "not_allowed")
            if before is None:
                return self._decline(origin, "frame_unparsed")
            if before.status not in {"idle", "done", "working"}:
                return self._decline(origin, "frame_status_other")
            if not self._allowed(origin, "processing"):
                return self._decline(origin, "not_allowed")
        except Exception:
            return self._decline(origin, "error")
        watch = Observation(origin, _baseline=before, _prompt=expected)
        self._active[origin.chat_id] = watch
        return watch

    def arm(self, watch: Observation) -> bool:
        if (self._active.get(watch.origin.chat_id) is not watch or watch.phase != "captured"
                or not self._allowed(watch.origin, "submitted")):
            self.cancel(watch)
            return False
        now = self._clock()
        if not math.isfinite(now):
            self.cancel(watch)
            return False
        watch.phase, watch.next_due, watch.deadline = "watching", now, now + LIFETIME
        self._emit(watch, "armed", "submitted")
        self._wake.set()
        return True

    def _guard(self, watch: Observation) -> bool:
        return (self._active.get(watch.origin.chat_id) is watch
                and self._allowed(watch.origin, "submitted"))

    def _deliver(self, watch: Observation, body: str, reason: str) -> None:
        if not self._guard(watch):
            self._close(watch, "guard_changed")
            return
        text = format_output(watch.origin.pane_id, body)
        watch.phase = "consumed"  # Before any external send; never reset on failure.
        result = "unknown"
        try:
            self._emit(watch, "attempted", reason)
            if not self._guard(watch):
                result = "guard_changed"
                return
            status = self._send(watch.origin, text)
            if status in {"sent", "failed", "unknown"}:
                result = status
        except Exception:
            pass
        finally:
            self._close(watch, result)

    def _read_failed(self, watch: Observation) -> None:
        try:
            if self._guard(watch):
                self._invalidate(watch.origin)
        except Exception:
            pass
        finally:
            self._close(watch, "read_failed")

    def tick(self) -> bool:
        """At most one due round; no sleeps, pending work queue, or prompt calls."""
        if self._stopping() or not self._lock.acquire(blocking=False):
            return False
        try:
            now = self._clock()
            if not math.isfinite(now):
                self.stop()
                return False
            due = [w for w in self._active.values() if w.phase == "watching"
                   and min(w.next_due, w.deadline) <= now]
            if not due:
                return False
            watch = min(due, key=lambda w: (min(w.next_due, w.deadline), w.watch_ref))
            if not self._guard(watch):
                self._close(watch, "guard_changed")
                return True
            if now >= watch.deadline or watch.polls >= MAX_POLLS:
                self._deliver(watch, NOTICE, "deadline")
                return True
            watch.polls += 1
            watch.next_due = now + INTERVAL
            try:
                state = self._get(watch.origin.pane_id)
                if not self._matches(watch.origin, state):
                    self._read_failed(watch)
                    return True
                if not self._guard(watch):
                    self._close(watch, "guard_changed")
                    return True
                if state.status == "working":
                    watch._candidate = None
                    watch._transient = 0
                    return True
                if state.status not in {"idle", "done"}:
                    self._deliver(watch, NOTICE, "attention")
                    return True
                raw = self._read(watch.origin.pane_id)
            except Exception:
                self._read_failed(watch)
                return True
            if not self._guard(watch):
                self._close(watch, "guard_changed")
                return True
            now = self._clock()
            watch.next_due = now + INTERVAL
            if not math.isfinite(now) or now >= watch.deadline:
                self._deliver(watch, NOTICE, "deadline")
                return True
            frame = _frame(watch.origin.kind, raw)
            result = (_extract(watch._baseline, frame, watch._prompt)
                      if frame is not None and watch._baseline is not None
                      else Extraction(None, "unrecognized"))
            if result.body is None:
                watch._candidate = None
                if result.reason in TRANSIENT_REASONS:
                    watch._transient += 1
                    if watch._transient <= TRANSIENT_POLLS:
                        return True
                else:
                    watch._transient = 0
                if result.reason not in {"unchanged", "waiting_for_prompt", "echo_only", "not_ready"}:
                    self._deliver(watch, NOTICE, result.reason)
                return True
            watch._transient = 0
            if watch._candidate == result.body and now - watch._candidate_at >= INTERVAL:
                self._deliver(watch, result.body, "body")
            else:
                watch._candidate, watch._candidate_at = result.body, now
            return True
        finally:
            self._lock.release()


    def run(self) -> None:
        """One runtime-owned thread; idle means an interruptible wait, not polling."""
        try:
            while not self._stopping():
                self._wake.clear()
                if self.tick():
                    continue
                delay = 0.1  # Foreground lock contention: yield without a work queue.
                if self._lock.acquire(blocking=False):
                    try:
                        due = [min(w.next_due, w.deadline) for w in self._active.values()
                               if w.phase == "watching"]
                        delay = max(0.01, min(due) - self._clock()) if due else None
                    finally:
                        self._lock.release()
                if not self._stopping():
                    self._wake.wait(delay)
        except Exception:
            self.request_stop()  # Never log raw exceptions or replay a failed tick.
        finally:
            if self._lock.acquire(blocking=False):
                try:
                    self.stop()
                finally:
                    self._lock.release()


def connect_output(core, reader, send, *, clock=time.monotonic, stopping=lambda: False):
    """Wire only existing read APIs and SQLite lookups; no scan, schema or writes."""
    import logging
    from .core import Message

    def binding_matches(origin, binding):
        return (binding is not None and binding.valid
                and (binding.herdr_session, binding.workspace_id, binding.pane_id, binding.revision)
                == (origin.session, origin.workspace_id, origin.pane_id, origin.revision))

    def current(origin):
        message = Message(origin.message_id, origin.chat_id, origin.user_id, "", None, origin.chat_type)
        return (not stopping() and origin.session == reader.session == core.herdr.session == "kpi-agg"
                and core.bot_open_id == origin.bot_id and core.is_authorized(message)
                and binding_matches(origin, core.store.get_binding(origin.chat_id)))

    def request_phase(origin):
        row = core.store.get_request(origin.message_id)
        if (row is None or (row.chat_id, row.user_id, row.action, row.herdr_session,
                            row.workspace_id, row.pane_id, row.binding_revision)
                != (origin.chat_id, origin.user_id, "prompt", origin.session,
                    origin.workspace_id, origin.pane_id, origin.revision)):
            return "other"
        if row.status == "done" and row.result_code == "submitted":
            return "submitted"
        return "processing" if row.status == "processing" and row.result_code == "" else "other"

    def invalidate(origin):
        binding = core.store.get_binding(origin.chat_id)
        if binding_matches(origin, binding):
            core.store.invalidate(binding)  # Existing conditional UPDATE protects newer revisions.

    def audit(ref, revision, event, reason):
        if ref == "capture":  # No watch exists yet; fixed codes only, no values.
            logging.getLogger(__name__).info("output capture=%s reason=%s", event, reason)
        else:
            logging.getLogger(__name__).info("output watch=%s revision=%s event=%s reason=%s",
                                            ref, revision, event, reason)

    observer = OutputObserver(current=current, request_phase=request_phase, get=reader.get_agent,
                              read=reader.read_agent, send=send, invalidate=invalidate,
                              operation_lock=core._lock, clock=clock, audit=audit, stopping=stopping)
    core.output = observer
    return observer
