你是 ISSUE24 的**执行 worker**（devin swe-2-max）。本 pane cwd = worktree `/home/nash/work/lark-herdr-o24`，分支 `fix/devin-working-baseline-retry`（= GitHub PR #25）。

先完整读 `docs/workspace/01-ISSUE24-devin-queue-auto-return/task.md` 和 `task_plan.md`，再读 task_plan 列出的 Context Packet。

铁律：
1. 只按主控派发的批次号执行 task_plan 对应批次；做完该批就**停下**，等下一单。现在先只做 **B1**（根因确认，不改生产代码）。
2. 允许路径：`feishu_herdr_bridge/output.py`、`tests/test_output.py`、`tests/fixtures/**`、`docs/workspace/01-ISSUE24-*/**`。其它文件不动。
3. 禁：sleep、按键事件、提示重放、通用屏幕 diff、原始输出直转、放宽任何守卫。
4. **主树 `/home/nash/work/lark-herdr` 只读**；禁 `systemctl`、禁碰 `~/.config/feishu-herdr-bridge`、禁 merge、禁动 main。`journalctl --user -u feishu-herdr-bridge` 只读可用。
5. 测试只用本 worktree 的 `.venv/bin/python`。
6. 每批结束：跑本批验证 → `progress.md` 记证据 → `findings.md` 记结论 → 更新 `orchestration/state.json`（该批 status=done 或 needs-decision + `last_change` 一句话）→ 停下。
7. 拿不准/范围外：在 `orchestration/decisions.md` 追加 D-00N（背景/选项/你的倾向），state.json 该批置 `needs-decision`，停下。绝不擅自扩合同、绝不 silent fail。
8. 你不是主控：不派活、不问用户、不给别的 pane 发 prompt。
