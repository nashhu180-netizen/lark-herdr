from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from feishu_herdr_bridge.store import (
    BindingChanged, Creation, CreationRejected, SchemaMismatch, Store, WorkspaceOccupied,
)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "bridge.sqlite3")

    def bind(self, chat="chat-a", workspace="w-a", pane="p-a", revision=None, session="session"):
        return self.store.bind(
            chat_id=chat, session=session, workspace_id=workspace, pane_id=pane,
            agent_name="lead", user_id="user", now=100.0, expected_revision=revision,
        )

    def claim(self, message_id="message", snapshot=None):
        return self.store.claim(
            message_id=message_id, chat_id="chat-a", user_id="user",
            action="prompt", snapshot=snapshot, now=101.0,
        )

    def test_schema_contains_only_the_four_design_tables(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"bindings", "requests", "create_requests", "group_requests"})

    def test_binding_survives_reopening_and_revisions_increase(self):
        first = self.bind()
        self.assertEqual(Store(self.store.path).get_binding("chat-a"), first)
        second = self.bind(workspace="w-b", pane="p-b", revision=first.revision)
        self.assertEqual(second.revision, first.revision + 1)
        self.assertTrue(second.valid)

    def test_workspace_uniqueness_is_enforced_by_database(self):
        self.bind()
        with self.assertRaises(WorkspaceOccupied):
            self.bind(chat="chat-b")
        with closing(sqlite3.connect(self.store.path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("""INSERT INTO bindings VALUES (
                    'chat-b','session','w-a','p-other',NULL,1,1,100,'user')""")
        self.assertIsNone(self.store.get_binding("chat-b"))

    def test_failed_rebinding_leaves_existing_binding_unchanged(self):
        first = self.bind()
        other = self.bind(chat="chat-b", workspace="w-b", pane="p-b")
        with self.assertRaises(WorkspaceOccupied):
            self.bind(workspace="w-b", pane="p-b", revision=first.revision)
        self.assertEqual(self.store.get_binding("chat-a"), first)
        self.assertEqual(self.store.get_binding("chat-b"), other)

    def test_same_workspace_id_in_different_sessions_is_distinct(self):
        self.bind()
        other = self.bind(chat="chat-b", session="other-session")
        self.assertEqual(other.workspace_id, "w-a")
        self.assertEqual(self.store.owners("session"), {"w-a": "chat-a"})

    def test_stale_revision_cannot_overwrite_binding(self):
        old = self.bind()
        current = self.bind(workspace="w-b", pane="p-b", revision=old.revision)
        with self.assertRaises(BindingChanged):
            self.bind(workspace="w-c", pane="p-c", revision=old.revision)
        self.assertEqual(self.store.get_binding("chat-a"), current)

    def test_stale_invalidation_cannot_break_a_new_binding(self):
        old = self.bind()
        current = self.bind(workspace="w-b", pane="p-b", revision=old.revision)
        self.store.invalidate(old)
        self.assertEqual(self.store.get_binding("chat-a"), current)
        self.store.invalidate(current)
        self.assertFalse(self.store.get_binding("chat-a").valid)

    def test_claim_is_atomic_across_worker_connections(self):
        snapshot = self.bind()
        barrier = threading.Barrier(4)

        def worker(_):
            barrier.wait()
            return self.claim(snapshot=snapshot)[1]

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, range(4)))
        self.assertEqual(sum(results), 1)
        record = self.store.get_request("message")
        self.assertEqual(record.binding_revision, snapshot.revision)
        self.assertEqual((record.herdr_session, record.workspace_id, record.pane_id),
                         ("session", "w-a", "p-a"))

    def test_duplicate_claim_does_not_rewrite_original_record(self):
        old = self.bind()
        self.claim(snapshot=old)
        self.store.finish("message", "done", "submitted", 102.0)
        current = self.bind(workspace="w-b", pane="p-b", revision=old.revision)
        record, created = self.claim(snapshot=current)
        self.assertFalse(created)
        self.assertEqual(record.status, "done")
        self.assertEqual(record.binding_revision, old.revision)
        self.assertEqual(record.pane_id, "p-a")
        self.assertEqual(record.result_code, "submitted")

    def test_recovery_marks_processing_unknown_but_preserves_done(self):
        snapshot = self.bind()
        self.claim("unfinished", snapshot)
        self.claim("finished", snapshot)
        self.store.finish("finished", "done", "submitted", 102.0)
        self.store.recover_incomplete(200.0)
        self.assertEqual(self.store.get_request("unfinished").status, "unknown")
        self.assertEqual(self.store.get_request("unfinished").result_code, "interrupted")
        self.assertEqual(self.store.get_request("finished").status, "done")
        self.assertFalse(self.store.get_binding("chat-a").valid)

    def test_recovery_does_not_invalidate_newer_binding(self):
        old = self.bind()
        self.claim(snapshot=old)
        current = self.bind(workspace="w-b", pane="p-b", revision=old.revision)
        self.store.recover_incomplete(200.0)
        self.assertEqual(self.store.get_binding("chat-a"), current)
        self.assertEqual(self.store.get_request("message").status, "unknown")

    def test_completed_request_cannot_be_overwritten_by_late_finish(self):
        self.claim()
        self.store.finish("message", "done", "submitted", 102.0)
        self.store.finish("message", "failed", "late_error", 103.0)
        self.assertEqual(self.store.get_request("message").status, "done")
        with self.assertRaises(ValueError):
            self.store.finish("message", "processing", "bad", 103.0)


_FROZEN_V1 = Path(__file__).parent / "fixtures/schema_v1.sql"
_TABLES = ("bindings", "requests", "create_requests", "group_requests")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "legacy.sqlite3"
        # Fixture DDL is independent of the production v2 constants.
        self.v1 = _FROZEN_V1.read_text(encoding="utf-8")
        self.v0 = self.v1.split("CREATE TABLE group_requests (", 1)[0]
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo"):
            guard = patch(target, side_effect=AssertionError("Network is forbidden"))
            blocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(blocked.assert_not_called)

    def legacy(self, version=1, populated=True):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(self.v1 if version == 1 else self.v0)
            if not populated:
                return
            rowids = (-11, 0, 7, 900, 2**40)
            for i, state in enumerate(("pending", "processing", "done", "failed", "unknown")):
                kind = "codex" if i % 2 == 0 else "claude"
                request_id, chat_id = f"fixture-request-{i}", f"fixture-chat-{i}"
                db.execute("INSERT INTO bindings VALUES (?,?,?,?,?,?,?,?,?)",
                           (chat_id, "kpi-agg", f"fixture-w-{i}", f"fixture-p-{i}",
                            None if i == 0 else f"fixture-agent-{i}", 1, 1, 100.5, "fixture-user"))
                db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (request_id, chat_id, "fixture-user", "new", 1, "kpi-agg",
                            f"fixture-w-{i}", f"fixture-p-{i}", "done" if state == "pending" else state,
                            "fixture-result", 100.5, 101.25))
                values = (request_id, f"fixture-code-{i}", chat_id, "fixture-user", "fixture-project",
                          "fixture label  with 'quotes'", kind, "fixture-agent", 1, 9999999.125,
                          state, None if i == 0 else f"fixture-w-{i}", None, None,
                          b"\x00fixture\xff" if i == 3 else "", 100.5, 101.25)
                db.execute("INSERT INTO create_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
                db.execute("UPDATE create_requests SET rowid=? WHERE request_id=?", (rowids[i], request_id))
                if version == 1:
                    db.execute("INSERT INTO group_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (f"fixture-group-{i}", f"fixture-group-code-{i}", "fixture-management",
                                "fixture-user", "synthetic group", f"fixture-uuid-{i}", "kpi-agg",
                                "fixture-bot", f"fixture-managed-{i}" if i >= 1 else None,
                                state, "fixture-result", 9999999.125, 100.5, 101.25))
            # Legacy TEXT PRIMARY KEY permits NULL. Do not strengthen it during recreation.
            for i, rowid in enumerate((-100, 2**63 - 1)):
                db.execute("""INSERT INTO create_requests
                           (rowid, request_id, confirmation_code, chat_id, requested_by,
                            project_path, workspace_label, agent_kind, agent_name,
                            expires_at, status, created_at, updated_at)
                           VALUES (?,NULL,?,'fixture-null-chat','fixture-user','fixture-project',
                                   'fixture-label','claude','fixture-agent',500,'failed',100,101)""",
                           (rowid, f"fixture-null-code-{i}"))

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {table: [tuple((type(value).__name__, value) for value in row)
                            for row in db.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid")]
                    for table in _TABLES if table in names}

    def schema(self):
        with closing(sqlite3.connect(self.path)) as db:
            return (db.execute("PRAGMA user_version").fetchone()[0], db.execute(
                "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall())

    def assert_rejected_unchanged(self):
        before, schema = self.snapshot(), self.schema()
        with patch.object(Store, "recover_incomplete") as recovery:
            with self.assertRaises(SchemaMismatch):
                Store(self.path)
            recovery.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.schema(), schema)

    def test_old_data_pending_codes_and_version_survive_idempotent_upgrade(self):
        self.legacy()
        before = self.snapshot()
        with patch.object(Store, "recover_incomplete") as recovery:
            Store(self.path)
            recovery.assert_not_called()
        self.assertEqual(self.schema()[0], 2)  # Genuine behavior red against v1 code.
        self.assertEqual(self.snapshot(), before)
        schema = self.schema()
        Store(self.path)
        self.assertEqual(self.schema(), schema)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(Store(self.path).find_creation('fixture-code-0', 'fixture-chat-0', 'fixture-user').status, 'pending')

    def test_fresh_v0_v1_and_renamed_v2_all_reopen(self):
        for source in ("empty", "v0", "v1"):
            with self.subTest(source=source):
                self.path = self.root / f"{source}.sqlite3"
                if source != "empty":
                    self.legacy(version=0 if source == "v0" else 1)
                before = self.snapshot()
                Store(self.path)
                self.assertEqual(self.schema()[0], 2)
                for name, rows in before.items():
                    self.assertEqual(self.snapshot()[name], rows)
                if source == "v0":
                    self.assertEqual(self.snapshot()["group_requests"], [])
                with closing(sqlite3.connect(self.path)) as db:
                    ddl = db.execute("SELECT sql FROM sqlite_master WHERE name='create_requests'").fetchone()[0]
                    self.assertIn('CREATE TABLE create_requests' if source == 'empty' else
                                  'CREATE TABLE "create_requests"', ddl)
                schema = self.schema()
                Store(self.path)
                self.assertEqual(self.schema(), schema)

    def test_only_kind_check_changes_and_index_semantics_survive(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as db:
            ddl_before = db.execute("SELECT sql FROM sqlite_master WHERE name='create_requests'").fetchone()[0]
            columns_before = db.execute("PRAGMA table_xinfo(create_requests)").fetchall()
            untouched = db.execute("""SELECT name,rootpage,sql FROM sqlite_master
                                      WHERE tbl_name <> 'create_requests' ORDER BY name""").fetchall()
        Store(self.path)
        expected = {
            'bindings': {('pk', ('chat_id',)), ('u', ('herdr_session', 'workspace_id'))},
            'requests': {('pk', ('message_id',))},
            'create_requests': {('pk', ('request_id',)), ('u', ('confirmation_code',))},
            'group_requests': {('pk', ('request_id',)), ('u', ('confirmation_code',)),
                               ('u', ('create_uuid',)), ('u', ('created_chat_id',))},
        }
        with closing(sqlite3.connect(self.path)) as db:
            ddl_after = db.execute("SELECT sql FROM sqlite_master WHERE name='create_requests'").fetchone()[0]
            self.assertEqual(ddl_after.replace('"create_requests"', 'create_requests', 1)
                             .replace("('codex','claude','devin')", "('codex','claude')"), ddl_before)
            self.assertEqual(db.execute("PRAGMA table_xinfo(create_requests)").fetchall(), columns_before)
            self.assertEqual(db.execute("""SELECT name,rootpage,sql FROM sqlite_master
                                           WHERE tbl_name <> 'create_requests' ORDER BY name""").fetchall(), untouched)
            for table, keys in expected.items():
                seen = set()
                indexes = db.execute("SELECT * FROM pragma_index_list(?)", (table,)).fetchall()
                for _, name, unique, origin, partial in indexes:
                    self.assertEqual((unique, partial), (1, 0))
                    columns = [row for row in db.execute("SELECT * FROM pragma_index_xinfo(?) ORDER BY seqno", (name,)) if row[5]]
                    self.assertTrue(all(row[1] >= 0 and row[3] == 0 and row[4] == 'BINARY' for row in columns))
                    seen.add((origin, tuple(row[2] for row in columns)))
                self.assertEqual(seen, keys)
                self.assertEqual(len(indexes), len(keys))
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchall(), [('ok',)])

    def test_migrated_table_accepts_devin_but_no_fourth_kind(self):
        self.legacy()
        store = Store(self.path)
        creation = Creation('fixture-devin', 'fixture-devin-code', 'fixture-new-chat', 'fixture-user',
                            'fixture-project', 'fixture-label', 'devin', 'fb-devin-0123456789ab',
                            None, 500, 'pending', None, None, None, '', 100, 100)
        store.propose_creation(creation)
        self.assertEqual(store.find_creation(creation.confirmation_code, creation.chat_id, creation.requested_by), creation)
        with closing(sqlite3.connect(self.path)) as db, db:
            for kind in ('codex', 'claude', 'devin'):
                db.execute("UPDATE create_requests SET agent_kind=? WHERE request_id='fixture-devin'", (kind,))
            for kind in ('Devin', 'shell', 'other', None):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE create_requests SET agent_kind=? WHERE request_id='fixture-devin'", (kind,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE create_requests SET status='invalid' WHERE request_id='fixture-devin'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE create_requests SET confirmation_code=NULL WHERE request_id='fixture-devin'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE create_requests SET confirmation_code='fixture-code-0' WHERE request_id='fixture-devin'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE create_requests SET request_id='fixture-request-0' WHERE request_id='fixture-devin'")

    def test_unknown_version_fails_before_any_recovery_or_schema_write(self):
        self.legacy()
        for version in (-1, 3, 42):
            with self.subTest(version=version), closing(sqlite3.connect(self.path)) as db:
                db.execute(f'PRAGMA user_version={version}')
                self.assert_rejected_unchanged()

    def test_existing_unversioned_group_table_is_not_silently_adopted(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA user_version=0')
        self.assert_rejected_unchanged()

    def test_version_ddl_disagreement_and_missing_table_fail_closed(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA user_version=2')
        self.assert_rejected_unchanged()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA user_version=1')
        Store(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA user_version=1')
        self.assert_rejected_unchanged()
        self.path = self.root / 'empty-v2.sqlite3'
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA user_version=2')
        self.assert_rejected_unchanged()

    def test_version_one_rejects_missing_check_unique_or_table(self):
        variants = (
            ("agent_kind IN ('codex','claude')", "agent_kind IN ('codex','claude','devin')"),
            ("confirmation_code TEXT NOT NULL UNIQUE", "confirmation_code TEXT NOT NULL"),
            ("confirmation_code TEXT NOT NULL UNIQUE", "confirmation_code TEXT COLLATE NOCASE NOT NULL UNIQUE"),
            ("request_id TEXT PRIMARY KEY,", "request_id TEXT PRIMARY KEY DESC,"),
            ("request_id TEXT PRIMARY KEY,", "request_id TEXT PRIMARY KEY NOT NULL,"),
            ("original_revision INTEGER,", "original_revision TEXT,"),
            ("workspace_label TEXT NOT NULL,", "workspace_label TEXT NOT NULL, extra TEXT,"),
            ("    tab_id TEXT,\n", ""),
            ("result_code TEXT NOT NULL DEFAULT ''", "result_code TEXT NOT NULL DEFAULT '  '"),
            ("created_chat_id TEXT UNIQUE", "created_chat_id TEXT"),
            (",\n    CHECK (status <> 'done' OR (created_chat_id IS NOT NULL AND length(created_chat_id) > 0))", ""),
            ("'pending','processing','done','failed','unknown'", "'pending','processing','done','failed','unknown','other'"),
            ("request_id TEXT PRIMARY KEY,", "request_id TEXT PRIMARY KEY REFERENCES requests(message_id),"),
        )
        for i, (old, new) in enumerate(variants):
            with self.subTest(variant=i):
                self.path = self.root / f'bad-shape-{i}.sqlite3'
                self.assertIn(old, self.v1)
                with closing(sqlite3.connect(self.path)) as db:
                    db.executescript(self.v1.replace(old, new, 1))
                self.assert_rejected_unchanged()
        self.path = self.root / 'missing-table.sqlite3'
        self.legacy()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('DROP TABLE group_requests')
        self.assert_rejected_unchanged()

    def test_unexpected_indexes_triggers_views_and_staging_tables_are_rejected(self):
        statements = (
            'CREATE TABLE rogue (value TEXT)',
            'CREATE TABLE sqliteXrogue (value TEXT)',
            'CREATE TABLE create_requests_v2_migration (value TEXT)',
            'CREATE INDEX fixture_index ON create_requests(chat_id)',
            'CREATE UNIQUE INDEX fixture_unique ON create_requests(chat_id)',
            'CREATE INDEX fixture_partial ON create_requests(chat_id) WHERE status=\'pending\'',
            'CREATE INDEX fixture_expression ON create_requests(lower(chat_id))',
            'CREATE TRIGGER fixture_trigger AFTER INSERT ON create_requests BEGIN SELECT 1; END',
            'CREATE TRIGGER other_trigger AFTER UPDATE ON bindings BEGIN SELECT 1; END',
            'CREATE VIEW fixture_view AS SELECT * FROM create_requests',
        )
        for version in (0, 1, 2):
            for i, sql in enumerate(statements):
                with self.subTest(version=version, statement=i):
                    self.path = self.root / f'objects-{version}-{i}.sqlite3'
                    self.legacy(version=0 if version == 0 else 1, populated=False)
                    if version == 2:
                        Store(self.path)
                    with closing(sqlite3.connect(self.path)) as db:
                        db.execute(sql)
                    self.assert_rejected_unchanged()

    def test_sqlite_managed_statistics_do_not_count_as_application_tables(self):
        self.legacy()
        before = self.snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('ANALYZE')
        Store(self.path)
        self.assertEqual(self.snapshot(), before)
        Store(self.path)

    def test_invalid_v1_rows_are_not_accepted_by_widening_the_check(self):
        mutations = (
            "UPDATE create_requests SET agent_kind='devin' WHERE request_id='fixture-request-0'",
            "UPDATE create_requests SET status='illegal' WHERE request_id='fixture-request-0'",
            "UPDATE group_requests SET created_chat_id=NULL WHERE status='done'",
            "UPDATE group_requests SET herdr_session='other' WHERE status='done'",
        )
        for i, sql in enumerate(mutations):
            with self.subTest(mutation=i):
                self.path = self.root / f'invalid-data-{i}.sqlite3'
                self.legacy()
                # Only a disposable source fixture has checks disabled to seed corruption.
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('PRAGMA ignore_check_constraints=ON')
                    db.execute(sql)
                self.assert_rejected_unchanged()

    def test_v2_reopening_does_not_repeat_ddl_dml_or_version_writes(self):
        self.legacy()
        Store(self.path)
        before = self.snapshot()
        seen = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(seen.append)
            return db

        with patch('feishu_herdr_bridge.store.sqlite3.connect', side_effect=traced):
            Store(self.path)
        writes = [sql for sql in seen if sql.lstrip().upper().startswith(
            ('CREATE ', 'INSERT ', 'UPDATE ', 'DELETE ', 'DROP ', 'ALTER ', 'PRAGMA USER_VERSION='))]
        self.assertEqual(writes, [])
        self.assertEqual(self.snapshot(), before)

    def test_database_enforces_done_nonempty_id_unique_and_fixed_session(self):
        self.legacy(populated=False)
        Store(self.path)
        first = ['r1', 'g-' + '1' * 16, 'management', 'admin', 'demo', 'uuid-1',
                 'kpi-agg', 'bot', None, 'done', '', 400, 100, 100]
        with closing(sqlite3.connect(self.path)) as db, db:
            for chat_id in (None, ''):
                first[8] = chat_id
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('INSERT INTO group_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', first)
            first[8], first[9] = None, 'pending'
            db.execute('INSERT INTO group_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', first)
            for chat_id in (None, ''):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE group_requests SET status='done', created_chat_id=? WHERE request_id='r1'", (chat_id,))
            second = first.copy()
            second[0], second[1], second[5] = 'r2', 'g-' + '2' * 16, 'uuid-2'
            db.execute('INSERT INTO group_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', second)
            db.execute("UPDATE group_requests SET status='done', created_chat_id='unique-chat' WHERE request_id='r1'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE group_requests SET created_chat_id='unique-chat' WHERE request_id='r2'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE group_requests SET herdr_session='other-session' WHERE request_id='r2'")
            self.assertEqual(db.execute('SELECT count(*) FROM group_requests').fetchone()[0], 2)

    def test_recovery_is_separate_from_migration_and_preserves_group_authorization(self):
        self.legacy()
        before = self.snapshot()
        store = Store(self.path)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(store.group_allowed('fixture-managed-2', 'kpi-agg', 'fixture-bot'))
        store.recover_incomplete(200.0)
        for i, original in enumerate(('pending', 'processing', 'done', 'failed', 'unknown')):
            creation = store.find_creation(f'fixture-code-{i}', f'fixture-chat-{i}', 'fixture-user')
            group = store.find_group(f'fixture-group-code-{i}', 'fixture-management', 'fixture-user', 'kpi-agg', 'fixture-bot')
            self.assertEqual(creation.status, 'unknown' if original == 'processing' else original)
            self.assertEqual(group.status, 'unknown' if original == 'processing' else original)
            self.assertEqual(creation.updated_at, 200.0 if original == 'processing' else 101.25)
        self.assertTrue(store.group_allowed('fixture-managed-2', 'kpi-agg', 'fixture-bot'))
        self.assertFalse(store.group_allowed('fixture-managed-1', 'kpi-agg', 'fixture-bot'))
        after = self.snapshot()
        Store(self.path).recover_incomplete(300.0)
        self.assertEqual(self.snapshot(), after)

    def test_pending_codex_and_claude_codes_remain_eligible_without_expiry_extension(self):
        self.legacy(populated=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            for kind in ('codex', 'claude'):
                db.execute("""INSERT INTO requests VALUES (?,?,?,'new',NULL,'kpi-agg',NULL,NULL,'done','',100,100)""",
                           (f'req-{kind}', f'chat-{kind}', 'fixture-user'))
                db.execute("""INSERT INTO create_requests VALUES
                           (?,?,?,'fixture-user','fixture-project','fixture-label',?,'fixture-agent',NULL,
                            400,'pending',NULL,NULL,NULL,'',100,100)""",
                           (f'req-{kind}', f'code-{kind}', f'chat-{kind}', kind))
        store = Store(self.path)
        for kind in ('codex', 'claude'):
            creation = store.find_creation(f'code-{kind}', f'chat-{kind}', 'fixture-user')
            self.assertEqual(creation.expires_at, 400.0)
            self.assertEqual(store.begin_creation(creation, 'kpi-agg', 101).status, 'processing')
            with self.assertRaises(CreationRejected):
                store.begin_creation(creation, 'kpi-agg', 102)

    def test_migration_failure_rolls_back_ddl_and_version_for_old_and_new_databases(self):
        connect = sqlite3.connect
        for source in ('empty', 'v0', 'v1'):
            with self.subTest(source=source):
                self.path = self.root / f'rollback-{source}.sqlite3'
                if source != 'empty':
                    self.legacy(version=0 if source == 'v0' else 1)
                before, schema = self.snapshot(), self.schema()
                reached = []

                class FailingVersion(sqlite3.Connection):
                    def execute(self, sql, parameters=()):
                        result = super().execute(sql, parameters)
                        if sql == 'PRAGMA user_version=2':
                            reached.append(True)
                            raise sqlite3.OperationalError('Injected version-write failure')
                        return result

                def broken(*args, **kwargs):
                    return connect(*args, factory=FailingVersion, **kwargs)

                with patch('feishu_herdr_bridge.store.sqlite3.connect', side_effect=broken):
                    with self.assertRaises(sqlite3.OperationalError):
                        Store(self.path)
                self.assertEqual(reached, [True])
                self.assertEqual((self.snapshot(), self.schema()), (before, schema))
                Store(self.path)
                self.assertEqual(self.schema()[0], 2)

    def test_each_destructive_boundary_and_commit_acknowledgement(self):
        connect = sqlite3.connect
        phases = ('create', 'copy', 'drop', 'rename', 'final_check', 'version', 'before_commit', 'after_commit')
        for phase in phases:
            with self.subTest(phase=phase):
                self.path = self.root / f'phase-{phase}.sqlite3'
                self.legacy()
                before, schema = self.snapshot(), self.schema()
                fired = []

                class FailingBoundary(sqlite3.Connection):
                    checks = 0

                    def execute(self, sql, parameters=()):
                        result = super().execute(sql, parameters)
                        text = ' '.join(sql.split())
                        if text == 'PRAGMA integrity_check':
                            self.checks += 1
                        matches = {
                            'create': text.startswith('CREATE TABLE create_requests_v2_migration '),
                            'copy': text.startswith('INSERT INTO create_requests_v2_migration '),
                            'drop': text == 'DROP TABLE create_requests',
                            'rename': text == 'ALTER TABLE create_requests_v2_migration RENAME TO create_requests',
                            'final_check': text == 'PRAGMA integrity_check' and self.checks == 2,
                            'version': text == 'PRAGMA user_version=2',
                        }
                        if matches.get(phase, False):
                            fired.append(phase)
                            raise sqlite3.OperationalError('Injected boundary failure')
                        return result

                    def commit(self):
                        if phase == 'before_commit':
                            fired.append(phase)
                            raise sqlite3.OperationalError('Injected commit failure')
                        super().commit()
                        if phase == 'after_commit':
                            fired.append(phase)
                            raise sqlite3.OperationalError('Lost commit acknowledgement')

                def broken(*args, **kwargs):
                    return connect(*args, factory=FailingBoundary, **kwargs)

                with patch('feishu_herdr_bridge.store.sqlite3.connect', side_effect=broken):
                    with self.assertRaises(sqlite3.OperationalError):
                        Store(self.path)
                self.assertEqual(fired, [phase])
                self.assertEqual(self.snapshot(), before)
                if phase == 'after_commit':
                    self.assertEqual(self.schema()[0], 2)
                else:
                    self.assertEqual(self.schema(), schema)
                Store(self.path)
                self.assertEqual(self.schema()[0], 2)
                self.assertEqual(self.snapshot(), before)

    def test_copy_validation_detects_missing_row_changed_value_or_storage_class(self):
        connect = sqlite3.connect
        mutations = (
            'DELETE FROM create_requests_v2_migration WHERE rowid=-11',
            "UPDATE create_requests_v2_migration SET workspace_label='changed' WHERE rowid=-11",
            "UPDATE create_requests_v2_migration SET original_revision=CAST(original_revision AS BLOB) WHERE rowid=-11",
        )
        for i, mutation in enumerate(mutations):
            with self.subTest(mutation=i):
                self.path = self.root / f'copy-{i}.sqlite3'
                self.legacy()
                before, schema = self.snapshot(), self.schema()
                changed = []

                class CorruptCopy(sqlite3.Connection):
                    def execute(self, sql, parameters=()):
                        result = super().execute(sql, parameters)
                        if sql.startswith('INSERT INTO create_requests_v2_migration '):
                            super().execute(mutation)
                            changed.append(True)
                        return result

                def broken(*args, **kwargs):
                    return connect(*args, factory=CorruptCopy, **kwargs)

                with patch('feishu_herdr_bridge.store.sqlite3.connect', side_effect=broken):
                    with self.assertRaises(SchemaMismatch):
                        Store(self.path)
                self.assertEqual(changed, [True])
                self.assertEqual((self.snapshot(), self.schema()), (before, schema))

    def test_busy_database_fails_without_retry_or_changes(self):
        self.legacy()
        before, schema = self.snapshot(), self.schema()
        connect = sqlite3.connect

        def impatient(*args, **kwargs):
            kwargs['timeout'] = 0.01
            return connect(*args, **kwargs)

        with closing(connect(self.path, isolation_level=None)) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            with patch('feishu_herdr_bridge.store.sqlite3.connect', side_effect=impatient) as attempts:
                with self.assertRaises(sqlite3.OperationalError):
                    Store(self.path)
            self.assertEqual(attempts.call_count, 1)
            blocker.rollback()
        self.assertEqual((self.snapshot(), self.schema()), (before, schema))

    def test_process_exit_before_and_after_commit_survives_restart(self):
        script = r'''
import os, sqlite3, sys
from unittest.mock import patch
from feishu_herdr_bridge.store import Store

def no_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Network forbidden')
sys.addaudithook(no_network)
phase = sys.argv[2]
connect = sqlite3.connect
class ExitAtBoundary(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if (phase == 'rename' and sql == 'ALTER TABLE create_requests_v2_migration RENAME TO create_requests'
                or phase == 'version' and sql == 'PRAGMA user_version=2'):
            os._exit(73)
        return result
    def commit(self):
        super().commit()
        if phase == 'commit':
            os._exit(73)
def interrupted(*args, **kwargs):
    return connect(*args, factory=ExitAtBoundary, **kwargs)
with patch('feishu_herdr_bridge.store.sqlite3.connect', side_effect=interrupted):
    Store(sys.argv[1])
'''
        for journal in ('delete', 'wal'):
            for phase in ('rename', 'version', 'commit'):
                with self.subTest(journal=journal, phase=phase):
                    self.path = self.root / f'crash-{journal}-{phase}.sqlite3'
                    self.legacy()
                    with closing(sqlite3.connect(self.path)) as db:
                        self.assertEqual(db.execute(f'PRAGMA journal_mode={journal}').fetchone()[0], journal)
                    before, schema = self.snapshot(), self.schema()
                    env = {key: value for key, value in os.environ.items()
                           if key not in {'FEISHU_APP_ID', 'FEISHU_APP_SECRET'}}
                    child = subprocess.run([sys.executable, '-c', script, str(self.path), phase],
                                           cwd=Path(__file__).resolve().parents[1], env=env,
                                           capture_output=True, text=True, timeout=10)
                    self.assertEqual(child.returncode, 73, child.stderr)
                    self.assertEqual(self.snapshot(), before)
                    if phase == 'commit':
                        self.assertEqual(self.schema()[0], 2)
                    else:
                        self.assertEqual(self.schema(), schema)
                    Store(self.path)
                    self.assertEqual(self.schema()[0], 2)
                    self.assertEqual(self.snapshot(), before)
