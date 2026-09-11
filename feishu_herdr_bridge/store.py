"""Small file-backed SQLite store. Each operation owns its connection."""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class SchemaMismatch(RuntimeError):
    """Unknown versions or conflicting schemas must not be repaired implicitly."""


@dataclass(frozen=True, repr=False)
class GroupRequest:
    request_id: str
    confirmation_code: str
    source_chat_id: str
    requested_by: str
    group_name: str
    create_uuid: str
    herdr_session: str
    bot_open_id: str
    created_chat_id: str | None
    status: str
    result_code: str
    expires_at: float
    created_at: float
    updated_at: float

    @property
    def reference(self) -> str:
        return f"G-{self.create_uuid}"


class BindingChanged(Exception):
    """The caller's binding snapshot is no longer current."""


class CreationRejected(Exception):
    """A confirmation is invalid, expired, already used, or no longer current."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Creation:
    request_id: str
    confirmation_code: str
    chat_id: str
    requested_by: str
    project_path: str
    workspace_label: str
    agent_kind: str
    agent_name: str
    original_revision: int | None
    expires_at: float
    status: str
    workspace_id: str | None
    tab_id: str | None
    pane_id: str | None
    result_code: str
    created_at: float
    updated_at: float


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


_GROUP_SCHEMA = """
CREATE TABLE group_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    confirmation_code TEXT NOT NULL UNIQUE,
    source_chat_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    group_name TEXT NOT NULL,
    create_uuid TEXT NOT NULL UNIQUE,
    herdr_session TEXT NOT NULL CHECK (herdr_session = 'kpi-agg'),
    bot_open_id TEXT NOT NULL,
    created_chat_id TEXT UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending','processing','done','failed','unknown')),
    result_code TEXT NOT NULL DEFAULT '',
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK (status <> 'done' OR (created_chat_id IS NOT NULL AND length(created_chat_id) > 0))
);
"""


# Keep _SCHEMA and _GROUP_SCHEMA frozen as the accepted v0/v1 definitions.
_CREATE_REQUESTS_V2 = """
CREATE TABLE create_requests (
    request_id TEXT PRIMARY KEY,
    confirmation_code TEXT NOT NULL UNIQUE,
    chat_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    project_path TEXT NOT NULL,
    workspace_label TEXT NOT NULL,
    agent_kind TEXT NOT NULL CHECK (agent_kind IN ('codex','claude','devin')),
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
_COPY_COLUMNS = (
    "rowid", "request_id", "confirmation_code", "chat_id", "requested_by", "project_path",
    "workspace_label", "agent_kind", "agent_name", "original_revision", "expires_at",
    "status", "workspace_id", "tab_id", "pane_id", "result_code", "created_at", "updated_at",
)
_INDEX_KEYS = {
    "bindings": {("pk", ("chat_id",)), ("u", ("herdr_session", "workspace_id"))},
    "requests": {("pk", ("message_id",))},
    "create_requests": {("pk", ("request_id",)), ("u", ("confirmation_code",))},
    "group_requests": {("pk", ("request_id",)), ("u", ("confirmation_code",)),
                       ("u", ("create_uuid",)), ("u", ("created_chat_id",))},
}


def _normalized_ddl(sql: str) -> str:
    # Whitespace outside literals is insignificant; literal contents are not.
    if not isinstance(sql, str):
        raise SchemaMismatch("Missing table definition")
    pieces = re.split(r"('(?:[^']|'')*')", sql.strip().removesuffix(";"))
    normalized = "".join(piece if i % 2 else re.sub(r"\s+", " ", piece)
                         for i, piece in enumerate(pieces))
    prefix = "CREATE TABLE IF NOT EXISTS "
    return "CREATE TABLE " + normalized[len(prefix):] if normalized.startswith(prefix) else normalized


def _schema_definitions(sql: str) -> dict[str, str]:
    # This splits only the fixed, trusted DDL constants, never database SQL.
    statements = (_normalized_ddl(part) for part in sql.split(";") if part.strip())
    return {part.split()[2]: part for part in statements}


def _expected_tables(version: int) -> dict[str, str]:
    tables = _schema_definitions(_SCHEMA + (_GROUP_SCHEMA if version >= 1 else ""))
    if version == 2:
        tables.update(_schema_definitions(_CREATE_REQUESTS_V2))
    return tables


def _validate_schema(db: sqlite3.Connection, version: int) -> dict[str, str]:
    objects = db.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    tables = {row["name"]: row["sql"] for row in objects
              if row["type"] == "table" and not row["name"].startswith("sqlite_")}
    expected = {} if version == 0 and not tables else _expected_tables(version)
    if tables.keys() != expected.keys():
        raise SchemaMismatch("Conflicting database tables")
    for name, sql in tables.items():
        accepted = {expected[name]}
        if version == 2 and name == "create_requests":
            # ALTER TABLE quotes the destination table identifier. No other
            # identifier/literal/constraint normalization is permitted.
            accepted.add(expected[name].replace("CREATE TABLE create_requests ",
                                               'CREATE TABLE "create_requests" ', 1))
        if _normalized_ddl(sql) not in accepted:
            raise SchemaMismatch("Conflicting table definition")
    catalog_indexes = set()
    for row in objects:
        if row["type"] in {"view", "trigger"}:
            raise SchemaMismatch("Unexpected view or trigger")
        if row["type"] == "index":
            if row["sql"] is not None or row["tbl_name"] not in expected:
                raise SchemaMismatch("Unexpected explicit index")
            catalog_indexes.add(row["name"])
    seen_indexes = set()
    for name in expected:
        if db.execute("SELECT 1 FROM pragma_foreign_key_list(?)", (name,)).fetchone():
            raise SchemaMismatch("Unexpected foreign key")
        indexes = db.execute("SELECT * FROM pragma_index_list(?)", (name,)).fetchall()
        keys = set()
        for index in indexes:
            if index["unique"] != 1 or index["partial"] != 0 or index["origin"] not in {"pk", "u"}:
                raise SchemaMismatch("Conflicting index properties")
            columns = [row for row in db.execute(
                "SELECT * FROM pragma_index_xinfo(?) ORDER BY seqno", (index["name"],)
            ) if row["key"] == 1]
            if not columns or any(row["cid"] < 0 or row["desc"] != 0 or row["coll"] != "BINARY"
                                  for row in columns):
                raise SchemaMismatch("Conflicting index columns")
            keys.add((index["origin"], tuple(row["name"] for row in columns)))
            seen_indexes.add(index["name"])
        if keys != _INDEX_KEYS[name] or len(indexes) != len(_INDEX_KEYS[name]):
            raise SchemaMismatch("Missing or extra constraint index")
    if seen_indexes != catalog_indexes:
        raise SchemaMismatch("Conflicting index inventory")
    return expected


def _check_integrity(db: sqlite3.Connection) -> None:
    rows = db.execute("PRAGMA integrity_check").fetchall()
    if len(rows) != 1 or rows[0][0] != "ok":
        # Do not disclose row contents or silently accept invalid old kinds.
        raise SchemaMismatch("Database integrity check failed")


def _migrate_create_requests(db: sqlite3.Connection) -> None:
    staging = "create_requests_v2_migration"
    db.execute(_CREATE_REQUESTS_V2.replace("CREATE TABLE create_requests ",
                                         f"CREATE TABLE {staging} ", 1))
    columns = ", ".join(_COPY_COLUMNS)
    db.execute(f"INSERT INTO {staging} ({columns}) SELECT {columns} FROM create_requests")
    sizes = db.execute(f"SELECT (SELECT count(*) FROM create_requests), "
                       f"(SELECT count(*) FROM {staging})").fetchone()
    if sizes[0] != sizes[1]:
        raise SchemaMismatch("Migration row count mismatch")
    # Include storage classes so SQL's numeric equality cannot hide conversion.
    values = ", ".join(f"{column}, typeof({column})" for column in _COPY_COLUMNS)
    for source, target in (("create_requests", staging), (staging, "create_requests")):
        if db.execute(f"SELECT {values} FROM {source} EXCEPT "
                      f"SELECT {values} FROM {target} LIMIT 1").fetchone() is not None:
            raise SchemaMismatch("Migration row value mismatch")
    db.execute("DROP TABLE create_requests")
    db.execute(f"ALTER TABLE {staging} RENAME TO create_requests")


class Store:
    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("Use a file-backed database, including in tests")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise SchemaMismatch("Unsupported database version")
            tables = _validate_schema(db, version)
            _check_integrity(db)
            if not tables:
                for definition in _expected_tables(2).values():
                    db.execute(definition)
            elif version < 2:
                if version == 0:
                    # Historical v0 -> v1 -> v2 shares this one outer transaction.
                    db.execute(_GROUP_SCHEMA)
                _migrate_create_requests(db)
            _validate_schema(db, 2)
            _check_integrity(db)
            if version != 2:
                # This is the last statement before the transaction commits.
                db.execute("PRAGMA user_version=2")

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
            return self._bind_in_transaction(
                db, chat_id=chat_id, session=session, workspace_id=workspace_id,
                pane_id=pane_id, agent_name=agent_name, user_id=user_id, now=now,
                expected_revision=expected_revision,
            )

    def _bind_in_transaction(
        self, db: sqlite3.Connection, *, chat_id: str, session: str,
        workspace_id: str, pane_id: str, agent_name: str | None, user_id: str,
        now: float, expected_revision: int | None,
    ) -> Binding:
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
        snapshot: Binding | None, now: float, session: str | None = None,
    ) -> tuple[Request, bool]:
        with self._transaction() as db:
            cursor = db.execute(
                """INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                   'processing', '', ?, ?) ON CONFLICT(message_id) DO NOTHING""",
                (message_id, chat_id, user_id, action,
                 snapshot.revision if snapshot else None,
                 snapshot.herdr_session if snapshot else session,
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
            db.execute(
                """UPDATE group_requests SET status='unknown',
                   result_code='interrupted', updated_at=? WHERE status='processing'""", (now,),
            )

    def propose_creation(self, creation: Creation) -> None:
        with self._transaction() as db:
            db.execute(
                """UPDATE create_requests SET status='failed', result_code='superseded',
                   updated_at=? WHERE chat_id=? AND status='pending'""",
                (creation.created_at, creation.chat_id),
            )
            db.execute(
                """INSERT INTO create_requests VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(getattr(creation, field) for field in Creation.__dataclass_fields__),
            )

    def find_creation(self, code: str, chat_id: str, user_id: str) -> Creation | None:
        with self._connection() as db:
            row = db.execute(
                """SELECT * FROM create_requests WHERE confirmation_code=?
                   AND chat_id=? AND requested_by=?""", (code, chat_id, user_id),
            ).fetchone()
            return Creation(**dict(row)) if row else None

    def begin_creation(self, creation: Creation, session: str, now: float) -> Creation:
        failure = None
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM create_requests WHERE request_id=?", (creation.request_id,),
            ).fetchone()
            if row is None or row["status"] != "pending":
                raise CreationRejected("confirmation_used")
            binding = db.execute(
                "SELECT revision FROM bindings WHERE chat_id=?", (creation.chat_id,),
            ).fetchone()
            origin = db.execute(
                "SELECT herdr_session FROM requests WHERE message_id=?", (creation.request_id,),
            ).fetchone()
            if now >= row["expires_at"]:
                failure = "confirmation_expired"
            elif (binding["revision"] if binding else None) != row["original_revision"]:
                failure = "binding_changed"
            elif origin is None or origin["herdr_session"] != session:
                failure = "session_changed"
            db.execute(
                """UPDATE create_requests SET status=?, result_code=?, updated_at=?
                   WHERE request_id=? AND status='pending'""",
                ("failed" if failure else "processing", failure or "", now, creation.request_id),
            )
            updated = db.execute(
                "SELECT * FROM create_requests WHERE request_id=?", (creation.request_id,),
            ).fetchone()
        if failure:
            raise CreationRejected(failure)
        return Creation(**dict(updated))

    def cancel_creation(self, chat_id: str, now: float) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                """UPDATE create_requests SET status='failed', result_code='cancelled',
                   updated_at=? WHERE chat_id=? AND status='pending'""", (now, chat_id),
            )
            return cursor.rowcount > 0

    def rename_creation(self, request_id: str, name: str, now: float) -> None:
        with self._connection() as db:
            cursor = db.execute(
                """UPDATE create_requests SET agent_name=?, updated_at=?
                   WHERE request_id=? AND status='processing'""", (name, now, request_id),
            )
            if cursor.rowcount != 1:
                raise CreationRejected("confirmation_used")

    def save_created_workspace(
        self, request_id: str, workspace_id: str, tab_id: str, pane_id: str, now: float,
    ) -> None:
        with self._connection() as db:
            cursor = db.execute(
                """UPDATE create_requests SET workspace_id=?, tab_id=?, pane_id=?, updated_at=?
                   WHERE request_id=? AND status='processing'""",
                (workspace_id, tab_id, pane_id, now, request_id),
            )
            if cursor.rowcount != 1:
                raise CreationRejected("confirmation_used")

    def finish_creation(self, request_id: str, status: str, code: str, now: float) -> None:
        if status not in {"failed", "unknown"}:
            raise ValueError("Use complete_creation for success")
        with self._connection() as db:
            db.execute(
                """UPDATE create_requests SET status=?, result_code=?, updated_at=?
                   WHERE request_id=? AND status='processing'""", (status, code, now, request_id),
            )

    def complete_creation(self, request_id: str, session: str, now: float) -> Binding:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM create_requests WHERE request_id=?", (request_id,),
            ).fetchone()
            if row is None or row["status"] != "processing" or not row["workspace_id"] or not row["pane_id"]:
                raise CreationRejected("confirmation_used")
            binding = self._bind_in_transaction(
                db, chat_id=row["chat_id"], session=session, workspace_id=row["workspace_id"],
                pane_id=row["pane_id"], agent_name=row["agent_name"], user_id=row["requested_by"],
                now=now, expected_revision=row["original_revision"],
            )
            db.execute(
                """UPDATE create_requests SET status='done', result_code='created',
                   updated_at=? WHERE request_id=?""", (now, request_id),
            )
            return binding


    def propose_group(self, group: GroupRequest) -> None:
        if group.status != "pending" or group.created_chat_id is not None:
            raise ValueError("Only pending group proposals may be inserted")
        with self._transaction() as db:
            db.execute(
                """UPDATE group_requests SET status='failed', result_code='superseded', updated_at=?
                   WHERE source_chat_id=? AND requested_by=? AND status='pending'""",
                (group.created_at, group.source_chat_id, group.requested_by),
            )
            db.execute(
                "INSERT INTO group_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(getattr(group, field) for field in GroupRequest.__dataclass_fields__),
            )

    def find_group(self, code: str, chat_id: str, user_id: str, session: str, bot_id: str) -> GroupRequest | None:
        with self._connection() as db:
            row = db.execute(
                """SELECT * FROM group_requests WHERE confirmation_code=? AND source_chat_id=?
                   AND requested_by=? AND herdr_session=? AND bot_open_id=?""",
                (code, chat_id, user_id, session, bot_id),
            ).fetchone()
            return GroupRequest(**dict(row)) if row else None

    def group_allowed(self, chat_id: str, session: str, bot_id: str) -> bool:
        with self._connection() as db:
            return db.execute(
                """SELECT 1 FROM group_requests WHERE created_chat_id=? AND status='done'
                   AND herdr_session='kpi-agg' AND herdr_session=? AND bot_open_id=?""",
                (chat_id, session, bot_id),
            ).fetchone() is not None

    def begin_group(self, group: GroupRequest, now: float) -> GroupRequest:
        with self._transaction() as db:
            expired = now >= group.expires_at
            cursor = db.execute(
                """UPDATE group_requests SET status=?, result_code=?, updated_at=?
                   WHERE request_id=? AND confirmation_code=? AND source_chat_id=?
                   AND requested_by=? AND herdr_session=? AND bot_open_id=?
                   AND expires_at=? AND status='pending'""",
                ("failed" if expired else "processing", "expired" if expired else "", now,
                 group.request_id, group.confirmation_code, group.source_chat_id, group.requested_by,
                 group.herdr_session, group.bot_open_id, group.expires_at),
            )
            if cursor.rowcount != 1:
                raise CreationRejected("confirmation_used")
            row = db.execute("SELECT * FROM group_requests WHERE request_id=?", (group.request_id,)).fetchone()
            return GroupRequest(**dict(row))

    def save_group_resource(self, request_id: str, chat_id: str, now: float) -> None:
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("A known group ID is required")
        with self._connection() as db:
            cursor = db.execute(
                """UPDATE group_requests SET created_chat_id=?, updated_at=?
                   WHERE request_id=? AND status='processing'
                   AND (created_chat_id IS NULL OR created_chat_id=?)""", (chat_id, now, request_id, chat_id),
            )
            if cursor.rowcount != 1:
                raise CreationRejected("confirmation_used")

    def complete_group(self, request_id: str, now: float) -> GroupRequest:
        with self._transaction() as db:
            cursor = db.execute(
                """UPDATE group_requests SET status='done', result_code='created', updated_at=?
                   WHERE request_id=? AND status='processing'""", (now, request_id),
            )
            if cursor.rowcount != 1:
                raise CreationRejected("confirmation_used")
            row = db.execute("SELECT * FROM group_requests WHERE request_id=?", (request_id,)).fetchone()
            return GroupRequest(**dict(row))

    def finish_group(self, request_id: str, status: str, code: str, now: float) -> None:
        if status not in {"failed", "unknown"}:
            raise ValueError("Use complete_group for success")
        with self._connection() as db:
            # Consumption can fail before commit. Atomically retire its pending
            # code on uncertainty too; never overwrite an existing terminal result.
            db.execute(
                """UPDATE group_requests SET status=?, result_code=?, updated_at=?
                   WHERE request_id=? AND
                   (status='processing' OR (status='pending' AND ?='unknown'))""",
                (status, code, now, request_id, status),
            )

    def cancel_group(self, chat_id: str, user_id: str, session: str, bot_id: str, now: float) -> bool:
        with self._connection() as db:
            cursor = db.execute(
                """UPDATE group_requests SET status='failed', result_code='cancelled', updated_at=?
                   WHERE source_chat_id=? AND requested_by=? AND herdr_session=? AND bot_open_id=?
                   AND status='pending'""", (now, chat_id, user_id, session, bot_id),
            )
            return cursor.rowcount > 0
