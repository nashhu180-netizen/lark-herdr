"""Synchronous offline routing core plus a poll-driven per-pane send queue.

No SDK or self-owned background task loop: the runtime drives SendQueue.poll
from its own thread, exactly like the output observer's tick.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
import threading
import time
from collections import deque
from _thread import LockType
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Collection, Mapping
from uuid import uuid4

from .output import Observation, Origin, OutputObserver
from .herdr import Agent, HerdrAdapter, HerdrError, Workspace, safe_identifier, safe_text, valid_agent_name
from .store import Binding, BindingChanged, Creation, CreationRejected, GroupRequest, Store, WorkspaceOccupied


_OPERATION_LOCK = threading.Lock()
_HELP = ("/agents | /bind | /bind <workspace_id> <pane_id> | /read | "
         "/new <项目别名> <codex或claude或devin> | /group-new <群名> | /confirm <确认码> | /cancel")


@dataclass(frozen=True)
class Message:
    message_id: str
    chat_id: str
    user_id: str
    text: str
    created_at: float | None
    chat_type: str | None = None


@dataclass(frozen=True)
class Reply:
    status: str
    code: str
    text: str


@dataclass(frozen=True)
class Prepared:
    message: Message
    action: str
    args: list[str]
    snapshot: Binding | None


@dataclass(frozen=True, repr=False)
class GroupCreateInput:
    group_name: str
    requested_by: str
    create_uuid: str


@dataclass(frozen=True, repr=False)
class GroupCreateResult:
    # The future transport sets verified only after validating the entire create
    # response (requester/owner, private internal group, and the bot contract).
    created_chat_id: str | None = None
    verified: bool = False


@dataclass(frozen=True, repr=False)
class QueuedPrompt:
    message: Message
    binding: Binding
    kind: str
    enqueued_at: float


_QUEUE_IDLE = {"idle", "done"}
QUEUE_INTERVAL = 2.0
QUEUE_TIMEOUT = 600.0
QUEUE_EDGE_CAP = 15.0


class SendQueue:
    """Per-pane in-memory FIFO for prompts that arrive while a pane is busy.

    An entry's request row stays 'processing' until it is sent or dropped, so
    capture/arm reuse the unchanged request-phase guards and a restart settles
    every lost entry as 'interrupted'. The queue itself is volatile: restart
    loss is accepted and announced by a fixed-code log, never silent. poll()
    performs at most one bounded head check per pane; the runtime owns run().

    After any send to a pane, an in-flight edge gate blocks the next send to
    that pane until agent_status is observed non-idle and back at idle/done.
    agent_status lags the TUI, so a still-'idle' read inside that window is not
    proof the pane can take the next prompt. If the edge is never observed the
    gate opens anyway after `edge_cap` seconds and the miss is logged.
    """

    def __init__(self, core: BridgeCore, *, get: Callable[[str], Agent],
                 prompt: Callable[[str, str], None], send: Callable[[Origin, str], str],
                 clock: Callable[[], float] = time.monotonic,
                 interval: float = QUEUE_INTERVAL, timeout: float = QUEUE_TIMEOUT,
                 edge_cap: float = QUEUE_EDGE_CAP,
                 audit: Callable[[str, int, str, str], None] | None = None,
                 stopping: Callable[[], bool] = lambda: False) -> None:
        for value in (interval, timeout, edge_cap):
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value <= 0):
                raise ValueError("Queue timings must be finite positive numbers")
        self._core, self._get, self._prompt, self._send = core, get, prompt, send
        self._clock, self._interval, self._timeout = clock, float(interval), float(timeout)
        self._edge_cap = float(edge_cap)
        self._audit, self._external_stopping = audit, stopping
        self._queues: dict[str, deque[QueuedPrompt]] = {}
        self._inflight: dict[str, float] = {}  # pane -> last submit time awaiting its busy edge
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = False

    def _stopping(self) -> bool:
        try:
            return self._stopped or bool(self._external_stopping())
        except Exception:
            return True

    def _emit(self, entry: QueuedPrompt | None, event: str, reason: str) -> None:
        if self._audit is not None:
            try:
                self._audit(entry.message.message_id if entry else "queue",
                            entry.binding.revision if entry else 0, event, reason)
            except Exception:
                pass  # Never leak exception text or retry an operation to log it.

    def pending(self, pane_id: str) -> bool:
        with self._lock:
            return bool(self._queues.get(pane_id)) or pane_id in self._inflight

    def enqueue(self, message: Message, binding: Binding, kind: str) -> None:
        """Caller holds the shared operation lock; the row stays 'processing'."""
        entry = QueuedPrompt(message, binding, kind, self._clock())
        with self._lock:
            self._queues.setdefault(binding.pane_id, deque()).append(entry)
        self._emit(entry, "enqueued", "busy")
        self._wake.set()

    def mark_submitted(self, pane_id: str) -> None:
        """Record a send; the pane must prove a busy edge before the next one."""
        with self._lock:
            self._inflight[pane_id] = self._clock()
        self._wake.set()

    def request_stop(self) -> None:
        self._stopped = True
        self._wake.set()

    def stop(self) -> None:
        self.request_stop()
        with self._lock:
            remaining = sum(len(queue) for queue in self._queues.values())
            self._queues.clear()
            self._inflight.clear()
        if remaining:
            # Their rows stay 'processing'; startup recovery settles them as
            # 'interrupted'. This log is the required record of the in-memory loss.
            self._emit(None, "lost", "restart")

    def _origin(self, entry: QueuedPrompt) -> Origin:
        return Origin(entry.message.message_id, entry.message.chat_id, entry.message.user_id,
                      entry.binding.herdr_session, entry.binding.workspace_id, entry.binding.pane_id,
                      entry.binding.revision, self._core.bot_open_id, entry.kind,
                      entry.message.chat_type)

    def _finish(self, entry: QueuedPrompt, status: str, code: str) -> bool:
        try:
            self._core.store.finish(entry.message.message_id, status, code, self._core.clock())
            return True
        except Exception:
            return False

    def _pop(self, entry: QueuedPrompt) -> None:
        with self._lock:
            queue = self._queues.get(entry.binding.pane_id)
            if queue and queue[0] is entry:
                queue.popleft()
            if queue is not None and not queue:
                del self._queues[entry.binding.pane_id]

    def _settle(self, entry: QueuedPrompt, status: str, code: str, notice: str | None,
                *, sent: bool = False) -> bool:
        """Persist the terminal state before any definitive receipt or dequeue.

        Entries whose send was never attempted stay queued when the write fails
        and are retried by the next poll; once a send was attempted the entry is
        consumed exactly once regardless of the bookkeeping result.
        """
        persisted = self._finish(entry, status, code)
        if not persisted and not sent:
            self._emit(entry, "poll", "settle_failed")
            return False
        self._emit(entry, "closed", code)
        if not persisted:
            self._emit(entry, "poll", "persist_failed")
        if notice is not None:
            try:
                self._send(self._origin(entry), notice)
            except Exception:
                pass
        self._pop(entry)
        return True

    def _gate(self, entry: QueuedPrompt, agent: Agent, now: float) -> bool:
        """In-flight edge gate: True when this pane may take the next send."""
        with self._lock:
            inflight_at = self._inflight.get(entry.binding.pane_id)
            if agent.status not in _QUEUE_IDLE:
                if inflight_at is not None:
                    del self._inflight[entry.binding.pane_id]
                    self._emit(entry, "edge", "observed")
                return False
            if inflight_at is None:
                return True
            if now - inflight_at < self._edge_cap:
                return False  # Stale-idle window: the busy edge is not proven yet.
            del self._inflight[entry.binding.pane_id]
            self._emit(entry, "edge", "unobserved")
            return True

    def _current_binding(self, entry: QueuedPrompt) -> Binding | None:
        try:
            current = self._core.store.get_binding(entry.message.chat_id)
        except Exception:
            current = None
        if (current is None or not current.valid
                or (current.revision, current.herdr_session, current.workspace_id, current.pane_id)
                != (entry.binding.revision, entry.binding.herdr_session,
                    entry.binding.workspace_id, entry.binding.pane_id)):
            return None
        return current

    def _submit(self, entry: QueuedPrompt) -> bool:
        """One bounded send attempt under the shared operation lock.

        The pane status is re-read inside the lock, the in-flight edge gate
        must be satisfied, and the captured baseline must itself prove an
        idle/done frame: a busy baseline or a send-point flip keeps the head
        queued instead of submitting.
        """
        core = self._core
        if not core._lock.acquire(blocking=False):
            return False  # A foreground operation owns the slot; retry next poll.
        try:
            try:
                agent = self._get(entry.binding.pane_id)
            except Exception:
                self._emit(entry, "poll", "get_failed")
                return False
            if agent.workspace_id != entry.binding.workspace_id:
                core._invalidate_quietly(entry.binding, "prompt")
                return self._settle(entry, "failed", "workspace_mismatch",
                                    "目标 pane 已变化，排队消息未发送。")
            if not self._gate(entry, agent, self._clock()):
                return False
            if self._current_binding(entry) is None:
                return self._settle(entry, "failed", "binding_changed",
                                    "绑定已变化，排队消息未发送。")
            label = core._label(entry.binding)
            capture = None
            armed = False
            output = core.output
            try:
                if output is not None and entry.message.chat_type == "group":
                    try:
                        output.cancel_current(entry.message.chat_id)
                        capture = output.capture(self._origin(entry), entry.message.text)
                    except Exception:
                        capture = None
                if capture is not None and getattr(
                        getattr(capture, "_baseline", None), "status", None) not in _QUEUE_IDLE:
                    # The baseline itself is busy or unproven: cancel and wait.
                    self._emit(entry, "poll", "baseline_busy")
                    return False
                try:
                    agent = self._get(entry.binding.pane_id)
                except Exception:
                    self._emit(entry, "poll", "get_failed")
                    return False
                if agent.workspace_id != entry.binding.workspace_id:
                    core._invalidate_quietly(entry.binding, "prompt")
                    return self._settle(entry, "failed", "workspace_mismatch",
                                        "目标 pane 已变化，排队消息未发送。")
                if agent.status not in _QUEUE_IDLE:
                    self._emit(entry, "poll", "status_changed")
                    return False  # Flipped between capture and send; keep waiting.
                if self._current_binding(entry) is None:
                    return self._settle(entry, "failed", "binding_changed",
                                        "绑定已变化，排队消息未发送。")
                try:
                    self._prompt(entry.binding.pane_id, entry.message.text)
                except HerdrError as exc:
                    core._invalidate_quietly(entry.binding, "prompt")
                    return self._settle(entry, "unknown" if exc.uncertain else "failed",
                                        exc.code, f"{label}排队消息发送未获确认（{exc.code}）。",
                                        sent=True)
                except Exception:
                    core._invalidate_quietly(entry.binding, "prompt")
                    return self._settle(entry, "unknown", "internal_error",
                                        f"{label}排队消息发送失败，请检查现场后重发。",
                                        sent=True)
                settled = self._settle(entry, "done", "submitted", None, sent=True)
                self.mark_submitted(entry.binding.pane_id)
                if capture is not None and settled:
                    try:
                        armed = bool(output.arm(capture))
                    except Exception:
                        armed = False  # Auxiliary observation never changes submission.
                return True
            finally:
                if capture is not None and not armed:
                    try:
                        output.cancel(capture)
                    except Exception:
                        pass
        finally:
            core._lock.release()

    def poll(self) -> bool:
        """Drive each pane's head once; True when at least one entry settled."""
        if self._stopping():
            return False
        now = self._clock()
        if not math.isfinite(now):
            self.request_stop()
            return False
        with self._lock:
            heads = [queue[0] for queue in self._queues.values() if queue]
        consumed = False
        for entry in heads:
            if self._stopping():
                break
            if now - entry.enqueued_at >= self._timeout:
                consumed = self._settle(entry, "failed", "queue_timeout",
                                        f"{self._core._label(entry.binding)}pane 持续忙，未发送。") or consumed
            else:
                consumed = self._submit(entry) or consumed
        return consumed

    def run(self) -> None:
        """One runtime-owned thread; waits are interruptible, never busy."""
        try:
            while not self._stopping():
                if self.poll():
                    continue
                self._wake.wait(self._interval)
                self._wake.clear()
        except Exception:
            self.request_stop()  # Never log raw exceptions or replay a failed poll.
        finally:
            self.stop()


def connect_send_queue(core: BridgeCore, adapter: HerdrAdapter, send: Callable[[Origin, str], str],
                       *, clock: Callable[[], float] = time.monotonic,
                       interval: float = QUEUE_INTERVAL, timeout: float = QUEUE_TIMEOUT,
                       edge_cap: float = QUEUE_EDGE_CAP,
                       stopping: Callable[[], bool] = lambda: False) -> SendQueue:
    """Wire one volatile send queue; pending entries are lost on restart."""

    def audit(ref: str, revision: int, event: str, reason: str) -> None:
        logging.getLogger(__name__).info(
            "send-queue message=%s revision=%s event=%s reason=%s", ref, revision, event, reason)

    queue = SendQueue(core, get=adapter.get_agent, prompt=adapter.prompt, send=send,
                      clock=clock, interval=interval, timeout=timeout, edge_cap=edge_cap,
                      audit=audit, stopping=stopping)
    core.send_queue = queue
    return queue


class GroupCreateError(Exception):
    def __init__(self, *, created_chat_id: str | None = None, uncertain: bool = True) -> None:
        super().__init__("group_create_failed")
        self.created_chat_id = created_chat_id
        self.uncertain = uncertain


def agent_name(kind: str) -> str:
    return f"fb-{kind}-{secrets.token_hex(6)}"


class BridgeCore:
    def __init__(
        self, store: Store, herdr: HerdrAdapter, *, allowed_chats: Collection[str],
        allowed_users: Collection[str], clock: Callable[[], float] = time.time,
        operation_lock: LockType | None = None,
        projects: Mapping[str, str | Path] | None = None,
        management_chat_id: str | None = None, admin_users: Collection[str] = (),
        bot_open_id: str | None = None,
        create_group: Callable[[GroupCreateInput], GroupCreateResult] | None = None,
    ) -> None:
        self.store = store
        self.herdr = herdr
        self.allowed_chats = frozenset(allowed_chats)
        self.allowed_users = frozenset(allowed_users)
        self.clock = clock
        self.projects = dict(projects or {})
        self.output: OutputObserver | None = None
        self.send_queue: SendQueue | None = None
        self._output_capture: Observation | None = None
        self.management_chat_id = management_chat_id
        self.admin_users = frozenset(admin_users)
        self.bot_open_id = bot_open_id
        self._create_group = create_group
        if (management_chat_id is not None or self.admin_users) and (
            not safe_identifier(management_chat_id) or management_chat_id not in self.allowed_chats
            or not self.admin_users or not self.admin_users <= self.allowed_users
            or herdr.session != "kpi-agg" or not safe_identifier(bot_open_id)
        ):
            raise ValueError("Invalid management group configuration")
        self._lock = operation_lock if operation_lock is not None else _OPERATION_LOCK
        # Construct one core at process startup, before accepting any messages.
        self.store.recover_incomplete(self.clock())

    def is_authorized(self, message: Message) -> bool:
        if message.user_id not in self.allowed_users:
            return False
        if message.chat_id in self.allowed_chats:
            return True
        if (message.chat_type != "group" or self.herdr.session != "kpi-agg"
                or not safe_identifier(self.bot_open_id)):
            return False
        try:
            return self.store.group_allowed(message.chat_id, self.herdr.session, self.bot_open_id)
        except Exception:
            return False

    def _group_admin(self, message: Message) -> bool:
        return (message.chat_type == "group" and message.chat_id == self.management_chat_id
                and message.chat_id in self.allowed_chats and message.user_id in self.allowed_users
                and message.user_id in self.admin_users and self.herdr.session == "kpi-agg"
                and safe_identifier(self.bot_open_id))

    def _authorized_action(self, message: Message, action: str) -> bool:
        return self.is_authorized(message) and (
            action not in {"group_new", "group_confirm"} or self._group_admin(message)
        )

    def handle(self, message: Message) -> Reply:
        prepared = self.prepare(message)
        return prepared if isinstance(prepared, Reply) else self.execute(prepared)

    def prepare(self, message: Message) -> Prepared | Reply:
        """Claim/deduplicate and acquire the slot before creating a worker.

        This performs only short SQLite work, never a CLI/network call.
        The caller must execute or abandon an accepted Prepared exactly once.
        """
        action, args = self._parse(message.text)
        if not self._authorized_action(message, action):
            return Reply("failed", "forbidden", "用户或会话未获授权。")
        if not all(safe_identifier(v) for v in (
            message.message_id, message.chat_id, message.user_id
        )):
            return Reply("failed", "invalid_metadata", "消息标识无效。")
        try:
            # Group creation never takes a workspace binding as its target.
            snapshot = (None if action in {"group_new", "group_confirm"}
                        else self.store.get_binding(message.chat_id))
            previous, is_new = self.store.claim(
                message_id=message.message_id, chat_id=message.chat_id,
                user_id=message.user_id, action=action, snapshot=snapshot,
                now=self.clock(), session=self.herdr.session,
            )
        except Exception:
            return Reply("unknown", "storage_error", "状态存储不可用，未执行操作。")
        if not is_new:
            if previous.chat_id != message.chat_id or previous.user_id != message.user_id:
                return Reply("failed", "message_conflict", "消息标识冲突，未执行操作。")
            if previous.action == action == "group_confirm":
                try:
                    group = self._find_group(message, args[0])
                    if group is not None and group.status != "pending":
                        return self._group_reply(group)
                except Exception:
                    return Reply("unknown", "storage_error", "状态无法核对，未重复建群。")
            # Do not replay cached terminal content or any external operation.
            return Reply(previous.status, "duplicate", f"该消息已有记录（{previous.status}），未重复执行。")
        if not self._lock.acquire(blocking=False):
            return self._record(message, snapshot, action, Reply(
                "failed", "busy", "桥接正处理其他短操作，请稍后发送新消息重试。"
            ))
        return Prepared(message, action, args, snapshot)

    def abandon(self, prepared: Prepared) -> Reply:
        try:
            return self._record(prepared.message, prepared.snapshot, prepared.action, Reply(
                "failed", "worker_unavailable", "工作线程未启动，未执行操作，请发送新消息。"
            ))
        finally:
            self._lock.release()

    def execute(self, prepared: Prepared) -> Reply:
        message, action, args, snapshot = (
            prepared.message, prepared.action, prepared.args, prepared.snapshot
        )
        self._output_capture = None
        try:
            try:
                if self._authorized_action(message, action):
                    reply = self._dispatch(message, action, args, snapshot)
                else:
                    reply = Reply("failed", "forbidden", "用户或会话未获授权。")
            except WorkspaceOccupied:
                reply = Reply("failed", "workspace_occupied", "该 workspace 已被其他会话绑定。")
            except BindingChanged:
                reply = Reply("failed", "binding_changed", "绑定已变化，原消息未改投，请发送新消息。")
            except CreationRejected as exc:
                reply = Reply("failed", exc.code, f"创建确认不可执行（{exc.code}），请重新 /new。")
            except HerdrError as exc:
                self._invalidate_quietly(snapshot, action)
                prefix = self._label(snapshot) if snapshot else ""
                reply = Reply(
                    "unknown" if exc.uncertain else "failed", exc.code,
                    f"{prefix}HerdR 调用未获确认或已拒绝（{exc.code}）。"
                    "未自动重试；检查目标后重新绑定。",
                )
            except Exception:
                # Never expose exception repr: it may contain a prompt or CLI output.
                self._invalidate_quietly(snapshot, action)
                reply = Reply("unknown", "internal_error", "操作结果不明，未自动重试，请检查现场。")
            if action == "prompt" and reply.code == "queued":
                return reply  # The row stays 'processing'; SendQueue settles it later.
            recorded = self._record(message, snapshot, action, reply)
            if action == "prompt" and recorded.status == "done" and recorded.code == "submitted":
                enabled = False
                try:
                    if self.output is not None and self._output_capture is not None:
                        enabled = self.output.arm(self._output_capture)
                except Exception:
                    pass  # Auxiliary observation must never change submission success.
                if enabled:
                    self._output_capture = None
                elif self.output is not None and message.chat_type == "group":
                    recorded = replace(recorded, text=recorded.text + "本次自动回传未启用，可用 /read。")
            return recorded
        finally:
            try:
                if self.output is not None and self._output_capture is not None:
                    self.output.cancel(self._output_capture)
            except Exception:
                self._stop_output()
            self._output_capture = None
            self._lock.release()

    def _record(self, message: Message, snapshot: Binding | None, action: str, reply: Reply) -> Reply:
        try:
            # Persist a small machine code, never reply.text or message.text.
            self.store.finish(message.message_id, reply.status, reply.code, self.clock())
            return reply
        except Exception:
            self._invalidate_quietly(snapshot, action)
            if action in {"group_new", "group_confirm"}:
                return Reply("unknown", "storage_error", reply.text + "\n消息记录未确认；请用原确认码核对，不会重建。")
            return Reply("unknown", "storage_error", "结果记录失败；未自动重试，请检查现场。")

    def _invalidate_quietly(self, snapshot: Binding | None, action: str) -> None:
        if snapshot and action in {"prompt", "read", "binding"}:
            try:
                self.store.invalidate(snapshot)
            except Exception:
                pass  # Startup recovery will retry invalidation for processing records.

    @staticmethod
    def _parse(text: str) -> tuple[str, list[str]]:
        if not safe_text(text) or not text.strip():
            return "invalid_input", []
        if not text.lstrip().startswith("/"):
            return "prompt", []
        words = text.split()
        if words[0] == "/group-new":
            if any(char in text for char in ("\n", "\u2028", "\u2029")):
                return "invalid_input", []
            return "group_new", [text.lstrip()[len("/group-new"):].strip()]
        if words == ["/agents"]:
            return "agents", []
        if words == ["/read"]:
            return "read", []
        if words == ["/bind"]:
            return "binding", []
        if words[0] == "/bind" and len(words) == 3:
            return "bind", words[1:]
        if words[0] == "/new" and len(words) == 3:
            return "new", words[1:]
        if words[0] == "/confirm" and len(words) == 2:
            return ("group_confirm" if words[1].startswith("g-") else "confirm"), words[1:]
        if words == ["/cancel"]:
            return "cancel", []
        return "unknown_command", []

    def _dispatch(self, message: Message, action: str, args: list[str], snapshot: Binding | None) -> Reply:
        if action == "invalid_input":
            return Reply("failed", action, "文本为空或含终端控制字符，未投递。")
        if action == "unknown_command":
            return Reply("failed", action, f"支持：{_HELP}。其他斜杠命令不会透传。")
        if action == "read":
            self._cancel_output(message.chat_id)
        if action == "group_new":
            return self._propose_group(message, args[0])
        if action == "group_confirm":
            return self._confirm_group(message, args[0])
        if action == "new":
            return self._propose(message, args, snapshot)
        if action == "confirm":
            return self._confirm(message, args[0])
        if action == "cancel":
            cancelled = self.store.cancel_creation(message.chat_id, self.clock())
            if self._group_admin(message):
                cancelled_group = self.store.cancel_group(
                    message.chat_id, message.user_id, self.herdr.session, self.bot_open_id, self.clock(),
                )
                cancelled = cancelled or cancelled_group
            return Reply("done", "cancelled" if cancelled else "nothing_to_cancel",
                         "已取消待执行请求。" if cancelled else "本会话没有待执行的创建请求。")
        if action == "agents":
            agents = sorted(self.herdr.list_agents_with_labels(),
                            key=lambda a: (a.workspace_id, a.pane_id))
            if not agents:
                return Reply("done", "agents", "当前 session 无 live Agent。")
            owners = self.store.owners(self.herdr.session)
            workspaces: dict[str, list[Agent]] = {}
            for agent in agents:
                workspaces.setdefault(agent.workspace_id, []).append(agent)
            lines = [f"Workspace（{len(workspaces)}）："]
            for workspace_id, members in workspaces.items():
                lines.append(
                    f"- {workspace_id} | {members[0].workspace_label or '(无 workspace 名称)'}"
                )
            lines.extend(("", f"Pane/Agent（{len(agents)}）："))
            for workspace_id, members in workspaces.items():
                lines.append(
                    f"{workspace_id} | {members[0].workspace_label or '(无 workspace 名称)'}"
                )
                owner = owners.get(workspace_id)
                occupancy = "本会话" if owner == message.chat_id else ("已占用" if owner else "未绑定")
                for agent in members:
                    lines.append(
                        f"  {agent.pane_id} | Tab: {agent.tab_label or '(无 Tab 名称)'}"
                        f" | Agent: {agent.name or '(未命名)'}"
                        f" | {agent.kind} | {agent.status or 'unknown'} | {occupancy}"
                    )
            return Reply("done", "agents", "\n".join(lines))
        if action == "bind":
            workspace_id, pane_id = args
            if not safe_identifier(workspace_id) or not safe_identifier(pane_id):
                return Reply("failed", "invalid_target", "目标 ID 无效。")
            agent = self.herdr.get_agent(pane_id)
            if agent.workspace_id != workspace_id:
                return Reply("failed", "workspace_mismatch", "Pane 不属于指定 workspace，未换绑。")
            bound = self.store.bind(
                chat_id=message.chat_id, session=self.herdr.session,
                workspace_id=workspace_id, pane_id=pane_id, agent_name=agent.name,
                user_id=message.user_id, now=self.clock(),
                expected_revision=snapshot.revision if snapshot else None,
            )
            return Reply("done", "bound", f"{self._label(bound)}绑定成功；后续文本发往此 Pane。")
        if snapshot is None:
            return Reply("failed", "unbound", f"本会话未绑定。{_HELP}")
        if not snapshot.valid:
            return Reply("failed", "binding_invalid", f"{self._label(snapshot)}绑定已失效，请重新 /bind。")
        if action == "prompt":
            stamp = message.created_at
            if (not isinstance(stamp, (int, float)) or isinstance(stamp, bool)
                    or not math.isfinite(stamp)):
                return Reply("failed", "invalid_time", "消息时间无效，未投递。")
            if stamp < snapshot.bound_at:
                return Reply("failed", "stale_message", "消息早于当前绑定，未投递。")
            if message.text.lstrip().startswith("-"):
                return Reply("failed", "leading_hyphen_unverified", "前导连字符文本尚未 live 验证，未投递。")
        agent = self._validated_target(snapshot)
        label = self._label(snapshot)
        if action == "binding":
            return Reply("done", "binding", f"{label}绑定有效，当前 Agent：{agent.name or agent.kind}。")
        if action == "read":
            raw = self.herdr.read_agent(snapshot.pane_id)
            lines = raw.splitlines(keepends=True)
            content = "".join(lines[:80])
            truncated = len(lines) > 80 or len(content) > 3000
            content = content[:3000]
            return Reply("done", "read", f"{label}\n{content}" + ("\n[输出已截断]" if truncated else ""))
        if (self.send_queue is not None and message.chat_type == "group"
                and (agent.status not in _QUEUE_IDLE or self.send_queue.pending(snapshot.pane_id))):
            # Group prompts capture a baseline, so a busy pane must not be sent
            # mid-render (Issue #24). Queue strictly FIFO; private chats never
            # captured baselines and keep the original immediate path.
            self.send_queue.enqueue(message, snapshot, agent.kind)
            return Reply("done", "queued", f"{label}已排队，等 pane 空闲后发送。")
        self._capture_output(message, snapshot, agent)
        self._assert_current(snapshot)  # Capture added reads; never bypass a changed binding.
        self.herdr.prompt(snapshot.pane_id, message.text)
        if self.send_queue is not None:
            self.send_queue.mark_submitted(snapshot.pane_id)
        return Reply("done", "submitted", f"{label}已提交，尚未确认任务完成。")

    def _stop_output(self) -> None:
        try:
            if self.output is not None:
                self.output.request_stop()
        except Exception:
            pass

    def _cancel_output(self, chat_id: str) -> None:
        try:
            if self.output is not None:
                self.output.cancel_current(chat_id)
        except Exception:
            self._stop_output()  # Do not leave an old round live after a broken cancellation.

    def _capture_output(self, message: Message, binding: Binding, agent: Agent) -> None:
        if self.output is None:
            return
        self._cancel_output(message.chat_id)
        if message.chat_type != "group":
            return
        origin = Origin(message.message_id, message.chat_id, message.user_id,
                        binding.herdr_session, binding.workspace_id, binding.pane_id,
                        binding.revision, self.bot_open_id, agent.kind, message.chat_type)
        try:
            self._output_capture = self.output.capture(origin, message.text)
        except Exception:
            self._cancel_output(message.chat_id)

    def _propose_group(self, message: Message, name: str) -> Reply:
        if not 1 <= len(name) <= 60:
            return Reply("failed", "invalid_group_name", "群名须为 1 至 60 个字符。")
        now = self.clock()
        group = GroupRequest(
            message.message_id, "g-" + secrets.token_hex(8), message.chat_id, message.user_id,
            name, str(uuid4()), "kpi-agg", self.bot_open_id, None, "pending", "", now + 300, now, now,
        )
        self.store.propose_group(group)
        return Reply("done", "group_pending", (
            f"群名：{name}\n参考号：{group.reference}\n固定 session：kpi-agg。"
            "请求者成为群主，机器人自动入群；不会创建 workspace。\n"
            f"5 分钟内由你在本管理群发送 /confirm {group.confirmation_code}。"
        ))

    def _find_group(self, message: Message, code: str) -> GroupRequest | None:
        if not self._group_admin(message) or re.fullmatch(r"g-[0-9a-f]{16}", code) is None:
            return None
        return self.store.find_group(code, message.chat_id, message.user_id, self.herdr.session, self.bot_open_id)

    @staticmethod
    def _group_reply(group: GroupRequest) -> Reply:
        if group.status == "done":
            return Reply("done", "group_created", f"群名：{group.group_name}\n参考号：{group.reference}\n已创建并启用，可在新群 @ 机器人使用 /agents。")
        if group.status == "failed":
            return Reply("failed", "group_failed", f"参考号：{group.reference}\n提案已终止，未自动重建。")
        return Reply("unknown", "group_unknown", f"参考号：{group.reference}\n请求已消费但结果未确认；请人工核对，勿重复建群。")

    def _settle_group(self, group: GroupRequest, status: str, code: str) -> Reply:
        try:
            # A completed authorization can never be overwritten by error handling.
            self.store.finish_group(group.request_id, status, code, self.clock())
        except Exception:
            pass
        try:
            saved = self.store.find_group(group.confirmation_code, group.source_chat_id, group.requested_by,
                                          group.herdr_session, group.bot_open_id)
            if saved is not None and saved.status in {"done", "failed", "unknown"}:
                return self._group_reply(saved)
        except Exception:
            pass
        return self._group_reply(replace(group, status="unknown"))

    def _confirm_group(self, message: Message, code: str) -> Reply:
        group = self._find_group(message, code)
        if group is None:
            return Reply("failed", "invalid_confirmation", "建群确认不可执行。")
        if group.status != "pending":
            return self._group_reply(group)
        try:
            group = self.store.begin_group(group, self.clock())
            if group.status != "processing":
                return self._group_reply(group)
        except Exception:
            return self._settle_group(group, "unknown", "storage_error")
        try:
            if not self._group_admin(message) or self.bot_open_id != group.bot_open_id:
                return self._settle_group(group, "failed", "authorization_changed")
            if self._create_group is None:
                return self._settle_group(group, "failed", "creator_unavailable")
            try:
                result = self._create_group(GroupCreateInput(group.group_name, group.requested_by, group.create_uuid))
            except GroupCreateError as exc:
                result = GroupCreateResult(exc.created_chat_id)
                if exc.created_chat_id is None and exc.uncertain is False:
                    return self._settle_group(group, "failed", "rejected")
            if not isinstance(result, GroupCreateResult):
                return self._settle_group(group, "unknown", "invalid_result")
            if safe_identifier(result.created_chat_id):
                self.store.save_group_resource(group.request_id, result.created_chat_id, self.clock())
            else:
                return self._settle_group(group, "unknown", "missing_resource")
            if result.verified is not True:
                return self._settle_group(group, "unknown", "unverified_result")
            # Only this committed row enables dynamic authorization, never memory.
            return self._group_reply(self.store.complete_group(group.request_id, self.clock()))
        except Exception:
            # Includes a success response followed by either local commit failing.
            return self._settle_group(group, "unknown", "creation_unknown")

    def _project_path(self, alias: str) -> str:
        configured = self.projects.get(alias)
        if configured is None or not safe_identifier(alias):
            raise CreationRejected("invalid_project")
        try:
            path = Path(configured)
            if not path.is_absolute():
                raise ValueError("Relative project path")
            path = path.resolve(strict=True)
            if not path.is_dir() or not safe_text(str(path)) or "\n" in str(path):
                raise ValueError("Invalid project directory")
            return str(path)
        except (OSError, ValueError, RuntimeError):
            raise CreationRejected("invalid_project") from None

    def _propose(self, message: Message, args: list[str], snapshot: Binding | None) -> Reply:
        alias, kind = args
        if kind not in {"codex", "claude", "devin"}:
            raise CreationRejected("invalid_agent")
        if snapshot is not None and snapshot.herdr_session != self.herdr.session:
            raise CreationRejected("session_changed")
        path = self._project_path(alias)
        now = self.clock()
        # Reuse the originating message ID so its saved session is authoritative.
        short_id = hashlib.sha256(message.message_id.encode("utf-8")).hexdigest()[:8]
        creation = Creation(
            message.message_id, secrets.token_hex(4), message.chat_id, message.user_id,
            path, f"{alias}-{short_id}", kind, agent_name(kind),
            snapshot.revision if snapshot else None, now + 300.0, "pending",
            None, None, None, "", now, now,
        )
        if not valid_agent_name(creation.agent_name):
            raise CreationRejected("invalid_agent_name")
        self.store.propose_creation(creation)
        old = self._label(snapshot) if snapshot else "未绑定"
        return Reply("done", "creation_pending", (
            f"目录：{path}\nWorkspace：{creation.workspace_label}\n"
            f"Agent：{kind} / {creation.agent_name}\n成功后替换：{old}\n"
            f"尚未创建资源。5 分钟内由发起人在本会话发送 /confirm {creation.confirmation_code}。"
        ))

    def _confirm(self, message: Message, code: str) -> Reply:
        creation = self.store.find_creation(code, message.chat_id, message.user_id)
        if creation is None:
            raise CreationRejected("invalid_confirmation")
        if creation.status != "pending":
            raise CreationRejected("confirmation_used")
        # Re-resolve configured directories. A changed symlink must not redirect creation.
        for alias in self.projects:
            try:
                current_path = self._project_path(alias)
            except CreationRejected:
                continue
            if current_path == creation.project_path:
                break
        else:
            raise CreationRejected("project_changed")
        creation = self.store.begin_creation(creation, self.herdr.session, self.clock())
        workspace: Workspace | None = None
        try:
            names = {agent.name for agent in self.herdr.list_agents() if agent.name is not None}
            name = creation.agent_name
            if name in names:
                name = agent_name(creation.agent_kind)
                if not valid_agent_name(name) or name in names:
                    raise HerdrError("name_conflict")
                self.store.rename_creation(creation.request_id, name, self.clock())
            workspace = self.herdr.create_workspace(creation.project_path, creation.workspace_label)
            self.store.save_created_workspace(
                creation.request_id, workspace.workspace_id, workspace.tab_id, workspace.pane_id, self.clock(),
            )
            started = self.herdr.start_agent(name, creation.agent_kind, workspace.pane_id)
            actual = self.herdr.get_agent(workspace.pane_id)
            for agent in (started, actual):
                if (agent.workspace_id != workspace.workspace_id or agent.pane_id != workspace.pane_id
                        or agent.kind != creation.agent_kind):
                    raise HerdrError("start_unverified", uncertain=True)
            binding = self.store.complete_creation(creation.request_id, self.herdr.session, self.clock())
            return Reply("done", "created", f"{self._label(binding)}已创建并绑定 {name}，可发送任务正文。")
        except HerdrError as exc:
            status, result = ("unknown" if exc.uncertain else "failed"), exc.code
        except (BindingChanged, WorkspaceOccupied, CreationRejected):
            status, result = "failed", "binding_changed_or_occupied"
        except Exception:
            status, result = "unknown", "creation_interrupted"
        try:
            self.store.finish_creation(creation.request_id, status, result, self.clock())
        except Exception:
            status, result = "unknown", "storage_error"
        resource = (f"[{workspace.workspace_id} / {workspace.pane_id}] 已创建的资源保留。"
                    if workspace else "尚未确认新资源 ID，请核对现场。")
        return Reply(status, result,
                     f"创建请求 {creation.request_id} 未完成（{result}）。{resource}"
                     "旧绑定未修改。没有自动重试；本确认码不可再次执行。")

    def _validated_target(self, snapshot: Binding) -> Agent:
        self._assert_current(snapshot)
        if snapshot.herdr_session != self.herdr.session:
            raise HerdrError("session_mismatch")
        agent = self.herdr.get_agent(snapshot.pane_id)
        if agent.workspace_id != snapshot.workspace_id:
            raise HerdrError("workspace_mismatch")
        # Protect against a concurrent binding change while get was in flight.
        self._assert_current(snapshot)
        return agent

    def _assert_current(self, snapshot: Binding) -> None:
        current = self.store.get_binding(snapshot.chat_id)
        if (current is None or not current.valid
                or (current.revision, current.herdr_session, current.workspace_id, current.pane_id)
                != (snapshot.revision, snapshot.herdr_session, snapshot.workspace_id, snapshot.pane_id)):
            raise BindingChanged()

    @staticmethod
    def _label(binding: Binding) -> str:
        return f"[{binding.workspace_id} / {binding.pane_id}] "
