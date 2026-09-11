"""Small file-backed SQLite store. Each operation owns its connection."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class BindingChanged(Exception):
    """The caller's binding snapshot is no longer current."""


class WorkspaceOccupied(Exception):
    """Another chat already owns this workspace in the configured session."""


@dataclass(frozen=True)
class Binding:
    chat_id: str
    herdr_session: str
    workspace_id: str
    pane_id: str
    agent_name: str | None
    valid: bool
    revision: int
    bound_at: float
    bound_by: str


@dataclass(frozen=True)
class Request:
    message_id: str
    chat_id: str
    user_id: str
    action: str
    binding_revision: int | None
    herdr_session: str | None
    workspace_id: str | None
    pane_id: str | None
    status: str
    result_code: str
    created_at: float
    updated_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    chat_id TEXT PRIMARY KEY,
    herdr_session TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    pane_id TEXT NOT NULL,
    agent_name TEXT,
    valid INTEGER NOT NULL CHECK (valid IN (0, 1)),
    revision INTEGER NOT NULL CHECK (revision > 0),
    bound_at REAL NOT NULL,
    bound_by TEXT NOT NULL,
    UNIQUE (herdr_session, workspace_id)
);
CREATE TABLE IF NOT EXISTS requests (
    message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    binding_revision INTEGER,
    herdr_session TEXT,
    workspace_id TEXT,
    pane_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('processing','done','failed','unknown')),
    result_code TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS create_requests (
    request_id TEXT PRIMARY KEY,
    confirmation_code TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    project_path TEXT NOT NULL,
    workspace_label TEXT NOT NULL,
    agent_kind TEXT NOT NULL CHECK (agent_kind IN ('codex','claude')),
    agent_name TEXT NOT NULL,
    original_revision INTEGER,
    expires_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending','processing','done','failed','unknown')
    ),
    workspace_id TEXT,
    tab_id TEXT,
    pane_id TEXT,
    result_code TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("Use a file-backed database, including in tests")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=1.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _binding(row: sqlite3.Row | None) -> Binding | None:
        if row is None:
            return None
        fields = dict(row)
        fields["valid"] = bool(fields["valid"])
        return Binding(**fields)

    def get_binding(self, chat_id: str) -> Binding | None:
        with self._connection() as db:
            return self._binding(db.execute(
                "SELECT * FROM bindings WHERE chat_id=?", (chat_id,)
            ).fetchone())

    def bind(
        self, *, chat_id: str, session: str, workspace_id: str, pane_id: str,
        agent_name: str | None, user_id: str, now: float,
        expected_revision: int | None,
    ) -> Binding:
        with self._transaction() as db:
            old = db.execute(
                "SELECT revision FROM bindings WHERE chat_id=?", (chat_id,)
            ).fetchone()
            revision = old["revision"] if old else None
            if revision != expected_revision:
                raise BindingChanged()
            owner = db.execute(
                "SELECT chat_id FROM bindings WHERE herdr_session=? AND workspace_id=?",
                (session, workspace_id),
            ).fetchone()
            if owner and owner["chat_id"] != chat_id:
                raise WorkspaceOccupied()
            db.execute(
                """INSERT INTO bindings VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     herdr_session=excluded.herdr_session,
                     workspace_id=excluded.workspace_id,
                     pane_id=excluded.pane_id,
                     agent_name=excluded.agent_name, valid=1,
                     revision=excluded.revision, bound_at=excluded.bound_at,
                     bound_by=excluded.bound_by""",
                (chat_id, session, workspace_id, pane_id, agent_name,
                 (revision or 0) + 1, now, user_id),
            )
            row = db.execute(
                "SELECT * FROM bindings WHERE chat_id=?", (chat_id,)
            ).fetchone()
            binding = self._binding(row)
            assert binding is not None
            return binding

    def invalidate(self, snapshot: Binding) -> None:
        # A failed old request must never invalidate a newer binding.
        with self._connection() as db:
            db.execute(
                """UPDATE bindings SET valid=0 WHERE chat_id=? AND revision=?
                   AND herdr_session=? AND workspace_id=? AND pane_id=?""",
                (snapshot.chat_id, snapshot.revision, snapshot.herdr_session,
                 snapshot.workspace_id, snapshot.pane_id),
            )

    def owners(self, session: str) -> dict[str, str]:
        with self._connection() as db:
            return dict(db.execute(
                "SELECT workspace_id, chat_id FROM bindings WHERE herdr_session=?",
                (session,),
            ).fetchall())

    def claim(
        self, *, message_id: str, chat_id: str, user_id: str, action: str,
        snapshot: Binding | None, now: float,
    ) -> tuple[Request, bool]:
        with self._transaction() as db:
            cursor = db.execute(
                """INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                   'processing', '', ?, ?) ON CONFLICT(message_id) DO NOTHING""",
                (message_id, chat_id, user_id, action,
                 snapshot.revision if snapshot else None,
                 snapshot.herdr_session if snapshot else None,
                 snapshot.workspace_id if snapshot else None,
                 snapshot.pane_id if snapshot else None, now, now),
            )
            row = db.execute(
                "SELECT * FROM requests WHERE message_id=?", (message_id,)
            ).fetchone()
            return Request(**dict(row)), cursor.rowcount == 1

    def finish(self, message_id: str, status: str, result_code: str, now: float) -> None:
        if status not in {"done", "failed", "unknown"}:
            raise ValueError("Invalid final request status")
        with self._connection() as db:
            db.execute(
                """UPDATE requests SET status=?, result_code=?, updated_at=?
                   WHERE message_id=? AND status='processing'""",
                (status, result_code, now, message_id),
            )

    def get_request(self, message_id: str) -> Request | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM requests WHERE message_id=?", (message_id,)
            ).fetchone()
            return Request(**dict(row)) if row else None

    def recover_incomplete(self, now: float) -> None:
        """Call once at bridge startup, never for each worker connection."""
        with self._transaction() as db:
            db.execute(
                """UPDATE bindings SET valid=0 WHERE EXISTS (
                     SELECT 1 FROM requests r WHERE r.status='processing'
                     AND r.action IN ('prompt','read','binding')
                     AND r.chat_id=bindings.chat_id
                     AND r.binding_revision=bindings.revision
                     AND r.herdr_session=bindings.herdr_session
                     AND r.workspace_id=bindings.workspace_id
                     AND r.pane_id=bindings.pane_id)"""
            )
            db.execute(
                """UPDATE requests SET status='unknown',
                   result_code='interrupted', updated_at=? WHERE status='processing'""",
                (now,),
            )
            db.execute(
                """UPDATE create_requests SET status='unknown',
                   result_code='interrupted', updated_at=? WHERE status='processing'""",
                (now,),
            )
