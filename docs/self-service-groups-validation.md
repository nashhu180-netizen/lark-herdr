# 自助任务群：本轮真实验收记录

编写及验收时间：2026-09-11。受测实现：`4fffffc`，分支 `feat/self-service-groups`，Issue #3 / PR #4；本次记录提交随后附加。
状态：`LIVE_CORE_ACCEPTED`。真实项由本地 Codex 与授权操作者 U1 逐项执行；仅亲自观察到的子场景标记通过，未安排的多用户、陌生群和自然过期场景继续标记未测。

## 1. 证据边界与执行前提

G2 的固定 `lark-oapi==1.7.3` targeted 71/71、full 165/165 及其他离线检查已由本地审核报告通过。真实验收中发现并修复建群确认失败收敛、workspace/Tab 展示问题；最终 full 166/166、compileall、pip check 与 diff check 通过。旧 MVP 真实冒烟使用 `lark-herdr-test`，详见 [历史记录](live-validation.md)，不代表本轮 `kpi-agg` 或自助建群通过。

本轮以 [批准设计](design.md) 第 10 节及 [G3 计划](self-service-groups-plan.md) 为准。所有 HerdR 调用只准指向 `kpi-agg`，不得改用其他测试 session；仅在本机规范允许的合法上下文运行，不设置 `HERDR_ENV=1` 冒充 Pane。使用该 session 内可安全操作的测试 workspace，避免现有任务和敏感内容。

本地先按 [runbook](runbook.md) 完成停服备份、管理群初始化、权限发布/审批/可用范围核对及一次部署启动。原程序、SDK、systemd 和测试代码不因填写本文件而修改。无法安全安排的测试保留未测，不临时扩大生产授权或绕过校验来取得通过结果。

## 2. 脱敏代号与记录信息

M 为已配置管理群，A/B 为本轮新建任务群；U1/U2 为配置中的管理员，N 为仅在用户白名单的普通用户，X 为非白名单用户，BOT 为现有应用机器人。参与者必须来自已核实的测试安排，不能用群主身份代替配置管理员身份。

W_A/P_A、W_B/P_B 为 `kpi-agg` 内不同 workspace/Pane；C_A/C_B 为回执给出的实际 `g-...` 确认码的代号；REF-A/REF-B 为随机 `G-<UUID>` 参考号的代号。命令中的占位符在本地替换，不能直接发送代号作为真实 ID 或确认码。

执行人为本地 Codex 与授权操作者 U1；时间为 2026-09-11；受测实现 SHA 为 `4fffffc`。环境为普通 Linux 用户的 user systemd，Python 3.12.3、`lark-oapi` 1.7.3、HerdR 0.9.0。旧库为 schema 0，受限备份位置仅保存在仓库外；真实 ID 映射、确认码、配置和数据库同样不进入公开记录。

实例与配置摘要原值仅私下核对为 I0/H0。A 从建群完成到首次 `/agents` 和未绑定文本检查期间，未编辑配置、未追加静态白名单、未手工写授权表且服务无自动重启，核对为 I1=I0、H1=H0。后续展示修复的计划重启与最终 R17 持久化重启均单独记录，不冒充即时准入证据。

## 3. 本地复核命令

下面仅供本地执行，不表示已运行。依赖须事先安装；无真实凭据并禁止真实网络时执行离线检查，SDK 契约测试不得跳过。

    .venv/bin/python -m pip check
    .venv/bin/python -m compileall -q feishu_herdr_bridge tests
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m feishu_herdr_bridge --help
    git diff --check

合法操作者按 runbook 完成首次部署后，记录实例与摘要；这些命令不读取凭据内容，输出仍只在本地保存：

    systemctl --user is-active feishu-herdr-bridge.service
    systemctl --user show feishu-herdr-bridge.service -p MainPID -p InvocationID
    sha256sum "$HOME/.config/feishu-herdr-bridge/config.json"

确有合法 CLI 核对需要时，`HERDR_BIN` 为私有配置中的绝对路径，命令必须显式使用固定 session；不得将此步骤用于查看其他 session：

    "$HERDR_BIN" --session kpi-agg agent list

以下真实步骤按行执行和记录，不整段自动运行，也不连续重发确认。发生未知结果立即转 runbook 的人工核查流程。

## 4. 正常与拒绝路径的真实验收

每行只把亲自观察到的结果写入实际列，并附脱敏证据；一个格子包含多个子场景时分别记录，不能用其中一项成功代表全部通过。待执行不等于失败，也不等于通过。

| 操作 | 目标 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|
| R01 核对应用机器人、`im:chat:create`、发布/审批及可用范围，检查私有配置 | M、U1、BOT | 同一应用；M 在静态白名单，U1 在管理员与用户白名单；固定 `kpi-agg`；管理群无需绑定 workspace | 私有配置经程序加载器校验；同一 BOT 以真实接口成功创建 A/B；M、U1 与固定 session 条件成立 | 通过 | 配置摘要与真实创建结果脱敏核对 |
| R02 按 runbook 停服备份并启动本轮版本，核对迁移/结构 | 现有私有数据库、普通用户服务 | 旧库 0 升 1 或已有 1 核验通过，仅增加 group_requests，旧绑定及去重记录保留；没有旧库时注明首次安装 | 停服备份后从 0 升 1；旧 2 条绑定、17 条请求保留，新增表初始为空 | 通过 | 备份目录 0700、文件 0600；迁移前后只读计数一致 |
| R03 记录 I0/H0，并在 M 使用真实 @ `/agents`，必要时核对显式 CLI | 预启动的 `kpi-agg` | 返回该 session 的目标；无默认 session 或焦点路由；尚未据历史冒烟宣称通过 | 显式 CLI 与 A/B `/agents` 均只返回 `kpi-agg`；本轮未在 M 重复执行 `/agents` | 部分通过 | 固定 session 调用与 7 workspace/实时 Pane 映射核对 |
| R04 U1 在 M @ BOT，发送 `/group-new <群名-A>` | A 的建群提案 | 仅 pending，回显 REF-A、群名、固定 session 和五分钟确认说明；尚未创建群或调用 HerdR | 返回 A 的独立 REF-A 与确认说明；确认前没有新群记录 | 通过 | 管理群回执与数据库 pending/done 转换摘要 |
| R05 U1 在 M 发送 `/confirm <C_A>` | 同一提案 | 只建一个群，返回群名和 REF-A，不返回真实群/用户 ID；授权完成为 done，不改变 M 的绑定 | 返回 A 与 REF-A；数据库 done 且资源唯一，M 未绑定 workspace | 通过 | 回执与 group_requests 脱敏只读核对 |
| R06 在客户端核对群主、成员与群类型 | A、U1、BOT | U1 为群主且已在群，原应用机器人自动在群，群为内部私密普通群；不以人工补拉成员替代成功 | A/B 均精确匹配一个内部私密普通群；owner=U1；CLI 摘要各为 1 user、1 bot | 通过 | 飞书只读查询摘要，无真实 ID |
| R07 A 内真实 @ `/agents`，再发普通文本 | 无初始绑定的新群 A | 即时准入；普通文本提示先绑定，不继承管理群 workspace；无需加白名单或重启 | A 立即列出 `kpi-agg`；普通文本返回未绑定；期间无配置写入或重启 | 通过 | A 回执、I1=I0/H1=H0 与空绑定核对 |
| R08 U1 在 M 用新消息再次发送已成功的 `/confirm <C_A>` | 已消费的建群码 | 只读同一状态和 REF-A，不再建第二个群；与重投同 message_id 的危险模拟分开记录 | A、B 原码均以新消息只读返回各自原参考号；数据库仍 2 条记录，飞书侧 A/B 各一个 | 通过 | 4 条 done confirm 消息、2 个唯一 create_uuid/resource |
| R09 为 B 独立执行 R04～R06，在 B @ `/agents` | 新提案、新群 B | B 有独立参考号和记录；请求者与机器人到位，未绑定即可列 Agent，不改 A 或 M 的绑定 | B 使用独立 REF-B 创建；成员/类型核对通过；未绑定即可列 Agent，W_A 显示已占用 | 通过 | B 回执、群与绑定脱敏摘要 |
| R10 A `/bind <W_A> <P_A>`，B 尝试同一绑定 | A/B 与同一测试 workspace | A 成功；B 因已被占用而拒绝，不影响 A；一群一个 workspace、一个 workspace 最多一群 | A 创建并绑定 W_A/P_A；B 抢绑返回 workspace 已被其他会话绑定，随后普通文本仍未绑定 | 通过 | 两群回执与唯一绑定查询 |
| R11 B `/new <测试项目别名> <codex或claude>`，核对后 `/confirm <workspace码>` | 独立 W_B/P_B | 新群可运行旧创建流程，只有创建/启动/核验成功才绑定；遇首次启动阻塞按 runbook 人工核对，不重放原请求 | B 创建 Claude W_B/P_B 并在核验后绑定；A 另以 Codex 建立 W_A/P_A；cwd 均为允许项目 | 通过 | create_requests=done、真实 Agent get 与绑定摘要 |
| R12 A/B 分别发 `A_ONLY`、`B_ONLY`，各执行 `/bind`、`/read`，记录 I1/H1 | 两个独立编排 Pane | 标记不串线，读取目标正确；I1=I0、H1=H0，期间未改配置/授权或重启 | A/B 分别返回 A_ROUTE_OK、B_ROUTE_OK；目标为 W_A/P_A、W_B/P_B，数据库为两个不同 chat 绑定 | 通过 | 两个 `/read` 回执和 prompt done/target 摘要 |
| R13 N 在 M、U1 在 A、U2 使用 U1 的待确认码，分别尝试建群/确认 | 非管理员、非管理群、非原请求者 | 都不能创建或消费他人的提案，不能获知他人请求详情；无隐式管理员继承 | U1 在 A 执行 `/group-new` 被拒绝；本轮只有 U1，未安排 N/U2 子场景 | 部分通过 | A 的 forbidden 回执；其余保留未测 |
| R14 X 在已授权群发消息；BOT 被手工加入但未授权的群发 `/agents`；群内省略真实 @ | 非白名单用户、陌生群、无 mention 消息 | 均不触发 HerdR/建群，不自动认领陌生群；无回执时结合本地状态核对，不能仅以沉默断言 | 待本地执行 | 未测 | 待附脱敏客户端与状态摘要 |
| R15 新提案自然超过五分钟后才确认，再用新消息复用该码 | 过期 group pending | 码失效，两次均不建群；不调整系统时间制造过期 | 待本地执行 | 未测 | 待附提案时间与拒绝回执 |
| R16 在 M 保留独立 workspace/group 提案，核对 `/new`、`/group-new` 替换及 `/cancel` | U1/U2/N 各自权限和提案 | 两类互不替换；N 的取消只按旧规则影响 workspace；当前授权管理员只能额外取消自己的 group pending，不影响他人或已消费请求 | 待本地执行 | 未测 | 待附各提案状态与操作者代号 |
| R17 完成 R12 后，在无在途写时正常重启，再在 A/B `/bind`、`/read` | 已完成受管群与原绑定 | 重启保留 done 和绑定，仍只访问 `kpi-agg`，不重建群；本步与不重启准入测试明确分段 | 三张表 processing=0 后重启；2 个群 done、A/B 两个绑定保留；两群 `/bind` 分别返回原 Agent | 通过 | schema=1、服务 active/NRestarts=0 与双群回执 |
| R18 核对私有文件权限、日志、公开脱敏回执与提交范围 | 配置、库、备份、锁、验收材料 | 文件 0600、受限目录 0700；无原始 ID/确认码/密钥/群名/异常泄露；代码、依赖、systemd 和旧真实记录未改 | 配置/库/备份文件 0600、备份目录 0700；仓库与服务日志敏感模式命中 0；旧记录保留 | 通过 | 权限、日志计数、git 范围和迁移前后摘要 |

R17 的正常重启仅由本地执行，先确认没有在途写，再使用 runbook 的 `systemctl --user restart`。不要为测试中断恢复而在真实 POST 期间停服务。

## 5. 危险故障：仅引用现有离线测试

本轮不对真实飞书或在用数据库做故障注入，不制造 SDK 重投、断网、磁盘失败、错误响应或进程中断。以下实际列只记录未做真实故障；结论保持未测，证据为离线引用，不能升级为真实通过。引用入口分别为 [SDK/HTTP 测试](../tests/test_feishu.py) 与 [核心状态测试](../tests/test_groups.py)。本地可另附离线运行的日期、提交及脱敏结果。

| 操作 | 目标 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|
| F01 仅离线模拟超时、连接异常、重定向、HTTP 错误和坏 JSON | 一次确认的创建 POST | 不隐式重发，结果不明按 unknown；新的 message_id 不能使原码重建 | 未进行真实注入 | 未测 | `GroupSDKTests.test_http_failures_redirects_and_invalid_json_never_repeat_post` |
| F02 仅离线模拟字段缺失或 SDK 反序列化失败 | 已知群 ID、owner/type/private/internal 校验 | 保留已知资源，不满足全部条件不授权，不补拉成员 | 未进行真实注入 | 未测 | `GroupSDKTests.test_every_invalid_success_field_preserves_resource_without_enabling`、`test_sdk_decode_error_preserves_raw_resource_and_restores_transport` |
| F03 仅离线模拟确认消费提交前失败，另发新消息复用原码 | 单次确认与原子状态转换 | pending/processing 收敛为 unknown，creator 不被绕过调用；重启不复活 | 未进行真实注入 | 未测 | `GroupTests.test_failure_before_consumption_does_not_bypass_the_failure`、`test_unknown_settlement_is_atomic_and_preserves_terminal_results` |
| F04 仅离线模拟远端成功后本地保存/启用/全库写入失败 | 群资源、动态授权和旧绑定 | 不作内存授权；保留能保存的 ID 与参考号，无法保存 ID 时仍不重建；不按群名认领 | 未进行真实注入 | 未测 | `GroupTests.test_resource_save_failure_never_enables_memory_authorization`、`test_authorization_commit_failure_is_read_back_without_retry`、`test_total_local_storage_failure_keeps_consumed_record_and_reference` |
| F05 仅离线模拟回执或消息记账失败、并发重复事件 | 已完成记录、原确认码 | done 授权不被撤销；只读核对结果，每次原请求远端调用至多一次 | 未进行真实注入 | 未测 | `GroupTests.test_done_is_authoritative_after_receipt_record_failure_and_restart`、`test_duplicate_during_inflight_create_cannot_start_a_second_call`；`GroupSDKTests.test_raw_id_name_code_and_secret_never_enter_receipt_failure_log` |
| F06 仅离线模拟 token 获取时停止、POST 中停止和进程中断恢复 | 未发出的写与在途写 | 停止后不开始新 POST；在途可能已成功，保留资源或 unknown，恢复不重做 | 未进行真实注入 | 未测 | `GroupSDKTests.test_stopping_before_send_including_token_fetch_never_starts_post`、`test_inflight_stop_preserves_known_resource_as_unknown`；`GroupTests.test_interrupted_remote_call_is_not_replayed_by_recovery` |

## 6. 结论与交接

本轮真实通过项：R01、R02、R04～R12、R17、R18。部分通过：R03、R13。真实失败项：无。剩余真实未测项：R14～R16，以及 R13 的 N/U2 子场景；F01～F06 按设计不做真实故障注入。R08 已用 A/B 两个已消费确认码分别复核，资源总数仍为二；旧 `docs/live-validation.md` 的历史结论未修改。

核心放行条件均已满足：一次确认只创建一个群，请求者与机器人实际到位，新群不改配置、不重启即可操作，两群分别绑定不同 workspace/Pane、唯一占用拒绝有效，A/B 标记不串线，正常重启后授权与绑定均保留。单一操作者环境无法安全安排的陌生用户、多管理员交叉确认和自然过期场景明确作为剩余风险接受；对应授权、过期、并发与失败收敛由本轮 166 项离线测试覆盖，但没有冒充真实通过。

异常发生时停止继续建群，按 runbook 用原参考号、私有记录和远端资源人工交叉核查。禁止删幂等记录、按群名认领、自动补建或直接恢复写前备份后重发。不添加群删除、归档、成员管理或一键建 workspace 流程。涉及实现窄修退回相应批次审核，不在文档批次修改代码。

当前交接状态：`LIVE_CORE_ACCEPTED / PR_READY_FOR_FINAL_REVIEW`。允许在 GitHub 检查通过且最终 diff 审核无新增问题后合并 PR #4、关闭 Issue #3；Devin 支持另见 Issue #5。
