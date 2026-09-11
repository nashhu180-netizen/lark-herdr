"""HerdR 0.9.0 / protocol 22 thin adapter.

CLI selection and envelope handling are schema-derived/static-not-live.
No live probe has run. Unknown shapes/codes fail closed; text reads stay text.
Transport is only enabled by explicit executable/session configuration.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class Agent:
    workspace_id: str
    pane_id: str
    name: str | None
    kind: str
    status: str | None = None


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    tab_id: str
    pane_id: str


@dataclass(frozen=True)
class ControlResult:
    agents: tuple[Agent, ...] = ()
    error: str | None = None
    workspace: Workspace | None = None


class HerdrError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        # Never include stdout, stderr, or the original command in an error.
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain


CommandBuilder = Callable[[str, tuple[str, ...]], Sequence[str]]
Runner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
Decoder = Callable[[str, str], ControlResult]


def safe_text(text: str) -> bool:
    return isinstance(text, str) and all(
        c == "\n" or unicodedata.category(c) not in {"Cc", "Cs"} for c in text
    )


def safe_identifier(value: str) -> bool:
    return (isinstance(value, str) and bool(value) and safe_text(value)
            and not value.startswith("-") and not any(c.isspace() for c in value))


def run_command(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    # subprocess.run kills and waits for its child on TimeoutExpired.
    return subprocess.run(
        list(command), stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=False,
    )


class CommandInterrupted(Exception):
    """Local shutdown; a started command may already have caused a write."""

    def __init__(self, *, started: bool) -> None:
        super().__init__("command_interrupted")
        self.started = started


class ManagedRunner:
    """One tracked CLI child; no queue, retry, server stop, or Agent signalling."""

    def __init__(self) -> None:
        self._stopping = threading.Event()
        self._guard = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def __call__(self, command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        with self._guard:
            if self._stopping.is_set():
                raise CommandInterrupted(started=False)
            if self._process is not None:
                raise RuntimeError("CLI operation already active")
            process = subprocess.Popen(
                list(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", shell=False,
            )
            self._process = process
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            if self._stopping.is_set():
                raise CommandInterrupted(started=True)
            return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)
        except BaseException:
            # Killing the CLI cannot retract a request already received by HerdR.
            self._terminate(process, grace=0.2)
            raise
        finally:
            if process.poll() is not None:
                with self._guard:
                    if self._process is process:
                        self._process = None
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    @staticmethod
    def _terminate(process: subprocess.Popen[str], grace: float) -> bool:
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=grace)
        except (OSError, subprocess.TimeoutExpired):
            return process.poll() is not None
        return True

    def stop(self, grace: float = 1.0) -> bool:
        # Set the flag before taking the guard, covering shutdown/spawn races.
        self._stopping.set()
        with self._guard:
            process = self._process
        return process is None or self._terminate(process, grace)


class HerdrAdapter:
    def __init__(
        self, session: str, *, command_builder: CommandBuilder | None = None,
        decoder: Decoder | None = None, runner: Runner = run_command,
        query_timeout: float = 5.0, write_timeout: float = 15.0,
    ) -> None:
        if not safe_identifier(session):
            raise ValueError("An explicit session is required")
        if any(isinstance(t, bool) or not math.isfinite(t) or t <= 0
               for t in (query_timeout, write_timeout)):
            raise ValueError("Timeouts must be positive")
        self._session = session
        self._builder = command_builder
        self._decoder = decoder
        self._runner = runner
        self._query_timeout = query_timeout
        self._write_timeout = write_timeout

    @property
    def session(self) -> str:
        return self._session

    def _run(self, args: tuple[str, ...], *, writing: bool = False) -> subprocess.CompletedProcess[str]:
        if self._builder is None or self._decoder is None:
            raise HerdrError("unverified_contract")
        try:
            command = tuple(self._builder(self.session, args))
            if (not command or not all(isinstance(p, str) and "\x00" not in p for p in command)
                    or not Path(command[0]).is_absolute()):
                raise ValueError("Invalid command builder result")
        except Exception:
            raise HerdrError("invalid_command") from None
        try:
            result = self._runner(
                command, self._write_timeout if writing else self._query_timeout
            )
            if not isinstance(result, subprocess.CompletedProcess):
                raise TypeError("Runner must return CompletedProcess")
            return result
        except CommandInterrupted as exc:
            raise HerdrError("interrupted", uncertain=exc.started) from None
        except subprocess.TimeoutExpired:
            raise HerdrError("timeout", uncertain=True) from None
        except (FileNotFoundError, PermissionError):
            raise HerdrError("cli_unavailable") from None
        except OSError:
            raise HerdrError("runner_error", uncertain=writing) from None
        except Exception:
            raise HerdrError("runner_error", uncertain=writing) from None

    def _decode(self, action: str, process: subprocess.CompletedProcess[str], *, writing: bool) -> ControlResult:
        try:
            assert self._decoder is not None
            # HerdR CLI successes are written to stdout while structured
            # server errors are written to stderr. Batch 1's synthetic fake
            # used stdout for both, so retain that fallback for old tests.
            payload = process.stdout if process.returncode == 0 or not process.stderr.strip() else process.stderr
            result = self._decoder(action, payload)
            if not isinstance(result, ControlResult):
                raise ValueError("Invalid decoder result")
            if result.error is not None:
                # Only a positively identified rejection is a known failure.
                if result.error == "remote_error":
                    raise HerdrError("remote_error", uncertain=writing)
                if result.error not in {"target_missing", "blocked", "rejected", "name_conflict"}:
                    raise ValueError("Unrecognized rejection")
                raise HerdrError(result.error)
            if process.returncode != 0:
                raise ValueError("Exit code contradicts success response")
            seen: set[str] = set()
            for agent in result.agents:
                if (not isinstance(agent, Agent)
                        or not safe_identifier(agent.workspace_id)
                        or not safe_identifier(agent.pane_id)
                        or not safe_identifier(agent.kind)
                        or (agent.name is not None and not safe_identifier(agent.name))
                        or (agent.status is not None and not safe_identifier(agent.status))
                        or agent.pane_id in seen):
                    raise ValueError("Invalid or duplicate agent")
                seen.add(agent.pane_id)
            if result.workspace is not None and (
                not isinstance(result.workspace, Workspace) or not all(safe_identifier(v) for v in (
                    result.workspace.workspace_id, result.workspace.tab_id, result.workspace.pane_id
                ))
            ):
                raise ValueError("Invalid workspace result")
            return result
        except HerdrError:
            raise
        except Exception:
            raise HerdrError("invalid_output", uncertain=writing) from None

    def list_agents(self) -> tuple[Agent, ...]:
        process = self._run(("agent", "list"))
        return self._decode("list", process, writing=False).agents

    def get_agent(self, pane_id: str) -> Agent:
        self._check_target(pane_id)
        process = self._run(("agent", "get", pane_id))
        result = self._decode("get", process, writing=False)
        if len(result.agents) != 1 or result.agents[0].pane_id != pane_id:
            raise HerdrError("wrong_target")
        return result.agents[0]

    def read_agent(self, pane_id: str) -> str:
        self._check_target(pane_id)
        process = self._run((
            "agent", "read", pane_id, "--source", "visible",
            "--lines", "80", "--format", "text",
        ))
        if process.returncode != 0:
            self._decode("read", process, writing=False)
            raise HerdrError("invalid_output")
        if not isinstance(process.stdout, str):
            raise HerdrError("invalid_output")
        return process.stdout

    def prompt(self, pane_id: str, text: str) -> None:
        self._check_target(pane_id)
        if not safe_text(text) or not text.strip():
            raise HerdrError("invalid_input")
        # Passing a leading '-' safely needs a live CLI probe. Do not invent
        # an unconfirmed '--' separator, escape sequence, or transport option.
        if text.lstrip().startswith("-"):
            raise HerdrError("leading_hyphen_unverified")
        process = self._run(("agent", "prompt", pane_id, text), writing=True)
        result = self._decode("prompt", process, writing=True)
        if result.agents:
            raise HerdrError("invalid_output", uncertain=True)

    def create_workspace(self, cwd: str, label: str) -> Workspace:
        if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not safe_text(cwd) or "\n" in cwd:
            raise HerdrError("invalid_project")
        if not safe_identifier(label):
            raise HerdrError("invalid_label")
        process = self._run((
            "workspace", "create", "--cwd", cwd, "--label", label, "--no-focus",
        ), writing=True)
        result = self._decode("create", process, writing=True)
        if result.workspace is None:
            raise HerdrError("invalid_output", uncertain=True)
        return result.workspace

    def start_agent(self, name: str, kind: str, pane_id: str) -> Agent:
        self._check_target(pane_id)
        if not valid_agent_name(name) or kind not in {"codex", "claude"}:
            raise HerdrError("invalid_agent")
        process = self._run((
            "agent", "start", name, "--kind", kind, "--pane", pane_id,
            "--timeout", str(max(1, int(self._write_timeout * 1000))),
        ), writing=True)
        result = self._decode("start", process, writing=True)
        if len(result.agents) != 1 or result.agents[0].pane_id != pane_id:
            raise HerdrError("wrong_target", uncertain=True)
        return result.agents[0]

    @staticmethod
    def _check_target(pane_id: str) -> None:
        if not safe_identifier(pane_id):
            raise HerdrError("invalid_target")


def valid_agent_name(name: str) -> bool:
    return isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) is not None


def session_command(executable: str) -> CommandBuilder:
    if not isinstance(executable, str) or not Path(executable).is_absolute() or not safe_text(executable):
        raise ValueError("HerdR executable must be an absolute path")

    def build(session: str, args: tuple[str, ...]) -> tuple[str, ...]:
        if not safe_identifier(session):
            raise ValueError("An explicit session is required")
        return (executable, "--session", session, *args)

    return build


def _agent_info(value: object) -> Agent:
    if not isinstance(value, dict):
        raise ValueError("AgentInfo must be an object")
    for key in ("workspace_id", "tab_id", "pane_id", "terminal_id", "agent_status"):
        if not safe_identifier(value.get(key)):
            raise ValueError("Missing AgentInfo field")
    if type(value.get("focused")) is not bool or type(value.get("revision")) is not int:
        raise ValueError("Invalid AgentInfo metadata")
    # Detection/display values are optional. Never infer a kind from the name.
    kind = next((value[key] for key in ("agent", "display_agent")
                 if isinstance(value.get(key), str) and value[key] in {"codex", "claude"}), "unknown")
    return Agent(value["workspace_id"], value["pane_id"], value.get("name"), kind, value["agent_status"])


def _resource_id(value: object, field: str) -> str:
    # The bundled protocol-22 schema defines a distinct ID field for each
    # nested resource. Accept no generic or inferred fallback.
    result = value.get(field) if isinstance(value, dict) else None
    if not safe_identifier(result):
        raise ValueError("Missing explicit resource ID")
    return result


def decode_protocol22(action: str, stdout: str) -> ControlResult:
    """Decode supplied static envelope shapes, not a claim of live compatibility."""
    envelope = json.loads(stdout)
    if not isinstance(envelope, dict) or not isinstance(envelope.get("id"), str):
        raise ValueError("Missing response envelope")
    if ("result" in envelope) == ("error" in envelope):
        raise ValueError("Ambiguous response envelope")
    if "error" in envelope:
        error = envelope["error"]
        if (not isinstance(error, dict) or type(error.get("code")) not in (str, int)
                or not isinstance(error.get("message"), str)):
            raise ValueError("Invalid error envelope")
        # Exact error-code meanings were not provided by the static evidence.
        # Do not classify timeouts/name conflicts by searching human messages.
        return ControlResult(error="remote_error")
    result = envelope["result"]
    expected = {"list": "agent_list", "get": "agent_info", "prompt": "agent_prompted",
                "create": "workspace_created", "start": "agent_started"}
    if not isinstance(result, dict) or result.get("type") != expected.get(action):
        raise ValueError("Unexpected result type")
    if action == "list":
        if not isinstance(result.get("agents"), list):
            raise ValueError("Missing agent list")
        return ControlResult(agents=tuple(_agent_info(item) for item in result["agents"]))
    if action in {"get", "start"}:
        if action == "start" and (
            not isinstance(result.get("argv"), list)
            or not all(isinstance(arg, str) for arg in result["argv"])
        ):
            raise ValueError("Missing start argv")
        return ControlResult(agents=(_agent_info(result.get("agent")),))
    if action == "create":
        workspace = Workspace(
            _resource_id(result.get("workspace"), "workspace_id"),
            _resource_id(result.get("tab"), "tab_id"),
            _resource_id(result.get("root_pane"), "pane_id"),
        )
        for key in ("tab", "root_pane"):
            item = result[key]
            if isinstance(item, dict) and "workspace_id" in item and item["workspace_id"] != workspace.workspace_id:
                raise ValueError("Inconsistent created workspace")
        pane = result["root_pane"]
        if isinstance(pane, dict) and "tab_id" in pane and pane["tab_id"] != workspace.tab_id:
            raise ValueError("Inconsistent created tab")
        return ControlResult(workspace=workspace)
    if action == "prompt":
        return ControlResult()
    raise ValueError("Unsupported response")
