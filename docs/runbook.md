# Linux 运行与恢复

编写时间：2026-09-11。范围：Batch 3；设计以 `docs/design.md` 为准。

HerdR 与飞书真实验收仍为未测。protocol-22 fixture 来自静态 schema，不能充当 live 证据。保留本地审核已修正的 `workspace.workspace_id`、`tab.tab_id`、`root_pane.pane_id` 及非零 CLI 从 stderr 解析结构化错误的契约。

## 1. 前置条件与离线检查

使用原生 Linux、Python 3.11+，以运行 HerdR 的同一普通用户操作。预先完成 Codex、Claude 登录及首次初始化；用户手动建群或打开私聊、加入机器人、配置飞书收发权限及长连接事件 `im.message.receive_v1`。

当前本地协调进程没有 `HERDR_ENV=1`，不得从该进程读取或控制现有 session。下文所有真实启动、查询和写入必须由用户在符合本机 HerdR 规范的合法上下文执行。不得手工设置 `HERDR_ENV=1` 冒充 Pane 上下文。用户服务访问预启动 session 的方式也须先获得本机规范允许；未确认前只安装文件和运行离线测试，不启动服务。

在仓库根目录执行。依赖安装可能访问包源，测试本身不需要凭据或网络；已有离线 wheel/依赖时按本地环境安装，不变更精确依赖 `lark-oapi==1.7.3`。

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

本地编辑私有副本，填写真实的 HerdR 绝对可执行路径、明确 session、机器人 `open_id`、操作者及会话白名单、允许创建的项目绝对目录。项目别名用于 `/new`。配置 JSON 不插值 `~`、环境变量或 systemd 的 `%h`。

密钥只写入私有 `credentials.env`。使用 systemd `EnvironmentFile` 支持的赋值格式；需要时引用值，不使用 `export`、命令替换或变量展开。不要 `cat` 凭据、开启 shell trace、将凭据粘贴到日志或仓库。

    chmod 600 "$HOME/.config/feishu-herdr-bridge/config.json" \
      "$HOME/.config/feishu-herdr-bridge/credentials.env"
    stat -c '%a %n' "$HOME/.config/feishu-herdr-bridge/"{config.json,credentials.env}

省略 `database` 时默认使用 `~/.local/state/feishu-herdr-bridge/bridge.sqlite3`。自定义数据库路径必须为仓库外绝对路径，父目录权限 `0700`，文件权限 `0600`。服务的 `UMask=0077` 保护新建文件。

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

先通过 HerdR 自身的受支持方式启动配置指定的 session，再按 `docs/live-validation.md` 记录实际 session、workspace 和 Pane。桥接不会启动或重启 HerdR server。

确认本机权限边界、私有配置和依赖均已就绪后，由合法操作者执行：

    systemctl --user enable --now feishu-herdr-bridge.service
    systemctl --user is-active feishu-herdr-bridge.service
    journalctl --user -u feishu-herdr-bridge.service -n 50 --no-pager

在白名单群内 @ 机器人后依次使用 `/agents`、`/bind <workspace_id> <pane_id>`。收到绑定成功回执再发普通文本；`/read` 查看当前画面。新任务使用 `/new <项目别名> <codex或claude>`，核对目录和换绑目标，再由原发起人在同一会话发送 `/confirm <确认码>`。

需要退出登录后持续运行时，由用户确认系统政策后执行 `loginctl enable-linger "$USER"`，再用 `loginctl show-user "$USER" -p Linger` 核验，必要时由本机管理员授权。linger 只保持用户服务，不保证 HerdR session 或 Agent 在主机重启后恢复。主机须接电、保持网络、禁用自动休眠；锁屏、退出登录和手机移动网络场景分别实测。

## 5. 停止、锁与结果不明的恢复

    systemctl --user stop feishu-herdr-bridge.service
    systemctl --user is-active feishu-herdr-bridge.service
    journalctl --user -u feishu-herdr-bridge.service -n 50 --no-pager

停止后应为 inactive。`SIGTERM` 和 `Ctrl+C` 关闭接收入口，终止并回收桥接当前 CLI 子进程，然后等待当前业务线程有限时间完成记录；不向 HerdR server 或已有 Agent 发退出命令。CLI 忽略 TERM 时升级为 KILL，仅作用于该 CLI。unit 使用 `KillMode=mixed`，超出 `TimeoutStopSec=10` 时由 systemd 清理服务控制组内的残留进程。

CLI 被终止不能撤销已经提交给 HerdR 的操作。未确认结果的中断写入保守记为 `unknown`；进程来不及落库时，下次启动将遗留 `processing` 转为 `unknown`。两种情况都不自动重做。停机时未发出的飞书回执可能丢失，不因此重复执行操作。

锁位于 `~/.local/state/feishu-herdr-bridge/locks/<规范配置路径的哈希>.lock`，在 SQLite 恢复和 SDK 构造前取得。相同路径及指向它的符号链接不能双开，重复实例退出码为 3。文件保留是正常现象，进程退出后内核释放锁；不得通过删锁文件绕过仍在运行的实例。不要复制配置指向同一数据库以绕过单实例边界。

遇到 `unknown`、创建部分失败、绑定失效或回执缺失，先由合法操作者核对对应 workspace/Pane 的真实现场。已创建资源和旧绑定保留；没有确认创建成功时，不假定创建请求已回滚。不要删除幂等记录、把状态手动改回 `processing` 或重复使用确认码。

确认现场后重新 `/bind`，仅将确实需要执行的工作作为新消息提交。原创建请求不可续跑，确需另建时重新 `/new`。重启桥接使用：

    systemctl --user restart feishu-herdr-bridge.service
    systemctl --user is-active feishu-herdr-bridge.service

恢复前不要删除数据库。需要备份时先停服务，再复制数据库及存在的 SQLite 辅助文件到受限目录。日志和验收证据先脱敏；不提交真实终端正文、凭据或公司项目内容。

服务 active 只说明进程存活。MVP 交付须另有 `docs/live-validation.md` 的逐项真实结果，未测项保持未测。
