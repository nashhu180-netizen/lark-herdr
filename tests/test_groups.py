"""G1 offline behavior tests; no SDK or event-transport integration."""

from __future__ import annotations

import re
import sqlite3
import tempfile
import threading
import unittest
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

from feishu_herdr_bridge.core import (
    BridgeCore, GroupCreateError, GroupCreateResult, Message, Prepared,
)
from feishu_herdr_bridge.herdr import HerdrAdapter
from feishu_herdr_bridge.store import Store
from tests.test_creation import StaticCLI


class GroupParsingTests(unittest.TestCase):
    def test_group_new_preserves_internal_spaces(self):
        self.assertEqual(BridgeCore._parse('/group-new   日报  回归  '),
                         ('group_new', ['日报  回归']))


class FakeGroupCreator:
    def __init__(self):
        self.calls = []
        self.behavior = None

    def __call__(self, request):
        self.calls.append(request)
        if self.behavior is not None:
            return self.behavior(request)
        return GroupCreateResult(f"created-chat-{len(self.calls)}", verified=True)


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'bridge.sqlite3')
        self.cli = StaticCLI()
        self.cli.session = 'kpi-agg'
        self.creator = FakeGroupCreator()
        self.now = 100.0
        self.serial = 0
        self.core = self.make_core()
        for target in ('socket.create_connection', 'socket.socket.connect',
                       'socket.socket.connect_ex', 'subprocess.Popen'):
            guard = patch(target, side_effect=AssertionError('No network or real CLI in G1 group tests'))
            blocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(blocked.assert_not_called)

    def make_core(self, **overrides):
        options = dict(allowed_users={'admin-a', 'admin-b', 'ordinary'},
                       allowed_chats={'management', 'legacy-chat'}, management_chat_id='management',
                       admin_users={'admin-a', 'admin-b'}, bot_open_id='test-bot',
                       create_group=self.creator, projects={'demo': self.root},
                       clock=lambda: self.now, operation_lock=threading.Lock())
        options.update(overrides)
        return BridgeCore(self.store, self.cli.adapter(), **options)

    def message(self, text, *, user='admin-a', chat='management', chat_type='group'):
        self.serial += 1
        return Message(f'm-{self.serial}', chat, user, text, self.now + 1, chat_type)

    def send(self, text, **kwargs):
        return self.core.handle(self.message(text, **kwargs))

    def propose(self, name='测试  工作群', user='admin-a'):
        message = self.message('/group-new ' + name, user=user)
        reply = self.core.handle(message)
        self.assertEqual(reply.code, 'group_pending', reply)
        code = re.search(r'g-[0-9a-f]{16}', reply.text).group()
        group = self.store.find_group(code, 'management', user, 'kpi-agg', 'test-bot')
        self.assertIsNotNone(group)
        return group

    def saved(self, group):
        return self.store.find_group(group.confirmation_code, group.source_chat_id, group.requested_by,
                                     group.herdr_session, group.bot_open_id)

    def confirm(self, group, **kwargs):
        options = {'user': group.requested_by, 'chat': group.source_chat_id}
        options.update(kwargs)
        return self.send('/confirm ' + group.confirmation_code, **options)

    def workspace_proposal(self, user='admin-a'):
        reply = self.send('/new demo codex', user=user)
        self.assertEqual(reply.code, 'creation_pending')
        code = re.search(r'/confirm ([0-9a-f]{8})', reply.text).group(1)
        return self.store.find_creation(code, 'management', user)

    def test_proposal_is_only_persisted_and_reference_has_no_real_ids(self):
        group = self.propose()
        self.assertEqual((group.status, group.created_chat_id, group.expires_at), ('pending', None, 400.0))
        self.assertEqual(UUID(group.create_uuid).version, 4)
        self.assertEqual(group.group_name, '测试  工作群')
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.creator.calls, [])
        self.assertIsNone(self.store.get_binding('management'))
        record = self.store.get_request(group.request_id)
        self.assertEqual((record.action, record.herdr_session, record.pane_id), ('group_new', 'kpi-agg', None))
        for sensitive in (group.request_id, group.requested_by, group.source_chat_id):
            self.assertNotIn(sensitive, group.reference)
            self.assertNotIn(sensitive, repr(group))

    def test_management_gate_refuses_without_reading_or_consuming_a_proposal(self):
        group = self.propose()
        cases = ({'user': 'ordinary'}, {'user': 'unknown'}, {'chat': 'legacy-chat'},
                 {'chat': 'stranger-chat'}, {'chat_type': 'p2p'}, {'chat_type': None})
        with patch.object(self.store, 'find_group') as lookup:
            for args in cases:
                with self.subTest(args=args):
                    self.assertEqual(self.send('/group-new nope', **args).code, 'forbidden')
                    reply = self.confirm(group, **args)
                    self.assertEqual(reply.code, 'forbidden')
                    self.assertNotIn(group.reference, reply.text)
            lookup.assert_not_called()
        self.assertEqual(self.saved(group).status, 'pending')
        self.assertEqual(self.creator.calls, [])

    def test_other_admin_cannot_confirm_or_inspect_owners_code(self):
        group = self.propose()
        reply = self.confirm(group, user='admin-b')
        self.assertEqual(reply.code, 'invalid_confirmation')
        self.assertNotIn(group.reference, reply.text)
        self.assertEqual(self.saved(group).status, 'pending')
        self.assertEqual(self.creator.calls, [])

    def test_invalid_names_create_nothing(self):
        for name in ('', '   ', 'x' * 61, 'first\nsecond', 'name\n', 'x\u2028y', 'x\u2029y', '\x1b[1m', 'x\x00y'):
            with self.subTest(name=repr(name)):
                self.assertEqual(self.send('/group-new ' + name).status, 'failed')
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM group_requests').fetchone()[0], 0)
        self.assertEqual(self.creator.calls, [])

    def test_namespaces_and_each_admins_pending_are_independent(self):
        a = self.propose(user='admin-a')
        b = self.propose(user='admin-b')
        old_workspace = self.workspace_proposal(user='ordinary')
        workspace = self.workspace_proposal(user='ordinary')
        self.assertEqual(self.store.find_creation(old_workspace.confirmation_code, 'management', 'ordinary').status, 'failed')
        a2 = self.propose('another', user='admin-a')
        self.assertEqual(self.saved(a).status, 'failed')
        self.assertEqual(self.saved(b).status, 'pending')
        self.assertEqual(self.store.find_creation(workspace.confirmation_code, 'management', 'ordinary').status, 'pending')
        # Workspace creation changes the management binding, not either group proposal.
        self.assertEqual(self.send('/confirm ' + workspace.confirmation_code, user='ordinary').code, 'created')
        binding = self.store.get_binding('management')
        before_cli = len(self.cli.calls)
        self.assertEqual(self.confirm(a2).code, 'group_created')
        self.assertEqual(self.confirm(b).code, 'group_created')
        self.assertEqual(self.store.get_binding('management'), binding)
        self.assertEqual(len(self.cli.calls), before_cli)
        self.assertEqual(len(self.creator.calls), 2)

    def test_cancel_preserves_workspace_semantics_but_only_cancels_own_group(self):
        a, b = self.propose(), self.propose(user='admin-b')
        workspace = self.workspace_proposal()
        self.assertEqual(self.send('/cancel', user='ordinary').code, 'cancelled')
        self.assertEqual(self.store.find_creation(workspace.confirmation_code, 'management', 'admin-a').status, 'failed')
        self.assertEqual((self.saved(a).status, self.saved(b).status), ('pending', 'pending'))
        self.core.admin_users = frozenset({'admin-b'})
        self.send('/cancel', user='admin-a')
        self.assertEqual(self.saved(a).status, 'pending')
        self.assertEqual(self.confirm(a).code, 'forbidden')
        self.send('/cancel', user='admin-b', chat='legacy-chat')
        self.assertEqual(self.saved(b).status, 'pending')
        self.send('/cancel', user='admin-b')
        self.assertEqual((self.saved(a).status, self.saved(b).status), ('pending', 'failed'))
        self.assertEqual(self.creator.calls, [])

    def test_cancel_never_revives_or_cancels_consumed_requests(self):
        group = self.propose()
        self.store.begin_group(group, self.now)
        self.send('/cancel')
        self.assertEqual(self.saved(group).status, 'processing')
        other = self.propose()
        self.assertEqual(self.saved(group).status, 'processing')
        self.confirm(other)
        self.send('/cancel')
        self.assertEqual(self.saved(other).status, 'done')
        self.assertEqual(len(self.creator.calls), 1)

    def test_code_prefix_has_no_cross_type_fallback(self):
        group = self.propose()
        with patch.object(self.store, 'find_creation') as workspace_lookup:
            self.assertEqual(self.send('/confirm g-bad').code, 'invalid_confirmation')
            self.assertEqual(self.send('/confirm ' + group.confirmation_code[:-1]).code, 'invalid_confirmation')
            workspace_lookup.assert_not_called()
        with patch.object(self.store, 'find_group') as group_lookup:
            self.assertEqual(self.send('/confirm 12345678').code, 'invalid_confirmation')
            group_lookup.assert_not_called()
        self.assertEqual(self.creator.calls, [])

    def test_duplicate_confirm_reads_group_done_without_another_call(self):
        group = self.propose()
        message = self.message('/confirm ' + group.confirmation_code)
        self.assertEqual(self.core.handle(message).code, 'group_created')
        self.assertEqual(self.core.handle(message).code, 'group_created')
        self.assertEqual(self.confirm(group).code, 'group_created')
        self.assertEqual(len(self.creator.calls), 1)
        self.assertEqual(self.creator.calls[0].create_uuid, group.create_uuid)
        self.assertEqual(self.creator.calls[0].requested_by, 'admin-a')
        self.assertEqual(self.cli.calls, [])

    def test_duplicate_proposal_does_not_replace_pending_or_regenerate_uuid(self):
        message = self.message('/group-new demo')
        with patch('feishu_herdr_bridge.core.uuid4', wraps=__import__('uuid').uuid4) as generate:
            self.assertEqual(self.core.handle(message).code, 'group_pending')
            self.assertEqual(self.core.handle(message).code, 'duplicate')
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(self.creator.calls, [])

    def test_expired_confirmation_and_busy_event_cannot_create(self):
        group = self.propose()
        message = self.message('/confirm ' + group.confirmation_code)
        self.core._lock.acquire()
        try:
            self.assertEqual(self.core.handle(message).code, 'busy')
        finally:
            self.core._lock.release()
        self.assertEqual(self.core.handle(message).code, 'duplicate')
        self.now = group.expires_at
        self.assertEqual(self.confirm(group).status, 'failed')
        self.assertEqual(self.saved(group).result_code, 'expired')
        self.assertEqual(self.creator.calls, [])

    def test_permissions_rechecked_before_execution_and_before_remote_call(self):
        group = self.propose()
        prepared = self.core.prepare(self.message('/confirm ' + group.confirmation_code))
        self.assertIsInstance(prepared, Prepared)
        self.core.admin_users = frozenset({'admin-b'})
        self.assertEqual(self.core.execute(prepared).code, 'forbidden')
        self.assertEqual(self.saved(group).status, 'pending')
        self.core.admin_users = frozenset({'admin-a', 'admin-b'})
        begin = self.store.begin_group

        def revoke(*args):
            result = begin(*args)
            self.core.admin_users = frozenset({'admin-b'})
            return result

        with patch.object(self.store, 'begin_group', side_effect=revoke):
            self.assertEqual(self.confirm(group).status, 'failed')
        self.assertEqual(self.creator.calls, [])

    def test_consumption_commit_failure_does_not_call_remote(self):
        group = self.propose()
        begin = self.store.begin_group

        def committed_then_lost(*args):
            begin(*args)
            raise sqlite3.OperationalError('PRIVATE')

        with patch.object(self.store, 'begin_group', side_effect=committed_then_lost):
            reply = self.confirm(group)
        self.assertEqual(reply.status, 'unknown')
        self.assertIn(group.reference, reply.text)
        self.assertNotIn('PRIVATE', reply.text)
        self.assertEqual(self.saved(group).status, 'unknown')
        self.confirm(group)
        self.assertEqual(self.creator.calls, [])

    def test_failure_before_consumption_does_not_bypass_the_failure(self):
        group = self.propose()
        message = self.message('/confirm ' + group.confirmation_code)
        with patch.object(self.store, 'begin_group', side_effect=sqlite3.OperationalError('PRIVATE')):
            self.assertEqual(self.core.handle(message).status, 'unknown')
        # A new event ID must not bypass the failed confirmation transaction.
        retry = self.message('/confirm ' + group.confirmation_code)
        self.assertNotEqual(retry.message_id, message.message_id)
        with patch.object(self.store, 'begin_group', wraps=self.store.begin_group) as consume:
            self.assertEqual(self.core.handle(retry).status, 'unknown')
            consume.assert_not_called()
        saved = self.saved(group)
        self.assertEqual((saved.status, saved.result_code), ('unknown', 'storage_error'))
        self.assertIsNone(saved.created_chat_id)
        self.assertEqual(self.core.handle(message).status, 'unknown')
        self.assertEqual(self.creator.calls, [])
        self.store = Store(self.store.path)
        self.core = self.make_core()
        self.assertEqual(self.confirm(group).status, 'unknown')
        self.assertEqual(self.saved(group).status, 'unknown')
        self.assertEqual(self.creator.calls, [])

    def test_unknown_settlement_is_atomic_and_preserves_terminal_results(self):
        for status in ('pending', 'processing', 'done', 'failed', 'unknown'):
            with self.subTest(status=status):
                group = self.propose()
                if status != 'pending':
                    self.store.begin_group(group, self.now)
                if status == 'done':
                    self.store.save_group_resource(group.request_id, 'settled-chat', self.now)
                    self.store.complete_group(group.request_id, self.now)
                elif status in {'failed', 'unknown'}:
                    self.store.finish_group(group.request_id, status, 'original_result', self.now)
                before = self.saved(group)
                self.store.finish_group(group.request_id, 'unknown', 'storage_error', self.now + 1)
                expected = (replace(before, status='unknown', result_code='storage_error',
                                    updated_at=self.now + 1)
                            if status in {'pending', 'processing'} else before)
                self.assertEqual(self.saved(group), expected)
                self.assertEqual(Store(self.store.path).find_group(
                    group.confirmation_code, group.source_chat_id, group.requested_by,
                    group.herdr_session, group.bot_open_id), expected)
        self.assertEqual(self.creator.calls, [])

    def test_unverified_or_invalid_results_never_authorize(self):
        for result in (GroupCreateResult('partial-chat'), GroupCreateResult(None, True),
                       GroupCreateResult('bad id', True), GroupCreateResult('partial-chat', 'yes'), {'chat_id': 'wrong-shape'}):
            with self.subTest(result=type(result).__name__):
                group = self.propose()
                self.creator.behavior = lambda request, result=result: result
                before = len(self.creator.calls)
                self.assertEqual(self.confirm(group).status, 'unknown')
                self.confirm(group)
                self.assertEqual(len(self.creator.calls), before + 1)
        self.assertFalse(self.core.is_authorized(self.message('/agents', chat='partial-chat')))
        self.assertEqual(self.cli.calls, [])

    def test_known_rejection_and_known_resource_on_exception(self):
        for error, expected in ((GroupCreateError(uncertain=False), 'failed'),
                                (GroupCreateError(created_chat_id='partial-error-chat', uncertain=False), 'unknown'),
                                (TimeoutError('PRIVATE'), 'unknown')):
            with self.subTest(expected=expected):
                group = self.propose()
                self.creator.behavior = Mock(side_effect=error)
                reply = self.confirm(group)
                self.assertEqual(reply.status, expected)
                self.assertNotIn('PRIVATE', reply.text)
                self.assertNotIn('partial-error-chat', reply.text)
                self.confirm(group)
                self.creator.behavior.assert_called_once()
                if isinstance(error, GroupCreateError):
                    self.assertEqual(self.saved(group).created_chat_id, error.created_chat_id)

    def test_resource_save_failure_never_enables_memory_authorization(self):
        old = self.store.bind(chat_id='management', session='kpi-agg', workspace_id='workspace-a',
                              pane_id='pane-a', agent_name='lead', user_id='admin-a', now=self.now,
                              expected_revision=None)
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit):
                group = self.propose()
                save = self.store.save_group_resource

                def broken(*args):
                    if after_commit:
                        save(*args)
                    raise sqlite3.OperationalError('PRIVATE')

                with patch.object(self.store, 'save_group_resource', side_effect=broken), \
                        patch.object(self.store, 'complete_group') as complete:
                    reply = self.confirm(group)
                    complete.assert_not_called()
                self.assertEqual(reply.status, 'unknown')
                self.assertIn(group.reference, reply.text)
                chat = f'created-chat-{len(self.creator.calls)}'
                self.assertEqual(self.saved(group).created_chat_id, chat if after_commit else None)
                self.assertFalse(self.core.is_authorized(self.message('/agents', chat=chat)))
                self.assertEqual(self.store.get_binding('management'), old)
                count = len(self.creator.calls)
                self.confirm(group)
                self.assertEqual(len(self.creator.calls), count)

    def test_authorization_commit_failure_is_read_back_without_retry(self):
        for after_commit in (False, True):
            with self.subTest(after_commit=after_commit):
                group = self.propose()
                complete = self.store.complete_group

                def broken(*args):
                    if after_commit:
                        complete(*args)
                    raise sqlite3.OperationalError('PRIVATE')

                with patch.object(self.store, 'complete_group', side_effect=broken) as once:
                    reply = self.confirm(group)
                once.assert_called_once()
                chat = f'created-chat-{len(self.creator.calls)}'
                self.assertEqual(self.saved(group).created_chat_id, chat)
                self.assertEqual(reply.status, 'done' if after_commit else 'unknown')
                self.assertEqual(self.core.is_authorized(self.message('/agents', chat=chat)), after_commit)
                count = len(self.creator.calls)
                self.confirm(group)
                self.assertEqual(len(self.creator.calls), count)

    def test_total_local_storage_failure_keeps_consumed_record_and_reference(self):
        group = self.propose()
        with ExitStack() as faults:
            def remote_then_storage_fails(request):
                for method in ('save_group_resource', 'finish_group', 'finish', 'find_group'):
                    faults.enter_context(patch.object(self.store, method, side_effect=sqlite3.OperationalError('PRIVATE')))
                return GroupCreateResult('lost-resource-id', True)

            self.creator.behavior = remote_then_storage_fails
            reply = self.confirm(group)
        self.assertEqual(reply.status, 'unknown')
        self.assertIn(group.reference, reply.text)
        self.assertNotIn('lost-resource-id', reply.text)
        self.assertNotIn('PRIVATE', reply.text)
        self.assertEqual((self.saved(group).status, self.saved(group).created_chat_id), ('processing', None))
        self.core = self.make_core()
        self.assertEqual(self.saved(group).status, 'unknown')
        self.confirm(group)
        self.assertEqual(len(self.creator.calls), 1)
        self.assertFalse(self.core.is_authorized(self.message('/agents', chat='lost-resource-id')))

    def test_done_is_authoritative_after_receipt_record_failure_and_restart(self):
        group = self.propose()
        message = self.message('/confirm ' + group.confirmation_code)
        with patch.object(self.store, 'finish', side_effect=sqlite3.OperationalError('PRIVATE')):
            self.assertEqual(self.core.handle(message).status, 'unknown')
        self.assertEqual(self.saved(group).status, 'done')
        self.core = self.make_core()
        self.assertEqual(self.core.handle(message).code, 'group_created')
        self.assertEqual(self.confirm(group).code, 'group_created')
        self.assertEqual(len(self.creator.calls), 1)
        self.assertTrue(self.core.is_authorized(self.message('/agents', chat='created-chat-1')))

    def test_interrupted_remote_call_is_not_replayed_by_recovery(self):
        group = self.propose()
        self.creator.behavior = Mock(side_effect=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.confirm(group)
        self.assertFalse(self.core._lock.locked())
        self.assertEqual(self.saved(group).status, 'processing')
        self.core = self.make_core()
        self.assertEqual(self.saved(group).status, 'unknown')
        self.confirm(group)
        self.assertEqual(len(self.creator.calls), 1)

    def test_resource_unique_conflict_preserves_first_groups_authorization(self):
        first = self.propose()
        self.confirm(first)
        second = self.propose(user='admin-b')
        self.creator.behavior = lambda request: GroupCreateResult('created-chat-1', True)
        self.assertEqual(self.confirm(second).status, 'unknown')
        self.assertIsNone(self.saved(second).created_chat_id)
        self.assertEqual(self.saved(first).status, 'done')
        self.assertTrue(self.core.is_authorized(self.message('/agents', chat='created-chat-1')))

    def test_dynamic_auth_checks_database_type_user_bot_and_single_session(self):
        group = self.propose()
        target = self.message('/agents', chat='created-chat-1')
        self.assertFalse(self.core.is_authorized(target))
        self.confirm(group)
        self.assertTrue(self.core.is_authorized(target))
        self.assertNotIn(target.chat_id, self.core.allowed_chats)
        for changed in (replace(target, chat_type=None), replace(target, chat_type='p2p'),
                        replace(target, user_id='outsider'), replace(target, chat_id='unmanaged')):
            self.assertFalse(self.core.is_authorized(changed))
        with patch.object(self.store, 'group_allowed', side_effect=sqlite3.OperationalError('PRIVATE')):
            self.assertEqual(self.core.handle(target).code, 'forbidden')
        self.core.bot_open_id = 'another-bot'
        self.assertFalse(self.core.is_authorized(target))
        self.assertEqual(self.confirm(group).code, 'invalid_confirmation')
        self.core.bot_open_id = 'test-bot'
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE group_requests SET status='unknown' WHERE request_id=?", (group.request_id,))
        self.assertFalse(self.core.is_authorized(target))
        self.core.herdr = HerdrAdapter('not-kpi-agg')
        self.assertFalse(self.core.is_authorized(target))

    def test_disabling_creation_keeps_existing_dynamic_authorization(self):
        group = self.propose()
        self.confirm(group)
        self.core = self.make_core(management_chat_id=None, admin_users=(), create_group=None)
        self.assertTrue(self.core.is_authorized(self.message('/agents', chat='created-chat-1')))
        self.assertEqual(self.send('/group-new nope').code, 'forbidden')
        self.assertEqual(self.confirm(group).code, 'forbidden')

    def test_enabled_management_requires_fixed_session_and_valid_admin_subset(self):
        for overrides in ({'management_chat_id': None}, {'admin_users': ()},
                          {'management_chat_id': 'untrusted'}, {'admin_users': {'outsider'}},
                          {'bot_open_id': None}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.make_core(**overrides)
        self.cli.session = 'not-kpi-agg'
        with self.assertRaises(ValueError):
            self.make_core()
        self.assertEqual(self.creator.calls, [])

    def test_dynamic_groups_keep_existing_commands_bindings_and_workspace_creation(self):
        first = self.propose()
        second = self.propose(user='admin-b')
        self.confirm(first)
        self.confirm(second)
        a, b = 'created-chat-1', 'created-chat-2'
        for chat, pane, workspace in ((a, 'pane-a', 'workspace-a'), (b, 'pane-b', 'workspace-b')):
            self.assertEqual(self.send('/agents', chat=chat).code, 'agents')
            self.assertEqual(self.send('before bind', chat=chat).code, 'unbound')
            self.assertEqual(self.send(f'/bind {workspace} {pane}', chat=chat).code, 'bound')
            self.assertEqual(self.send('/bind', chat=chat).code, 'binding')
            self.assertEqual(self.send('only-' + chat, chat=chat).code, 'submitted')
            self.assertIn(pane, self.send('/read', chat=chat).text)
            self.assertEqual(self.send('/group-new nested', chat=chat).code, 'forbidden')
        self.assertEqual(self.cli.submitted, [('pane-a', 'only-' + a), ('pane-b', 'only-' + b)])
        self.assertEqual(self.send('/bind workspace-a pane-a', chat=b).code, 'workspace_occupied')
        self.assertEqual(self.send('/new demo claude', chat=a).code, 'creation_pending')
        self.assertEqual(self.send('/cancel', chat=a).code, 'cancelled')
        reply = self.send('/new demo claude', chat=a)
        code = re.search(r'/confirm ([0-9a-f]{8})', reply.text).group(1)
        self.assertEqual(self.send('/confirm ' + code, chat=a).code, 'created')
        binding = self.store.get_binding(a)
        self.core = self.make_core()
        self.assertEqual(self.store.get_binding(a), binding)
        self.assertEqual(self.send('/read', chat=b).code, 'read')
        self.assertEqual(len(self.creator.calls), 2)

    def test_duplicate_during_inflight_create_cannot_start_a_second_call(self):
        entered, release = threading.Event(), threading.Event()
        group = self.propose()
        message = self.message('/confirm ' + group.confirmation_code)
        replies = []

        def remote(request):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fake deadline')
            return GroupCreateResult('inflight-chat', True)

        self.creator.behavior = remote
        worker = threading.Thread(target=lambda: replies.append(self.core.handle(message)), daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.core.handle(message).code, 'group_unknown')
            self.assertEqual(len(self.creator.calls), 1)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(replies[0].code, 'group_created')
        self.assertEqual(self.confirm(group).code, 'group_created')
        self.assertEqual(len(self.creator.calls), 1)


    def test_proposal_unique_collision_rolls_back_only_own_replacement(self):
        group = self.propose()
        with patch('feishu_herdr_bridge.core.secrets.token_hex', return_value=group.confirmation_code[2:]):
            self.assertEqual(self.send('/group-new new name').status, 'unknown')
        self.assertEqual(self.saved(group).status, 'pending')
        self.assertEqual(self.creator.calls, [])

    def test_dynamic_authorization_is_rechecked_after_prepare(self):
        group = self.propose()
        self.confirm(group)
        prepared = self.core.prepare(self.message('/agents', chat='created-chat-1'))
        self.assertIsInstance(prepared, Prepared)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE group_requests SET status='unknown' WHERE request_id=?", (group.request_id,))
        before = len(self.cli.calls)
        self.assertEqual(self.core.execute(prepared).code, 'forbidden')
        self.assertEqual(len(self.cli.calls), before)
        self.assertFalse(self.core._lock.locked())


class GroupEventEntryTests(unittest.TestCase):
    def test_entry_uses_core_authorization_and_rejects_mismatched_bot(self):
        from feishu_herdr_bridge.feishu import FeishuBridge
        from tests.test_feishu import event, BOT

        core = Mock(bot_open_id=BOT)
        core.is_authorized.return_value = False
        bridge = FeishuBridge(core, BOT, Mock())
        self.assertIsNone(bridge.receive(event('/agents', group=True)))
        normalized = core.is_authorized.call_args.args[0]
        self.assertEqual(normalized.chat_type, 'group')
        core.prepare.assert_not_called()
        with self.assertRaises(ValueError):
            FeishuBridge(core, 'another-bot', Mock())
        core.bot_open_id = 'changed-bot'
        self.assertIsNone(bridge.receive(event('/agents', group=True)))
        core.prepare.assert_not_called()


class SDKGroupEntryTests(unittest.TestCase):
    def test_created_group_is_immediately_allowed_with_all_existing_commands(self):
        from feishu_herdr_bridge.feishu import FeishuBridge
        from tests.test_feishu import GroupHTTPFixture, event

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wire = GroupHTTPFixture(self)
            core = wire.core(root)
            received = []
            bridge = FeishuBridge(core, wire.bot, lambda m, r: received.append(r))
            sequence = 0

            def send(text, chat='management', user=None, group=True):
                nonlocal sequence
                sequence += 1
                data = event(text, f'entry-{sequence}', chat, user or wire.owner, group)
                for mention in data['event']['message']['mentions']:
                    mention['id']['open_id'] = wire.bot
                before = len(received)
                worker = bridge.receive(data)
                if worker:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                return received[-1] if len(received) > before else None

            groups = []
            for suffix in ('a', 'b'):
                proposal = send('/group-new ' + suffix)
                self.assertEqual(proposal.code, 'group_pending')
                code = re.search(r'g-[0-9a-f]{16}', proposal.text).group()
                chat_id = 'oc_entry_fixture_' + suffix
                wire.payload['data']['chat_id'] = chat_id
                result = send('/confirm ' + code)
                self.assertEqual(result.code, 'group_created')
                self.assertNotIn(chat_id, result.text)
                groups.append(chat_id)
                self.assertNotIn(chat_id, core.allowed_chats)
                self.assertIsNone(core.store.get_binding(chat_id))
                self.assertEqual(send('before binding', chat_id).code, 'unbound')
                self.assertEqual(send('/agents', chat_id).code, 'agents')
                self.assertEqual(send('/group-new nested', chat_id).code, 'forbidden')
            a, b = groups
            self.assertEqual(send('/bind workspace-a pane-a', a).code, 'bound')
            self.assertEqual(send('/bind workspace-a pane-a', b).code, 'workspace_occupied')
            self.assertEqual(send('/bind workspace-b pane-b', b).code, 'bound')
            self.assertEqual(send('/bind', a).code, 'binding')
            self.assertEqual(send('A_ONLY', a).code, 'submitted')
            self.assertEqual(send('B_ONLY', b).code, 'submitted')
            self.assertEqual(wire.cli.submitted, [('pane-a', 'A_ONLY'), ('pane-b', 'B_ONLY')])
            self.assertIn('pane-a', send('/read', a).text)
            self.assertIn('pane-b', send('/read', b).text)
            self.assertEqual(send('/new demo codex', a).code, 'creation_pending')
            self.assertEqual(send('/cancel', a).code, 'cancelled')
            pending = send('/new demo codex', a)
            code = re.search(r'/confirm ([0-9a-f]{8})', pending.text).group(1)
            self.assertEqual(send('/confirm ' + code, a).code, 'created')
            self.assertEqual(len(wire.posts), 2)
            self.assertIsNone(core.store.get_binding('management'))
            self.assertIsNone(send('/agents', a, user='not-allowed'))
            self.assertIsNone(send('/agents', 'stranger-chat'))
            self.assertIsNone(send('/agents', a, group=False))
            with patch.object(core.store, 'group_allowed', side_effect=sqlite3.OperationalError('PRIVATE')):
                self.assertIsNone(send('/agents', a))
            # Restart with creation disabled, but retain the current bot identity.
            binding = core.store.get_binding(a)
            core = BridgeCore(Store(core.store.path), wire.cli.adapter(), allowed_users={wire.owner},
                              allowed_chats={'management'}, bot_open_id=wire.bot,
                              clock=lambda: 100.0, operation_lock=threading.Lock())
            bridge = FeishuBridge(core, wire.bot, lambda m, r: received.append(r))
            self.assertEqual(core.store.get_binding(a), binding)
            self.assertEqual(send('/read', a).code, 'read')
            self.assertEqual(send('/group-new disabled').code, 'forbidden')
            self.assertEqual(len(wire.posts), 2)
