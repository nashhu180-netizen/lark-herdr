from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from feishu_herdr_bridge.store import BindingChanged, Store, WorkspaceOccupied


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

    def test_schema_contains_only_the_three_design_tables(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"bindings", "requests", "create_requests"})

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
