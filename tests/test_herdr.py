from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from feishu_herdr_bridge.herdr import ControlResult, HerdrAdapter, HerdrError, run_command
from tests.fake_herdr import CONTRACT, FakeHerdR, SESSION, decode_control


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake = FakeHerdR(Path(self.temp.name))
        self.adapter = self.fake.adapter()

    def assert_error(self, code, operation, *, uncertain=None):
        with self.assertRaises(HerdrError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        if uncertain is not None:
            self.assertEqual(caught.exception.uncertain, uncertain)
        return caught.exception

    def test_unconfigured_live_contract_fails_without_starting_a_process(self):
        with patch("feishu_herdr_bridge.herdr.subprocess.run") as run:
            self.assert_error("unverified_contract", HerdrAdapter(SESSION).list_agents)
        run.assert_not_called()

    def test_injected_runner_and_builder_receive_explicit_session(self):
        commands = []

        def builder(session, args):
            self.assertEqual(session, SESSION)
            return ["/offline/fake", *args]

        def runner(command, timeout):
            commands.append((list(command), timeout))
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "contract": CONTRACT, "ok": True, "agents": [],
            }), "")

        adapter = HerdrAdapter(SESSION, command_builder=builder,
                               decoder=decode_control, runner=runner)
        self.assertEqual(adapter.list_agents(), ())
        self.assertEqual(commands, [(["/offline/fake", "agent", "list"], 5.0)])

    def test_confirmed_verbs_and_options_are_used_without_invented_flags(self):
        self.assertEqual(len(self.adapter.list_agents()), 2)
        self.assertEqual(self.adapter.get_agent("pane-a").workspace_id, "workspace-a")
        self.assertEqual(self.adapter.read_agent("pane-a"), "screen-A\n")
        self.adapter.prompt("pane-b", "任务\n第二行")
        self.assertEqual([event["argv"] for event in self.fake.events("call")], [
            ["agent", "list"],
            ["agent", "get", "pane-a"],
            ["agent", "read", "pane-a", "--source", "visible", "--lines", "80", "--format", "text"],
            ["agent", "prompt", "pane-b", "任务\n第二行"],
        ])
        self.assertTrue(all(event["session"] == SESSION for event in self.fake.events()))

    def test_unknown_session_never_falls_back_to_another_server(self):
        adapter = self.fake.adapter(session="not-the-configured-session")
        self.assert_error("target_missing", adapter.list_agents)
        self.assertEqual(self.fake.events("call")[0]["session"], "not-the-configured-session")

    def test_wrong_pane_in_get_response_is_rejected(self):
        self.fake.mode("get", "wrong_pane")
        self.assert_error("wrong_target", lambda: self.adapter.get_agent("pane-a"))

    def test_malformed_control_output_is_not_guessed(self):
        self.fake.mode("list", "malformed")
        self.assert_error("invalid_output", self.adapter.list_agents, uncertain=False)

    def test_duplicate_panes_in_list_are_rejected(self):
        state = self.fake.load()
        state["sessions"][SESSION]["agents"]["pane-b"]["pane_id"] = "pane-a"
        self.fake.save(state)
        self.assert_error("invalid_output", self.adapter.list_agents)

    def test_blocked_is_a_known_rejection_without_submission(self):
        self.fake.mode("prompt", "blocked")
        self.assert_error("blocked", lambda: self.adapter.prompt("pane-a", "task"), uncertain=False)
        self.assertEqual(self.fake.events("submitted"), [])

    def test_missing_agent_target_is_rejected(self):
        self.assert_error("target_missing", lambda: self.adapter.prompt("not-live", "task"))
        self.assertEqual(self.fake.events("submitted"), [])

    def test_invalid_targets_and_unsafe_text_never_reach_runner(self):
        for pane in ("", "--option", "pane a", "pane\x1b"):
            with self.subTest(pane=pane):
                self.assert_error("invalid_target", lambda: self.adapter.get_agent(pane))
        for text in ("", " ", "line\rrewind", "\x1b[31m", "bad\x00text", "\ud800"):
            with self.subTest(text=repr(text)):
                self.assert_error("invalid_input", lambda: self.adapter.prompt("pane-a", text))
        self.assert_error("leading_hyphen_unverified", lambda: self.adapter.prompt("pane-a", "--wait"))
        self.assertEqual(self.fake.events(), [])

    def test_timeout_kills_child_and_does_not_retry(self):
        self.fake.mode("prompt", "timeout_before")
        adapter = self.fake.adapter(write_timeout=0.3)
        children = []
        original = subprocess.Popen

        def capture(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child

        with patch("feishu_herdr_bridge.herdr.subprocess.Popen", side_effect=capture):
            self.assert_error("timeout", lambda: adapter.prompt("pane-a", "task"), uncertain=True)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertEqual(self.fake.events("submitted"), [])
        self.assertEqual(len(self.fake.events("call")), 1)

    def test_unparseable_result_after_submission_is_unknown(self):
        self.fake.mode("prompt", "malformed_after")
        self.assert_error("invalid_output", lambda: self.adapter.prompt("pane-a", "task"), uncertain=True)
        self.assertEqual(len(self.fake.events("submitted")), 1)

    def test_nonzero_without_recognized_rejection_is_unknown_for_prompt(self):
        self.fake.mode("prompt", "exit_error")
        error = self.assert_error("invalid_output", lambda: self.adapter.prompt("pane-a", "private prompt"), uncertain=True)
        self.assertNotIn("private", str(error))

    def test_missing_executable_is_a_known_non_submission(self):
        adapter = HerdrAdapter(SESSION, command_builder=lambda s, args: ["/missing/batch1-cli", *args],
                               decoder=decode_control)
        self.assert_error("cli_unavailable", lambda: adapter.prompt("pane-a", "task"), uncertain=False)

    def test_relative_executable_is_not_launched(self):
        adapter = HerdrAdapter(SESSION, command_builder=lambda s, args: ["herdr", *args],
                               decoder=decode_control)
        self.assert_error("invalid_command", adapter.list_agents)
        self.assertEqual(self.fake.events(), [])

    def test_runner_does_not_use_shell_or_pipe_input(self):
        result = subprocess.CompletedProcess(["/offline/fake"], 0, "ok", "")
        with patch("feishu_herdr_bridge.herdr.subprocess.run", return_value=result) as run:
            self.assertIs(run_command(["/offline/fake", "literal;$(x)"], 5), result)
        self.assertEqual(run.call_args.args[0], ["/offline/fake", "literal;$(x)"])
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_contradictory_success_and_error_exit_is_not_accepted(self):
        adapter = HerdrAdapter(
            SESSION, command_builder=self.fake.command,
            decoder=lambda action, output: ControlResult(),
            runner=lambda cmd, t: subprocess.CompletedProcess(cmd, 1, "{}", ""),
        )
        self.assert_error("invalid_output", lambda: adapter.prompt("pane-a", "task"), uncertain=True)


class Protocol22Tests(unittest.TestCase):
    def setUp(self):
        from feishu_herdr_bridge.herdr import decode_protocol22, session_command

        self.fixture = json.loads((Path(__file__).parent / "fixtures/herdr_0_9_0.json").read_text(encoding="utf-8"))
        self.calls = []

        def runner(command, timeout):
            self.calls.append(list(command))
            args = command[3:]
            action = "create" if args[:2] == ("workspace", "create") else args[1]
            output = self.fixture["read_text"] if action == "read" else json.dumps(self.fixture[action])
            return subprocess.CompletedProcess(command, 0, output, "")

        self.adapter = HerdrAdapter("explicit-session", command_builder=session_command("/configured/herdr"),
                                    decoder=decode_protocol22, runner=runner)

    def test_fixture_is_explicitly_static_not_live(self):
        self.assertEqual(self.fixture["_meta"]["protocol"], 22)
        self.assertEqual(self.fixture["_meta"]["source"], "schema-derived/static-not-live")
        self.assertEqual(self.fixture["_meta"]["live_probe"], "pending")

    def test_all_configured_commands_have_explicit_session_and_confirmed_options(self):
        self.assertEqual(self.adapter.list_agents()[0].kind, "codex")
        self.assertEqual(self.adapter.get_agent("pane-a").workspace_id, "workspace-a")
        self.assertEqual(self.adapter.read_agent("pane-a"), self.fixture["read_text"])
        self.adapter.prompt("pane-a", "中文\nsecond line")
        workspace = self.adapter.create_workspace("/allowed/project", "demo-static")
        self.assertEqual((workspace.workspace_id, workspace.tab_id, workspace.pane_id),
                         ("workspace-new", "tab-new", "pane-new"))
        self.adapter.start_agent("fb-codex-0123456789ab", "codex", "pane-new")
        prefix = ["/configured/herdr", "--session", "explicit-session"]
        self.assertEqual(self.calls, [
            prefix + ["agent", "list"],
            prefix + ["agent", "get", "pane-a"],
            prefix + ["agent", "read", "pane-a", "--source", "visible", "--lines", "80", "--format", "text"],
            prefix + ["agent", "prompt", "pane-a", "中文\nsecond line"],
            prefix + ["workspace", "create", "--cwd", "/allowed/project", "--label", "demo-static", "--no-focus"],
            prefix + ["agent", "start", "fb-codex-0123456789ab", "--kind", "codex", "--pane", "pane-new", "--timeout", "15000"],
        ])

    def test_optional_detection_fields_do_not_become_required_identity(self):
        agent = self.fixture["get"]["result"]["agent"]
        for key in ("name", "agent", "display_agent"):
            agent.pop(key, None)
        actual = self.adapter.get_agent("pane-a")
        self.assertIsNone(actual.name)
        self.assertEqual(actual.kind, "unknown")

    def test_missing_required_agent_info_field_is_rejected(self):
        del self.fixture["get"]["result"]["agent"]["workspace_id"]
        with self.assertRaises(HerdrError) as error:
            self.adapter.get_agent("pane-a")
        self.assertEqual(error.exception.code, "invalid_output")

    def test_unrecognized_result_shape_is_unknown_for_writes(self):
        self.fixture["create"]["result"]["root_pane"] = {"unverified_key": "pane"}
        with self.assertRaises(HerdrError) as error:
            self.adapter.create_workspace("/project", "label")
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_error_codes_are_not_guessed_from_human_messages(self):
        self.fixture["prompt"] = self.fixture["error"]
        self.fixture["prompt"]["error"]["message"] = "blocked name conflict SECRET"
        with self.assertRaises(HerdrError) as error:
            self.adapter.prompt("pane-a", "task")
        self.assertEqual(error.exception.code, "remote_error")
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(len(self.calls), 1)

    def test_cli_error_envelope_is_read_from_stderr(self):
        error_output = json.dumps(self.fixture["error"])

        def runner(command, timeout):
            self.calls.append(list(command))
            return subprocess.CompletedProcess(command, 1, "", error_output)

        self.adapter._runner = runner
        with self.assertRaises(HerdrError) as error:
            self.adapter.get_agent("pane-a")
        self.assertEqual(error.exception.code, "remote_error")
        self.assertFalse(error.exception.uncertain)
        self.assertEqual(len(self.calls), 1)

    def test_ambiguous_envelope_does_not_confirm_success(self):
        self.fixture["prompt"]["error"] = self.fixture["error"]["error"]
        with self.assertRaises(HerdrError) as error:
            self.adapter.prompt("pane-a", "task")
        self.assertTrue(error.exception.uncertain)

    def test_invalid_names_kinds_and_relative_paths_do_not_call_runner(self):
        for name, kind in (("UPPER", "codex"), ("a" * 33, "codex"), ("a", "shell")):
            with self.assertRaises(HerdrError):
                self.adapter.start_agent(name, kind, "pane-new")
        with self.assertRaises(HerdrError):
            self.adapter.create_workspace("relative", "label")
        self.assertEqual(self.calls, [])
