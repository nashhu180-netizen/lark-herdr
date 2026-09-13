# AGENTS.md — lark-herdr · 跨 agent 唯一权威入口

> 所有 coding agent（Claude / Codex / Devin / …）进本仓库的**唯一权威入口**。`CLAUDE.md` 只是 Claude 兼容壳。
> 方法论全文在 dev-harness skill（按下方矩阵触发读）；本文件**只放宪章 + 索引**，不抄方法论、不堆事实。
> 写法见 dev-harness `references/AGENTS-CLAUDE-写法.md`：规则只住本文件，不写"谁没装 skill"豁免话术。

## 项目概况

- 技术栈：Python 3.11+；唯一直接依赖 `lark-oapi==1.7.3`（固定版本），其余标准库（sqlite3 / subprocess / threading / logging）。
- 仓库形态：单仓；`feishu_herdr_bridge/` 源码、`tests/` 离线测试、`docs/` 权威文档、`deploy/` systemd 模板、`examples/` 配置样例。
- 默认分支：`main`；live 服务从已验收的 `main` 运行，开发一律在隔离 worktree + 独立 venv + 临时 DB。
- dev-harness 落点：任务户口本 `docs/dev-plan.md`（唯一发任务 ID 处，沿用 GitHub Issue/PR 号）；工作区 `docs/workspace/<批次>/`；verify scope 用 DevPlan 登记的英文 slug。

## 不可违反的硬规则（宪章）

> 与 dev-harness SKILL §硬规则同源；这里是常驻每轮的最小子集。少而硬。

1. **【入口闸】** 非平凡任务**动代码前先落户**：先按 dev-harness D 动作分流给出档位 / 目标 / 范围 / 验收 / 落点（`docs/dev-plan.md` 登记 + `docs/workspace/<批次>/` 建工作区）。**标准档 · 高危必须用户在对话里明确确认后才开工**；用户说"做一下 / 改一下"只是提需求、不等于授权开工。**禁止先改代码、后补工件**；已发生必须标"**跳步补录**"，不得伪装成正常流程。
2. **【出口闸】** 高危五类（生产接入 / 迁移 / 上线 / 组件接线 / 数据口径）标"完成"前必须有 `verify(<slug>):` 提交；无 verify 只能"待验收"。Evidence 必须可一键复跑。
3. **【自动收口闸】** 标准档开发完成 / 代码提交后直接进入代码复核、需求复核、教训复核、check、交付汇报与人验证据展示备料；**不得以"已提交/测试通过/待复核"作为最终答复**。"待复核 / 待收口"只能描述中间状态，不能作为停工理由。
4. **【需求对齐闸】** 标准档进"待验收"前必须有「需求对齐证据」（需求/人验项 + 场景操作路径 + 证据 ID + 结论）；本仓的人验主要是 live 冒烟/验收，离线 fake 测试不替代真实证据。
5. **【确认闸】** 没有用户对话里的明确确认（点选或明文），AI 不得代签 verify、不得勾人类签名区、不得部署/重启 live 服务（文档勾选不算）。
6. **【复核闸】** 标准档进"待验收"前需两轮独立换人复核（第一轮全面排查 + 第二轮另派 fresh-context、未参与实施且不继承第一轮会话上下文的独立实例，可只读仓内已落账的第一轮记录）；模型/账号可相同，不得复用同一会话冒充两轮。这是放行条件，不是暂停点。
7. **【密钥红线】** 密钥 / 凭据值只允许存在于仓库外 `~/.config/feishu-herdr-bridge/`（`config.json` / `credentials.env`，0600）；永不进入 findings / progress / 设计文档 / 测试 / 日志 / commit message；不 `cat`、不贴日志、不带外复述。
8. **【遗留闸】** 问题遗留必须用户点头 + 标去处（findings 状态列写 `遗留→<去处>（已确认）`），AI 不得自行把 open 问题划给下一个计划。
9. **【决策记录闸】** 一次活动（摸排 / 体检 / 调研 / 复盘）产生 ≥2 个发现、且要写入权威工件（DevPlan / 设计文档 / backlog / 验收池 / 教训库）时，写入前必须先有决策记录；纯长知识只落 `knowledge/` 不受此闸。
10. **【部署闸 · 项目专属】** live 服务升级只在用户批准的维护窗口进行：先按 `docs/runbook.md` §2.2 停服、备份再更新部署；不并行启动第二个消费者；候选环境不得指向 live 数据库作探针；`systemctl` 与 `~/.config/feishu-herdr-bridge/` 只在运维窗口内按 runbook 触碰。
11. **【HerdR 边界闸 · 项目专属】** 无 `HERDR_ENV=1` 的进程不得读取或控制现有 session，不伪造该变量；自动回传/输出观察一律被动 `agent get/read`，不向 pane 发按键或输入操纵；离线测试只用 fake CLI / 临时 DB / 隔离 worktree。

## 回合收尾契约

- 阶段边界（一批步骤完 / 一道闸过 / 收口前）必须给七段汇报，段首用 ①~⑦ 编号锚点（骨架见 dev-harness `references/verify-代签与汇报.md`）。
- 汇报后在工作区 `progress.md` 记机读足迹：`阶段汇报@<批次/节点>`。
- 每次收尾必带「项目摘要一句」+「下一步一句」；无下一步固定写「无，等你验收」。

## Quick-Path 路由（开工前对号入座）

> 与 dev-harness SKILL 主干 Quick-Path 同源；「先读」列路径相对 dev-harness skill 根，不在本仓内。

<!-- dh:quickpath:start -->
**动手前先认这条路**——dev-harness 里干活先立项 / 建工作区，不"先码后补"（见「硬规则·入口闸」）。用户说的话对号入座，走对那一条：

| 用户说的话 | 走哪条动作 | 先读 | 产出 |
|---|---|---|---|
| 立项 / 起个方案 / 这个需求… | **A 立项** | `references/动作-A-立项.md` + `references/查漏清单.md` | `design/README.md` 入口 + 同级正式设计输入 |
| 摸排 / 体检 / 调研 / 对标 / 复盘 / 用户反馈整理完，产出要写权威工件 | **A 立项·增补型** | `references/动作-A-立项.md`（A′ 增补型 + 交叉审核） | `records/` 决策理由 + `evidence/` 审核证据 |
| 拆计划 / 排先后 / 建任务卡 | **B 拆计划** | `references/动作-B-拆计划.md` + `references/查漏清单.md` | DevPlan 任务卡（唯一发 ID·验收口径承接 A/H） |
| 开工做 X / 做一下 / 改一下 / 帮我实现 | **D 开工** | `references/动作-D-开工.md` + `references/节点表.md` | 工作区七件套（v2 新标准档）/ `task.md`（轻）·**先落户经用户确认再改代码** |
| 验收 / 签收 / 能标完成了吗 | **E 收口** | `references/动作-E-收口.md` + `references/verify-代签与汇报.md` | 开发后自动复核/check/备料 → 最后人验/verify |
| 继续 / 上次做到哪 | **R 恢复** | `references/动作-R-恢复.md` | 读 DevPlan「进行中」+ workspace/findings + Git 现场，复述现场再动手 |
| 修 bug / 数字不对 / 改口径 | **维护任务** | `references/维护任务.md` | 分流轻 / 标准档；**动指标口径 = 高危、标准档 + 数据口径校验** |
| 进度汇报 / 汇报进度 | **跑 `dh report`** | `references/动作-R-恢复.md` | 逐模块老板汇报；**原样贴回命令输出** |
<!-- dh:quickpath:end -->

## 任务类型阅读矩阵（索引）

> 按手上干的活读最少够用的上下文，别一次性全加载。

| 干什么 | 先读 |
|---|---|
| 立项 / 拆计划 | dev-harness `references/动作-A-立项.md` / `动作-B-拆计划.md` + `查漏清单.md` |
| 开工 / 档位判定 | dev-harness `references/动作-D-开工.md` + `节点表.md` + 本文件宪章#1 |
| 收口 / verify / 复核 | dev-harness `references/动作-E-收口.md` + `节点表.md` + `verify-代签与汇报.md` + 宪章#3 |
| 恢复现场 / 继续 | dev-harness `references/动作-R-恢复.md` + `docs/dev-plan.md` + `docs/workspace/<批次>/`（findings/progress）+ Git 现场 |
| 修 bug / 优化分流 | dev-harness `references/维护任务.md`；户口登 `docs/dev-plan.md` |
| 改/增/移/退权威设计文档 | dev-harness `references/design-治理.md`；`docs/design.md` 是 MVP 权威设计基线，各 `*-design.md` 为增量设计 |
| 核心契约 / 绑定 / 幂等 / 群授权 | `docs/design.md`（自助建群为 §10） |
| 自动回传 / 输出观察 | `docs/auto-pane-output-design.md`（被动只读 / fail-closed / 单条上限）+ `docs/auto-pane-output-plan.md` |
| Devin agent 支持 / DB schema | `docs/devin-agent-design.md` + `docs/devin-agent-plan.md`（user_version=2 规则见 runbook §2.2） |
| 自助任务群 | `docs/self-service-groups-plan.md` + `docs/self-service-groups-validation.md` |
| 部署 / 升级 / 恢复 / 凭据 | `docs/runbook.md`（**§2.2 停服备份是部署唯一入口**） |
| 真实验收口径 / 历史证据 | `docs/live-validation.md` + 各 `*-validation.md`（离线测试不替代真实证据） |
| 历史 MVP 实施计划 | `docs/development-plan.md`（历史档案，不按其旧分支重跑） |
| 某任务的活 | `docs/dev-plan.md` 户口 → `docs/workspace/<批次>/` |
| 体检 / 现场恢复 | `dh status` / `dh next` / `dh <模块>` |

## 测试与门禁命令（仓库根目录，本仓 venv）

- 全量离线测试（唯一入口；fake CLI，不需凭据/网络）：
  `env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests`
- 语法检查：`.venv/bin/python -m compileall -q feishu_herdr_bridge tests`
- 依赖核验：`.venv/bin/python -m pip check`
- 范围级空白门禁：`git diff --check <base>..HEAD`（裸 `git diff --check` 只查未提交 diff，不算数）
- 更多离线检查顺序以 `docs/runbook.md` §1 命令块为准。

## review 调度表

| 环节 | 审核人 | 兜底 |
|---|---|---|
| A/B 方案审核 | 独立 pane fresh-context agent（herdr workspace 另派、未参与原稿） | 独立 subagent（侦测型降级、非机器只读；派出前后核对 git 基线） |
| 收口第二轮换人复核 | 同上新派 fresh 实例 | 同上 |

## dev-harness 落点 / slug

- 任务户口本 `docs/dev-plan.md`（唯一发 ID；沿用 GitHub Issue/PR 号，如 `issue-24`）；工作区 `docs/workspace/<批次>/`；模块级工件若需 `docs/modules/<slug>/` 再建。
- verify scope = DevPlan 登记的英文 slug（中文 scope 会让 grep 闸门失效）。
- 任务状态以 `docs/dev-plan.md` + 各工作区为权威，本文件只登记落点不抄状态。
