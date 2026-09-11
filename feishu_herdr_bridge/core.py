"""Synchronous offline routing core. No SDK, queue, or background task loop."""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import threading
import time
from _thread import LockType
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Collection, Mapping
from uuid import uuid4

from .herdr import Agent, HerdrAdapter, HerdrError, Workspace, safe_identifier, safe_text, valid_agent_name
from .store import Binding, BindingChanged, Creation, CreationRejected, GroupRequest, Store, WorkspaceOccupied


_OPERATION_LOCK = threading.Lock()
_HELP = ("/agents | /bind | /bind <workspace_id> <pane_id> | /read | "
         "/new <项目别名> <codex或claude> | /group-new <群名> | /confirm <确认码> | /cancel")


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
        self.herdr.prompt(snapshot.pane_id, message.text)
        return Reply("done", "submitted", f"{label}已提交，尚未确认任务完成。")

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
        if kind not in {"codex", "claude"}:
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
