"""Explicit Batch 2 entry point; HerdR live acceptance remains pending.

No CLI, SDK connection, or credential lookup is performed on import or --help.
Service installation, example files, and process-lock deployment are Batch 3.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .core import BridgeCore
from .feishu import Credentials, FeishuBridge, LarkTransport
from .herdr import HerdrAdapter, decode_protocol22, safe_identifier, session_command
from .store import Store


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


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    required = {"herdr_executable", "herdr_session", "bot_open_id", "allowed_users", "allowed_chats", "projects"}
    optional = {"database", "query_timeout", "write_timeout"}
    if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - optional:
        raise ValueError("Invalid configuration keys; credentials must be supplied through the environment")
    executable, session = raw["herdr_executable"], raw["herdr_session"]
    session_command(executable)  # Validation only, never calls the executable.
    if not safe_identifier(session) or not safe_identifier(raw["bot_open_id"]):
        raise ValueError("Explicit session and bot_open_id are required")
    for key in ("allowed_users", "allowed_chats"):
        if not isinstance(raw[key], list) or not raw[key] or not all(safe_identifier(v) for v in raw[key]):
            raise ValueError("Nonempty user and chat allowlists are required")
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
                  frozenset(raw["allowed_chats"]), projects, Path(database), *timeouts)


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
        adapter = HerdrAdapter(config.session, command_builder=session_command(config.executable),
                               decoder=decode_protocol22, query_timeout=config.query_timeout,
                               write_timeout=config.write_timeout)
        core = BridgeCore(Store(config.database), adapter, allowed_users=config.users,
                          allowed_chats=config.chats, projects=config.projects)
        transport = LarkTransport(credentials)
        bridge = FeishuBridge(core, config.bot_open_id, transport.send)
        logging.getLogger(__name__).warning("HerdR contract=static-not-live; live validation pending")
        transport.start(bridge)
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Do not print SDK exceptions, config contents, tokens, or command text.
        logging.getLogger(__name__).error("Bridge stopped; inspect configuration and local acceptance results")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
