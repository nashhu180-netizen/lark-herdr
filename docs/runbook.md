# Linux 运行与恢复

修订时间：2026-09-12。范围：核心 MVP、Issue #3 的 Batch G3 操作说明，以及 Issue #7 自动 Pane 回传的使用边界；设计分别以 `docs/design.md`、`docs/auto-pane-output-design.md` 为准。第 6 节对应实现尚未部署，其真实验收步骤全部未执行；`fix/real-devin-output` 补充的真实 Devin CLI 布局识别同样未经真实验收。

既有 Linux 核心 MVP 冒烟使用 `lark-herdr-test`，历史结果保留在 `docs/live-validation.md`。本轮基线为 `ec1f6e1`，G2 的固定 SDK 离线测试已由本地审核通过；自助建群、部署切到 `kpi-agg` 及只命中该 session 的真实验收均未测，另记录在 `docs/self-service-groups-validation.md`。静态 schema、fake 测试和历史冒烟不能替代本轮真实证据。保留 protocol-22 的精确 ID 字段及 stderr 错误解析，不在文档批次修改实现。

## 1. 前置条件与离线检查

使用原生 Linux、Python 3.11+，以运行 HerdR 的同一普通用户操作。预先完成 Codex、Claude 登录及首次初始化；用户手动建群或打开私聊、加入机器人、配置飞书收发权限及长连接事件 `im.message.receive_v1`。

没有 `HERDR_ENV=1` 的协调进程不得读取或控制现有 session。下文所有真实启动、查询和写入必须由用户在符合本机 HerdR 规范的合法上下文执行。不得手工设置 `HERDR_ENV=1` 冒充 Pane 上下文。用户服务访问预启动 session 的方式也须先获得本机规范允许；未确认前只安装文件和运行离线测试，不启动服务。

已有服务升级须先按 2.2 停服、备份，再更新部署代码或环境；不要让新版本先打开原数据库。新安装无需旧库迁移。以下在仓库根目录执行：依赖安装可能访问包源，测试本身不需要凭据或网络；已有离线 wheel/依赖时按本地环境安装，不变更精确依赖 `lark-oapi==1.7.3`。

    python3 -m venv .venv
    .venv/bin/python -m pip install -e .
    .venv/bin/python -m pip check
    .venv/bin/python -m compileall -q feishu_herdr_bridge tests
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m feishu_herdr_bridge --help

验收要求测试无失败，已安装 SDK 的离线契约测试不能跳过。`tests/test_runtime.py` 只启动临时假 CLI，测试进程锁、退出回收和恢复；不会调用配置中的真实 HerdR。

## 2. 仓库外配置

仅首次安装复制以下文件；存在私有配置时停止并人工比较，不覆盖现有值。

    umask 077
    install -d -m 700 "$HOME/.config/feishu-herdr-bridge"
    install -d -m 700 "$HOME/.local/state/feishu-herdr-bridge"
    test ! -e "$HOME/.config/feishu-herdr-bridge/config.json" && \
      install -m 600 examples/config.example.json "$HOME/.config/feishu-herdr-bridge/config.json"
    test ! -e "$HOME/.config/feishu-herdr-bridge/credentials.env" && \
      install -m 600 examples/credentials.env.example "$HOME/.config/feishu-herdr-bridge/credentials.env"

本地编辑私有副本，填写真实的 HerdR 绝对可执行路径、机器人 `open_id`、操作者及会话白名单、允许创建的项目绝对目录。示例固定 `herdr_session="kpi-agg"`，以 `management_chat_id=null`、`admin_users=[]` 默认关闭建群；其余占位值也须替换。项目别名用于 `/new`。配置 JSON 不插值 `~`、环境变量或 systemd 的 `%h`。

密钥只写入私有 `credentials.env`。使用 systemd `EnvironmentFile` 支持的赋值格式；需要时引用值，不使用 `export`、命令替换或变量展开。不要 `cat` 凭据、开启 shell trace、将凭据粘贴到日志或仓库。

    chmod 600 "$HOME/.config/feishu-herdr-bridge/config.json" \
      "$HOME/.config/feishu-herdr-bridge/credentials.env"
    stat -c '%a %n' "$HOME/.config/feishu-herdr-bridge/"{config.json,credentials.env}

省略 `database` 时默认使用 `~/.local/state/feishu-herdr-bridge/bridge.sqlite3`。自定义数据库路径必须为仓库外绝对路径，父目录权限 `0700`，文件权限 `0600`。服务的 `UMask=0077` 保护新建文件。

### 2.1 一次可信管理群初始化

由用户手动创建或选择一个可信管理群，加入现有应用的机器人。核对群的真实 ID、机器人 `bot_open_id` 与当前 `FEISHU_APP_ID` 属于同一应用；管理员 ID 使用该应用的用户 `open_id`。真实 ID 仅填私有配置，不写入仓库或公开验收材料。

在原应用中启用机器人能力，保留已有消息收发和事件权限，并申请 `im:chat:create`。权限变更须完成应用版本发布及租户所需审批，实际可用范围须覆盖请求管理员；仅在开发后台勾选权限不等于已生效。沿用应用 tenant 身份，不配置用户 OAuth，不新建另一机器人，也不以手动补拉成员代替建群验收。API 边界及依据见 `docs/design.md` 10.5。

启用时同时填写 `management_chat_id` 与非空 `admin_users`：管理群必须已在静态 `allowed_chats`，管理员必须全部属于 `allowed_users`，session 必须为 `kpi-agg`。群主或飞书群管理员身份不授予桥接管理员权限。半配置、非白名单管理群、管理员越出用户白名单或启用时 session 错误，均停止启动，不猜默认值。

旧配置同时省略两项时继续原有功能；显式 `null` 与空列表也关闭建群。只填写其中一项不合法。关闭建群入口不删除已完成受管群；它们仍须匹配当前 `kpi-agg`、机器人身份和用户白名单。不要通过改机器人 ID 认领其他应用的群。切换旧部署的 session 不会迁移旧绑定；旧 session 绑定须人工核对后重新绑定，不能直接改数据库里的 session 字段。

上述应用权限与管理配置只需初始化一次，并在部署时重启。后续正常建群不改配置、不重启，也不把新群 ID 追加进 `allowed_chats`。

### 2.2 旧实例停服、备份与迁移

下面是供本地操作者执行的旧实例备份步骤，不是配置校验或 dry-run。先核实当前服务使用的数据库路径；如配置了 `database`，将 `DB` 改成该绝对路径。新安装没有旧库时跳过备份，不通过创建空库替代已有数据。确保没有其他进程写同一数据库，任一步失败即停止，不继续部署。

    (
      set -eu
      umask 077
      CFG="$HOME/.config/feishu-herdr-bridge/config.json"
      DB="$HOME/.local/state/feishu-herdr-bridge/bridge.sqlite3"
      systemctl --user stop feishu-herdr-bridge.service
      test "$(systemctl --user show feishu-herdr-bridge.service -p ActiveState --value)" = inactive
      test -f "$CFG"
      test -f "$DB"
      install -d -m 700 "$HOME/.local/state/feishu-herdr-bridge"
      BACKUP="$(mktemp -d "$HOME/.local/state/feishu-herdr-bridge/backup-XXXXXXXX")"
      install -m 600 "$CFG" "$BACKUP/config.json"
      for FILE in "$DB" "$DB-wal" "$DB-shm" "$DB-journal"; do
        if [ -f "$FILE" ]; then install -m 600 "$FILE" "$BACKUP/"; fi
      done
      printf '%s\n' "$BACKUP"
    )

私下记录原运行提交和备份目录；不要把刚拉取的工作树提交误记为原运行版本。备份保持目录 `0700`、文件 `0600`，不入仓库。保留原凭据文件的 `0600` 权限，不输出其内容。

数据库当前目标版本为 `user_version=2`。启动时在实例锁内严格识别历史 v0、四表 v1 和 v2：v1→v2 在同一事务中重建 `create_requests`，仅将 `agent_kind` 的 CHECK 扩大为 `codex/claude/devin`，保留 rowid、全部列值和原有约束；历史 v0 同事务升级，新库直接得到 v2，已有 v2 只核验、不重复复制。`group_requests`、群授权、绑定和幂等数据不变。未知版本、表结构、索引或依赖冲突会停止启动，不自动修库；详见 `docs/devin-agent-design.md` 第 4 节。启动程序会打开数据库并连接 SDK，不能将它当作无副作用的校验命令。

迁移失败时停住服务以中止自动重启，保留原库、备份和脱敏日志，交本地 Codex 核查。不得删表、重置 `user_version` 或删除数据库重建。若部署后可能发生过远端写，不能直接恢复写前备份再启动；旧备份可能丢失已消费确认码和受管群记录，须先核对远端与最新本地状态，再审核恢复步骤。

Devin 开发和离线回归只在隔离 worktree、独立环境与临时数据库中进行，live 服务继续使用已接受的 main。只有本地协调者批准的维护窗口才能按上述步骤停服、备份并部署候选版本，不并行启动第二个消费者，不把候选 `Store` 指向 live 数据库作探针。

v2 数据库不能交给只接受 v1 的旧程序（包括 `ee57eca`）直接启动；不提供自动降级。升级后已有远端写时，恢复写前备份可能丢失已消费确认码和受管群，禁止据此重发创建。保留最新 v2 状态并交本地审核恢复或使用兼容 v2 的窄修版本，不手改 `user_version` 绕过检查。

## 3. 安装用户服务模板

模板只替换 `@REPO_DIR@` 和 `@VENV_PYTHON@`。从仓库根目录执行以下本地替换，不启动服务；路径含换行时拒绝。

    .venv/bin/python - <<'PY'
    from pathlib import Path
    repo = Path.cwd().resolve()
    python = repo / '.venv/bin/python'
    if not python.is_file():
        raise SystemExit('Missing virtual-environment Python')
    def unit_path(path, quoted=False):
        value = str(path)
        if '\n' in value or '\r' in value:
            raise SystemExit('Unsupported path')
        value = value.replace('%', '%%')
        return value.replace('\\', '\\\\').replace('"', '\\"') if quoted else value
    template = (repo / 'deploy/feishu-herdr-bridge.service').read_text()
    template = template.replace('@REPO_DIR@', unit_path(repo))
    template = template.replace('@VENV_PYTHON@', unit_path(python, quoted=True))
    target = Path.home() / '.config/systemd/user/feishu-herdr-bridge.service'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(template)
    target.chmod(0o600)
    PY
    systemd-analyze --user verify "$HOME/.config/systemd/user/feishu-herdr-bridge.service"
    systemctl --user daemon-reload

不添加 `User=root`，不将凭据内嵌到 unit，不添加 `HERDR_ENV=1`。仓库或虚拟环境路径改变后重新替换模板。

## 4. 真实启动与日常使用

在合法上下文通过 HerdR 自身受支持的方式预启动 `kpi-agg`。本轮只使用该 session 内经授权的测试 workspace，不切到 `lark-herdr-test` 代替验收，也不通过群命令选择 session。桥接不会启动或重启 HerdR server。按 `docs/self-service-groups-validation.md` 记录本轮结果，旧 `docs/live-validation.md` 不改写。

确认本机权限边界、私有配置和依赖均已就绪后，由合法操作者执行：

    systemctl --user enable --now feishu-herdr-bridge.service
    systemctl --user is-active feishu-herdr-bridge.service
    journalctl --user -u feishu-herdr-bridge.service -n 50 --no-pager

在白名单群内 @ 机器人后依次使用 `/agents`、`/bind <workspace_id> <pane_id>`。收到绑定成功回执再发普通文本；`/read` 查看当前画面。新任务使用 `/new <项目别名> <codex或claude或devin>`，核对目录和换绑目标，再由原发起人在同一会话发送 `/confirm <确认码>`。

Devin 使用 `/new <项目别名> devin`，kind 区分大小写且只接受 `codex`、`claude`、`devin`。提案阶段不创建资源，名称为 `fb-devin-<12位小写十六进制随机串>`；由原请求者在原群五分钟内确认。只有新 workspace 的根 Pane 在启动结果及后续 get 中都通过 workspace/Pane/kind 核验才换绑；冲突、阻塞或未知结果按既有规则保留资源，不自动重试或强制绑定。

Devin 的受控真实验收另按 `docs/devin-agent-plan.md` 第 4 节由本地协调者执行，本补丁不宣称已通过。仅使用新建飞书任务群和该请求新建的 Devin workspace；禁止绑定或发送任务到既有 KPI workspace/Pane，也不增加权限绕过、初始化自动处理或其他 kind。

群消息必须使用飞书选择出的真实 @ mention，手工输入同形文本不会进入桥接。管理群可以不绑定 workspace，建群动作不应调用 HerdR。

仅当前配置管理员在管理群 @ 机器人发送 `/group-new <群名>`；群名为 1～60 个字符，可有内部空格，不能含换行或控制字符。核对提案的群名、固定 session 和 `G-<UUID>` 参考号，再由同一管理员于 5 分钟内在同一管理群发送 `/confirm g-<确认码后缀>`。必须使用机器人返回的实际码，不能照抄占位符。

预期请求者成为群主，现有机器人按创建契约自动入群。成功回执只返回群名和稳定参考号；本轮不取分享链接、不回显真实群 ID。须在客户端核对请求者、机器人和内部私密群属性；不符时停止并人工核查，不能补加成员后把原建群流程记为通过。

授权落库为 `done` 后，新群立即可 @ 机器人使用 `/agents`、`/bind`、`/new`、`/confirm`、`/cancel`、`/read` 及普通文本。初始没有绑定，普通文本会提示先绑定；新群不继承管理群绑定。成员仍须在 `allowed_users` 中，管理员在新群也不能执行 `/group-new`。仅把机器人拉进陌生群不会产生授权。

`/new` 只替换本会话的 workspace 提案；`/group-new` 只替换本管理群内当前管理员自己的建群提案，两类 pending 可同时存在。`/cancel` 保持原 workspace 取消语义，对建群只允许当前仍获授权的原请求管理员在管理群取消自己的 pending，不能取消他人的或已消费的请求。

首次部署启动后、开始建群前，在本地记录服务实例和配置摘要：

    systemctl --user show feishu-herdr-bridge.service -p MainPID -p InvocationID
    sha256sum "$HOME/.config/feishu-herdr-bridge/config.json"

完成两群即时准入测试后再次比较，期间不编辑配置、不重启服务、不手工写入授权表。公开材料只保留服务实例、摘要和群的脱敏代号；发生自动重启也必须如实记录，不能据此宣称完成了不重启准入验证。

Codex 或 Claude 首次在新 Pane 启动时，可能被信任目录、登录或首次运行界面阻塞。此时 `/confirm` 可能返回 `remote_error`，同时报告已保留的 workspace/Pane；这是“结果不明”，不是可自动重试错误。打开该 Pane 人工处理阻塞，再用 `/agents` 核对 Agent 已为 idle，最后执行 `/bind <workspace_id> <pane_id>`。不要复用原确认码，也不要在检查现场前重新 `/new`。

需要退出登录后持续运行时，由用户确认系统政策后执行 `loginctl enable-linger "$USER"`，再用 `loginctl show-user "$USER" -p Linger` 核验，必要时由本机管理员授权。linger 只保持用户服务，不保证 HerdR session 或 Agent 在主机重启后恢复。主机须接电、保持网络、禁用自动休眠；锁屏、退出登录和手机移动网络场景分别实测。

## 5. 停止、锁与结果不明的恢复

    systemctl --user stop feishu-herdr-bridge.service
    systemctl --user is-active feishu-herdr-bridge.service
    journalctl --user -u feishu-herdr-bridge.service -n 50 --no-pager

停止后应为 inactive。`SIGTERM` 和 `Ctrl+C` 关闭接收入口，终止并回收桥接当前 CLI 子进程，然后等待当前业务线程有限时间完成记录；不向 HerdR server 或已有 Agent 发退出命令。CLI 忽略 TERM 时升级为 KILL，仅作用于该 CLI。unit 使用 `KillMode=mixed`，超出 `TimeoutStopSec=10` 时由 systemd 清理服务控制组内的残留进程。

CLI 被终止不能撤销已经提交给 HerdR 的操作。未确认结果的中断写入保守记为 `unknown`；进程来不及落库时，下次启动将遗留 `processing` 转为 `unknown`。两种情况都不自动重做。停机时未发出的飞书回执可能丢失，不因此重复执行操作。

锁位于 `~/.local/state/feishu-herdr-bridge/locks/<规范配置路径的哈希>.lock`，在 SQLite 恢复和 SDK 构造前取得。相同路径及指向它的符号链接不能双开，重复实例退出码为 3。文件保留是正常现象，进程退出后内核释放锁；不得通过删锁文件绕过仍在运行的实例。不要复制配置指向同一数据库以绕过单实例边界。

workspace 操作遇到 `unknown`、创建部分失败、绑定失效或回执缺失时，先由合法操作者核对对应 workspace/Pane 的真实现场。已创建资源和旧绑定保留；没有确认成功时，不假定创建请求已回滚。不要删除幂等记录、把状态改回 `pending/processing` 或重复执行原 workspace 确认。建群异常按下节单独处理。

确认现场后重新 `/bind`，仅将确实需要执行的工作作为新消息提交。原创建请求不可续跑，确需另建时重新 `/new`。重启桥接使用：

    systemctl --user restart feishu-herdr-bridge.service
    systemctl --user is-active feishu-herdr-bridge.service

恢复前不要删除数据库。需要备份时先停服务，再复制数据库及存在的 SQLite 辅助文件到受限目录。日志和验收证据先脱敏；不提交真实终端正文、凭据或公司项目内容。

### 5.1 建群 unknown、回执缺失与部分成功

建群 POST 至多发送一次；在途 SDK/HTTP 写无法撤回。停止入口会拒绝尚未开始的 POST，但超时、取消本地等待或停止服务都不能证明飞书未建群。在途结果不明按 `unknown` 处理，能保存的资源 ID 留在私有库；来不及写回的 `processing` 在下次启动转为 `unknown`，不自动重试。

仅 `group_requests.status=done` 启用动态群。若群已创建但其余返回字段无法确认，或保存资源/授权事务失败，保留已知资源且不作内存授权；整个数据库不可写时，甚至可能无法保存真实群 ID。不得将这个群按名称认领、手工追加静态白名单绕过审核、删除幂等记录、把状态退回 pending、复用 UUID 重新 POST，或在未排除重复资源前重新 `/group-new`。

已知确认码被消费后，原请求管理员仍获授权且处于管理群时，可再发送同一 `/confirm g-...` 只读核对保存的状态和参考号，不发起第二次创建。若第一次完全没有回执且不知是否消费，不能假定确认只读；先人工核对，禁止凭群名推断成功。`done` 已提交但回执或消息级记账失败时，群仍保持启用，不撤销授权或重建。

人工核查使用原提案的 `G-<UUID>`、群描述中的相同参考号，以及私有数据库已知的资源 ID、发起人、机器人和 session 记录交叉确认。参考号用于定位，不是授权凭证。仅群名相同不能证明资源归属。无法确认时保留 `unknown`，不扫描认领同名群；若需窄修原记录，先停相关操作并备份，由本地 Codex 审核，不能提供可重复执行远端写的恢复入口。

日志不得记录真实 chat/user/message ID、密钥、token、确认码、群名、原始异常或 API 响应；测试样本仅用合成值，公开截图须遮盖真实值。群名和实际确认码只在必要的授权会话回执中展示，不复制到公开证据。不要开启 SDK DEBUG 或原始 HTTP 日志排查。危险故障只引用已有离线注入测试，不在真实服务中制造落库失败、断网或中断 POST。

服务 active 只说明进程存活。本轮交付须由本地 Codex 填写 `docs/self-service-groups-validation.md` 的真实结果；未测项保持未测。文档补丁本身不构成真实飞书建群或 `kpi-agg` 验收通过。

## 6. 绑定 Pane 的自动回传

本节描述 `feat/auto-pane-output` 引入的有限自动回传：群内普通文本提示提交成功后，桥接对该群绑定的主控 Pane 做一次限时观察，取得可证明的新正文后向原群自动补发一条。`fix/real-devin-output` 起，画面解析除原 synthetic/not-live 样例语法外，新增对真实 Devin CLI 可见文本布局的严格 fail-closed 识别（仅限 kind=devin；由授权的 disposable Pane `w1V:p1` 采样而来）；其他 kind 与一切未识别形态仍整体拒绝。功能尚未部署，6.3 的真实验收步骤全部未执行。

### 6.1 使用语义与固定上限

- 只有授权群聊中成功提交的普通文本才可能触发；私聊和全部斜杠命令维持原行为，不启动观察。
- 每条成功提示从其提交确认起独立计时 120 秒，与该群或绑定已发送过的轮次无关；同一绑定 Pane 上连续发送新提示即可多轮对话，每轮各有自己的 120 秒窗口，不需要重绑或重启。
- 每个成功提示最多一条自动消息。消息格式固定为 `主控 Pane <pane_id>` 一行加原文正文，不含 watch_ref、revision、源消息 ID 或其他调试字段；正文是 Pane 文本片段，不改写、不总结、不调用额外模型。
- 正文超过 80 行或 3000 个码点时从开头保留，以 `[已截断；可用 /read 查看当前画面]` 收尾；被截去的尾部不会补发，用 `/read` 查看当前画面。
- 提交回执仍是原有简短确认。本次无法启用自动回传时（Pane blocked/unknown、布局未识别、快照超过 32 KiB、全桥 16 群观察容量已满等），提示照常提交，回执追加“本次自动回传未启用，可用 /read”。
- Pane 处于 working 时发送的提示作为原生 guidance 照常提交，并以其提交前的 working 画面为基线启动观察：基线之后继续流式的在途输出不算本轮正文，回显块之后的 ASSISTANT 文本才是候选；同群更新的提示照常取消上一轮观察，由最新被接受的消息独占该轮最终自动回复，不补发中间回复。
- 观察开始后被重绑、授权撤销、机器人身份变化时静默丢弃，不发说明；观察期限结束、状态 blocked/unknown、读取失败或解析不可靠时，最多补发一条“本次自动回传已停止，可用 /read 查看当前画面”。
- 同群新的普通提示在实际提交前取消上一轮未发送的观察；同群手动 `/read` 也取消该群未消费的观察，避免读完又收到同一自动片段。`/cancel` 不控制观察。
- 观察只对该绑定 Pane 执行只读 `agent get/read`（visible、80 行、text），不发送按键、不读历史、不列 workspace、不访问其他 Pane。看到 working 时不发送变化中的画面；候选须在两个相隔至少 2 秒的 idle/done 采样中一致才发送。
- 没有自动消息不代表 Agent 没有执行；`/read` 始终是人工兜底。
- 真实 Devin 布局的识别边界：画面底部须为固定输入区——可选的工作指示行（`(esc twice/again to interrupt)` 提示行结尾，允许提示内或提示后带可选后缀）、一条可带 `(模式)` 标记的分隔线、一行 `❭ ` 输入行、一条分隔线、末尾 `… Context: N / N tokens (N%)` 状态栏；其上方转录区逐行须属于已知行型（空行、`❭ ` 用户回显及其 `  ` 两空格续行、` <glyph> ` 工具标题、` │ `/` └ ` 工具体、缩进正文）。出现任何未知行型（启动 banner、审批卡片、其他终端布局、硬控制字符、窄行宽导致的折行 chrome 等）即整帧拒绝，回执按“未启用”/“已停止”处理，`/read` 兜底。
- 观察期内单次采样取到不可证帧（TUI 重绘中途的撕裂帧、滚动使重叠边界暂时不可证或落在块中间等）不立即停止：该轮候选清空并继续按 2 秒间隔采样；连续超过 5 次仍不可证、或观察期限结束，才补发一条“已停止”说明。出现可证帧（含 working 状态轮询）即重置该计数；不可证内容在任何情况下都不回传。回显归属不符（不同或多余的 `❭ ` 用户块）与审批卡片仍属证据级异常，发现即停止，不适用该容忍。
- 用户提示归属按 `❭ ` 回显块与提交文本的去空白相等判定（TUI 会硬换行并重排段落）；归属不可证或出现第二个 `❭ ` 块时停止，不猜测转发。

### 6.2 可见风险、结果不明与恢复边界

- 自动回传扩大了授权群成员看到该 Pane 原文的时机。只应在内容适合该群可见范围的主控 Pane 上使用；界面清洗只移除回显和界面元素，不构成密钥或隐私内容的审查保证。
- 自动发送在原操作锁内做至多一次有界 POST（不超过 3 秒），不重试。超时、断连或回执缺失记为结果不明：已发出的消息可能稍后仍送达原群，且始终显示冻结的原 Pane 标识，不能撤回、不重发、不改投新群或新 Pane。看到可疑的晚到自动消息时人工 `/read` 核对现场。
- 服务停止或重启丢弃全部未发送观察，不从旧记录恢复、不补发停机期间输出；启动后的下一条新提示重新捕获基线，只回传新正文。
- 观察期间同一 Pane 被本地终端或其他客户端插入新提示时轮次归属无法证明；发现不同用户块、未识别格式或 kind 变化即停止，不猜测转发。
- 脱敏日志只含随机 watch_ref、原绑定 revision、事件（armed/attempted/closed）与原因码（如 sent/failed/unknown/cancelled/stopped/deadline/guard_changed），不含提示、正文、hash 或真实 ID；只有 armed/attempted 而无终态的记录视为中断未确认，不能据此重发。

### 6.3 受控验收交接（待部署批准，全部未测）

部署须由本地协调者在批准的维护窗口执行；候选分支未验收前不得部署、重启或并行启动第二个消费者。以下步骤由合法操作者在合规上下文执行，本批次只做文档准备：

1. 前置核实（私下进行，不写入公开材料）：指定 disposable 验收群 T 仍属本验收用途；`kpi-agg` 中的 P_TEST（计划记录的真实目标为 `w1V:p1`，对应 workspace 记 W_TEST）仍属验收用途；T 的当前绑定仍指向该 workspace/Pane 且为有效 revision。ID、用途或绑定任一不符即停止，不搜索其他 KPI Pane 替代。
2. 只读核对：仅对 P_TEST 执行 `agent get` 与 `visible` 快照，核对画面与 6.1 所列真实 Devin 识别边界逐项相符——末尾状态栏含 `Context: … tokens (…)`、其上是分隔线、`❭ ` 输入行与可带 `(模式)` 标记的分隔线，转录区只含已知行型；并确认基线在 idle 下可识别（否则桥接只回复“本次自动回传未启用”）。不滚动历史、不访问其他 Pane。当前布局不能按既定规则可靠识别时停止，保持 `/read` 兜底，不放宽为全屏转发。
3. 连续两轮：同一 T、同一 P_TEST、同一绑定 revision 和同一服务实例中，发送普通提示 P1，在其 120 秒内应收一条自动正文 A1；A1 到达后发送另一条普通提示 P2，在 P2 自身 120 秒内应收不同的 A2。断言：P1/P2 各只提交一次、各只自动回传一次、自动 POST 合计两次；A2 不含 P1/A1；两条消息仅 `主控 Pane <pane_id>` 加各自正文，无回显、banner 或调试字段；两轮期间不 `/read`、不重绑、不重启；超过 P2 观察期限后无第三条自动消息。
4. `/read` 核对：单独 `/read` 验证原兜底；再在观察未完成时执行一次 `/read`，确认之后不出现同一自动片段的复读。
5. 重绑守卫：在 T 对同一 W_TEST/P_TEST 再次 `/bind`，确认 revision 递增后旧观察不再开始发送；不得绑定其他 KPI 目标。
6. 受控重启：在无自动 POST 在途时按计划停启一次，确认不主动读屏、不补发；随后新提示从新基线正常回传。
7. 跨群转移、双 Pane 并发、Pane 删除/替换、断网、发送失败与写入窗口故障不触碰真实 Pane，只引用离线 fake/barrier 证据。

公开记录只使用 T/P_TEST/W_TEST 代号，不贴真实群 ID、确认码、密钥或原始终端内容；完整预期与验收表见 `docs/auto-pane-output-plan.md` 第 3 节。逐项如实记录实际结果，未执行项保持未测，不得以离线全绿替代 live 结论。
