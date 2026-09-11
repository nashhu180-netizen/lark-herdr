# 飞书轻量桥接 HerdR

修订时间：2026-09-11。
状态：草案，待 live 验证与本地验收。
目标：当天在原生 Linux 完成最小 MVP。

本机版本为 HerdR 0.9.0。本轮本地审核进程的 `HERDR_ENV` 为空，默认 server 未运行，因此已取得的证据仅覆盖 CLI 静态契约，尚未验证真实运行边界。

## 1. 范围

用户手动创建飞书群或打开与机器人的私聊，并将机器人加入群聊。桥接不创建飞书群。

一个飞书 `chat_id` 绑定一个 HerdR workspace 中的一个编排 Agent Pane。多个会话分别绑定不同 workspace，普通文本提交到各自绑定的原 Pane，不另起独立聊天或 Agent。

支持列出 Agent、绑定已有目标、手动读取输出，以及经确认后创建 workspace 并启动 Codex 或 Claude。创建成功后绑定新目标，后续任务说明通过普通文本发送。

桥接仅连接配置指定的一个 HerdR session。手机无需直接连接 Linux，Linux 不开放公网入站端口。

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

操作状态使用 `processing / done / failed / unknown`。创建请求另有 `pending`；取消或过期后不可执行。

不持久化普通消息正文、完整终端输出和密钥。重启后保留绑定，首次使用重新核验目标。遗留的 `processing` 标记为 `unknown`，不自动续跑；结果不明的投递要求人工检查并重新绑定。

## 4. 飞书命令与交互

仅处理用户和会话白名单内的文本消息。群聊要求明确 @ 本机器人，私聊直接处理。先剥离机器人的提及，再解析命令。忽略机器人自身消息、非文本消息和编辑事件。

以下均为桥接命令。

| 输入 | 行为 |
|---|---|
| `/agents` | 列出配置 session 内的 Agent，按 workspace 展示实际 workspace ID、Pane ID、名称、类型、可获得的状态及绑定占用情况。 |
| `/bind` | 显示本会话当前绑定及有效性。 |
| `/bind <workspace_id> <pane_id>` | 核验目标后建立或替换绑定；workspace 已被其他会话占用则拒绝。 |
| `/read` | 读取绑定 Pane 的可见终端输出，最多 80 行、3000 字符，截断时提示。 |
| `/new <项目别名> <codex或claude>` | 建立待确认请求，不创建 workspace，也不启动 Agent。 |
| `/confirm <确认码>` | 执行本会话内、同一发起人的有效创建请求。 |
| `/cancel` | 取消本会话尚未执行的创建请求，不中断任何 Agent。 |
| 普通文本 | 提交到本会话绑定的原 Pane Agent。 |

未知的 `/` 命令返回帮助，不透传。未绑定或绑定失效时，普通文本只返回绑定提示。

`/read` 是终端快照，不保证包含完整对话。每条目标相关回执都带 workspace 和 Pane 标识。

### 创建确认与 Agent 名称

项目别名映射为配置中的固定绝对路径。workspace 标签使用项目别名和请求短 ID，不允许用户传入任意目录或 CLI 参数。

Agent 名称在创建请求中生成并保存：

- Codex：`fb-codex-<12位小写十六进制随机串>`。
- Claude：`fb-claude-<12位小写十六进制随机串>`。

实际名称必须满足 `[a-z][a-z0-9_-]{0,31}`。启动前用 `agent list` 检查 live 同名，冲突时重新生成一次并更新请求。再次冲突，或 `agent start` 因并发竞争明确返回名称冲突时，本次操作失败；不复用同名 Agent，不自动重建 workspace。最终唯一性以 HerdR 启动结果为准。

确认消息显示项目实际路径、workspace 标签、Agent 类型、拟用名称及成功后将替换的绑定。确认码有效期为 5 分钟，只能使用一次。同一会话的新请求使旧的未执行请求失效。

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
| 显式 session 选择 | 桥接从 `HERDR_ENV` 为空的普通环境运行时，所有命令仍进入配置指定的 session；不依赖桌面焦点或隐式默认 server。选择方法需另行核对，不假定存在 `--session`。 |
| `agent list/get` | 保存实际输出样本，确认 workspace 与 Pane 归属、名称、类型等字段，以及错误返回形状。 |
| `agent read` | 验证指定 Pane 的文本输出、行数限制和失效目标错误，不将读取结果预设为 JSON。 |
| `agent prompt` | 验证中文、多行及前导连字符文本的传参方式；确认成功回执含义、运行中输入行为和 blocked 拒绝结果。 |
| 目标失效 | Agent 退出、Pane 回到 Shell、Pane 或 workspace 删除时，调用必须拒绝；不得将消息落入 Shell。另行记录同一 Pane 替换 Agent 后的实际行为，不承诺识别进程替换。 |
| `workspace create` | 验证 JSON 字段和嵌套结构，准确取得新 workspace、tab、根 Pane ID，确认目录和不抢焦点行为。 |
| `agent start` | 分别验证 Codex、Claude 的真实启动、工作目录、返回结果、名称冲突及启动失败表现。 |
| 中断与重启 | 验证 CLI 超时、HerdR 不可用及桥接重启后的保守处理，不自动重做未知操作。 |

解析失败立即停止操作。无法证明命令命中指定 session、核验 workspace/Pane 归属或安全提交时，投递验收不通过。创建结果无法取得可靠 ID 时禁用创建能力，不通过选择最新 workspace 猜测目标。

## 7. 最小安全边界

固定飞书用户 `open_id` 和会话 `chat_id` 白名单，默认拒绝其他来源。用户建群并加入机器人后，仍需将会话加入配置白名单。

新建项目目录仅来自配置别名。用户不能决定可执行文件、任意路径、CLI 选项或权限模式。拒绝终端控制字符，允许正常文本换行；无法确认可安全传参的文本不提交。

禁止任意 Shell 执行、Pane 裸输入、按键注入和权限确认代点。创建确认仅授权新建 workspace 与启动 Agent，后续权限提示仍由用户在原终端处理。

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

## 10. 非目标

本版不自动创建飞书群，不管理群成员，不做网页前端、插件系统、多主机、多 HerdR session 或远程 Shell。

不做监控 workspace、总监控 Agent、自然语言控制命令解析、自动拆任务、多 Agent 编排、任务队列、主动完成通知或持续输出推送。

不支持 `/clear`、审批代点、文件上传下载或 Agent 自动恢复。飞书负责发指令和按需读输出，现有编排 Agent 继续承担任务内部协作。
