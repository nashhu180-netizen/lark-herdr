<!-- dh:v1 · task_plan.md（轻档·派 worker）— 施工说明书。一次性消耗品：跑偏记 progress.md，不回改本文件。 -->
# task_plan — ISSUE24 Devin 工作中追加消息的自动回传

## 要读的上下文 (Context Packet) ★前置

> **执行契约头（zero-context）**：你（执行 worker）默认只知道本文件 + 同目录 `task.md` + 下列来源。本 pane cwd = worktree `/home/nash/work/lark-herdr-o24`，分支 `fix/devin-working-baseline-retry`（= PR #25）。live 服务跑在主树 `/home/nash/work/lark-herdr`（main@04f1580），**主树只读、不动、不重启服务**。

| ID | 来源 (path) | 为什么 |
|----|------------|--------|
| C-001 | `docs/auto-pane-output-design.md` | 自动回传合同：§"同群已有观察时…取消旧观察"（约 L41）、working 态规则（L67/L101）、发送守卫（L91）。改法不得越出合同。 |
| C-002 | `feishu_herdr_bridge/output.py` `capture()` L431–470、`arm()`、观察循环 `_observe*` L495–600、`_frame()`/Devin 布局解析 L90–130、`_close/cancel` L400–420 | 失效路径全在这里。 |
| C-003 | `feishu_herdr_bridge/core.py` L225–245 | 提交成功后 `output.arm(self._output_capture)`；capture 返回 None → 回复追加"本次自动回传未启用，可用 /read。"；finally 里 cancel 旧 capture。 |
| C-004 | GitHub Issue #24 正文 + PR #25 diff（`git show 83c4302`） | 昨夜 GPT 的假设：首次基线读撕裂 → 对 devin+working 重试一次基线读。你要验证这个假设是否覆盖现场。 |
| C-005 | 现场日志：`journalctl --user -u feishu-herdr-bridge -S "2026-09-13 00:00" -U "2026-09-13 00:06" --no-pager` | 00:01:50 armed(submitted) → 00:02:04 closed(cancelled) → 00:02:05 回复；00:04:22 armed → 00:04:35 closed(cancelled)。两次都没有第二个 watch armed。**注意 cancelled 是被下一条提示取消，还是被 /read 取消，要分清（core.py `cancel_current` 只在 /read 前调）。** |
| C-006 | `tests/test_output.py`：`Rig`、`devin_screen()`、`DEVIN_HISTORY`、PR #25 新增的 `test_working_devin_retries_one_torn_baseline_without_guessing` | 测试样板；新测试照这个写。 |
| C-007 | `docs/runbook.md` §2.2、§4；`docs/devin-agent-plan.md` §4 | 部署/真实验收只能由用户批准的维护窗口执行；worktree 内不得指向 live 数据库。 |
| C-008 | 主树 `git log --oneline 64f685e^..04f1580` | 最近 20 个 Devin 相关修复的脉络（working 态放行 armed、原生队列不动、Did-you-know tip 排除）。 |

## 权威源铁律（必守）

1. 只改 `feishu_herdr_bridge/output.py`、`tests/test_output.py`、`tests/fixtures/**`、本工作区 `docs/workspace/01-ISSUE24-*/**`；其它文件范围外 → 写 `orchestration/decisions.md` 停下。
2. 禁：sleep/轮询延时、send-keys/按键、提示重放、通用屏幕 diff、原始输出直转、放宽守卫（授权/绑定/请求核对）、猜测状态。读错误、两帧无效、目标不匹配一律 fail-closed。
3. 测试用本 worktree 自己的 `.venv`；先 `.venv/bin/python -c "import feishu_herdr_bridge;print(feishu_herdr_bridge.__file__)"` 确认指向本 worktree。
4. 允许 `git commit`（按路径精确 `git add`，禁 `-A/.`）并 `git push origin fix/devin-working-baseline-retry`；**禁 merge、禁动 main、禁 systemctl、禁改 `~/.config/feishu-herdr-bridge`**。
5. 每批结束：跑本批验证 → `progress.md` 记证据 → 更新 `orchestration/state.json`（该批 `status` + `last_change` 一句话）→ **停下等主控**。

---

## 施工步骤 (Steps) — worker 粒度

### B1 根因确认（不改生产代码）

| # | 改动文件 | 怎么做 | 怎么验 |
|---|---|---|---|
| 1.1 | Read · C-001~C-005 | 画出"群内 Pane 正在 working，用户发第二条文本"的完整调用链：core 取锁 → cancel 旧 watch → `herdr agent prompt` 提交成功 → `output.arm()` → `capture()`：`agent get` 状态、`_read` 画面、`_frame()` 解析、`_matches/_allowed`。逐个分支写出"返回 None 的条件"。 | `findings.md` F-001 列出 capture 返回 None 的每个条件与对应日志/代码行。 |
| 1.2 | Test · `tests/test_output.py` 新增 `test_live_issue24_second_prompt_during_working_turn_arms` | 用 `devin_screen()` 构造**真实 live 形态**的 working 帧：含 `Running tools`、bypass 规则行、`Guide Devin while it works`、状态/活动行，**并包含原生队列行（第二条提示已排入 Devin 队列的显示形态，参考 fix/devin-guidance-queue 分支的 fixture）**。第一次 Rig：先 arm 一轮并 cancel（模拟旧 watch 被取消），再对同 chat 立刻 capture+arm。断言 watch 不为 None。 | `.venv/bin/python -m unittest tests.test_output -k issue24 -v` → 在 PR #25 代码上**先看结果**：红 = PR #25 不够；绿 = 换用"首帧撕裂"以外的现场变量再试（见 1.3）。把结果原样贴 progress。 |
| 1.3 | Test · 同上，变体 | 若 1.2 绿，逐个加入现场变量再试：(a) `agent get` 返回 `working` 但画面首帧是 Devin 排队后重绘中的帧（两帧都含队列行）；(b) 旧 watch 处于 `watching` 且已记录 `_prompt` 时被取消后 `_active` 残留；(c) 第二条提示与第一条正文相同前缀。每个变体一个 subTest。 | 至少找到一个红变体，或证明全部绿并在 findings 写明"离线不可复现，需要 live 帧"。 |
| 1.4 | Record · `findings.md` F-001 | 结论三选一：PR #25 足够 / PR #25 不够且根因是 X / 离线不可复现。附红测试输出。 | `orchestration/state.json` B1=done，`last_change` 写结论一句话。**停下。** |

### B2 修复 + 测试（B1 判定后由主控派发）

| # | 改动文件 | 怎么做 | 怎么验 |
|---|---|---|---|
| 2.1 | Modify · `feishu_herdr_bridge/output.py` | 只在 `capture()`/`_frame()`/Devin 解析里做最小改动让 B1 红测试转绿。若根因是队列行未被 Devin 解析器识别 → 在解析器**白名单精确匹配**该行形态（不做通配）；若根因是取消后残留状态 → 在 `_close()` 清理对应字段。保留"至多两次基线读，仅 devin+working"。 | `.venv/bin/python -m unittest tests.test_output -v` → OK。 |
| 2.2 | Test · 反向用例 | 加"两帧都无效 → capture 返回 None、calls 为 get,read,read"与"非 devin 或非 working 仍单读"断言（若 PR #25 已有则复用）。 | 同上 OK。 |
| 2.3 | Verify · 全量 | `env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests` → `OK`（≥298 tests）；`.venv/bin/python -m compileall -q feishu_herdr_bridge tests`；`git diff --check`。 | 三条输出贴 progress。 |
| 2.4 | Record · commit+push | `git add feishu_herdr_bridge/output.py tests/test_output.py docs/workspace/01-ISSUE24-devin-queue-auto-return`；`git commit -m "fix: <一句话>"`；`git push origin fix/devin-working-baseline-retry`；在 PR #25 评论里贴 B1 结论与测试计数（`gh pr comment 25 --body-file <文件>`）。 | state.json B2=done，`last_change` 含 commit hash。**停下。** |

### B3 审核 / B4 复核（由审核 pane、复核 pane 执行，见 `orchestration/prompts/`）

- 审核报告 → `orchestration/reviews/audit-B2.md`；`changes-requested` 时主控派回执行 pane 走 `rework-N`。
- 复核报告 → `orchestration/reviews/round2.md`；approved 后主控向用户申请合入 + 维护窗口。

### B5 部署与真实验收（用户 2026-09-13 已授权主控在 B4 approved 且 PR 合入后执行）

1. runbook §2.2 停服备份 → 主树 `git pull --ff-only` 到合入后的 main → `systemctl --user restart` → `is-active`。
2. 真实验收：在已绑定 Devin Pane 的测试群里发 `sleep 20`，working 期间再发第二条文本；预期第二条也得到自动回传（不是"本次自动回传未启用"）。同时 `journalctl` 里两条各自有 `armed → sent`。
3. 结果写 `docs/live-validation.md` 追加段落（脱敏）。

## 关键决策（一句话各一行）

- Worktree：是，`/home/nash/work/lark-herdr-o24`，分支 = `fix/devin-working-baseline-retry`（PR #25）
- 派子 agent：五角色独立 pane（见 `orchestration/README.md`）
- 部署闸：用户已授权（2026-09-13）；B4 approved → 合 PR → 主控按 runbook §2.2 部署 → 真实验收
