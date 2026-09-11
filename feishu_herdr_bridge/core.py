"""Synchronous offline routing core. No SDK, queue, or background task loop."""

from __future__ import annotations

import math
import threading
import time
from _thread import LockType
from dataclasses import dataclass
from typing import Callable, Collection

from .herdr import Agent, HerdrAdapter, HerdrError, safe_identifier, safe_text
from .store import Binding, BindingChanged, Store, WorkspaceOccupied


_OPERATION_LOCK = threading.Lock()
_HELP = "/agents | /bind | /bind <workspace_id> <pane_id> | /read"


@dataclass(frozen=True)
class Message:
    message_id: str
    chat_id: str
    user_id: str
    text: str
    created_at: float | None


@dataclass(frozen=True)
class Reply:
    status: str
    code: str
    text: str


class BridgeCore:
    def __init__(
        self, store: Store, herdr: HerdrAdapter, *, allowed_chats: Collection[str],
        allowed_users: Collection[str], clock: Callable[[], float] = time.time,
        operation_lock: LockType | None = None,
    ) -> None:
        self.store = store
        self.herdr = herdr
        self.allowed_chats = frozenset(allowed_chats)
        self.allowed_users = frozenset(allowed_users)
        self.clock = clock
        self._lock = operation_lock if operation_lock is not None else _OPERATION_LOCK
        # Construct one core at process startup, before accepting any messages.
        self.store.recover_incomplete(self.clock())

    def handle(self, message: Message) -> Reply:
        if (message.chat_id not in self.allowed_chats
                or message.user_id not in self.allowed_users):
            return Reply("failed", "forbidden", "用户或会话未获授权。")
        if not all(safe_identifier(v) for v in (
            message.message_id, message.chat_id, message.user_id
        )):
            return Reply("failed", "invalid_metadata", "消息标识无效。")
        action, args = self._parse(message.text)
        try:
            snapshot = self.store.get_binding(message.chat_id)
            previous, is_new = self.store.claim(
                message_id=message.message_id, chat_id=message.chat_id,
                user_id=message.user_id, action=action, snapshot=snapshot,
                now=self.clock(),
            )
        except Exception:
            return Reply("unknown", "storage_error", "状态存储不可用，未执行操作。")
        if not is_new:
            if previous.chat_id != message.chat_id or previous.user_id != message.user_id:
                return Reply("failed", "message_conflict", "消息标识冲突，未执行操作。")
            # Do not replay cached terminal content or any external operation.
            return Reply(previous.status, "duplicate", f"该消息已有记录（{previous.status}），未重复执行。")
        if not self._lock.acquire(blocking=False):
            return self._record(message, snapshot, action, Reply(
                "failed", "busy", "桥接正处理其他短操作，请稍后发送新消息重试。"
            ))
        try:
            try:
                reply = self._dispatch(message, action, args, snapshot)
            except WorkspaceOccupied:
                reply = Reply("failed", "workspace_occupied", "该 workspace 已被其他会话绑定。")
            except BindingChanged:
                reply = Reply("failed", "binding_changed", "绑定已变化，原消息未改投，请发送新消息。")
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
            return self._record(message, snapshot, action, reply)
        finally:
            self._lock.release()

    def _record(self, message: Message, snapshot: Binding | None, action: str, reply: Reply) -> Reply:
        try:
            # Persist a small machine code, never reply.text or message.text.
            self.store.finish(message.message_id, reply.status, reply.code, self.clock())
            return reply
        except Exception:
            self._invalidate_quietly(snapshot, action)
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
        if words == ["/agents"]:
            return "agents", []
        if words == ["/read"]:
            return "read", []
        if words == ["/bind"]:
            return "binding", []
        if words[0] == "/bind" and len(words) == 3:
            return "bind", words[1:]
        return "unknown_command", []

    def _dispatch(self, message: Message, action: str, args: list[str], snapshot: Binding | None) -> Reply:
        if action == "invalid_input":
            return Reply("failed", action, "文本为空或含终端控制字符，未投递。")
        if action == "unknown_command":
            return Reply("failed", action, f"Batch 1 支持：{_HELP}。其他斜杠命令不会透传。")
        if action == "agents":
            agents = self.herdr.list_agents()
            owners = self.store.owners(self.herdr.session)
            lines = []
            for agent in sorted(agents, key=lambda a: (a.workspace_id, a.pane_id)):
                owner = owners.get(agent.workspace_id)
                occupancy = "本会话" if owner == message.chat_id else ("已占用" if owner else "未绑定")
                lines.append(
                    f"{agent.workspace_id} / {agent.pane_id} | {agent.name or '(无名称)'}"
                    f" | {agent.kind} | {agent.status or 'unknown'} | {occupancy}"
                )
            return Reply("done", "agents", "\n".join(lines) or "当前 session 无 live Agent。")
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
        self.herdr.prompt(snapshot.pane_id, message.text)
        return Reply("done", "submitted", f"{label}已提交，尚未确认任务完成。")

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
