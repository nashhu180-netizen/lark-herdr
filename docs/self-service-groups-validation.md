# 自助任务群：本轮真实验收记录

编写时间：2026-09-11。基线：`ec1f6e1`，分支 `feat/self-service-groups`，Issue #3 / Draft PR #4。
状态：`G3_READY_FOR_LOCAL_VALIDATION`。本文件所有真实项初始为未测，由本地 Codex 与授权操作者填写；文档编写阶段不启动服务、不调用真实飞书或 HerdR。

## 1. 证据边界与执行前提

G2 的固定 `lark-oapi==1.7.3` targeted 71/71、full 165/165 及其他离线检查已由本地审核报告通过。这是基线前置证据，本文件未重新执行。旧 MVP 真实冒烟使用 `lark-herdr-test`，详见 [历史记录](live-validation.md)，不代表本轮 `kpi-agg` 或自助建群通过。

本轮以 [批准设计](design.md) 第 10 节及 [G3 计划](self-service-groups-plan.md) 为准。所有 HerdR 调用只准指向 `kpi-agg`，不得改用其他测试 session；仅在本机规范允许的合法上下文运行，不设置 `HERDR_ENV=1` 冒充 Pane。使用该 session 内可安全操作的测试 workspace，避免现有任务和敏感内容。

本地先按 [runbook](runbook.md) 完成停服备份、管理群初始化、权限发布/审批/可用范围核对及一次部署启动。原程序、SDK、systemd 和测试代码不因填写本文件而修改。无法安全安排的测试保留未测，不临时扩大生产授权或绕过校验来取得通过结果。

## 2. 脱敏代号与记录信息

M 为已配置管理群，A/B 为本轮新建任务群；U1/U2 为配置中的管理员，N 为仅在用户白名单的普通用户，X 为非白名单用户，BOT 为现有应用机器人。参与者必须来自已核实的测试安排，不能用群主身份代替配置管理员身份。

W_A/P_A、W_B/P_B 为 `kpi-agg` 内不同 workspace/Pane；C_A/C_B 为回执给出的实际 `g-...` 确认码的代号；REF-A/REF-B 为随机 `G-<UUID>` 参考号的代号。命令中的占位符在本地替换，不能直接发送代号作为真实 ID 或确认码。

执行人、时间、受测代码/文档 SHA、Python/SDK/HerdR 版本、合法上下文、普通 Linux 服务用户、旧库版本和备份位置：待本地填写。真实 ID 映射、确认码、配置和数据库仅保存在仓库外受限位置；公开记录只用上述代号，不附原始异常/响应、token、密钥或实际群名。

首次部署完成、发出第一条建群提案前，记录服务 `MainPID/InvocationID` 和私有配置文件 SHA-256 摘要；后续以 I0/H0 等代号公开。完成即时准入步骤后再次记录 I1/H1；这一时间窗内不能编辑配置、追加静态白名单、手工写授权表或重启服务。最后再单独测试正常重启保持。若中途实例变化，先查明原因，该轮不能标记不重启准入通过。

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
| R01 核对应用机器人、`im:chat:create`、发布/审批及可用范围，检查私有配置 | M、U1、BOT | 同一应用；M 在静态白名单，U1 在管理员与用户白名单；固定 `kpi-agg`；管理群无需绑定 workspace | 待本地执行 | 未测 | 待附脱敏核对记录 |
| R02 按 runbook 停服备份并启动本轮版本，核对迁移/结构 | 现有私有数据库、普通用户服务 | 旧库 0 升 1 或已有 1 核验通过，仅增加 group_requests，旧绑定及去重记录保留；没有旧库时注明首次安装 | 待本地执行 | 未测 | 待附备份与只读核对摘要 |
| R03 记录 I0/H0，并在 M 使用真实 @ `/agents`，必要时核对显式 CLI | 预启动的 `kpi-agg` | 返回该 session 的目标；无默认 session 或焦点路由；尚未据历史冒烟宣称通过 | 待本地执行 | 未测 | 待附脱敏实例/目标对应关系 |
| R04 U1 在 M @ BOT，发送 `/group-new <群名-A>` | A 的建群提案 | 仅 pending，回显 REF-A、群名、固定 session 和五分钟确认说明；尚未创建群或调用 HerdR | 待本地执行 | 未测 | 待附脱敏提案及本地记录摘要 |
| R05 U1 在 M 发送 `/confirm <C_A>` | 同一提案 | 只建一个群，返回群名和 REF-A，不返回真实群/用户 ID；授权完成为 done，不改变 M 的绑定 | 待本地执行 | 未测 | 待附脱敏回执及状态摘要 |
| R06 在客户端核对群主、成员与群类型 | A、U1、BOT | U1 为群主且已在群，原应用机器人自动在群，群为内部私密普通群；不以人工补拉成员替代成功 | 待本地执行 | 未测 | 待附脱敏客户端核对 |
| R07 A 内真实 @ `/agents`，再发普通文本 | 无初始绑定的新群 A | 即时准入；普通文本提示先绑定，不继承管理群 workspace；无需加白名单或重启 | 待本地执行 | 未测 | 待附新群回执及绑定摘要 |
| R08 U1 在 M 用新消息再次发送已成功的 `/confirm <C_A>` | 已消费的建群码 | 只读同一状态和 REF-A，不再建第二个群；与重投同 message_id 的危险模拟分开记录 | 待本地执行 | 未测 | 待附同参考号回执与资源核对 |
| R09 为 B 独立执行 R04～R06，在 B @ `/agents` | 新提案、新群 B | B 有独立参考号和记录；请求者与机器人到位，未绑定即可列 Agent，不改 A 或 M 的绑定 | 待本地执行 | 未测 | 待附 B 的脱敏回执与核对 |
| R10 A `/bind <W_A> <P_A>`，B 尝试同一绑定 | A/B 与同一测试 workspace | A 成功；B 因已被占用而拒绝，不影响 A；一群一个 workspace、一个 workspace 最多一群 | 待本地执行 | 未测 | 待附绑定与拒绝回执 |
| R11 B `/new <测试项目别名> <codex或claude>`，核对后 `/confirm <workspace码>` | 独立 W_B/P_B | 新群可运行旧创建流程，只有创建/启动/核验成功才绑定；遇首次启动阻塞按 runbook 人工核对，不重放原请求 | 待本地执行 | 未测 | 待附 workspace 回执与绑定摘要 |
| R12 A/B 分别发 `A_ONLY`、`B_ONLY`，各执行 `/bind`、`/read`，记录 I1/H1 | 两个独立编排 Pane | 标记不串线，读取目标正确；I1=I0、H1=H0，期间未改配置/授权或重启 | 待本地执行 | 未测 | 待附脱敏输出与实例/摘要比较 |
| R13 N 在 M、U1 在 A、U2 使用 U1 的待确认码，分别尝试建群/确认 | 非管理员、非管理群、非原请求者 | 都不能创建或消费他人的提案，不能获知他人请求详情；无隐式管理员继承 | 待本地执行 | 未测 | 待附各子场景拒绝记录 |
| R14 X 在已授权群发消息；BOT 被手工加入但未授权的群发 `/agents`；群内省略真实 @ | 非白名单用户、陌生群、无 mention 消息 | 均不触发 HerdR/建群，不自动认领陌生群；无回执时结合本地状态核对，不能仅以沉默断言 | 待本地执行 | 未测 | 待附脱敏客户端与状态摘要 |
| R15 新提案自然超过五分钟后才确认，再用新消息复用该码 | 过期 group pending | 码失效，两次均不建群；不调整系统时间制造过期 | 待本地执行 | 未测 | 待附提案时间与拒绝回执 |
| R16 在 M 保留独立 workspace/group 提案，核对 `/new`、`/group-new` 替换及 `/cancel` | U1/U2/N 各自权限和提案 | 两类互不替换；N 的取消只按旧规则影响 workspace；当前授权管理员只能额外取消自己的 group pending，不影响他人或已消费请求 | 待本地执行 | 未测 | 待附各提案状态与操作者代号 |
| R17 完成 R12 后，在无在途写时正常重启，再在 A/B `/bind`、`/read` | 已完成受管群与原绑定 | 重启保留 done 和绑定，仍只访问 `kpi-agg`，不重建群；本步与不重启准入测试明确分段 | 待本地执行 | 未测 | 待附重启前后脱敏状态 |
| R18 核对私有文件权限、日志、公开脱敏回执与提交范围 | 配置、库、备份、锁、验收材料 | 文件 0600、受限目录 0700；无原始 ID/确认码/密钥/群名/异常泄露；代码、依赖、systemd 和旧真实记录未改 | 待本地执行 | 未测 | 待附脱敏检查摘要 |

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

本轮真实通过项：暂无。真实失败项：尚未执行，不能判定。剩余真实未测项：R01～R18、F01～F06 全部。本地执行后逐项更新本文件，不修改旧 `docs/live-validation.md` 的历史结论。

放行至少需要证明一次确认只创建一个群、请求者与机器人实际到位、新群不改配置/不重启即可操作、两群隔离及 workspace 唯一占用、正常重启后授权与绑定仍在。越权、过期、重复确认等拒绝路径须分别记录；任何已知安全失败先修复并回归，未测项由审核者显式接受为剩余风险，不能用离线数量填成真实通过。

异常发生时停止继续建群，按 runbook 用原参考号、私有记录和远端资源人工交叉核查。禁止删幂等记录、按群名认领、自动补建或直接恢复写前备份后重发。不添加群删除、归档、成员管理或一键建 workspace 流程。涉及实现窄修退回相应批次审核，不在文档批次修改代码。

当前交接状态：`G3_READY_FOR_LOCAL_VALIDATION`。等待本地 Codex 受控验收，不合并 PR、不关闭 Issue、不进入下一批开发。
