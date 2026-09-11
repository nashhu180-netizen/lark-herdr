from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from feishu_herdr_bridge.store import (
    BindingChanged, SchemaMismatch, Store, WorkspaceOccupied, _GROUP_SCHEMA, _SCHEMA,
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


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "legacy.sqlite3"

    def legacy(self):
        # _SCHEMA is the unchanged three-table DDL from ea6b22a.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(_SCHEMA)
            db.execute("INSERT INTO bindings VALUES ('chat','kpi-agg','w','p','lead',1,1,100,'user')")
            db.execute("""INSERT INTO requests VALUES
                       ('m','chat','user','new',1,'kpi-agg','w','p','processing','',100,100)""")
            db.execute("""INSERT INTO create_requests VALUES
                       ('m','12345678','chat','user','/project','label','codex','lead',1,
                        9999999999,'pending',NULL,NULL,NULL,'',100,100)""")

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {table: db.execute(f"SELECT * FROM {table}").fetchall()
                    for table in ("bindings", "requests", "create_requests")}

    def test_old_data_pending_codes_and_version_survive_idempotent_upgrade(self):
        self.legacy()
        before = self.snapshot()
        Store(self.path)
        Store(self.path)
        self.assertEqual(self.snapshot(), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM group_requests').fetchone()[0], 0)
        self.assertEqual(Store(self.path).find_creation('12345678', 'chat', 'user').status, 'pending')

    def test_unknown_version_fails_before_any_recovery_or_schema_write(self):
        self.legacy()
        before = self.snapshot()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA user_version=42')
        with self.assertRaises(SchemaMismatch):
            Store(self.path)
        self.assertEqual(self.snapshot(), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 42)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='group_requests'").fetchone())

    def test_migration_failure_rolls_back_ddl_and_version_for_old_and_new_databases(self):
        connect = sqlite3.connect

        class FailingVersion(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == 'PRAGMA user_version=1':
                    raise sqlite3.OperationalError('Injected version-write failure')
                return super().execute(sql, parameters)

        def broken_connection(*args, **kwargs):
            return connect(*args, factory=FailingVersion, **kwargs)

        for old in (False, True):
            with self.subTest(old=old):
                if old:
                    self.legacy()
                with patch('feishu_herdr_bridge.store.sqlite3.connect', side_effect=broken_connection):
                    with self.assertRaises(sqlite3.OperationalError):
                        Store(self.path)
                with closing(connect(self.path)) as db:
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
                    self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='group_requests'").fetchone())
                    count = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                    self.assertEqual(count, 3 if old else 0)
        self.assertEqual(Store(self.path).get_request('m').status, 'processing')

    def test_existing_unversioned_group_table_is_not_silently_adopted(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(_GROUP_SCHEMA)
        with self.assertRaises(SchemaMismatch):
            Store(self.path)

    def test_version_one_rejects_missing_check_unique_or_table(self):
        Store(self.path)
        check = ",\n    CHECK (status <> 'done' OR (created_chat_id IS NOT NULL AND length(created_chat_id) > 0))"
        variants = (_GROUP_SCHEMA.replace(check, ''),
                    _GROUP_SCHEMA.replace('created_chat_id TEXT UNIQUE', 'created_chat_id TEXT'), None)
        for ddl in variants:
            with self.subTest(ddl=ddl is not None), closing(sqlite3.connect(self.path)) as db:
                db.execute('DROP TABLE IF EXISTS group_requests')
                if ddl:
                    db.execute(ddl)
                with self.assertRaises(SchemaMismatch):
                    Store(self.path)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_database_enforces_done_nonempty_id_unique_and_fixed_session(self):
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
