# docs/ 入口索引

> 每份文档一行：用途 / 权威等级 / 修订时间 / 是否已实测。新增、退役、挪位文档时同批更新本表，并在仓根 `authority-docs.manifest.json` 登记或销户。

| 文档 | 用途 | 权威等级 | 修订时间 | 已实测 |
|---|---|---|---|---|
| `design.md` | 核心 MVP 权威设计（含 Issue #3 自助建群增量） | 权威设计基线 | 2026-09-11 | 是（Linux 真实冒烟；Issue #3 增量仅本地审核） |
| `auto-pane-output-design.md` | Issue #7 自动 Pane 回传设计契约（Rev2） | 增量设计契约 | 2026-09-12 | 否（PR #25 在途、未部署未验收） |
| `devin-agent-design.md` | Issue #5 Devin 支持设计（三类 allowlist + schema v2） | 增量设计契约 | 2026-09-11 | 实现已合入；文档自身未声明验收，真实验收记录未见 |
| `runbook.md` | 运维/部署手册（§2.2 为部署唯一入口、§6 回传验收交接） | 运维手册 | 2026-09-12 | §6 对应实现未部署未验收；真实 Devin 布局识别未验收 |
| `live-validation.md` | 核心 MVP 真实验收记录 | 验收证据（不可改写） | 2026-09-11 | 本身即验收记录 |
| `self-service-groups-validation.md` | Issue #3 自助建群真实验收记录 | 验收证据（不可改写） | 2026-09-11 | 本身即验收记录（`LIVE_CORE_ACCEPTED`） |
| `development-plan.md` | MVP 实施计划（历史档案） | 历史计划 | 2026-09-11 | 其产物已实测（见 live-validation.md） |
| `devin-agent-plan.md` | Issue #5 两批实施计划 | 批次计划 | 2026-09-11 | 计划非被测物 |
| `auto-pane-output-plan.md` | Issue #7 三批实施计划 | 批次计划 | 2026-09-12 | 计划非被测物 |
| `self-service-groups-plan.md` | Issue #3 批次实施计划 | 批次计划 | 2026-09-11 | 计划非被测物 |
| `dev-plan.md` | DevPlan 任务户口本 | 户口登记 | 2026-09-13 | n/a（结构性工件） |

## 权威文档看护

仓根 `authority-docs.manifest.json` 按 dev-harness `DH_54`~`DH_57` 约定登记：`watched` 条目把文档钉在其描述的代码路径上，`last_verified` 取该文档最后一次被人工修订的提交（首登以真实核验点为基线，存量欠账交给 `dh docs` 如实产出，不做橡皮图章）；`exempt` 条目为证据快照 / 批次计划 / 户口与索引类工件。

## 挪位提案（未执行，待裁决）

1. `dh-check` R5 按 `设计文档.md` 中文文件名找正式设计输入；本仓权威设计为 `design.md`，命名不一致。选项：改名承接（会断外部引用）、在 manifest/配置登记豁免、或维持警告不处理。倾向：维持现状并将该警告作为已知噪音记录。
2. `docs/workspace/<批次>/` 编排工件（task/progress/findings/reviews 等）若随 Issue #24 分支合入 main，会落入 `document_roots: ["docs"]` 扫描面；`exclude` 只支持逐条精确路径不支持 glob，届时需批量登记或调整目录组织。
