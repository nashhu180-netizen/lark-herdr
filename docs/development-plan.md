# 飞书轻量桥接 HerdR：开发方案

编写时间：2026-09-11。
设计基线：`docs/design.md`，按本地审核反馈已合入 `main`，基线提交为 `643af3f`。
实施方式：三个顺序批次，每批审核通过后再进入下一批。

GPT-6 Pro 负责主体代码及对应测试。本地 Codex 负责拉取或代落库、本机测试、live probe、审核和必要小修。小修必须附回归测试；涉及设计变化时停止施工，返回审核。

本方案不修改 `docs/design.md`。HerdR 静态契约已确认，真实 JSON、session 选择和运行边界仍以本机验证为准。

## 通用同步与交接规则

下列命令均从仓库根目录执行。拉取前确认当前位于 `main` 且工作区干净；存在本地改动时先保留现场，不执行强制覆盖。采用代落库方式时，记录实际应用的补丁，不重复拉取覆盖。

    git status --short
    git pull --ff-only
    git merge-base --is-ancestor 643af3f HEAD
    git diff --exit-code 643af3f -- docs/design.md

通过标准：基线提交可追溯，设计文件无差异，本批变更仅覆盖本批列出的文件。

每批交接只需说明实际提交或补丁、变更文件、测试结果、未验证事项及阻塞。不得把 fake CLI 通过记为真实 HerdR 通过。

## 第一批：离线可测试骨架、SQLite 与路由核心

### 目标和非目标

建立无飞书凭据、无 HerdR server 也能运行测试的核心。完成 SQLite、已有 Agent 的列出与绑定、读取与文本投递，以及 HerdR 薄适配层。

本批不接飞书 SDK，不实现创建确认流程，不部署服务，不声称真实 HerdR 可用。

### 精确文件范围

新增：

- `pyproject.toml`。
- `feishu_herdr_bridge/__init__.py`、`feishu_herdr_bridge/store.py`、`feishu_herdr_bridge/core.py`、`feishu_herdr_bridge/herdr.py`。
- `tests/__init__.py`、`tests/fake_herdr.py`、`tests/test_store.py`、`tests/test_core.py`、`tests/test_herdr.py`。

修改：

- `.gitignore`。

`.gitignore` 排除虚拟环境、缓存、本地凭据和 SQLite 文件。`pyproject.toml` 声明 Python 3.11+，本批不引入额外运行依赖。

### 关键实现点

SQLite 建立设计中的三张表及唯一约束，不做迁移框架。路由固定使用配置 session、workspace ID 和 Pane ID；不引入进程指纹。实现绑定版本、迟到消息拒绝、消息 ID 去重及重启后的 `processing → unknown`。

核心先实现 `/agents`、`/bind`、`/read` 和普通文本处理。操作使用全局非阻塞锁，繁忙立即拒绝，不排队。线程使用独立 SQLite 连接，事务不跨越 CLI 调用。

适配层通过参数数组、`shell=False` 调用 CLI，集中处理目标、超时和输出解析。`get/read/prompt` 均使用 Pane ID，投递不使用 `--wait`，不提供裸输入或按键接口。失败、blocked 或结果不明时按设计使绑定失效，不自动重试。

fake CLI 必须作为真实子进程运行，用临时文件记录调用次数和目标。初期 JSON 明确标为合成样本；session 选择和真实输出解析保持在薄适配层，尚未验证的真实路径默认拒绝执行。

### 单元和集成测试

先写可执行的失败断言，再补最小实现。至少保留一项隔离或去重用例的红转绿记录，不能仅用缺少模块的导入错误代替红灯。

覆盖双会话不同 workspace、不允许重复占用、换绑后旧请求拒绝、迟到消息拒绝、同名不同 Pane、重复消息只投递一次、操作锁繁忙拒绝，以及目标退出、blocked、超时和异常输出。

fake 集成测试检查实际 CLI 参数与调用日志，证明无焦点依赖、无裸输入回退、超时后无重投。重启数据库后未知操作不得恢复执行，正文和终端输出不得进入持久化记录。

### 本地 Codex 验收命令与标准

完成通用同步后执行：

    python3 -m venv .venv
    .venv/bin/python -m pip install -e .
    .venv/bin/python -m unittest -v tests.test_store tests.test_herdr tests.test_core

通过标准：使用 Python 3.11+；全部测试退出码为 0；测试不需要真实 HerdR、飞书凭据或网络。人工抽查 fake 调用日志，确认目标、调用次数和拒绝行为符合设计。

### GPT-6 Pro 停止交接点

提交本批代码、测试及红绿结果后停止，等待本地 Codex 审核。不开始飞书集成。

本批通过只代表离线路由与适配边界可测试。真实 session 选择和 JSON 形状列入第二批前置输入。

## 第二批：飞书接入与完整命令闭环

### 目标和非目标

接入飞书长连接，完成白名单、消息转换、回执，以及 `/new`、`/confirm`、`/cancel`。使全部 MVP 命令能通过假飞书消息与 fake CLI 联合测试。

本批不自动建群，不做主动通知、持续输出、部署配置或 systemd，不进行真实创建和破坏性边界验收。

### 精确文件范围

新增：

- `feishu_herdr_bridge/feishu.py`、`feishu_herdr_bridge/__main__.py`。
- `tests/test_feishu.py`、`tests/test_creation.py`、`tests/fixtures/herdr_0_9_0.json`。

修改：

- `pyproject.toml`。
- `feishu_herdr_bridge/core.py`、`feishu_herdr_bridge/store.py`、`feishu_herdr_bridge/herdr.py`。
- `tests/test_core.py`、`tests/test_herdr.py`。

### 关键实现点

本批主体代码开始前，由本地 Codex 启动目标 HerdR session，确认从普通进程环境显式选择该 session 的方法，并执行只读 probe。将脱敏后的 `list/get/read` 输出保存到测试样本，回传确切调用方式。无法取得这些输入时停止真实适配，不猜测 `--session` 或 JSON 字段。

GPT-6 Pro 据此修正薄适配层。创建和启动的真实输出仍保留为待第三批验证，不用合成样本代替本机证据。

SDK 仅封装在 `feishu.py`，锁定本机确认可用的 `lark-oapi` 精确版本。模块导入不得连接网络或要求凭据；测试可注入假收发对象，不能引入插件机制。真实凭据只从进程环境读取，不进入仓库、测试样本或日志。

实现事件 ID、会话、操作者、时间和 @ 提及解析。只处理白名单文本消息；SDK 回调取得非阻塞执行资格后才启动工作线程，最多一个业务操作，不使用隐式排队的线程池。

创建流程严格按设计落库、确认和执行。名称预检冲突最多重新生成一次；启动时冲突不接管同名 Agent、不自动重试。部分失败保留旧绑定和已创建资源，未知结果禁止再次执行。飞书回执发送失败不得重做 HerdR 操作。

`__main__.py` 提供 `--help` 和 `--config` 入口，完成配置、环境凭据、SQLite 和 SDK 初始化。本批配置测试使用临时文件，不提交部署示例。

### 单元和集成测试

飞书离线测试覆盖群聊 @、私聊、双重白名单、非文本和自身消息忽略、未知命令、迟到事件、重复事件、重连后重复投递及回执失败。

创建测试覆盖确认码过期、跨用户或跨会话确认、原绑定变化、重复确认、名称格式与冲突、创建成功但启动失败、启动结果不明，以及只有全部成功才换绑。

完整集成路径必须经过飞书事件处理入口、核心、SQLite 和 fake CLI。SDK 无凭据测试不得建立真实网络连接；SDK 的导入和所用事件解析接口也须有离线检查。

### 本地 Codex 验收命令与标准

只读 probe 前必须先建立并记录已验证的 session 选择上下文。以下命令中的 `PANE_ID` 为该 session 内的实测目标，不能在默认 server 未运行的原环境直接执行并据此判定契约失效。

    herdr agent list
    herdr agent get "${PANE_ID:?先设置只读探针目标 Pane ID}"
    herdr agent read "$PANE_ID" --source visible --lines 10 --format text

完成通用同步和适配后执行：

    .venv/bin/python -m pip install -e .
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v
    .venv/bin/python -m feishu_herdr_bridge --help

通过标准：只读 probe 的 session 和目标明确，真实样本能被适配层解析；全部离线测试在无凭据下通过且不联网。创建流程通过 fake 测试，但真实创建、blocked 和目标失效尚不能标记通过。

### GPT-6 Pro 停止交接点

提交完整命令闭环、SDK 固定版本、真实只读样本及测试结果后停止，等待审核。

交接明确区分已实测的只读能力与仅通过 fake 的写操作。不得提前启动真实机器人服务或继续部署。

## 第三批：Linux 运行交付与真实边界验收

### 目标和非目标

补齐单进程运行保障、systemd、示例配置和运行文档，由本地 Codex 完成真实飞书与 HerdR 边界验收。

不新增命令或业务功能，不扩展监控、审批、群管理和自动恢复。

### 精确文件范围

新增：

- `deploy/feishu-herdr-bridge.service`。
- `examples/config.example.json`、`examples/credentials.env.example`。
- `docs/runbook.md`、`docs/live-validation.md`。
- `tests/test_runtime.py`。

修改：

- `feishu_herdr_bridge/__main__.py`、`feishu_herdr_bridge/herdr.py`。
- `tests/test_herdr.py`、`tests/fixtures/herdr_0_9_0.json`。

示例凭据只包含空值或占位内容。真实路径、白名单和密钥由本地填写；测试样本必须脱敏。

### 关键实现点

加入本机进程锁，防止同配置双开；服务退出时停止接收新操作并回收 CLI 子进程，未确认完成的操作保持未知状态。日志只记录必要标识和结果。

systemd 使用同一普通 Linux 用户、虚拟环境解释器、配置路径和外部凭据文件。运行文档说明文件权限、用户 linger、供电与休眠、HerdR 预先启动、服务重启和日志查看。

`docs/live-validation.md` 提供设计第九节的逐项记录表，包含命令或操作、目标、预期、实际、结论和证据。初始状态全部为未测，不能预填通过。

本地 Codex 在临时 workspace 和授权测试会话中执行写操作及目标失效测试，不干扰正在工作的任务。真实创建和启动样本回填 fixture；薄适配层的字段修正必须附回归测试。其他必要小修须明确列出，不静默扩大文件范围。

### 单元和集成测试

补充进程锁、配置缺失、凭据缺失、重启后未知操作不重做和退出回收测试。继续运行全部离线测试，确认无需真实密钥。

真实验收至少覆盖两个飞书会话与两个 workspace 的隔离、换绑、重复消息、Codex 和 Claude 各一次确认创建、名称冲突、部分失败、blocked、Agent 退出后不落入 Shell，以及桥接重启和飞书重连。

同一 Pane 被外部替换 Agent 只记录实测行为，不添加进程身份保证。无法安全触发的场景标记未测，不能用单元测试替代真实通过结论。

### 本地 Codex 验收命令与标准

完成通用同步后执行：

    .venv/bin/python -m pip install -e .
    env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests -v

按 `docs/runbook.md` 在仓库外填写配置和凭据，安装已替换本机路径的服务文件，再执行：

    systemd-analyze --user verify "$HOME/.config/systemd/user/feishu-herdr-bridge.service"
    systemctl --user daemon-reload
    systemctl --user enable --now feishu-herdr-bridge.service
    systemctl --user is-active feishu-herdr-bridge.service
    journalctl --user -u feishu-herdr-bridge.service -n 50 --no-pager

按真实验收记录执行飞书交互，并在适当测试节点重启桥接：

    systemctl --user restart feishu-herdr-bridge.service
    systemctl --user is-active feishu-herdr-bridge.service

通过标准：离线测试全绿；服务为 active 且同配置只能运行一个桥接；凭据未被跟踪、日志无正文或密钥泄露；真实隔离、投递安全、确认创建与重连结果符合设计。任何真实阻塞项必须保留，不得将服务启动成功等同于 MVP 验收通过。

### GPT-6 Pro 停止交接点

完成运行文件、验收记录模板和离线测试后停止，交给本地 Codex 执行真实验收。

真实验收后的字段修正或小修必须回归并再次交接。最终由本地 Codex 提交逐项结果，待审核确认后结束本次 MVP，不自动进入下一阶段。
