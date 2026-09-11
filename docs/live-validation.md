# 真实边界验收记录

编写时间：2026-09-11。初始结论：全部未测，等待本地 Codex 与合法操作者验收。

当前自动化进程没有 `HERDR_ENV=1`，不读取或控制已有 session，也不伪造该变量。以下命令只能在符合 HerdR 本机规范的合法上下文执行；权限前提不满足时停止，保留未测。

使用隔离的测试项目、临时 workspace 和授权测试群，避免现有任务及敏感输出。用户手动建群、加机器人并配置白名单。桥接不创建群。本文不把静态帮助、schema-derived fixture、fake CLI、SDK 离线检查或 service active 当作真实功能通过。

## 记录信息

执行人、执行时间、代码提交、Python/SDK/HerdR 版本、合法操作上下文、服务用户、session、测试 chat/workspace/Pane ID：均待填。标识在回传或入库前按需脱敏，保持各步骤的目标映射一致。

全部项目初始为未测。完成后逐行填写实际结果、结论及脱敏证据路径；不能安全构造的场景写明原因并保留未测，不用离线模拟替代。任何涉及操作结果不明的场景都先查现场，不自动重试。

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
| 合法上下文、预启动 session、`agent list/get/read` | 配置 session 与测试 Pane | CLI 全部显式选 session；得到正确归属，文本读取可用 | 待记录 | 未测 | 待附 |
| 核对成功及错误 envelope | protocol 22 | 实际字段匹配适配层；创建使用 workspace_id/tab_id/pane_id；非零错误由 stderr 解码；无法识别即拒绝 | 待记录 | 未测 | 待附 |
| 飞书群 @、私聊、未知命令、非白名单消息 | 授权与未授权会话 | 仅授权文本进入核心；非文本、自身消息被忽略；未知斜杠命令不透传 | 待记录 | 未测 | 待附 |
| 两群分别 `/bind`，发送不同测试标记并 `/read` | workspace A/Pane A、workspace B/Pane B | 各自只收到本群文本，回执目标正确；另一群占用同 workspace 时拒绝 | 待记录 | 未测 | 待附 |
| 切换桌面焦点、相近名称、换绑后迟到消息 | 原绑定与新绑定 | 路由只用配置 session 与 ID；旧消息不改投新目标 | 待记录 | 未测 | 待附 |
| 中文、多行、特殊字符与前导连字符输入 | 测试 Pane | 正常文字准确传递，控制字符拒绝；未获支持的前导连字符继续拒绝，不猜参数 | 待记录 | 未测 | 待附 |
| 同一 message_id 重投与回执失败 | 单条测试任务 | HerdR 最多执行一次，回执发送失败不重做；无法触发真实重投时记录未测 | 待记录 | 未测 | 待附 |
| `/new` 不确认、错误/过期/跨用户确认、`/cancel` | 待创建请求 | 无资源写入；只有有效同人同会话确认可执行 | 待记录 | 未测 | 待附 |
| 分别确认创建 Codex、Claude | 临时 workspace 的根 Pane | 名称符合格式且 live 唯一；create、start、get 都确认后才换绑，旧 workspace 不变 | 待记录 | 未测 | 待附 |
| 名称碰撞与启动时冲突 | 临时创建请求 | 最多一次换名；未接管同名 Agent，未自动重建 workspace | 待记录 | 未测 | 待附 |
| 创建成功但启动失败、创建/启动响应不明 | 临时资源及原绑定 | 旧绑定保留，已知资源 ID 记录并报告；未知操作与确认码不重复执行 | 待记录 | 未测 | 待附 |
| blocked、Agent 退出、Pane 回到 Shell、目标删除 | 可丢弃的测试 Pane | HerdR 拒绝不合法输入，桥接使绑定失效，无裸输入或确认代点；未知错误不假称已识别 | 待记录 | 未测 | 待附 |
| 同 Pane 人工替换 Agent | 可丢弃的测试 Pane | 仅记录实际行为；本版没有进程身份保证，替换后人工重新绑定 | 待记录 | 未测 | 待附 |
| 群中任务运行时 `systemctl --user stop`；合法前台运行时 Ctrl+C | 桥接及当前 CLI 子进程 | 停止接收新工作、CLI 被回收；既有 Agent 不被桥接杀死；不确定写不重试 | 待记录 | 未测 | 待附 |
| 同配置再启动、正常退出后重启 | 用户服务与同配置前台入口 | 第二实例退出 3，未运行 SQLite 恢复/连接 SDK；首实例退出后可正常取锁 | 待记录 | 未测 | 待附 |
| 桥接执行中断、`systemctl --user restart`、SDK 断线重连 | 持久化状态和原任务 | 遗留 processing 转 unknown，不自动重做；绑定使用前重验，确认请求不续跑 | 待记录 | 未测 | 待附 |
| 手机移动网络、退后台、主机锁屏/退出登录 | 手机、用户服务、原 session | 能重新交流且不串线；linger、生存状态和不休眠分别确认 | 待记录 | 未测 | 待附 |
| `stat` 权限、用户服务日志与 Git 差异检查 | 私有配置、凭据、数据库、锁文件 | 私有文件 0600，受限目录 0700；凭据与正文不进入仓库/日志；锁文件在仓库外 | 待记录 | 未测 | 待附 |

## 交接结论

真实通过项：暂无。真实失败项：待记录。剩余未测项：上表全部。

本地 Codex 填写实测结果与必要小修的回归证据后交回审核。当前不得宣称 MVP 已完成真实验收，不进入新功能开发。
