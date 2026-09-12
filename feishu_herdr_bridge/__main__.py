"""Linux entry point; HerdR and Feishu live acceptance remain pending.

Import and --help perform no CLI calls, SDK connections, or credential lookup.
The process lock is acquired before SQLite recovery or SDK construction.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import math
import os
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .core import BridgeCore
from .feishu import Credentials, FeishuBridge, LarkTransport
from .herdr import HerdrAdapter, ManagedRunner, decode_protocol22, safe_identifier, session_command
from .store import Store
from .output import connect_output


class AlreadyRunning(RuntimeError):
    """Another bridge holds the same canonical configuration lock."""


class InstanceLock:
    def __init__(self, config_path: Path, *, directory: Path | None = None) -> None:
        canonical = str(config_path.resolve(strict=True))
        directory = directory or Path.home() / ".local/state/feishu-herdr-bridge/locks"
        self.path = directory / (hashlib.sha256(os.fsencode(canonical)).hexdigest() + ".lock")
        self._fd: int | None = None

    def __enter__(self) -> InstanceLock:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(fd, 0o600)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning("Configuration is already in use") from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, *_exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # Never unlink the file: a second inode would permit a second holder.


class BridgeRuntime:
    """Admission gate and lifetime tracking around the existing Feishu bridge."""

    def __init__(self, core: BridgeCore, bot_open_id: str, send, runner: ManagedRunner,
                 *, output_reader=None, send_output=None) -> None:
        self.stopping = False
        self.runner = runner
        self._guard = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self.bridge = FeishuBridge(core, bot_open_id, send, thread_factory=self._worker)
        self.output = (connect_output(core, output_reader, send_output, stopping=lambda: self.stopping)
                       if output_reader is not None and send_output is not None else None)
        self._output_thread: threading.Thread | None = None

    def start_output(self) -> None:
        if self.output is not None and self._output_thread is None and not self.stopping:
            self._output_thread = threading.Thread(target=self.output.run, name="pane-output", daemon=True)
            self._output_thread.start()

    def _worker(self, *, target, args, daemon):
        def execute():
            try:
                target(*args)
            finally:
                with self._guard:
                    self._workers.discard(threading.current_thread())

        worker = threading.Thread(target=execute, daemon=daemon)
        with self._guard:
            if self.stopping:
                raise RuntimeError("Bridge is stopping")
            self._workers.add(worker)
        return worker

    def receive(self, data):
        if self.stopping:
            return None
        return self.bridge.receive(data)

    def close(self) -> None:
        self.stopping = True
        if self.output is not None:
            self.output.request_stop()
        reaped = self.runner.stop()
        deadline = time.monotonic() + 3.0
        with self._guard:
            workers = tuple(self._workers)
        for worker in workers:
            if worker.ident is not None:
                worker.join(max(0.0, deadline - time.monotonic()))
        if self._output_thread is not None and self._output_thread.ident is not None:
            self._output_thread.join(max(0.0, deadline - time.monotonic()))
        if (not reaped or any(worker.is_alive() for worker in workers)
                or (self._output_thread is not None and self._output_thread.is_alive())):
            raise RuntimeError("Shutdown did not complete within the local grace period")
        if self.output is not None:
            self.output.stop()  # Workers are quiescent; clear all remaining captures without sending.


@contextmanager
def shutdown_signals(runtime: BridgeRuntime):
    previous = {}

    def interrupt(_signum, _frame):
        if not runtime.stopping:
            # No locks, SQLite calls, or child waits inside a signal handler.
            runtime.stopping = True
            # KeyboardInterrupt also escapes asyncio's generic callback handler.
            raise KeyboardInterrupt()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupt)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


@dataclass(frozen=True)
class Config:
    executable: str
    session: str
    bot_open_id: str
    users: frozenset[str]
    chats: frozenset[str]
    projects: dict[str, str]
    database: Path
    query_timeout: float
    write_timeout: float
    management_chat_id: str | None = None
    admin_users: frozenset[str] = frozenset()


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    required = {"herdr_executable", "herdr_session", "bot_open_id", "allowed_users", "allowed_chats", "projects"}
    optional = {"database", "query_timeout", "write_timeout", "management_chat_id", "admin_users"}
    if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - optional:
        raise ValueError("Invalid configuration keys; credentials must be supplied through the environment")
    executable, session = raw["herdr_executable"], raw["herdr_session"]
    session_command(executable)  # Validation only, never calls the executable.
    if not safe_identifier(session) or not safe_identifier(raw["bot_open_id"]):
        raise ValueError("Explicit session and bot_open_id are required")
    for key in ("allowed_users", "allowed_chats"):
        if not isinstance(raw[key], list) or not raw[key] or not all(safe_identifier(v) for v in raw[key]):
            raise ValueError("Nonempty user and chat allowlists are required")
    management = raw.get("management_chat_id")
    admins = raw.get("admin_users", [])
    if (("management_chat_id" in raw) != ("admin_users" in raw)
            or not isinstance(admins, list) or not all(safe_identifier(v) for v in admins)):
        raise ValueError("Management configuration must be complete")
    if management is not None or admins:
        if (not safe_identifier(management) or management not in raw["allowed_chats"]
                or not admins or not set(admins) <= set(raw["allowed_users"]) or session != "kpi-agg"):
            raise ValueError("Invalid management group authorization")
    projects = raw["projects"]
    if not isinstance(projects, dict) or not all(
        safe_identifier(alias) and isinstance(directory, str) and Path(directory).is_absolute()
        for alias, directory in projects.items()
    ):
        raise ValueError("Projects must map aliases to absolute directories")
    database = raw.get("database", str(Path.home() / ".local/state/feishu-herdr-bridge/bridge.sqlite3"))
    if not isinstance(database, str) or not Path(database).is_absolute():
        raise ValueError("Database must use an absolute path")
    timeouts = (raw.get("query_timeout", 5.0), raw.get("write_timeout", 15.0))
    if any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0 for t in timeouts):
        raise ValueError("Timeouts must be finite positive numbers")
    return Config(executable, session, raw["bot_open_id"], frozenset(raw["allowed_users"]),
                  frozenset(raw["allowed_chats"]), projects, Path(database), *timeouts,
                  management, frozenset(admins))


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Feishu/HerdR bridge; live boundary validation pending")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        credentials = Credentials.from_env(os.environ if env is None else env)
    except (OSError, ValueError, TypeError):
        parser.exit(2, "Invalid configuration or missing FEISHU_APP_ID/FEISHU_APP_SECRET.\n")
    logging.basicConfig(level=logging.INFO)
    try:
        # Acquire before SQLite recovery and SDK construction.
        with InstanceLock(args.config):
            runner = ManagedRunner()
            try:
                adapter = HerdrAdapter(config.session, command_builder=session_command(config.executable),
                                       decoder=decode_protocol22, runner=runner,
                                       query_timeout=config.query_timeout, write_timeout=config.write_timeout)
                store = Store(config.database)
                transport = LarkTransport(credentials)
                core = BridgeCore(store, adapter, allowed_users=config.users,
                                  allowed_chats=config.chats, projects=config.projects,
                                  management_chat_id=config.management_chat_id, admin_users=config.admin_users,
                                  bot_open_id=config.bot_open_id,
                                  create_group=transport.create_group if config.management_chat_id is not None else None)
                reader = HerdrAdapter(config.session, command_builder=session_command(config.executable),
                                      decoder=decode_protocol22, runner=runner,
                                      query_timeout=min(config.query_timeout, 2.0),
                                      write_timeout=config.write_timeout)
                runtime = BridgeRuntime(core, config.bot_open_id, transport.send, runner,
                                        output_reader=reader, send_output=transport.send_output_once)
                transport.is_stopping = lambda: runtime.stopping
                with shutdown_signals(runtime):
                    try:
                        runtime.start_output()
                        logging.getLogger(__name__).warning("HerdR contract=static-not-live; live validation pending")
                        transport.start(runtime)
                    finally:
                        runtime.close()
            finally:
                if not runner.stop():
                    logging.getLogger(__name__).error("CLI child could not be reaped; inspect the local service")
    except AlreadyRunning:
        logging.getLogger(__name__).error("This configuration is already running")
        return 3
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Do not print SDK exceptions, config contents, tokens, or command text.
        logging.getLogger(__name__).error("Bridge stopped; inspect configuration and local acceptance results")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
