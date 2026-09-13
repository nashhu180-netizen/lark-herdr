# progress — ISSUE24

| 时间 | 谁 | 做了什么 | 证据（命令/路径/结果） |
|------|----|---------|----------------------|
| 2026-09-13 10:20 | 主控 | 建 worktree `lark-herdr-o24`（PR #25 分支）、工作区、五角色 pane | 本目录 |
| 2026-09-13 | o24-exec | **B1 根因确认**。读完 task/task_plan/Context Packet（C-001~C-008）；还原调用链与日志现场；新增 B1 测试；全量验证 | 见下 |

## B1 证据

1. 现场日志（`journalctl --user -u feishu-herdr-bridge -S "2026-09-13 00:00" -U "00:06"`）：
   `00:01:50 armed c6d1… → 00:02:04 closed cancelled → 00:02:05 POST`；`00:04:22 armed fd17… → 00:04:35 closed cancelled → 00:04:35 POST`。
   **两次都是：旧 watch 被新提示取消，新提示的 `capture()` 返回 None（无第二个 armed/closed 事件）**。
   更早历史（23:42/23:50/23:51 三个 armed→sent）证明 armed→sent 链路本身工作。

2. 调用链（live=main@04f1580 与本分支此处相同）：`execute → _dispatch(prompt) → _capture_output`（先 `_cancel_output` 杀旧 watch → `output.capture`）→ `_assert_current → herdr.prompt`。**capture 严格先于 prompt 提交**——所以 capture 时刻画面里的队列只可能含*前一条*提示，不可能含本条。
   `capture()`→None 的语义条件组枚举并评估见 `findings.md` F-001。

3. 新测试 `tests/test_output.py`（本节原名/原表述已被 audit-B1 撤回，B2 段整改后现行名为）：
   - `test_issue24_second_prompt_during_working_turn_arms`（原 `test_live_issue24_…`）：Issue #24 建模场景，synthetic fixtures 非 live 捕获帧，working 帧 → armed。
   - `test_working_capture_variants`（原 `test_live_issue24_working_capture_variants`）：`torn_then_queued_frame` 为 synthetic hypothesis（PR #25 重试路径生效，不代表已复现现场）；`torn_twice`/`two_queued_rows`/`read_raises` 按合同 fail-closed；`queue_chrome_*` 两例为解析器回归（新提示在 capture 时刻不可能在队列）；`cancelled_watch_leaves_no_residue`、`shared_prefix_prompt`（变体 b/c 排除）。

4. 验证输出：
   - `.venv/bin/python -m unittest tests.test_output -k issue24 -v` → `Ran 2 tests … OK`
   - `env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests` → `Ran 300 tests … OK`
   - `.venv/bin/python -m compileall -q feishu_herdr_bridge tests` → 无输出
   - `git diff --check` → 干净；`git status` → 仅 `M tests/test_output.py` + 未跟踪 `docs/workspace/`

5. 结论（**B1 当时记录；audit-B1 changes-requested 后以下口径作废**，现行口径以 findings.md F-001 为准）：原写"撕裂假设为最简解释"系无判别证据的排序，已撤回。现行：capture()→None 候选集非穷尽，分「已排除 / 有旁证未排除 / 未知」三栏；撕裂帧仅为假设 (i)，区分需 live capture 时刻证据（B2 取证日志为此而加）。

## B2 证据（修复 + 测试；D-001 裁决 b + audit-B1 changes-requested 整改）

audit-B1.md 开工前已落地（changes-requested），A-B1-001/002/003 三条全部并入本批整改：

- **A-B1-001**：`test_live_issue24_*` 改名 `test_issue24_*`/`test_working_capture_variants`；queue 两例降格为 `queue_chrome_*` 解析器回归（注释写明 capture 先于提交、新提示不可能在队列）；torn 例标 `synthetic hypothesis`；删除"忠实现场/红→绿已钉住"表述。
- **A-B1-002**：findings F-001 改为候选集（非穷尽）+「已排除 / 有旁证未排除 / 未知」三栏；撤回"最可能"；补齐原三分法漏掉的守卫翻转/目标漂移/容量/提示拒绝候选；B2 表述统一为"验证 PR #25 对假设 (i) 的最小修复 + 取证日志"。
- **A-B1-003**：11 条改标"语义条件组"；三处评估降确定性（全进程容量计数无日志 / 秒级时间戳不能排除 2s 边界 / 注释非 agent get 实测）；本行下方补记非失败警告。

变更：`feishu_herdr_bridge/output.py`
- `capture()` 每个返回 None 出口经新增 `_decline(origin, code)` 发一条固定码审计（`ref="capture"`/`event="declined"`），码白名单：`invalid_origin / not_allowed / same_message / capacity / prompt_invalid / get_failed / kind_mismatch / status_other / read_failed / frame_unparsed / frame_status_other / error`；未知 status/kind 归一固定码，不拼接外部值。
- `connect_output.audit` 对 `ref=="capture"` 输出 `output capture=declined reason=<码>`（watch_ref 为 token_hex(8)，不可能撞名）。
- `_decline` 与 `_emit` 同口径吞异常；**守卫、读取次数、出口语义逐条保持等价**（拆分合并条件为顺序独立的 return，评估顺序不变）。

测试：`tests/test_output.py` 新增 `test_capture_declines_emit_fixed_reason_codes`（15 个出口各断言 reason 码 + 无 get/read 越界）与 `test_capture_decline_audit_failure_never_changes_outcome`（审计汇抛异常时 decline 仍 None、成功路径照常 arm）；`test_non_group_…` 补 6×`invalid_origin` 断言；B1 issue24 测试保留并随整改改名。

验证输出：
- `.venv/bin/python -m unittest tests.test_output -v`（聚焦）：`test_capture_declines_emit_fixed_reason_codes`、`test_capture_decline_audit_failure_never_changes_outcome`、`test_issue24_second_prompt_during_working_turn_arms`、`test_working_capture_variants`、`test_non_group_…invalid_origin…` 均 ok。
- `env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests` → `Ran 302 tests in 42.043s — OK`。
- `.venv/bin/python -m compileall -q feishu_herdr_bridge tests` → 无输出。
- `git diff --check` → 干净。
- **非失败警告（A-B1-003 补记）**：全量运行含 2 条依赖 DeprecationWarning（lark_oapi `utcfromtimestamp`、`get_event_loop`）与末尾 1 条 `ResourceWarning: unclosed event loop`；另有 `HerdR contract=static-not-live` WARNING 与既有 `event=closed reason=cancelled` INFO 样例行——均不影响 exit 0。

## rework-1 证据（audit-B2 changes-requested 整改，并入 B2 收尾）

- **A-B2-001**：本文件 B1 段逐处清理被 D-002 撤回的表述——测试名改回现行名并标注"已被 audit-B1 撤回"、删"3 个忠实真实形态帧"、删"在 live 单读上为红"、第 5 条结论标注作废并指向 F-001 现行候选集口径；同步把"11 个出口逐条枚举"改为"语义条件组"。
- **A-B2-002**：`orchestration/reviews/audit-B1.md:3-4` 两处尾随空格已删（仅空格，内容未动）。纠正验证口径：范围级 `git diff --check 83c4302..HEAD` 才是本 PR 的门禁，裸 `git diff --check` 只查未提交 diff——B2 段"干净"记录对应后者，前者当时实际失败，本条如实更正。

rework-1 验证输出：
- `env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests` → `Ran 302 tests in 42.717s — OK`（同样的 2 条 DeprecationWarning + 末尾 1 条 ResourceWarning，非失败）。
- `.venv/bin/python -m compileall -q feishu_herdr_bridge tests` → 无输出。
- `git diff --check`（工作区）→ 干净；`git diff --check 83c4302..HEAD`（范围级）结果见 commit 后复跑行。
