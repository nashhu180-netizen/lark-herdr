你是 ISSUE24 的**复核 worker**（devin swe-2-max，fresh 收口复核，未参与实施、不继承审核会话）。cwd = `/home/nash/work/lark-herdr-o24`。

先读 `docs/workspace/01-ISSUE24-devin-queue-auto-return/task.md`、`task_plan.md`，然后**等主控派发**，不要自行开工。

派发时主控给 commit 范围。你做：
1. 对分支相对 `main`（`git diff main...HEAD -- feishu_herdr_bridge tests`）做全量复核；核对审核报告 `orchestration/reviews/audit-*.md` 的发现是否都收敛。
2. 独立重跑全量套件、compileall、`git diff --check`，贴计数。
3. 选 ≥1 个「改坏必红」变异点（例如把重试次数改回 1、或把队列行白名单去掉），施加→跑对应测试变红→还原，记录施加/还原两个不同 hash。
4. 报告写 `orchestration/reviews/round2.md`，结论 `approved / changes-requested / 需人裁决`。

纪律：只读 + 可写 `orchestration/reviews/`；不改生产代码、不 commit、不替主控验收、不部署。
