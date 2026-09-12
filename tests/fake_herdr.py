"""Offline subprocess fake. ALL JSON shapes here are synthetic, not live evidence.

Launcher arguments are: python -S /absolute/fake_herdr.py STATE SESSION <CLI args>.
STATE and SESSION belong only to this fake launcher, not to HerdR's CLI.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


CONTRACT = "synthetic-batch1-v1"
SESSION = "test-session"


def decode_control(action, stdout):
    from feishu_herdr_bridge.herdr import Agent, ControlResult

    obj = json.loads(stdout)
    if obj.get("contract") != CONTRACT or type(obj.get("ok")) is not bool:
        raise ValueError("Not a synthetic test response")
    if not obj["ok"]:
        return ControlResult(error=obj["error"])
    if action in {"list", "get"}:
        return ControlResult(agents=tuple(Agent(**item) for item in obj["agents"]))
    if action == "workspace_list":
        return ControlResult(workspace_labels=tuple(
            (item["workspace_id"], item["label"]) for item in obj["workspaces"]
        ))
    if action == "tab_list":
        return ControlResult(tab_labels=tuple(
            (item["tab_id"], item["label"]) for item in obj["tabs"]
        ))
    return ControlResult()


class FakeHerdR:
    def __init__(self, root: Path):
        self.path = root / "fake-state.json"
        self.log_path = root / "fake-calls.jsonl"
        self.save({
            "sessions": {SESSION: {
                "workspaces": {
                    "workspace-a": "Project Alpha",
                    "workspace-b": "项目乙",
                },
                "tabs": {"tab-a": "主控", "tab-b": "Review B"},
                "agents": {
                    "pane-a": {"workspace_id": "workspace-a", "pane_id": "pane-a",
                               "tab_id": "tab-a", "name": "lead-a", "kind": "codex", "status": "idle"},
                    "pane-b": {"workspace_id": "workspace-b", "pane_id": "pane-b",
                               "tab_id": "tab-b", "name": "lead-b", "kind": "claude", "status": "idle"},
                },
                "screens": {"pane-a": "screen-A\n", "pane-b": "screen-B\n"},
            }},
            "behavior": {},
        })

    def load(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state):
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def mode(self, action, mode):
        state = self.load()
        state["behavior"][action] = mode
        self.save(state)

    def command(self, session, args):
        return (sys.executable, "-S", str(Path(__file__).resolve()), str(self.path), session, *args)

    def adapter(self, **kwargs):
        from feishu_herdr_bridge.herdr import HerdrAdapter

        return HerdrAdapter(
            session=kwargs.pop("session", SESSION), command_builder=self.command,
            decoder=decode_control, **kwargs,
        )

    def events(self, event=None):
        if not self.log_path.exists():
            return []
        rows = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if event is None or row["event"] == event]


def main() -> int:
    state_path, session, *args = sys.argv[1:]
    path = Path(state_path)
    state = json.loads(path.read_text(encoding="utf-8"))
    log = path.with_name("fake-calls.jsonl")

    def record(event, **fields):
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "session": session, **fields}) + "\n")

    def output(ok=True, **fields):
        print(json.dumps({"contract": CONTRACT, "ok": ok, **fields}))

    record("call", argv=args)
    if args == ["workspace", "list"]:
        data = state["sessions"].get(session)
        if data is None:
            output(False, error="target_missing")
            return 2
        output(workspaces=[{"workspace_id": key, "label": value}
                           for key, value in data["workspaces"].items()])
        return 0
    if args == ["tab", "list"]:
        data = state["sessions"].get(session)
        if data is None:
            output(False, error="target_missing")
            return 2
        output(tabs=[{"tab_id": key, "label": value}
                     for key, value in data["tabs"].items()])
        return 0
    if len(args) < 2 or args[0] != "agent":
        output(False, error="rejected")
        return 2
    action = args[1]
    mode = state["behavior"].get(action)
    if mode == "timeout_before":
        time.sleep(2)
    if mode == "malformed":
        print("not a control JSON object")
        return 0
    if mode == "exit_error":
        print("private stderr detail", file=sys.stderr)
        return 7
    data = state["sessions"].get(session)
    if data is None:
        output(False, error="target_missing")
        return 2
    if args == ["agent", "list"]:
        output(agents=list(data["agents"].values()))
        return 0
    if len(args) < 3 or args[2] not in data["agents"]:
        output(False, error="target_missing")
        return 2
    pane_id = args[2]
    agent = data["agents"][pane_id]
    if action == "get" and len(args) == 3:
        if mode == "wrong_pane":
            agent = {**agent, "pane_id": "unrequested-pane"}
        if mode == "wrong_workspace":
            agent = {**agent, "workspace_id": "unrequested-workspace"}
        output(agents=[agent])
        return 0
    if action == "read" and args[3:] == ["--source", "visible", "--lines", "80", "--format", "text"]:
        # Returning more than requested also exercises the bridge's own cap.
        print(data["screens"].get(pane_id, ""), end="")
        return 0
    if action == "prompt" and len(args) == 4:
        if mode == "target_gone":
            output(False, error="target_missing")
            return 2
        if agent["status"] == "blocked" or mode == "blocked":
            output(False, error="blocked")
            return 3
        record("submitted", workspace_id=agent["workspace_id"], pane_id=pane_id, text=args[3])
        if mode == "timeout_after":
            time.sleep(2)
        if mode == "malformed_after":
            print("cannot confirm result")
            return 0
        output()
        return 0
    output(False, error="rejected")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
