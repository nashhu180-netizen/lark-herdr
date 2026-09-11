# 飞书轻量桥接 HerdR

修订时间：2026-09-11。
状态：核心 MVP 已通过 Linux 真实冒烟；Issue #3 增量设计已通过本地审核，等待 Batch G1。
目标：保持单进程最小实现，增加受控的自助任务群创建。

基线为 `main@7ec9dfb`，已有实测范围见 `docs/live-validation.md`，不等同于全部异常边界通过。本轮只增补第 10 节；新功能尚未实测，不重新设计 HerdR 执行链。

## 1. 范围

保留用户手动建群及私聊方式。另允许预先配置的管理员仅在可信管理群中，经确认创建新的受管任务群，具体见第 10 节。

一个飞书 `chat_id` 绑定一个 HerdR workspace 中的一个编排 Agent Pane。多个会话分别绑定不同 workspace，普通文本提交到各自绑定的原 Pane，不另起独立聊天或 Agent。

支持列出 Agent、绑定已有目标、手动读取输出，以及经确认后创建 workspace 并启动 Codex 或 Claude。创建成功后绑定新目标，后续任务说明通过普通文本发送。

桥接始终只连接一个配置指定的 HerdR session；本轮自助群功能固定使用 `kpi-agg`。手机无需直接连接 Linux，Linux 不开放公网入站端口。

## 2. 最小架构与技术选型

通信路径：

手机飞书 → 飞书长连接 → Python 桥接进程 → HerdR CLI → 指定 workspace / Pane Agent。

使用 Python 3.11+。唯一直接第三方依赖为飞书官方 Python SDK `lark-oapi`，固定为本机验证过的版本。其余使用标准库，包括 `sqlite3`、`subprocess`、`threading`、`pathlib` 和 `logging`。

桥接只有一个常驻进程。SDK 负责长连接，同进程工作线程执行短操作，HerdR CLI 作为短命子进程调用。接收回调不得等待 CLI 完成。

采用一个全局非阻塞操作锁，最多执行一个桥接操作。繁忙时立即回复，要求发送新消息重试，不建立等待队列。普通文本提交不使用 `--wait`，桥接不等待 Agent 完成任务，已有多个 workspace 的 Agent 仍可并行工作。

内部只划分三个职责：飞书接入、业务与 SQLite、HerdR 薄适配层。不增加网页、服务拆分或插件机制。

## 3. 绑定与持久化

SQLite 是绑定和操作记录的唯一持久化来源。只使用短事务，不在事务内等待外部命令。

### 3.1 绑定表 `bindings`

| 字段 | 含义 |
|---|---|
| `chat_id` | 主键，飞书群聊或私聊会话 ID。 |
| `herdr_session` | 配置指定的 HerdR session。 |
| `workspace_id` | workspace 的实际 ID。 |
| `pane_id` | 编排 Agent 所在 Pane 的实际 ID。 |
| `agent_name` | 可空，仅用于展示，不作为持久化路由依据。 |
| `valid` | 绑定是否有效。 |
| `revision` | 每次绑定或换绑递增。 |
| `bound_at` | 当前绑定的生效时间。 |
| `bound_by` | 操作者的飞书 `open_id`。 |

约束：

- 一个 `chat_id` 只有一个当前绑定；同一 `herdr_session` 内，一个 `workspace_id` 最多绑定一个会话，通过数据库唯一约束保证。
- 路由固定使用 `herdr_session + workspace_id + pane_id`。每次 `agent get/read/prompt` 的目标参数均使用 `pane_id`，不使用名称、桌面焦点或当前 workspace。
- 操作前通过适配层核验 Pane 归属；提交时由 HerdR 自身再次验证该 Pane 当前仍承载 Agent。核验失败、调用失败或结果无法确认时，当前绑定置为失效，要求重新 `/bind`。不得降级为 Pane 裸输入。

本版不追踪进程身份。绑定表示该 workspace 中的目标 Pane，无法保证同一 Pane 被外部替换 Agent 后仍是原进程。人工更换编排 Agent 后应重新绑定；此限制不通过 PID、启动时间或额外实例指纹解决。

### 3.2 操作记录

| 表 | 最小内容 |
|---|---|
| `requests` | `message_id` 主键、会话、操作者、动作、绑定版本与目标快照、状态、简短结果、更新时间。 |
| `create_requests` | 请求 ID、确认码、会话、发起人、项目目录、workspace 标签、Agent 类型与名称、原绑定版本、过期时间、状态、已创建的 workspace 与 Pane ID。 |
| `group_requests` | 本轮新增：建群提案、确认状态、远端群 ID 与受管授权，结构见 10.3。 |

操作状态使用 `processing / done / failed / unknown`。创建请求另有 `pending`；取消或过期后不可执行。

不持久化普通消息正文、完整终端输出和密钥。重启后保留绑定，首次使用重新核验目标。遗留的 `processing` 标记为 `unknown`，不自动续跑；结果不明的投递要求人工检查并重新绑定。

## 4. 飞书命令与交互

仅处理用户白名单与已授权会话内的文本消息；会话授权含静态配置和第 10 节的动态受管群。群聊要求明确 @ 本机器人，私聊直接处理。先剥离机器人的提及，再解析命令。忽略机器人自身消息、非文本消息和编辑事件。

以下均为桥接命令。

| 输入 | 行为 |
|---|---|
| `/agents` | 列出配置 session 内的 Agent，按 workspace 展示实际 workspace ID、Pane ID、名称、类型、可获得的状态及绑定占用情况。 |
| `/bind` | 显示本会话当前绑定及有效性。 |
| `/bind <workspace_id> <pane_id>` | 核验目标后建立或替换绑定；workspace 已被其他会话占用则拒绝。 |
| `/read` | 读取绑定 Pane 的可见终端输出，最多 80 行、3000 字符，截断时提示。 |
| `/new <项目别名> <codex或claude>` | 建立待确认请求，不创建 workspace，也不启动 Agent。 |
| `/group-new <群名>` | 仅管理员在管理群提出建群请求，不立即创建。 |
| `/confirm <确认码>` | 执行同人同会话的有效 workspace 或建群提案；按确认码区分类型。 |
| `/cancel` | 保持既有 workspace 取消语义；建群提案仅允许当前仍获授权的原请求管理员在管理群取消自己的 `pending`，不取消他人的建群提案，不中断任何 Agent。 |
| 普通文本 | 提交到本会话绑定的原 Pane Agent。 |

未知的 `/` 命令返回帮助，不透传。未绑定或绑定失效时，普通文本只返回绑定提示。

`/read` 是终端快照，不保证包含完整对话。每条目标相关回执都带 workspace 和 Pane 标识。

### workspace 创建确认与 Agent 名称

项目别名映射为配置中的固定绝对路径。workspace 标签使用项目别名和请求短 ID，不允许用户传入任意目录或 CLI 参数。

Agent 名称在创建请求中生成并保存：

- Codex：`fb-codex-<12位小写十六进制随机串>`。
- Claude：`fb-claude-<12位小写十六进制随机串>`。

实际名称必须满足 `[a-z][a-z0-9_-]{0,31}`。启动前用 `agent list` 检查 live 同名，冲突时重新生成一次并更新请求。再次冲突，或 `agent start` 因并发竞争明确返回名称冲突时，本次操作失败；不复用同名 Agent，不自动重建 workspace。最终唯一性以 HerdR 启动结果为准。

确认消息显示项目实际路径、workspace 标签、Agent 类型、拟用名称及成功后将替换的绑定。确认码有效期为 5 分钟，只能使用一次。同一会话的新 `/new` 只使该会话旧的 workspace `pending` 失效，不影响建群提案。

确认后的执行顺序：

1. 核验会话、发起人、有效期、目录与原绑定版本。绑定已变化则要求重新 `/new`。
2. 将创建请求从 `pending` 原子更新为 `processing`，阻止重复确认，并完成名称检查。
3. 创建 workspace，从实际返回 JSON 中取得并保存 workspace、tab 和根 Pane 的 ID。
4. 在返回的根 Pane 启动指定 Agent，通过 `agent get <pane_id>` 核验实际目标和类型。
5. 全部成功后，以事务替换绑定并记录完成。旧 workspace 保持原样。

失败时不修改旧绑定。已经创建的 workspace 保留并报告其 ID，不自动删除。创建或启动超时、返回无法解析或结果不明时标记 `unknown`，禁止自动重试和再次执行同一确认请求，先人工核对现场。

## 5. 投递、去重与重连

先校验权限，再按飞书 `message_id` 去重。重复消息只返回已有操作状态，不再次调用 HerdR。因繁忙被拒绝的消息记录为失败，用户需发送新消息重试。

接受操作时固定绑定版本和目标快照，执行前再次核验。绑定已变化时拒绝原请求，不改投新目标。早于当前绑定生效时间的迟到普通消息拒绝投递；无法解析消息时间时也拒绝。用户应收到绑定成功回执后再发送任务正文。

提交前写入 `processing`，随后调用：

`herdr agent prompt <pane_id> <text>`

不使用等待任务结束的参数，不在桥接中排队输入。HerdR 在提交时验证目标并拒绝 blocked 状态输入，桥接不绕过该检查，也不自动发送确认键。

结果处理：

- 明确提交成功：记录 `done`，回复“已提交，尚未确认任务完成”。
- 明确拒绝或未提交：记录 `failed`，报告原因并使绑定失效。blocked 同样停止后续投递，处理阻塞并重新绑定后恢复。
- 超时、执行中断或无法确认是否提交：记录 `unknown`，使绑定失效，不自动重发。

CLI 超时后终止并回收子进程，但不能据此断言 HerdR 未收到请求。

长连接由 SDK 重连，异常退出由 systemd 重启桥接。重连后不重放数据库中的业务操作，飞书重复投递仍按消息 ID 去重。

飞书回执失败只记录发送失败，不重新执行 HerdR 操作，也不撤销已成功完成的绑定。本版不保证消息必达或严格一次执行，优先避免重复副作用。

## 6. HerdR 适配层与验证边界

薄适配层只提供六项能力：列出 Agent、核验目标、读取输出、提交文本、创建 workspace、启动 Agent。

session 选择、命令参数、JSON 解析和错误分类全部集中在此层。业务层不解析 CLI 原始输出，也不自行拼接命令。

使用 HerdR 可执行文件的绝对路径、参数数组和 `shell=False`。查询超时默认 5 秒，提交、创建和启动默认 15 秒。超时代表结果不明，不代表操作已撤销。

### 6.1 静态契约已确认

以下语法已由本机 HerdR 0.9.0 帮助确认：

| 命令 | 已确认语法 |
|---|---|
| 创建 workspace | `herdr workspace create [--cwd PATH] [--label TEXT] [--env KEY=VALUE] [--focus] [--no-focus]` |
| 列出 Agent | `herdr agent list` |
| 查询 Agent | `herdr agent get <target>` |
| 读取 Agent | `herdr agent read <target> [--source visible\|recent\|recent-unwrapped\|detection] [--lines N] [--format text\|ansi]` |
| 提交文本 | `herdr agent prompt <target> <text> [--wait] [--until STATUS]... [--timeout MS]` |
| 启动 Agent | `herdr agent start <name> --kind KIND --pane ID [--timeout MS] [-- <agent-args...>]` |

`KIND` 包含 `codex` 和 `claude`。

权威契约已说明：

- Agent 目标接受唯一 live Agent 名称，或当前承载 Agent 的 Pane ID。名称会在退出、释放或替换时清除。
- `agent prompt` 在提交时验证目标，并在 blocked 状态拒绝输入。
- `workspace create` 返回 workspace、tab、root_pane 相关 JSON；大部分控制命令返回 JSON。

这些契约的实际返回字段、错误表现及运行效果仍须 live 验证。

MVP 创建时只使用 `--cwd`、`--label`、`--no-focus`；启动时只传生成名称、`--kind` 和 `--pane`；读取时使用 `--source visible --lines 80 --format text`。不追加 Agent 权限绕过参数。

### 6.2 必须完成的 live 验证

| 验证项 | 本机必须确认的结果 |
|---|---|
| 显式 session 选择 | 显式 `herdr --session <name> ...` 能力已有证据，现有真实冒烟在 `lark-herdr-test`。部署切到 `kpi-agg` 及只命中该 session 须在 G3 真实验证，未验证前不得宣称通过；仅在本机规范允许的上下文执行。 |
| `agent list/get` | 保存实际输出样本，确认 workspace 与 Pane 归属、名称、类型等字段，以及错误返回形状。 |
| `agent read` | 验证指定 Pane 的文本输出、行数限制和失效目标错误，不将读取结果预设为 JSON。 |
| `agent prompt` | 验证中文、多行及前导连字符文本的传参方式；确认成功回执含义、运行中输入行为和 blocked 拒绝结果。 |
| 目标失效 | Agent 退出、Pane 回到 Shell、Pane 或 workspace 删除时，调用必须拒绝；不得将消息落入 Shell。另行记录同一 Pane 替换 Agent 后的实际行为，不承诺识别进程替换。 |
| `workspace create` | 验证 JSON 字段和嵌套结构，准确取得新 workspace、tab、根 Pane ID，确认目录和不抢焦点行为。 |
| `agent start` | 分别验证 Codex、Claude 的真实启动、工作目录、返回结果、名称冲突及启动失败表现。 |
| 中断与重启 | 验证 CLI 超时、HerdR 不可用及桥接重启后的保守处理，不自动重做未知操作。 |

解析失败立即停止操作。无法证明命令命中指定 session、核验 workspace/Pane 归属或安全提交时，投递验收不通过。创建结果无法取得可靠 ID 时禁用创建能力，不通过选择最新 workspace 猜测目标。

## 7. 最小安全边界

固定飞书用户白名单，默认拒绝其他来源。会话必须位于配置白名单，或已由第 10 节流程持久化启用；仅把机器人加入群不产生授权。

新建项目目录仅来自配置别名。用户不能决定可执行文件、任意路径、CLI 选项或权限模式。拒绝终端控制字符，允许正常文本换行；无法确认可安全传参的文本不提交。

禁止任意 Shell 执行、Pane 裸输入、按键注入和权限确认代点。workspace 确认仅授权该 workspace 和 Agent；建群确认仅授权该群。后续 Agent 权限提示仍由用户在原终端处理。

`/read` 会将终端内容发送到对应飞书会话，群成员可以看到。日志不记录密钥、完整消息正文或完整终端输出。

## 8. Linux 运行

以与 HerdR 相同的普通 Linux 用户运行桥接。用户预先启动目标 HerdR session，并完成 Codex、Claude 的登录和首次交互初始化。桥接不负责启动或重启 HerdR server。

配置与状态路径：

| 路径 | 内容 |
|---|---|
| `~/.config/feishu-herdr-bridge/config.json` | HerdR 可执行文件、明确 session 选择配置、用户和会话白名单、项目别名、超时。 |
| `~/.config/feishu-herdr-bridge/credentials.env` | 飞书凭据，权限 `0600`，不进入仓库。 |
| `~/.local/state/feishu-herdr-bridge/bridge.sqlite3` | 绑定和操作状态，所在目录权限 `0700`。 |

使用一个 systemd 用户服务，异常退出自动重启，日志写入 journald。本机进程锁防止相同配置启动两个桥接实例。需要退出登录后持续运行时启用用户 linger，并实际验证。

主机保持供电、禁止自动休眠、保持时间同步及到飞书和模型服务的网络连接。主机重启后不保证原 Agent 进程恢复。

飞书接入需本机验证 SDK 初始化、机器人收发权限、@识别、消息 ID、会话与操作者字段、消息时间和重连。接收事件使用 `im.message.receive_v1`，具体 SDK 调用以安装版本实测为准。

## 9. 验收标准

| 场景 | 通过标准 |
|---|---|
| 双会话隔离 | 手动建立的群 A、群 B 分别绑定 workspace A、B；不同测试文本只进入对应原 Pane。 |
| 显式目标 | 普通环境启动桥接、切换桌面焦点、使用相似名称后，读取与投递仍命中配置 session 中的正确目标。 |
| 一对一绑定 | 一个 workspace 被其他会话占用时拒绝绑定；换绑后旧请求不得改投新目标。 |
| 去重 | 同一 `message_id` 重复到达只提交一次；飞书回执失败不触发重复提交。 |
| 目标失效与 blocked | 无 Agent 的 Pane、已删除目标和 blocked 拒绝均不产生裸输入；失败后绑定失效，需重新绑定。 |
| 创建确认 | 未确认、错误或过期确认、其他用户确认均不创建；分别验证 Codex 和 Claude 成功启动并绑定正确根 Pane。 |
| 名称冲突 | 生成名称符合格式；同名预检按限定次数换名；最终启动冲突明确失败，不接管已有同名 Agent。 |
| 部分失败 | 创建后启动失败保留旧绑定并报告新资源；结果不明和重复确认均不创建第二份资源。 |
| 重连与移动端 | 手机移动网络下完成列出、绑定、发送、读取；桥接重启和飞书重连后不重复执行，原任务不受影响。 |
| 安全拒绝 | 非白名单来源、任意目录、未知命令和不安全输入均不触发 HerdR 写操作。 |

同一 Pane 被外部替换 Agent 的进程身份连续性不属于本版保证；必须记录实测行为，不能将其描述为已实现自动识别。

未经 live 验证的能力不得标记可用。

## 10. 自助任务群增量设计（Issue #3）

### 10.1 边界与配置

本轮只增加 `/group-new <群名>`，建群与 workspace/Agent 创建分开确认。所有新群使用同一配置中的 `kpi-agg`；不增加 session 选择参数、`/use`、自然语言路由或群到 session 的映射服务。

新增可选配置 `management_chat_id` 和 `admin_users`。两项都未配置，或分别为 `null` 与空列表时，关闭建群入口，旧配置继续使用原有功能。启用时必须同时满足：管理群是一个非空 ID 且在 `allowed_chats` 中；管理员列表非空且为 `allowed_users` 的子集；`herdr_session` 严格等于 `kpi-agg`。不完整配置或启用时 session 不符，启动失败，不猜默认管理群。

用户仅需初始化一次真实管理群、加入既有机器人、配置上述值并重启部署。管理群的真实性及机器人身份由该次受信初始化核对；不新增自动发现或认领管理群的命令。管理员权限来自配置，与飞书群主、群管理员身份无关。

关闭建群入口不删除既有受管群。它们仍按持久化授权工作，但要求当前 session 为 `kpi-agg`、机器人身份匹配、操作者仍在 `allowed_users` 中。旧绑定指向其他 session 时沿用原有拒绝逻辑，不自动迁移或改投。

### 10.2 动态授权

普通操作同时要求用户获准和会话获准：操作者必须在 `allowed_users` 中；会话还必须位于静态 `allowed_chats`，或在 `group_requests` 中存在 `status=done`、`created_chat_id` 相同、session 和 `bot_open_id` 匹配的记录。受管群不会绕过用户白名单。动态记录只认可真实群聊事件。建群提案与确认还必须满足当前管理员、当前管理群、群聊类型三个条件。

事件入口 `FeishuBridge.receive`、`BridgeCore.prepare` 及执行前复核共用同一授权判定，不再各自只检查静态 frozenset。判定直接读 SQLite，不复制动态 ID 到内存白名单，不靠重启刷新。数据库读取失败时拒绝操作。事件归一化保留 `chat_type`；旧命令的直接调用保持兼容，建群不能把缺失类型当作群聊。

受管群中的其他成员不会自动获得操作权限，群主也不会自动成为桥接管理员。机器人被手动拉入的陌生群不自动入库、不被认领；群名和事件正文中的 ID 均不能改变授权。受管群不能执行 `/group-new`，即使发消息的人是管理员。

新群在授权事务提交后即可使用原有全部命令，无需修改配置或重启。初始不创建 `bindings`：`/agents` 可用，普通文本提示先绑定，后续 `/bind` 或 `/new` 沿用现有流程。原有 workspace 唯一占用约束不变；建群成功不会改动管理群或其他群的绑定。提交前到达的新群消息不缓存、不主动回放。

### 10.3 SQLite 最小迁移

原三表及其已有字段保持不变。`create_requests` 强制要求项目目录和 Agent 类型，不能用假路径或假 Agent 存建群提案。仅新增 `group_requests`，同时承担建群操作记录与受管群登记，不再新增独立群目录表。

| 字段 | 约束与用途 |
|---|---|
| `request_id` | 主键，提案消息 ID，与现有 `requests` 对应。 |
| `confirmation_code` | 唯一，固定为 `g-` 加 16 位随机十六进制字符串。 |
| `source_chat_id`、`requested_by` | 非空，原管理群和发起人的真实 ID，只在私有数据库使用。 |
| `group_name`、`create_uuid` | 非空；群名为确认的原值，UUID 在提案时生成并唯一持久化。 |
| `herdr_session`、`bot_open_id` | 非空身份快照；session 约束为 `kpi-agg`，确认与动态授权均复核。 |
| `created_chat_id` | 可空，数据库必须设 `UNIQUE(created_chat_id)`；记录已知远端资源，禁止覆盖其他请求已登记的群。 |
| `status`、`result_code` | 状态为 `pending / processing / done / failed / unknown`；结果仅存受控原因码。数据库必须设 `CHECK(status <> 'done' OR (created_chat_id IS NOT NULL AND length(created_chat_id) > 0))`，不能仅靠业务层保证 `done` 有群 ID。 |
| `expires_at`、`created_at`、`updated_at` | 提案过期及状态时间，沿用现有秒级时间口径。 |

群的公开稳定参考号为 `G-<create_uuid>`，不增加另一列。UUID 为随机值，不包含或推导自 Feishu 用户、群、消息 ID，也不充当授权凭证。

取得现有进程锁后、接收事件前，用一次 SQLite 事务将旧库 `user_version=0` 升到 1：创建新表及 CHECK/UNIQUE 约束，最后设置版本。重复启动不重复迁移；仅接受 `user_version=0`（执行迁移）或 `1`（核验已迁移结构）。其他 `user_version` 或结构/约束冲突均 fail closed：停止启动，不执行恢复或接收事件，不重置版本、不丢表重建。迁移不改变旧绑定、去重记录和未过期 workspace 确认码。新库使用相同最终结构；不引入迁移框架。备份、数据库和锁文件均留在仓库外。

### 10.4 命令、确认与两个提案类型

`/group-new` 将命令后的剩余文本整体作为群名，保留内部空格；去除首尾空白后限定 1～60 个字符，拒绝换行及终端控制字符。群名不用于查重、路由或资源认领。重复群名不代表同一个操作。

提案只落库，不访问建群 API 或 HerdR。回执展示群名、固定 session、请求者将成为群主、机器人自动入群、不会创建 workspace，以及有效期 5 分钟的 `/confirm g-...` 和参考号。用户不能指定成员列表、群主、token、API 参数或 UUID。

workspace 确认码保持现有十六进制格式，包含升级前已经发出的码；`g-` 前缀只查建群表，其他合法旧码只查 workspace 表，不尝试跨类型回退。确认始终核验同用户、同会话、有效期和当前权限。建群不依赖管理群当前绑定版本，因为它不修改该绑定。

两种提案使用独立命名空间，可以同时存在。`/new` 只沿用并替换本会话 `create_requests` 中的 workspace 提案；`/group-new` 只替换同一管理群、同一请求管理员自己的 `pending` group request，不影响其他管理员的建群提案。成功写入新提案与使对应旧提案失效仍在同一事务内完成，不跨表互相失效。

`/cancel` 对 workspace 保持既有取消语义；对 group request 单独核验当前管理员、当前管理群、群聊类型和原请求者身份，只允许仍获授权的原请求管理员取消自己在该管理群中的 `pending`，不能取消他人的建群提案。普通已授权用户的取消操作不触碰 group request。取消不调用 API、不取消 `processing`、不删除资源。

已消费的建群确认码不能再次触发创建。同一发起人在当前管理群重发该码，只返回已保存的状态与参考号；重复事件也只读取结果。权限已撤销或来自其他群/用户时，不泄露该请求是否存在。无需新增查询或恢复命令。

### 10.5 飞书 API 与 SDK 边界

继续固定 `lark-oapi==1.7.3`，使用现有应用的 tenant 身份，不引入用户 OAuth、飞书 CLI 或新机器人。新增范围内的写 API 仅为 `POST /open-apis/im/v1/chats`，官方权限为 `im:chat:create`；应用须启用机器人能力，权限发布及用户可见范围由本地核实。[S1][S2]

在 `feishu.py` 内增加可注入的薄建群调用；SDK 对象由入口装配，业务层只接收必要的结果和受控错误。调用使用 `CreateChatRequest`、`CreateChatRequestBody` 及 `client.im.v1.chat.create`，在现有业务线程执行，沿用同一个非阻塞操作锁；不在 WebSocket 回调中等待 HTTP，不新增线程池或队列。[S3]

固定请求为：查询 `user_id_type=open_id`、`uuid=create_uuid`；请求体包含确认的 `name`、带参考号和 `kpi-agg` 的简短 `description`、`owner_id=请求者`、`user_id_list=[请求者]`、`chat_mode=group`、`chat_type=private`、`external=false`。不指定其他机器人，不把 `bot_open_id` 填入要求 app ID 的 `bot_id_list`；调用接口的机器人按官方契约自动入群，因此省略该列表，也不额外调用成员添加 API。无需为本轮把机器人设为群管理员。[S1]

官方对相同 `uuid + owner_id` 的建群去重窗口为 10 小时。这只是额外保护，不能替代本地幂等；本实现对已开始的同一请求不再次发送建群 POST，即使窗口已过也不重建。测试须核对 SDK/HTTP 层不会在不确定写后隐式重发。[S1]

成功判定要求 SDK 响应成功、存在有效 `data.chat_id`，且 `owner_id` 等于请求者、`owner_id_type=open_id`、群模式/类型和内部群标记符合请求。机器人入群使用官方创建契约，真实验收再核对请求者与机器人均实际在群及能收到 @ 消息。缺失、矛盾或无法核实的返回按未知处理，不以 HTTP 200 单独判成功。薄调用不能在抛异常时丢弃已经解析出的群 ID。

只返回群名和 `G-<create_uuid>`，满足稳定标识要求；不另取分享链接，不回显真实 chat/user/message ID。现有 workspace/Pane ID 的受控操作回执不变。SDK 示例中的 DEBUG、原始响应和原始异常打印不得带入实现。

### 10.6 执行、部分成功和恢复

执行顺序固定：重新授权及查重 → 事务消费确认码并提交 `processing` → 建群 POST 至多一次 → 单独事务保存已知群 ID，仍未授权 → 核对成功响应 → 事务置 `done` → 返回结果。所有网络等待都在事务外；能解析的已知资源应先保留，再判断其他字段是否足以启用。

`group_requests.status=done` 是动态授权的唯一完成依据。确认消息在 `requests` 的最终状态与它分开记账；授权提交后即使回执发送失败、`requests` 落库失败或进程退出，也不能把已完成的群撤销或重新创建。服务重启只把遗留 `group_requests.processing` 转为 `unknown`，保持 `done` 不变，恢复过程不调用创建 API。

| 边界 | 持久化与对外行为 |
|---|---|
| 消费确认码前本地失败 | 不调用 API；原消息按已有去重机制记录，禁止绕过失败继续创建。 |
| 明确未发送或有已核实的无副作用拒绝 | `failed`，无授权；其他 API 错误不靠错误文案猜测是否建成。 |
| POST 超时、连接断开、服务异常、返回无法解析或执行中退出 | `unknown`；无 ID 时保留 UUID 和提案，绝不重新提交。 |
| 已知群 ID，但响应字段不符、成员初始化未获可信确认或后续步骤失败 | 保存群 ID，`unknown` 且不授权；不补拉成员、不删群、不另建。 |
| 保存群 ID 成功、启用事务失败或结果不明 | 保留资源；未确认 `done` 时不作内存授权。可做一次本地只读复核，不重发远端写、不重跑启用事务。 |
| 授权已提交，回执失败或消息级状态未知 | 群仍可用；重发原确认码只读出群记录的真实完成状态和参考号。 |

特别处理 API 已成功但 SQLite 完全不可写：不能承诺群 ID 已持久化。保持数据库中已消费请求的 `processing/unknown`，不把群加入内存白名单。仅回报已创建但尚未确认启用或结果待核对及参考号，不在日志泄露真实 ID。若回复也失败，人工仍可通过预先存储的 UUID 和群描述参考号核对。服务不会扫描同名群、补建、自动认领，亦不新增本地旁路日志数据库。

人工恢复仅用于异常：停止相关操作，在合法环境核实远端群、请求者、机器人和原 UUID；由本地 Codex 审核后窄修原记录。不得把状态改回 `pending` 或再次发送原建群请求。无法确定真实群时维持 `unknown`。即使要重新 `/group-new`，也须先人工排除重复资源。

停止时关闭新工作入口；已经发出的 HTTP 写可能仍在飞书完成，本地取消不代表远端回滚。建群调用设置有限超时，收到停止信号后不开始新的 POST；结束时尽力保存已知资源，否则由下次启动转 `unknown`。不延伸为自动恢复任务或后台轮询。

### 10.7 隐私与验收依据

已知真实 ID 只允许进入必要的 SDK 参数和受限 SQLite；数据库文件 `0600`、状态目录 `0700`。修正现有 `FeishuBridge._deliver`、`LarkTransport._send_once` 的回执失败日志，移除原始 chat/message ID。建群相关日志仅保留动作、受控结果码和随机参考号，不记群名、确认码、原始异常、HTTP 头或响应正文。公开 fixture 和验收证据必须为合成或脱敏数据。

本轮验收与批次见 `docs/self-service-groups-plan.md`。原 MVP 的历史实测记录不改写成本轮通过；新建群、动态授权和异常恢复均须单独验收。HerdR 调用继续遵守本机合法上下文要求，不伪造 `HERDR_ENV`。

资料核对日期为 2026-09-11；以下为官方接口/源码证据，不能替代安装版本和租户的实际验收：

- [S1] [官方 CLI 内置 API 目录：建群字段、机器人自动入群、UUID 去重窗口](https://github.com/larksuite/cli/blob/b8b21da3a57b5634b0dc6f5074d479f1e751e658/internal/registry/catalog/services/im.json)。
- [S2] [官方建群封装：应用身份及 im:chat:create 权限](https://github.com/larksuite/cli/blob/b8b21da3a57b5634b0dc6f5074d479f1e751e658/shortcuts/im/im_chat_create.go)。本项目不调用该 CLI。
- [S3] [官方 Python 建群示例](https://github.com/larksuite/oapi-sdk-python/blob/0b9e6e48b74bb4b34462fc67b7e738b27e73e697/samples/api/im/v1/create_chat_sample.py)及同目录 SDK 模型。所查为该源码提交，1.7.3 的精确接口、超时及重试行为另由本地离线核验。

## 11. 非目标

本版不做一键建群加 workspace/Agent，不做群删除、归档或后续成员管理，不做广义 RBAC、网页前端、插件系统、多主机、多 HerdR session、`/use` 或远程 Shell。

不做监控 workspace、总监控 Agent、自然语言控制命令解析、自动拆任务、多 Agent 编排、任务队列、主动完成通知或持续输出推送。

不支持 `/clear`、审批代点、文件上传下载或 Agent 自动恢复。飞书负责发指令和按需读输出，现有编排 Agent 继续承担任务内部协作。
