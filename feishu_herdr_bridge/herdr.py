"""Thin HerdR 0.9.0 adapter with deliberately unconfigured live transport.

Batch 1 injects a command builder and a synthetic control-output decoder.
Their real equivalents require local session/JSON probes; there is no default
server fallback. CLI verbs/options below follow the confirmed static help.
"""

from __future__ import annotations

import subprocess
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
class ControlResult:
    agents: tuple[Agent, ...] = ()
    error: str | None = None


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


class HerdrAdapter:
    def __init__(
        self, session: str, *, command_builder: CommandBuilder | None = None,
        decoder: Decoder | None = None, runner: Runner = run_command,
        query_timeout: float = 5.0, write_timeout: float = 15.0,
    ) -> None:
        if not safe_identifier(session):
            raise ValueError("An explicit session is required")
        if query_timeout <= 0 or write_timeout <= 0:
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
            result = self._decoder(action, process.stdout)
            if not isinstance(result, ControlResult):
                raise ValueError("Invalid decoder result")
            if result.error is not None:
                # Only a positively identified rejection is a known failure.
                if result.error not in {"target_missing", "blocked", "rejected"}:
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

    @staticmethod
    def _check_target(pane_id: str) -> None:
        if not safe_identifier(pane_id):
            raise HerdrError("invalid_target")
