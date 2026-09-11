# 自助任务群：分批开发与验收方案

编写时间：2026-09-11。状态：`DESIGN_APPROVED`，等待 Batch G1，未开始实现。

设计基线为本次修订的 `docs/design.md`，代码基线为 `main@7ec9dfb`。已读取 Issue #3、Draft PR #4 及 main 的设计、核心、存储、飞书接入、运行入口和真实验收记录。工作分支为 `feat/self-service-groups`，启动提交 `fea9f87`；PR #4 当前无文件差异。

本轮仅为固定 `kpi-agg` 增加管理群确认建群和动态授权。旧 `docs/development-plan.md` 保留为 MVP 历史，不按其中的旧分支和未测状态重跑开发。GPT-6 Pro 负责主体实现，本地 Codex 负责拉取或代落库、测试、审核、真实验收及有回归证据的窄修。每批通过审核后才能进入下一批。

## 0. 不变项与通用门禁

不修改 `herdr.py`、HerdR fixture、`pyproject.toml` 或 systemd 模板，不升级 `lark-oapi==1.7.3`。不新增群删除、成员维护、查询命令、多 session、`/use`、自然语言路由、队列或通用工作流。正常建群只发一次创建 POST，原有 workspace 创建链不被包进同一操作。

配置与凭据留在仓库外。一次初始化管理群和管理员需要部署重启；此后新受管群即时生效不需要改配置、重启或人工加白名单。后台事件、群名、群主身份均不能授予管理员权限。

本地同步前确认工作区干净且分支正确，禁止强制覆盖。已有代落库补丁时记录其来源，不盲目重复拉取。

    git status --short
    git branch --show-current
    git pull --ff-only
    git merge-base --is-ancestor 7ec9dfb HEAD

通过标准：当前分支为 `feat/self-service-groups`，继承上述基线，本批改动仅在列出的路径。设计审核通过后记录设计提交 SHA；后续批次不得顺带改设计。每批先得到至少一个行为断言红灯，再实现为绿灯，不能用导入失败替代。

离线验收使用本地 `.venv`，无真实凭据并禁止网络；依赖须已安装，SDK 契约测试不得因缺包而跳过。每批执行针对性测试和全量回归，交接实际通过数，不预填结果。

## 1. Batch G1：持久化、授权和确认核心

### 文件范围

新增 `tests/test_groups.py`。

仅修改 `feishu_herdr_bridge/store.py`、`feishu_herdr_bridge/core.py`、`tests/test_store.py`、`tests/test_core.py`、`tests/test_creation.py`。

### 行为

完成一次加表迁移、动态授权查询、管理群与管理员检查、`/group-new` 解析、两类确认码分流、独立命名空间的提案替换/取消和保守恢复。`Message` 增加可兼容旧构造方式的群聊类型信息，建群必须验证其真实值。

核心使用一个可注入的薄建群调用，输入为已确认群名、发起人和持久化 UUID，输出保留已知资源及受控结果。测试用 fake 返回，不引入 SDK 或真实建群。保存已知 ID 与启用授权分成短事务；`done` 群记录为完成依据，建群分支不调用 HerdR，不改变已有绑定。

旧 workspace 确认码、原三表记录、去重和恢复逻辑必须兼容。workspace 提案沿用本会话原有替换规则；group 提案仅替换同一管理群、同一请求管理员自己的 pending。两类提案可同时存在，不跨表互相失效。`/cancel` 保持 workspace 原有语义，对 group 仅允许当前仍获授权的原请求管理员在管理群取消自己的 pending。已有 processing/unknown 不因新提案或取消被复活。权限不足时不得消耗其他用户的确认码。

非目标：不接真实 HTTP，不修改事件入口或配置加载，不宣称飞书新群已能实际使用。

### 测试与验收

迁移测试从真实旧三表形状构造临时库，覆盖数据保留、重复启动、迁移中失败回滚、未知 `user_version` 在恢复和事件接入前拒绝、旧确认码可继续使用。直接向数据库 INSERT/UPDATE，验证 `done` 缺少或具有空 `created_chat_id`、重复非空 `created_chat_id` 均被数据库约束拒绝。更新原有精确三表断言为本轮四表结构，保留旧表约束测试，不增加其他表。

核心测试覆盖管理员与管理群的交叉拒绝、缺失/私聊类型拒绝、群名含空格、同人同群及过期确认、码前缀无回退、重复消息和重复确认、UUID 只生成一次、忙时不排队。分别断言 `/new` 不使 group pending 失效，`/group-new` 不使 workspace 或其他管理员的 group pending 失效，两类提案可同时存在并各自确认；普通用户的 `/cancel` 只按原规则取消 workspace，当前仍授权的原请求管理员只能额外取消自己的 group pending，不能取消他人的，管理员权限撤销后也不能取消自己的 group pending。模拟动态群记录提交前后，证明未完成群不授权、完成群立即获得旧命令能力，其他用户及陌生群仍拒绝。

故障注入覆盖 POST 前失败、远端已创建后超时、已知 ID 后失败、两次本地提交分别失败、启用已提交但消息记录失败、回执丢失后的状态读取、重启不复活未知请求。检查旧绑定不变，所有不确定请求的 fake 创建次数不超过一次，不出现内存授权旁路。

    .venv/bin/python -m unittest -v tests.test_store tests.test_core tests.test_creation tests.test_groups
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v

通过标准：新旧测试全绿；无 SDK/网络副作用；旧库升级无数据损失；只能由已完成记录授予动态群权限。测试失败不能靠删除旧测试或放松原来的路由隔离断言解决。

### 停止点

GPT-6 Pro 交接补丁、红绿证据、迁移前后结构差异和故障结果，停在 `G1_READY_FOR_LOCAL_REVIEW`。本地审核通过前不接 SDK。

## 2. Batch G2：SDK、事件入口和运行配置接线

### 文件范围

新增 `tests/fixtures/feishu_group_create.json`，只存合成数据并标注 `schema-derived/static-not-live`。

仅修改 `feishu_herdr_bridge/feishu.py`、`feishu_herdr_bridge/__main__.py`、`tests/test_feishu.py`、`tests/test_groups.py`、`tests/test_runtime.py`。

### 行为

按设计构造 `CreateChatRequest`，在原业务线程同步调用 SDK，使用 tenant 身份、固定请求参数和提案 UUID。请求者同时为群主与受邀用户；当前应用机器人依官方创建契约自动入群。不开用户 OAuth，不额外调用加成员、分享链接、列群或搜索 API。

在实例锁内完成存储初始化与依赖装配。配置支持可选管理群和管理员，旧配置默认关闭建群；启用时校验固定 session、静态管理群和管理员子集。入口保留 `chat_type`，入口与核心共用动态授权判定，不再用启动时 frozenset 拦掉受管群。新群不能继承管理群的绑定。

正常回应和错误处理只传必要字段；有效群 ID 必须可交给核心保存，即使其他响应字段异常。移除现有回执失败日志中的原始 chat/message ID。停止入口和 HTTP 有界等待沿用当前运行结构，停止前尚未发出的建群 POST 不再开始；无法取消的在途调用按未知结果处理，不自动重试。

非目标：不在本批调用真实飞书或 HerdR，不生成真实配置，不改变依赖或服务架构。

### 测试与验收

在已安装的 1.7.3 上检查 `CreateChatRequest`、请求体、tenant 请求身份及响应模型；拦截实际 SDK HTTP 边界，验证 endpoint、参数与请求体。模拟网络异常、HTTP 5xx、错误 envelope 和不完整成功响应，断言创建 POST 不会被 SDK/HTTP 隐式重发。只 mock 高层方法不足以证明没有隐藏重试。

集成测试从飞书事件进入核心与 SQLite，再进入 fake SDK 和 fake HerdR：新群不在静态 allowed_chats 中，创建并启用后，不重建服务对象即可执行 `/agents`、`/bind`、`/new`、`/confirm`、`/cancel`、`/read` 和普通文本；两群仍绑定不同 workspace。新群的管理员不能继续建群，非白名单成员无权操作。

还需覆盖旧配置、半配置、管理员不在用户白名单、管理群不在静态白名单、错误 session、机器人身份变化、只关闭建群入口但保留既有授权、权限撤销后旧码拒绝、重启保留 done、POST 中停机及重复事件。以敏感标记注入异常/响应，断言回执和日志不泄露原始 ID、密钥或原始 API 正文。日志不得包含确认码；确认码只在该提案的授权会话回执中展示。

    .venv/bin/python -m pip check
    .venv/bin/python -m compileall -q feishu_herdr_bridge tests
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest -v tests.test_feishu tests.test_groups tests.test_runtime
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m feishu_herdr_bridge --help

通过标准：依赖版本未变，SDK 契约测试不跳过，无凭据且禁止网络时全部通过；新群事件入口闭环已被离线证据覆盖，所有建群写计数及旧路由回归符合设计。仍不能标记真实创建成功。

### 停止点

GPT-6 Pro 交接 SDK 边界证据、动态入口回归和隐私测试结果，停在 `G2_READY_FOR_LOCAL_REVIEW`。若 SDK 行为要求改核心设计，退回 G1/设计审核，不在本批暗加新流程。

## 3. Batch G3：配置样例、运行说明和受控真实验收

### 文件范围

新增 `docs/self-service-groups-validation.md`。

仅修改 `examples/config.example.json`、`docs/runbook.md`。不改旧 `docs/live-validation.md` 的历史结果，不新增真实凭据，不修改 systemd 模板。

### 行为与测试准备

示例使用 `herdr_session=kpi-agg`，新增 `management_chat_id=null`、`admin_users=[]`，默认不启用建群。runbook 说明一次可信管理群初始化、应用 `im:chat:create` 权限与可见范围、管理员子集、旧配置兼容及正常新群无需改配置重启。

补充加表前停服务/备份、迁移失败处理和 unknown 的人工核查。远端成功但本地持久化失败时，参考号用于寻找同一操作的资源，不用群名自动认领，不删除幂等记录，不重复建群。记录 SDK 在途 HTTP 无法保证撤回的限制。

新验收文档逐项包含命令/操作、目标、预期、实际、结论、证据，所有真实项初始为未测，群与用户只用脱敏代号。主体验收流程为：管理群提出并确认 → 请求者和机器人真实入群 → 管理群收到稳定参考号 → 不改配置/不重启，在新群绑定或新建 workspace → 两群分别发标记并读取 → 重启后继续操作。

非目标：不补做群删除/归档，不将人工测试资源管理写成产品功能，不把既有 MVP 冒烟等同于本轮通过。

### 本地 Codex 验收

先完成全量离线回归及补丁范围检查，再由符合本机 HerdR 规范的授权操作者运行真实验收。管理群不必绑定 workspace，建群动作不应产生任何 HerdR 命令。

    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v
    git diff --check
    systemctl --user restart feishu-herdr-bridge.service
    systemctl --user is-active feishu-herdr-bridge.service

首次重启只用于部署新版本与管理配置。记录服务实例和配置摘要后，通过新群实际操作证明建群过程中没有第二次配置修改或重启。群成员与私密类型需在客户端核对；所有 HerdR 调用只允许 `kpi-agg`，不能切到其他测试 session 绕过边界。

真实矩阵至少分别记录：正常建群、越权群/越权用户拒绝、过期与重复确认、新群即时准入、陌生群仍拒绝、两群隔离、workspace 占用拒绝、重启保留动态群及绑定。重复事件、API 超时、落库失败等危险场景若无法安全实测，保留未测并引用对应离线故障测试，不伪称真实通过。运行日志、配置、SQLite 与公开证据必须再做脱敏检查。

通过标准：实际证明一次确认只产生一个群、两名所需参与者到位、新群无需配置修改或服务重启即可操作，重启后授权与绑定仍在，旧功能无回归。已知安全失败必须修复并回归；无法安全构造的异常边界由本地审核明确列为剩余风险。

### 停止点

GPT-6 Pro 交接文档与未测矩阵后停在 `G3_READY_FOR_LOCAL_VALIDATION`。真实返回引起的实现窄修应回到对应批次文件并附测试，不在文档批次无记录地改代码。只有本地 Codex 的真实结果和最终审核才能宣布本轮交付，不自动推进下一任务或合并 PR。

## 4. 设计审核前待核实

安装版 SDK 1.7.3 的创建请求、响应字段和隐藏重试行为；当前应用创建群权限、发布与可见范围；请求者作为群主和调用机器人自动入群的实际表现；成功响应能否完整核对 owner/type/private/internal 条件。字段或权限不符时保留群资源并停止启用，不靠默认值放行。

SQLite 与飞书之间没有跨系统事务，API 成功后整个本地存储不可写时可能无法保住真实群 ID。本轮明确接受人工核对这一残余风险，不增加补偿引擎。远端 UUID 的 10 小时去重不提供无限期恢复保证，不能据此允许自动重试。

审核状态：`DESIGN_APPROVED`。本地审核修正了 `kpi-agg` 尚未实测的表述，并隔离 workspace 与建群提案的替换和取消权限；Batch G1 可开始。
