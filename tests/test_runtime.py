"""Linux runtime checks: fake children only, no credentials and no network."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from feishu_herdr_bridge import __main__ as entry
from feishu_herdr_bridge.core import BridgeCore, Message
from feishu_herdr_bridge.herdr import (
    CommandInterrupted, HerdrAdapter, HerdrError, ManagedRunner, decode_protocol22, session_command,
)
from feishu_herdr_bridge.store import Creation, Store


_FIXTURE = Path(__file__).parent / "fixtures/herdr_0_9_0.json"
_NO_NETWORK = """
import sys

def reject_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Network forbidden in offline runtime test')
sys.addaudithook(reject_network)
"""


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "herdr_executable": "/offline/not-a-real-herdr", "herdr_session": "offline-session",
            "bot_open_id": "offline-bot", "allowed_users": ["user"], "allowed_chats": ["chat"],
            "projects": {"demo": str(self.root)}, "database": str(self.root / "bridge.sqlite3"),
        }), encoding="utf-8")
        self.fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        self.credentials = {"FEISHU_APP_ID": "offline-app", "FEISHU_APP_SECRET": "offline-placeholder"}
        for name in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex"):
            guard = patch(name, side_effect=AssertionError("Network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def child(self, script: str, *args: str) -> subprocess.Popen[str]:
        env = {k: v for k, v in os.environ.items() if k not in {"FEISHU_APP_ID", "FEISHU_APP_SECRET"}}
        child = subprocess.Popen([sys.executable, "-c", _NO_NETWORK + script, *args],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=env, start_new_session=True)

        def cleanup():
            # This process group belongs only to this test, never to HerdR.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate(timeout=5)

        self.addCleanup(cleanup)
        return child

    def wait_for(self, predicate, child=None):
        deadline = time.monotonic() + 5.0
        while not predicate():
            if time.monotonic() >= deadline or (child is not None and child.poll() is not None):
                self.fail("Offline child did not reach the expected boundary")
            time.sleep(0.01)

    def test_same_config_and_symlink_cannot_lock_twice_and_file_is_private(self):
        alias = self.root / "alias.json"
        alias.symlink_to(self.config)
        with entry.InstanceLock(self.config, directory=self.root / "locks") as held:
            self.assertEqual(stat.S_IMODE(held.path.stat().st_mode), 0o600)
            with self.assertRaises(entry.AlreadyRunning):
                with entry.InstanceLock(alias, directory=self.root / "locks"):
                    self.fail("Second holder was admitted")
            inode = held.path.stat().st_ino
        self.assertTrue(held.path.exists())
        with entry.InstanceLock(alias, directory=self.root / "locks") as acquired:
            self.assertEqual(acquired.path.stat().st_ino, inode)

    def test_separate_process_holds_lock_until_exit_and_no_unlink_is_needed(self):
        ready = self.root / "holder-ready"
        child = self.child("""
import signal
from pathlib import Path
from feishu_herdr_bridge.__main__ import InstanceLock
with InstanceLock(Path(sys.argv[1]), directory=Path(sys.argv[2])):
    Path(sys.argv[3]).write_text('ready')
    signal.pause()
""", str(self.config), str(self.root / "locks"), str(ready))
        self.wait_for(ready.exists, child)
        with self.assertRaises(entry.AlreadyRunning):
            with entry.InstanceLock(self.config, directory=self.root / "locks"):
                self.fail("Cross-process lock failed")
        child.kill()
        child.communicate(timeout=5)
        with entry.InstanceLock(self.config, directory=self.root / "locks"):
            pass

    def test_duplicate_main_exits_before_database_recovery_or_sdk_construction(self):
        with patch.object(entry.Path, "home", return_value=self.root), \
                entry.InstanceLock(self.config), patch.object(entry, "Store") as store, \
                patch.object(entry, "LarkTransport") as sdk, \
                self.assertLogs(entry.__name__, level="ERROR"):
            self.assertEqual(entry.main(["--config", str(self.config)], env=self.credentials), 3)
        store.assert_not_called()
        sdk.assert_not_called()

    def test_bad_configuration_or_missing_credentials_has_no_runtime_side_effects(self):
        for path, env in ((self.root / "missing.json", {}), (self.config, {})):
            with self.subTest(path=path), patch.object(entry, "InstanceLock") as lock, \
                    patch.object(entry, "Store") as store, patch.object(entry, "LarkTransport") as sdk, \
                    redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    entry.main(["--config", str(path)], env=env)
                self.assertEqual(stopped.exception.code, 2)
                lock.assert_not_called()
                store.assert_not_called()
                sdk.assert_not_called()

    def test_managed_runner_preserves_streams_and_does_not_use_shell(self):
        runner = ManagedRunner()
        self.addCleanup(runner.stop)
        script = "import sys; print(sys.argv[1]); print('structured-error', file=sys.stderr); sys.exit(7)"
        marker = self.root / "must-not-exist"
        literal = f"$(touch {marker}); literal"
        result = runner([sys.executable, "-c", script, literal], 5.0)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (7, literal + "\n", "structured-error\n"))
        self.assertFalse(marker.exists())
        self.assertTrue(runner.stop())

    def test_stopped_runner_never_spawns_and_adapter_marks_known_non_submission(self):
        runner = ManagedRunner()
        runner.stop()
        with patch("feishu_herdr_bridge.herdr.subprocess.Popen") as spawn:
            with self.assertRaises(CommandInterrupted) as error:
                runner([sys.executable, "-c", "pass"], 5.0)
            self.assertFalse(error.exception.started)
            adapter = HerdrAdapter("offline", command_builder=session_command("/offline/herdr"),
                                   decoder=decode_protocol22, runner=runner)
            with self.assertRaises(HerdrError) as rejected:
                adapter.prompt("pane-a", "task")
            self.assertEqual(rejected.exception.code, "interrupted")
            self.assertFalse(rejected.exception.uncertain)
        spawn.assert_not_called()

    def test_shutdown_kills_and_reaps_child_that_ignores_term_and_write_is_unknown(self):
        runner = ManagedRunner()
        self.addCleanup(runner.stop)
        marker = self.root / "child.pid"
        script = ("import os,signal,sys,time; from pathlib import Path; "
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
        adapter = HerdrAdapter("offline", command_builder=lambda session, args: (
            sys.executable, "-c", script, str(marker)), decoder=decode_protocol22,
            runner=runner, write_timeout=30.0)
        errors = []

        def run():
            try:
                adapter.prompt("pane-a", "accepted-before-shutdown")
            except HerdrError as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.wait_for(lambda: marker.exists() and bool(marker.read_text()))
        pid = int(marker.read_text())
        self.assertTrue(runner.stop(grace=0.1))
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, "interrupted")
        self.assertTrue(errors[0].uncertain)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_managed_timeout_reaps_once_without_retry(self):
        runner = ManagedRunner()
        self.addCleanup(runner.stop)
        children = []
        original = subprocess.Popen

        def record(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child

        with patch("feishu_herdr_bridge.herdr.subprocess.Popen", side_effect=record):
            with self.assertRaises(subprocess.TimeoutExpired):
                runner([sys.executable, "-c", "import time; time.sleep(60)"], 0.1)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(children[0].stderr.closed)

    def test_shutdown_before_registered_worker_starts_prevents_cli_and_preserves_processing(self):
        store = Store(self.root / "bridge.sqlite3")
        runner = ManagedRunner()
        core = BridgeCore(store, HerdrAdapter("offline"), allowed_chats={"chat"}, allowed_users={"user"},
                          operation_lock=threading.Lock())
        runtime = entry.BridgeRuntime(core, "bot", Mock(), runner)
        message = Message("interrupted-before-start", "chat", "user", "/agents", time.time())
        prepared = core.prepare(message)
        runtime._worker(target=core.execute, args=(prepared,), daemon=True)
        runtime.close()
        with patch.object(core, "prepare") as prepare:
            self.assertIsNone(runtime.receive({"untrusted": "new event"}))
        prepare.assert_not_called()
        self.assertEqual(store.get_request(message.message_id).status, "processing")
        # Process exit releases in-memory locks; recovery must never execute this work.
        restarted = BridgeCore(store, HerdrAdapter("offline"), allowed_chats={"chat"},
                               allowed_users={"user"}, operation_lock=threading.Lock())
        self.assertEqual(store.get_request(message.message_id).status, "unknown")
        self.assertEqual(restarted.handle(message).code, "duplicate")
        core.abandon(prepared)  # Release this test process's old in-memory lock only.

    def test_entrypoint_recovery_does_not_replay_prompt_or_partial_creation(self):
        store = Store(self.root / "bridge.sqlite3")
        binding = store.bind(chat_id="chat", session="offline-session", workspace_id="workspace-a",
                             pane_id="pane-a", agent_name="lead", user_id="user", now=100.0,
                             expected_revision=None)
        store.claim(message_id="interrupted", chat_id="chat", user_id="user", action="prompt",
                    snapshot=binding, now=101.0)
        creation = Creation("new-request", "confirm-code", "chat", "user", str(self.root), "label",
                            "codex", "fb-codex-0123456789ab", binding.revision, 9999999999.0,
                            "processing", "kept-workspace", "kept-tab", "kept-pane", "", 100.0, 100.0)
        store.propose_creation(creation)
        sdk = Mock()

        def start(runtime):
            current = runtime.bridge.core
            self.assertEqual(current.store.get_request("interrupted").status, "unknown")
            self.assertFalse(current.store.get_binding("chat").valid)
            self.assertEqual(current.handle(Message("interrupted", "chat", "user", "task", 101.0)).code, "duplicate")
            saved = current.store.find_creation("confirm-code", "chat", "user")
            self.assertEqual((saved.status, saved.workspace_id), ("unknown", "kept-workspace"))

        sdk.start.side_effect = start
        with patch.object(entry.Path, "home", return_value=self.root), \
                patch.object(entry, "LarkTransport", return_value=sdk), \
                patch("feishu_herdr_bridge.herdr.subprocess.Popen") as spawn:
            self.assertEqual(entry.main(["--config", str(self.config)], env=self.credentials), 0)
        spawn.assert_not_called()

    def test_existing_protocol22_fields_and_stderr_error_contract_are_preserved(self):
        result = self.fixture["create"]["result"]
        actual = decode_protocol22("create", json.dumps(self.fixture["create"])).workspace
        self.assertEqual((actual.workspace_id, actual.tab_id, actual.pane_id),
                         (result["workspace"]["workspace_id"], result["tab"]["tab_id"], result["root_pane"]["pane_id"]))
        self.assertEqual(self.fixture["_meta"]["source"], "schema-derived/static-not-live")
        self.assertEqual(self.fixture["_meta"]["live_probe"], "pending")
        runner = lambda command, timeout: subprocess.CompletedProcess(
            command, 1, "not-json-stdout", json.dumps(self.fixture["error"]))
        adapter = HerdrAdapter("offline", command_builder=session_command("/offline/herdr"),
                               decoder=decode_protocol22, runner=runner)
        with self.assertRaises(HerdrError) as error:
            adapter.prompt("pane-a", "task")
        self.assertEqual(error.exception.code, "remote_error")
        self.assertTrue(error.exception.uncertain)

    def test_actual_sigterm_closes_entrypoint_reaps_fake_cli_and_keeps_unknown(self):
        executable = self.root / "fake-herdr"
        executable.write_text(f"#!{sys.executable}\n" + """
import json, os, signal, sys, time
from pathlib import Path
root = Path(__file__).parent
assert sys.argv[1:3] == ['--session', 'offline-session']
if sys.argv[3:5] == ['agent', 'get']:
    print((root / 'get.json').read_text())
elif sys.argv[3:5] == ['agent', 'prompt']:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / 'cli.pid').write_text(str(os.getpid()))
    with (root / 'submissions').open('a') as stream:
        stream.write('submitted-once\\n')
    time.sleep(60)
else:
    raise AssertionError('Unexpected fake CLI operation')
""", encoding="utf-8")
        executable.chmod(0o700)
        (self.root / "get.json").write_text(json.dumps(self.fixture["get"]), encoding="utf-8")
        config = json.loads(self.config.read_text())
        config["herdr_executable"] = str(executable)
        self.config.write_text(json.dumps(config), encoding="utf-8")
        script = """
import json, signal, time
from pathlib import Path
from unittest.mock import patch
from feishu_herdr_bridge import __main__ as entry
root = Path(sys.argv[1])

class OfflineTransport:
    def __init__(self, credentials): pass
    def send(self, message, reply): pass
    def start(self, runtime):
        core = runtime.bridge.core
        core.store.bind(chat_id='chat', session='offline-session', workspace_id='workspace-a',
                        pane_id='pane-a', agent_name='lead', user_id='user', now=100.0, expected_revision=None)
        data = {'header': {'event_type': 'im.message.receive_v1'}, 'event': {
            'sender': {'sender_type': 'user', 'sender_id': {'open_id': 'user'}},
            'message': {'message_id': 'signal-task', 'chat_id': 'chat', 'chat_type': 'p2p',
                        'message_type': 'text', 'create_time': '101000',
                        'content': json.dumps({'text': 'offline task'}), 'mentions': []}}}
        runtime.receive(data)
        while True:
            signal.pause()

with patch.object(entry.Path, 'home', return_value=root), patch.object(entry, 'LarkTransport', OfflineTransport):
    raise SystemExit(entry.main(['--config', str(root / 'config.json')],
                               env={'FEISHU_APP_ID': 'offline-app', 'FEISHU_APP_SECRET': 'offline-placeholder'}))
"""
        child = self.child(script, str(self.root))
        self.wait_for((self.root / "submissions").exists, child)
        pid = int((self.root / "cli.pid").read_text())
        child.terminate()
        _, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertNotIn("Traceback", stderr)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        store = Store(self.root / "bridge.sqlite3")
        self.assertEqual(store.get_request("signal-task").status, "unknown")
        self.assertFalse(store.get_binding("chat").valid)
        self.assertEqual((self.root / "submissions").read_text().splitlines(), ["submitted-once"])
        with patch.object(entry.Path, "home", return_value=self.root), entry.InstanceLock(self.config):
            pass

    def test_signal_handlers_restore_and_second_signal_does_not_interrupt_cleanup(self):
        runtime = entry.BridgeRuntime(Mock(), "bot", Mock(), ManagedRunner())
        previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        with entry.shutdown_signals(runtime):
            handler = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
            self.assertTrue(runtime.stopping)
            handler(signal.SIGTERM, None)
            runtime.close()
        for signum, handler in previous.items():
            self.assertEqual(signal.getsignal(signum), handler)

    def test_packaged_config_and_unit_do_not_embed_credentials_or_new_cli_flags(self):
        repo = Path(__file__).resolve().parents[1]
        parsed = entry.load_config(repo / "examples/config.example.json")
        self.assertTrue(Path(parsed.executable).is_absolute())
        credentials = (repo / "examples/credentials.env.example").read_text()
        for line in credentials.splitlines():
            if line and not line.startswith("#"):
                self.assertEqual(line.split("=", 1)[1], "")
        service = (repo / "deploy/feishu-herdr-bridge.service").read_text()
        self.assertIn("EnvironmentFile=%h/.config/feishu-herdr-bridge/credentials.env", service)
        self.assertIn("WorkingDirectory=@REPO_DIR@", service)
        self.assertIn("KillMode=mixed", service)
        self.assertIn("UMask=0077", service)
        self.assertNotIn("HERDR_ENV=1", service)
        self.assertNotIn("FEISHU_APP_SECRET=", service)
