# 自动回传：三批最小实施与验收计划

编写时间：2026-09-12。状态：`AUTO_OUTPUT_DESIGN_REV2_READY_FOR_LOCAL_REVIEW`。本文件只提出计划，不代表测试、部署或真实回传已执行。

## 0. 基线与共同门禁

基线 `main@0de93b578f28f5cbe9b20dff70a7a6b146ba2981`；Issue #7 / Draft PR #8；分支 `feat/auto-pane-output`，起始 `b727245e5a2b130af7547ad6520c762f2c2eea80`。范围以 [增量设计](auto-pane-output-design.md) 为准。此前 199/199 和 Devin 已接受的 live 结果不能充当本功能的验收证据。

GPT-6 Pro 负责主体设计、实现及测试；本地 Codex 应用/拉取、审核、运行 Linux 测试及批准范围内窄修。每批明确停止，不自动推进或部署。开发只用隔离 worktree、虚拟环境和临时 DB；main 上的现有服务继续运行，不读写 live 数据库、不用真实凭据启动测试消费者。

不改 schema、`store.py`、`herdr.py`、建群/Devin 业务语义、依赖、配置文件或 systemd，不新增 LLM、命令或输出队列。观察只复用现有 get/read CLI，失败时不得调用 prompt、start、send-keys、focus 或其他 Pane。设计审核后如发现必须改变这些边界，先回到审核，不能夹带修改。

以下命令仅供后续本地执行，工作区不干净时保留现场，不强制覆盖：

    git status --short
    git branch --show-current
    git rev-parse HEAD
    git merge-base --is-ancestor 0de93b578f28f5cbe9b20dff70a7a6b146ba2981 HEAD

每批先有真正行为断言红灯，再实现为绿灯，不能把 import、fixture/setup 或 SDK 缺失当红测。现有测试必须保留；无网络检查不得通过吞掉网络异常或跳过必需 SDK 测试获得假绿。实际运行与未运行逐项注明。

## 1. Batch O1：快照提取和有界观察内核

### 精确文件

仅新增 `feishu_herdr_bridge/output.py`、`tests/test_output.py`、`tests/fixtures/pane_output.json`。

### 行为及非目标

实现设计中的快照大小门限、正文区域识别、基线增量、新用户块匹配、双采样稳定、Unicode 截断和每个源提示至多一次消费。每轮结束后同一群和绑定可承接下一条新提示，不设群级累计一次限制。把时钟、绑定/授权读取、原 Pane get/read 和消息发送都作为可注入的直接依赖；本批只用 fake 驱动 `tick`，不启动生产线程，不接入 core/SDK/runtime。

临时记录携带源提示与原绑定键、用户、bot、kind、截止时间和随机引用；用独立于真实睡眠的时钟测试每群同时至多一条活动观察、16 群容量、2 秒最小间隔、每源提示独立 120 秒终止和旧记录被取消。只存内存，不建数据库表或磁盘游标，不扫描已有 bindings 自动启动。

fixture 包含三种 Agent 的可识别/不可识别样例、提交前屏幕、提示回显、生成中/稳定输出和 UI 边界。新造的数据明确标为 `synthetic/not-live`，以后经人工脱敏的指定测试 Pane 样本要注明来源类别，不能标为真实用户记录。每个启用的正文边界规则都要有正例与反例；仅识别 agent kind、删除几行关键词或拿到任意变化快照不算正文提取完成。

本地在合法上下文已有指定 disposable Pane 的无敏感样本时可供审核核对，作者不自行操作。没有实际模板依据的布局保持拒绝提取，不能以虚构 TUI 格式宣称兼容。至少 Devin 的真实边界能否满足规则须在最终 live 验收说明。

### 红测与验收

先建立可导入、接口可调用且保守返回“无候选”的最小骨架，用完整新用户块和新正文样例断言应得到正文；红灯须为正文/计数不符。随后实现规则并保留同一断言。

测试至少覆盖：完全未变、只提示回显、只有 banner/chrome、同 prompt 的旧回显、两个新用户块、用户文字被引用、缺少边界、ANSI 残留、Unicode/缩进、正文含类似 UI 符号、整段追加、唯一滚动重叠、不唯一重叠、清屏/resize、锚点丢失、输出超长/超行、基线超限和空基线。

用虚拟时钟覆盖 working 不发送、两个 idle/done 稳定快照才发送、无需先看到 working 的快速回复、候选变化重新计稳定、blocked/unknown、deadline、容量已满。同一源提示 consumed 后，相同/不同快照都不能使其再发；发送失败不能恢复本轮待发送状态，但不阻止后续新提示注册。

必须单列两轮用例：同一 chat/session/workspace/Pane/revision 下，P1 成功后得到 A1 并 consumed；不重建观察器、不重绑、不重启，再注册不同源 message_id 的 P2，捕获包含 P1/A1 的新基线 B2，稳定后只得到不同正文 A2。断言自动消息总数为 2、每个源提示各 1 次，第二条不含 P1/A1，重复 tick 或旧轮清理不产生第三条，也不删除 P2 记录。P2 有独立的基线、稳定计数和 120 秒预算；正常两条消息均严格为 Pane 标识加各自正文，不含 watch_ref/revision。

    .venv/bin/python -m unittest -v tests.test_output
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python -m compileall -q feishu_herdr_bridge tests
    git diff --check

通过标准：同一观察器连续两轮各得到一条对应的新正文，未复读或串轮；规则具有可检查的误报拒绝证据，没有原提示/UI/旧输出或调试标识进入自动正文，无其他 Pane 请求或模型调用，无真实网络。说明尚未核实的布局，不用 full 计数代替兼容性结论。

停止：`AUTO_OUTPUT_O1_READY_FOR_LOCAL_REVIEW`。交付三个文件、红绿结果、规则/样例对应关系与剩余歧义，等待本地审核。

## 2. Batch O2：提交接线、轮询、一次发送与退出

### 精确文件

仅修改 `feishu_herdr_bridge/output.py`、`feishu_herdr_bridge/core.py`、`feishu_herdr_bridge/feishu.py`、`feishu_herdr_bridge/__main__.py`、`tests/test_output.py`、`tests/test_core.py`、`tests/test_feishu.py`、`tests/test_runtime.py`。不新增文件。

### 行为及非目标

core 只增加可选观察接线：每条新普通文本在实际提示前重新捕获基线并取消旧观察，只有原 prompt 与原请求记账成功才在锁释放前注册；手动 `/read` 取消待发观察。第一轮 consumed 不得阻止同群同 revision 的第二条成功提示注册，也不沿用首轮基线或稳定计数。观察辅助故障不重写 prompt 的成功/失败状态。保持三 kind 创建、群确认 namespaces、动态授权、绑定独占及原 `_HELP` 不变。

runtime 装配同 session/executable/ManagedRunner 的有界只读适配对象与唯一观察线程。线程无工作时等待，有工作时逐条取得原操作锁进行短采样；不为每群新建线程，不持锁睡眠。以原 snapshot 条件失效，不能让旧观察使新 revision 失效。

feishu 增加只给观察调用的 `send_output_once`，使用现有 text 请求与固定 tenant 身份，在原操作锁覆盖的最终守卫后开始一次有界 POST。它返回明确的成功/失败/未知状态，不能沿用异步回执入队就返回的语义。保留现有普通回执和建群实现，不抽象消息工作流或改写其重试语义。

自动正文与停止说明共用该源提示的一次发送额度；下一条新成功提示有自己的额度。自动正文只显示绑定主控 Pane 标识和正文，不显示 watch_ref/revision；停止说明也不携带调试字段。发送失败或线程中断不写回原提示失败、不重发 prompt、无消息 outbox。watch_ref/revision 只留在内存和脱敏日志，日志仅另含事件/原因码及计数，不记录原文、敏感 ID 或内容 hash。退出取消观察，禁止退出时 flush 最后一屏；启动不恢复任何旧观察。

### 红测与验收

先在 O1 后尚未接线的现有路径加一条集成断言：一条成功提示经 fake 返回新正文、推进时钟后原群应收到一次自动消息。已有路径只有提交回执，红灯应明确为自动消息计数 `0 != 1`，不能由缺 API/依赖造成；接线后还必须通过下面的两轮端到端断言。

必须覆盖这些时序：

- 原提示只提交一次；duplicate、unbound、stale、forbidden、busy、提交 unknown、记账失败及斜杠命令均不注册；可选基线读取失败仍保持原提示语义。观察不调用 LLM、其他 Pane 或修改提示内容。
- 两群不同绑定并行；同群再发提示时旧观察取消；working 时新提示沿用原投递行为但不承接旧输出。手动 `/read` 不被自动片段复读，其他群观察继续。
- 同一运行实例、同群同绑定连续发送 P1、P2：等 A1 自动回传、首轮 consumed 后再发 P2，中途不 `/read`、不重绑或重启。走真实事件入口、core、临时 DB、fake Pane 与被拦截的 HTTP 边界，第二轮快照保留第一轮历史。断言用户 prompt 各只提交一次、A1/A2 各自动发送一次且不同、自动正文 POST 合计 2 次；A2 不含 P1/A1，重投任一源事件或继续采样均不新增回复。两条可见消息严格为 Pane 标识和对应正文，watch_ref/revision 只可在内部检查，不出现在任一回执或自动消息。
- 读取前/读取中/发送前分别重绑、同 Pane 重新绑定导致 revision 增加、建新 workspace 后换绑、workspace 转给另一群、撤销用户/群授权或更换 bot。所有旧内容均丢弃，不重定向；以原 snapshot 失效不能破坏新绑定。
- 用 barriers 证明一次发送与桥接换绑不能交错穿过最终守卫；已开始的 HTTP 只发到冻结的原 chat，即使回执延迟也不读取新目标。前台操作锁争用保持原 busy 语义，tick 不积压。
- 用固定 `lark-oapi==1.7.3` 的真实请求模型及被拦截的实际 HTTP 边界检查自动消息正文/目标、POST 次数、超时和禁止重定向/隐式重试。验证拒绝、网络错误、响应不完整、回执丢失都只有一次尝试，不能只 mock 高层 send 就声称没有隐藏重试。
- 服务重启丢弃未发观察、同 message_id 重投不重注册、新提示基线排除旧屏；SIGTERM 停止新读取/发送并回收子进程，半完成读取不发送。无 DB schema 差异、无持久化快照/提示，后台发送失败不会触发原任务重做。

    .venv/bin/python -m unittest -v tests.test_output tests.test_core tests.test_feishu tests.test_runtime
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python -m pip check
    .venv/bin/python -m compileall -q feishu_herdr_bridge tests
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m feishu_herdr_bridge --help
    git diff --check

通过标准：连续两轮的两个源提示各提交一次、各自动回复一次，原绑定不变且无复读/串轮；targeted/full 与环境检查全部通过，固定 SDK 检查不跳过，记录实际计数和退出码。无网络、凭据、live 数据库或真实 HerdR 调用。原有 199 项应回归保留；新增观察不能靠改变旧错误状态或放松幂等断言求绿。

停止：`AUTO_OUTPUT_O2_READY_FOR_LOCAL_REVIEW`。交付完整补丁、测试日志、发送竞争与退出证据，等待审核，不部署。

## 3. Batch O3：使用边界文档与受控验收交接

### 精确文件

仅修改 `docs/runbook.md`，补充每条成功源提示独立 120 秒、每轮最多一条且同 Pane 可连续多轮对话的体验，以及原文可见风险、无重启补发、简洁消息格式、截断与 `/read`、发送结果不明及手工核查。不得改旧 live-validation 或群/Devin 验收历史，不增加配置、命令或生产代码。

本批作者只完成文档与本计划中的验收清单；真实操作由本地 Codex 与合法操作者执行。部署前再跑全量回归及 `git diff --check`。除已有模型对用户验收提示的正常执行外，不让观察器发送额外问题或调用其他模型。

### 仅指定 disposable 目标的 live 步骤

只使用用户已提供的 disposable Devin 验收群和 `kpi-agg` 中的 `w1V:p1`。本地核实它仍属于该验收用途；ID、用途或现有绑定不符就停止，不搜索替代 KPI Pane。公开材料用 T/P_TEST/W_TEST 代号，不贴真实群 ID、确认码或原始终端内容。正常开发继续留在隔离 worktree，旧服务不变；部署须由本地另行批准，不能并行启动第二个 bot 消费者。

1. 仅在合法上下文读取 P_TEST 的 get 和 `visible` 快照，核对 UI 分界、状态字面值和基线可用性。不滚动历史、不访问其他 Pane。若 Devin 当前布局不能按规则可靠识别，先保留 `/read` 并回到 O1 范围窄修，不放宽成全屏转发。
2. 在同一 T、同一 P_TEST、同一绑定 revision 和服务实例中连续完成两轮。先发送无文件修改、要求两行 Unicode 测试文本的普通提示 P1，观察提交回执及 120 秒内的自动正文 A1；A1 到达且 Pane 空闲后，无需等首轮 120 秒耗尽，发送另一条普通提示 P2，要求不同正文 A2，并在 P2 自身的 120 秒内收到第二条自动正文。两轮期间不手动 `/read`、不重绑、不重启；终端保留上一轮可见历史以验证 B2 排除旧内容。分别记录 P1→A1、P2→A2，各自动回复一次，A2 不含 P1/A1，两条可见消息只有主控 Pane 标识和各自正文，无 prompt 回显、banner、watch_ref/revision 或其他 Pane 内容。随后等待超过 P2 观察期限，确认两轮均无额外自动消息。
3. 单独运行 `/read` 验证原兜底；再做一次在观察期间手动 `/read` 的用例，确认读取不被稍后的自动片段复读。模型只执行明确发送的用户测试提示。
4. 重绑保护仅在 T 对同一个 W_TEST/P_TEST 再次 `/bind`，验证 revision 递增后旧观察不再开始发送；不得绑定其他 KPI 目标。安排在旧观察尚未领取发送额度时操作。若消息已进入 HTTP，则记录条件不满足，不能把不可撤回的晚到消息当作新观察发送或宣称已证明取消。
5. 在没有自动 POST 在途的检查点，做一次受控 bridge 重启，确认不主动读屏、不补发旧输出；原群/绑定仍在。随后发一个新测试提示，正常建立新基线并只返回新结果。不得用重启来要求重做旧提示。

跨群转移、两个不同 Pane 并发、Pane 删除/替换、断网、发送失败和写入窗口故障只用离线 fake/barrier 测试，本轮不为这些场景触碰其他 KPI Pane 或破坏验收 Pane。现场无法满足的步骤保留未测，不反复触发远端工作来制造通过。

| 验收项 | 目标 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|
| 可见快照格式和状态核对 | P_TEST | 规则区分提示/正文/UI，读取被动且只读此 Pane | 待本地执行 | 未测 | 待附脱敏规则核对 |
| 第一轮 P1→A1 | T、P_TEST | P1 提交一次并自动回 A1 一次；仅 Pane 标识和正文，Unicode/行序保留，无回显、chrome 或调试字段 | 待本地执行 | 未测 | 待附首轮脱敏时序 |
| 紧接第二轮 P2→A2 | 同一 T、P_TEST、revision 和服务实例 | 首轮 consumed 后不重绑/重启，P2 取新基线且独立限时；自动回不同 A2 一次，不含 P1/A1，两轮合计两条自动正文，无复读/串轮 | 待本地执行 | 未测 | 待附双轮脱敏时序及内部状态核对 |
| `/read` 兜底与取消重复 | T、P_TEST | 手动读取保持原功能，无后续重复自动片段 | 待本地执行 | 未测 | 待附脱敏时序 |
| 同目标重绑 revision 守卫 | T、P_TEST | 重绑提交后旧观察不开始发送，无其他 Pane 访问 | 待本地执行 | 未测 | 待附仅在内部核对的原/新 revision 代号 |
| 重启不重放、新提示可跟踪 | T、P_TEST | 重启不补旧屏，绑定保持，新提示从新基线开始 | 待本地执行 | 未测 | 待附实例与脱敏消息时序 |
| 跨群/失败/中断边界 | 仅离线 fake | 最终守卫、一次发送、无重试、无原文落盘 | 不进行真实故障注入 | 未测 | 待引用 O1/O2 离线结果 |

通过条件必须包含同群同绑定的两条顺序提示分别可靠回传两条不同的真实 Devin 正文，各自一次且没有首轮复读或串轮。只完成第一轮、第二轮因 consumed 被永久阻止，或任一轮仅收到“请 /read”，都不能记为连续多轮自动输出通过。消息只能显示绑定主控 Pane 标识和正文；watch_ref/revision 仅作内部核对。生命周期状态与可见快照无法提供全轮次/完整历史保证，剩余边界需如实记录。所有 live 项初始未测，作者不预填通过。

O3 文档交接停止：`AUTO_OUTPUT_READY_FOR_LOCAL_VALIDATION`。结果写入 Issue #7 / PR #8 的脱敏记录，需代码窄修时回对应批次审核；不自动提交、推送、合并或关闭 Issue。

当前仅设计交接：`AUTO_OUTPUT_DESIGN_REV2_READY_FOR_LOCAL_REVIEW`。
