#!/usr/bin/env python3
"""B14 rework1 receipt — deterministic replay of the queued-send chain.

Re-runnable proof of the reworked chain
  bound → submitted/armed → working → queued → edge/observed →
  closed/submitted → armed → attempted/body → sent → closed/sent
using only in-repo objects: real BridgeCore, real Store (SQLite file), real
SendQueue with the real audit logger, and the real OutputObserver stepped by
hand through tick(). HerdR is the tests' subprocess fake (screens are the
minimal synthetic Devin frames from tests.test_output); the Feishu outbound is
a stub that records receipts. Fake clocks throughout — no sleeps, no live pane.

Run:
    /home/nash/work/lark-herdr-o34/.venv/bin/python \
        /home/nash/work/lark-herdr-o24/docs/workspace/\
01-ISSUE24-devin-queue-auto-return/orchestration/receipts/b14_rework1_receipt.py \
        <outdir>
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import threading
from pathlib import Path

O34 = Path("/home/nash/work/lark-herdr-o34")
sys.path.insert(0, str(O34))

from feishu_herdr_bridge.core import BridgeCore, Message, connect_send_queue  # noqa: E402
from feishu_herdr_bridge.output import connect_output  # noqa: E402
from feishu_herdr_bridge.store import Store  # noqa: E402
from tests.fake_herdr import FakeHerdR  # noqa: E402
from tests.test_output import DEVIN_HISTORY, devin_screen  # noqa: E402

PANE, CHAT = "pane-a", "chat-b14r"
P1 = "请只执行 sleep 30 这一条命令，不要做别的"
P2 = "只回复 PONG-K"


def main() -> int:
    outdir = Path(sys.argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = outdir / "bridge.log"
    logging.basicConfig(filename=str(log_path), level=logging.INFO, force=True,
                        format="%(asctime)s %(name)s %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S")

    fake = FakeHerdR(outdir)
    state = fake.load()
    session = state["sessions"].pop("test-session")
    session["agents"][PANE]["kind"] = "devin"
    state["sessions"]["kpi-agg"] = session
    fake.save(state)

    def set_status(status):
        data = fake.load()
        data["sessions"]["kpi-agg"]["agents"][PANE]["status"] = status
        fake.save(data)

    def set_screen(text):
        data = fake.load()
        data["sessions"]["kpi-agg"]["screens"][PANE] = text
        fake.save(data)

    wall, mono = [1751500000.0], [5000.0]  # Fixed clocks: receipt is reproducible.
    adapter = fake.adapter(session="kpi-agg")
    store = Store(outdir / "receipt.sqlite3")
    core = BridgeCore(store, adapter, allowed_chats={CHAT}, allowed_users={"user"},
                      clock=lambda: wall[0], operation_lock=threading.Lock(),
                      bot_open_id="bot-x")
    receipts = []

    def send(origin, text):
        receipts.append(f"wall={wall[0]:.0f} SEND→{origin.chat_id} {text!r}")
        return "sent"

    queue = connect_send_queue(core, adapter, send, clock=lambda: mono[0],
                               interval=2.0, timeout=600.0, edge_cap=15.0)
    observer = connect_output(core, adapter, send, clock=lambda: mono[0])

    seq = [0]

    def handle(text, group=True):
        seq[0] += 1
        wall[0] += 1
        reply = core.handle(Message(f"m-{seq[0]}", CHAT, "user", text, wall[0],
                                    "group" if group else None))
        print(f"wall={wall[0]:.0f} handle {text[:20]!r} → {reply.status}/{reply.code} "
              f"{reply.text!r}")
        return reply

    # Baseline frame: pane idle with earlier history only.
    set_screen(devin_screen(DEVIN_HISTORY))
    set_status("idle")

    handle("/bind workspace-a pane-a", group=False)
    handle(P1)                                   # Immediate path: submitted.
    set_status("working")                        # HerdR flips after the send.
    handle(P2)                                   # Busy: queued, not sent.

    mono[0] += 3.0
    print(f"mono={mono[0]:.0f} poll() → {queue.poll()} (pane working: "
          "busy edge observed, head kept)")
    set_status("done")                           # Pane finishes round one.
    # Idle baseline for P2's deferred capture (P1 transcript, no P2 echo yet).
    set_screen(devin_screen(
        DEVIN_HISTORY + ["", "❭ " + P1, "", " Done — sleep 30 completed."]))
    mono[0] += 3.0
    print(f"mono={mono[0]:.0f} poll() → {queue.poll()} (gate open: send, arm)")
    # Pane renders P2's echo and answer.
    set_screen(devin_screen(
        DEVIN_HISTORY + ["", "❭ " + P1, "", " Done — sleep 30 completed.",
                         "", "❭ " + P2, "", " PONG-K"]))
    mono[0] += 3.0
    print(f"mono={mono[0]:.0f} tick() → {observer.tick()} (first stable candidate)")
    mono[0] += 3.0
    print(f"mono={mono[0]:.0f} tick() → {observer.tick()} (stability met: deliver)")

    print("\n--- receipts (stubbed Feishu outbound) ---")
    print("\n".join(receipts))
    print("\n--- send-queue + output audit lines (bridge.log) ---")
    for line in log_path.read_text().splitlines():
        if "send-queue" in line or "output " in line:
            print(line)
    print("\n--- SQLite requests (receipt.sqlite3) ---")
    con = sqlite3.connect(outdir / "receipt.sqlite3")
    for row in con.execute(
            "SELECT message_id, action, status, result_code, updated_at "
            "FROM requests ORDER BY created_at"):
        print(row)
    print("\n--- SQLite bindings ---")
    for row in con.execute("SELECT chat_id, workspace_id, pane_id, revision, valid "
                           "FROM bindings"):
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
