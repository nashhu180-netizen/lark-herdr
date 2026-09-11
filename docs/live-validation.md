# 真实边界验收记录

编写时间：2026-09-11。当前结论：Linux 上的核心 MVP 真实闭环已通过；破坏性、异常重投和移动网络等扩展边界仍保留未测。

当前自动化进程没有 `HERDR_ENV=1`，不读取或控制已有 session，也不伪造该变量。以下命令只能在符合 HerdR 本机规范的合法上下文执行；权限前提不满足时停止，保留未测。

使用隔离的测试项目、临时 workspace 和授权测试群，避免现有任务及敏感输出。本次测试群由官方 `lark-cli` 创建并加入既有机器人；桥接本身不创建群。本文不把静态帮助、schema-derived fixture、fake CLI、SDK 离线检查或 service active 当作真实功能通过。

## 记录信息

执行人为本地 Codex 与授权用户，时间为 2026-09-11，受测提交为 `7da970c`。环境为 Linux ThinkPad、Python 3.12.3、`lark-oapi` 1.7.3、HerdR 0.9.0、`lark-cli` 1.0.94，用户级 systemd 服务以普通用户运行。专用 session 为 `lark-herdr-test`；公开记录仅称测试群 A/B。A 绑定 `w3 / w3:p1` 的 Codex，B 绑定 `w4 / w4:p1` 的 Claude。真实 chat、用户和 Agent 标识不写入仓库。

下表只把实际跑过的边界标为通过或部分通过；不能安全构造的场景保留未测，不用离线模拟替代。任何涉及操作结果不明的场景都先查现场，不自动重试。

## 可复用的真实 CLI 命令

仅在合法上下文设置真实的绝对 `HERDR_BIN`、已预启动的 `SESSION`、测试 `PANE`；创建/启动测试另设置 `PROJECT`、`LABEL`、`NAME`、`NEW_PANE`。不要用示例标识操作业务任务。

    "$HERDR_BIN" --session "$SESSION" agent list
    "$HERDR_BIN" --session "$SESSION" agent get "$PANE"
    "$HERDR_BIN" --session "$SESSION" agent read "$PANE" --source visible --lines 80 --format text
    "$HERDR_BIN" --session "$SESSION" agent prompt "$PANE" '仅回复本次测试标记，不修改项目'
    "$HERDR_BIN" --session "$SESSION" workspace create --cwd "$PROJECT" --label "$LABEL" --no-focus
    "$HERDR_BIN" --session "$SESSION" agent start "$NAME" --kind codex --pane "$NEW_PANE" --timeout 15000

只在该测试行需要时运行对应命令，不能整段重复执行创建操作。Claude 启动测试把 `--kind` 改为 `claude`。记录退出码，并分开保存脱敏 stdout 和 stderr；成功 envelope 从 stdout 检查，非零 CLI 的结构化错误从 stderr 检查。

## 验收表

| 项目与命令/操作 | 目标 | 预期 | 实际 | 结论 | 证据 |
|---|---|---|---|---|---|
| 合法上下文、预启动 session、`agent list/get/read` | 配置 session 与测试 Pane | CLI 全部显式选 session；得到正确归属，文本读取可用 | 两群 `/agents` 和 `/read` 均返回专用 session 内的正确 Pane 内容 | 通过 | 飞书回执：A=`w3:p1/A_ROUTE_OK`，B=`w4:p1/B_ROUTE_OK` |
| 核对成功及错误 envelope | protocol 22 | 实际字段匹配适配层；创建使用 workspace_id/tab_id/pane_id；非零错误由 stderr 解码；无法识别即拒绝 | Codex 创建解析出 workspace/tab/pane；Claude 首次启动的非零错误被保守报告为 `remote_error` | 通过 | 创建记录及脱敏飞书回执 |
| 飞书群 @、私聊、未知命令、非白名单消息 | 授权与未授权会话 | 仅授权文本进入核心；非文本、自身消息被忽略；未知斜杠命令不透传 | 两个白名单群的真实 @ 文本进入；未带真实 mention 的 `/agents` 被忽略；其余边界未实测 | 部分通过 | 服务请求计数及两群回执 |
| 两群分别 `/bind`，发送不同测试标记并 `/read` | workspace A/Pane A、workspace B/Pane B | 各自只收到本群文本，回执目标正确；另一群占用同 workspace 时拒绝 | A 仅向 `w3:p1` 投递 `A_ROUTE_OK`，B 仅向 `w4:p1` 投递 `B_ROUTE_OK`；读取无串线 | 通过 | 两个 Agent 的终端回显与持久化绑定 |
| 切换桌面焦点、相近名称、换绑后迟到消息 | 原绑定与新绑定 | 路由只用配置 session 与 ID；旧消息不改投新目标 | 待记录 | 未测 | 待附 |
| 中文、多行、特殊字符与前导连字符输入 | 测试 Pane | 正常文字准确传递，控制字符拒绝；未获支持的前导连字符继续拒绝，不猜参数 | 两条中文指令均准确投递一次；多行、特殊字符、控制字符和前导连字符未实测 | 部分通过 | A/B 终端原样回显 |
| 同一 message_id 重投与回执失败 | 单条测试任务 | HerdR 最多执行一次，回执发送失败不重做；无法触发真实重投时记录未测 | 待记录 | 未测 | 待附 |
| `/new` 不确认、错误/过期/跨用户确认、`/cancel` | 待创建请求 | 无资源写入；只有有效同人同会话确认可执行 | 两次有效同人同会话确认均只执行一次；错误、过期、跨用户与取消未实测 | 部分通过 | 脱敏请求状态记录 |
| 分别确认创建 Codex、Claude | 临时 workspace 的根 Pane | 名称符合格式且 live 唯一；create、start、get 都确认后才换绑，旧 workspace 不变 | Codex 完整创建并自动绑定；Claude workspace/Agent 已创建，但首启被客户端首次运行界面阻塞，人工解除后显式绑定 | 部分通过 | `/agents`、创建回执及最终绑定 |
| 名称碰撞与启动时冲突 | 临时创建请求 | 最多一次换名；未接管同名 Agent，未自动重建 workspace | 待记录 | 未测 | 待附 |
| 创建成功但启动失败、创建/启动响应不明 | 临时资源及原绑定 | 旧绑定保留，已知资源 ID 记录并报告；未知操作与确认码不重复执行 | Claude 首启返回 `remote_error`；`w4/w4:p1` 保留、旧绑定未改、确认码未重试，人工核实后用新 `/bind` 恢复 | 通过 | 飞书失败回执、`/agents` 与请求状态 |
| blocked、Agent 退出、Pane 回到 Shell、目标删除 | 可丢弃的测试 Pane | HerdR 拒绝不合法输入，桥接使绑定失效，无裸输入或确认代点；未知错误不假称已识别 | 实测 Claude blocked 时不自动绑定，解除后才人工绑定；退出、Shell 和删除未实测 | 部分通过 | Claude 首启失败及恢复回执 |
| 同 Pane 人工替换 Agent | 可丢弃的测试 Pane | 仅记录实际行为；本版没有进程身份保证，替换后人工重新绑定 | 待记录 | 未测 | 待附 |
| 群中任务运行时 `systemctl --user stop`；合法前台运行时 Ctrl+C | 桥接及当前 CLI 子进程 | 停止接收新工作、CLI 被回收；既有 Agent 不被桥接杀死；不确定写不重试 | 待记录 | 未测 | 待附 |
| 同配置再启动、正常退出后重启 | 用户服务与同配置前台入口 | 第二实例退出 3，未运行 SQLite 恢复/连接 SDK；首实例退出后可正常取锁 | 用户服务正常重启并重新建立连接；真实双开未执行 | 部分通过 | systemd 状态与重启前后进程变化 |
| 桥接执行中断、`systemctl --user restart`、SDK 断线重连 | 持久化状态和原任务 | 遗留 processing 转 unknown，不自动重做；绑定使用前重验，确认请求不续跑 | 重启后 A/B 绑定仍有效，两个 Agent 均存活，分别 `/read` 仍返回各自标记；执行中断未实测 | 部分通过 | 重启后两群 `/read` 回执 |
| 手机移动网络、退后台、主机锁屏/退出登录 | 手机、用户服务、原 session | 能重新交流且不串线；linger、生存状态和不休眠分别确认 | 待记录 | 未测 | 待附 |
| `stat` 权限、用户服务日志与 Git 差异检查 | 私有配置、凭据、数据库、锁文件 | 私有文件 0600，受限目录 0700；凭据与正文不进入仓库/日志；锁文件在仓库外 | 配置目录和状态目录为 0700，凭据、配置及 unit 为 0600；运行状态位于仓库外，日志未记录正文或凭据 | 通过 | 本机 `stat`、journal 与 Git 检查 |

## 交接结论

真实通过的核心闭环包括：飞书 WebSocket 接入、群聊真实 @ 门禁、`/agents`、Codex `/new` + `/confirm`、Claude 部分失败后的人工核实与 `/bind`、普通文本投递、`/read`、双群隔离，以及服务重启后的绑定持久化。

本次真实失败点是 Claude 首次启动被客户端首次运行界面阻塞，桥接按未知远端结果保留资源、不换绑、不重试；人工处理客户端后可恢复。这符合安全边界，但属于需要运维手册明确说明的体验限制。

仍未覆盖：私聊、非白名单和非文本输入、未知命令、恶意字符、重复投递与回执失败、确认过期/跨用户/取消、名称碰撞、Agent 退出或目标删除、执行中停机、真实双开、移动网络/锁屏/linger，以及 SDK 断线重连。结论仅为“核心 MVP 真实冒烟通过”，不是完整破坏性矩阵通过。
