<!-- task.md — 轻档（一件套）。维护任务：修 bug。 -->
# task — ISSUE24 飞书→Devin Pane 工作中追加消息时自动回传失效（"本次自动回传未启用"）

> 执行者：headless worker
> 户口：GitHub Issue #24（候选补丁 PR #25 / 分支 `fix/devin-working-baseline-retry`，未审、未合、未部署）。live 服务运行 `main@04f1580`。

## 完成条件 ★前置

1. **根因有证据**：对 2026-09-13 00:01:50 / 00:04:22 两次 `armed → closed reason=cancelled` 且无新 watch 的现场，给出代码级路径说明，并用离线测试（真实 Devin 画面 fixture）复现为红。
2. **修复在合同内**：修复后该场景转绿；全量离线套件 `unittest discover` 全绿、`compileall` 通过、`git diff --check` 干净；不引入 sleep、按键事件、提示重放、通用屏幕 diff、原始输出直转（Issue #24 最小验收原文）。
3. **独立审核 + fresh 复核均 approved**，PR #25（或其替代 PR）可合入；部署与真实验收由主控在合入后按 runbook §2.2 执行（用户 2026-09-13 授权）。

## 施工步骤 ★精简（详见同目录 `task_plan.md`）

1. B1 根因确认：读 output.py/core.py + 日志证据，判定 PR #25 假设（首帧撕裂）是否成立，findings 落 F-001。
2. B2 修复+测试：钉住 live 场景红测试 → 最小实现 → 全量绿 → 提交推送到 PR 分支。
3. B3 审核（gpt-5.6-sol）→ 整改 → B4 fresh 复核（devin swe-2-max）→ 合入决定。
4. B5（用户批准后）停服备份部署 + 真实验收记录。

## 进度 + 证据（边做边记）

见 `progress.md`。

## 验收

- AI 自评 + 审核/复核报告（`orchestration/reviews/`）+ 用户口头确认；真实验收单独记录。
