# orchestration — ISSUE24 多 pane 编排协议

> 用户 2026-09-13 指定：主控（Claude Fable，本 pane）只编排分发；五角色独立 pane，全部为 herdr workspace `w1V` 的标签页。

## 角色与 pane

| 角色 | pane | agent 名 | 启动 | 模型 |
|---|---|---|---|---|
| 编排（主控） | `w1V:p1` | 本会话 | — | claude fable |
| 执行 | `w1V:p2` | `o24-exec` | `devin --model swe-2-max --permission-mode dangerous` | swe-2-max |
| 审核 | `w1V:p3` | `o24-audit` | `codex -m gpt-5.6-sol -c model_reasoning_effort=medium`（不加 -s/-a，沿用用户 config 的 danger-full-access） | gpt-5.6-sol medium |
| 决策 | `w1V:p4` | `o24-decide` | `codex -m gpt-6-astra -c model_reasoning_effort=low`（同上，无沙盒） | gpt-6-astra low |
| 复核 | `w1V:p5` | `o24-review2` | `devin --model swe-2-max --permission-mode dangerous` | swe-2-max（fresh，dangerous） |
| 监督 | `w1V:p6` | `o24-monitor` | `devin --model swe-2-medium --permission-mode dangerous` | swe-2-medium |

worktree = `/home/nash/work/lark-herdr-o24`，branch = `fix/devin-working-baseline-retry`（PR #25）。live 主树 `/home/nash/work/lark-herdr` 只读。

## 状态机（state.json）

- `current_batch`：B1 / B2 / B3 / rework-N / B4 / B5 / blocked-*
- `batches.<id>.status`：`pending → dispatched → working → done | needs-decision | blocked`
- 执行 pane 每批收尾必须更新 state.json（status + `last_change`）；监督 pane 只读它 + 可写 `heartbeat`。

## 分发协议

1. 主控 `herdr agent prompt o24-exec "执行 task_plan B<n>"`。
2. 执行完工 → 更新 state.json → 监督提醒主控 → 主控派审核：`o24-audit "审查 <old>..HEAD 与 B<n> 证据，报告写 orchestration/reviews/audit-B<n>.md"`。
3. 小决策 → 主控派决策 pane 裁；方向性（部署、改合同）→ 主控问用户。
4. 审核 approved → 主控派复核 `o24-review2`（fresh）→ approved → 主控向用户申请合入与维护窗口 → B5。

## 监督回报协议

监督 pane 约每 2 分钟一轮：`herdr agent wait o24-exec --until idle,done,blocked --timeout 120000` + 读 state.json + `herdr agent list`；有变化 → `herdr agent prompt w1V:p1 "<一行汇报>"`（这条 prompt 就是对主控的 enter 提醒）。
