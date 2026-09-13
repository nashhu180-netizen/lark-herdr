# round2 — ISSUE24 B4 fresh 收口复核

复核者：devin swe-2-max fresh 复核 worker（未参与实施、未参与 B1/B3 审核会话）
复核范围：`main(04f1580)...HEAD(c6584fa)`，commits `83c4302` / `b795191` / `c6584fa`（PR #25 分支 `fix/devin-working-baseline-retry`）
复核边界：只读代码与账本；仅写本报告；不改生产代码、不 commit、不替主控验收。

## 结论先行

**approved**。

两份审核报告的全部发现（A-B1-001/002/003、A-B2-001/002）逐条核对均已收敛；生产 diff 与原 `main@04f1580` 单读版 `capture()` 逐出口等价性复核通过，全部行为差异方向为 fail-closed 或合同允许项；独立重跑 302 tests 全绿、compileall 干净、三级 `git diff --check` 干净；两个变异点均按预期转红并已还原（文件 hash 回到基线）。

## 一、全量 diff 复核（feishu_herdr_bridge + tests）

分支 diff 触及文件：`feishu_herdr_bridge/output.py`（+67/-15 等效）、`tests/test_output.py`（+325）、`docs/workspace/01-ISSUE24-*/**` 账本与编排文档（task_plan 铁律 1 允许路径内）。无越界文件。

### capture() 等价性（对 `git show main:output.py` L431–453 逐条比对）

| 原出口（合并条件） | 新出口 + 固定码 | 等价性 |
|---|---|---|
| `not _valid or not _allowed` | `invalid_origin` / `not_allowed`（拆分） | PASS，求值顺序不变 |
| `old.message_id == message_id` | `same_message` | PASS，仍在 `cancel_current` 之前 |
| `len(_active)>=CAPACITY or expected is None` | `capacity` / `prompt_invalid`（拆分，顺序同 `or`） | PASS |
| `try: state=_get ...` 全段异常 → None | `get_failed`（get 异常）/ 兜底 `error`（其余异常） | PASS，仍 fail-closed |
| `not _matches or status∉集 or not _allowed` | `kind_mismatch` / `status_other` / `not_allowed` | PASS |
| `read+_frame` 单读 → `before is None or status∉集 or not _allowed` → None | 循环至多两读（仅 devin+working）→ `read_failed` / `not_allowed`(loop内) / `frame_unparsed` / `frame_status_other` / `not_allowed`(loop后) | PASS，见下 |

行为差异（全部可接受方向）：

1. **devin+working 至多两读**——PR #25 本意；`attempts = 2 if kind=="devin" and status=="working" else 1`（output.py:468）。无 sleep/轮询；`read` 抛异常立即 `read_failed` 不重试；第二读仅当首帧不可用（unparsed 或 status∉允许集）。
2. **坏帧路径多一次 `_allowed` 复查**——原版 `before is None` 短路时不再调 `_allowed`；新版在 loop 内每个坏帧后复查。更保守（fail-closed），结果不变。
3. **每个 None 出口发恰好一条 `_decline` 审计**——15 个调用点，闭集 12 个码（`invalid_origin/not_allowed/same_message/capacity/prompt_invalid/get_failed/kind_mismatch/status_other/read_failed/frame_unparsed/frame_status_other/error`）；`not_allowed` 复用于 4 个守卫时点属同一语义组，与 findings F-001 口径一致。

### 日志最小化

- `_decline`（output.py:400–408）：`ref="capture"`、`event="declined"`、固定字面量 reason；revision 仅在 `isinstance(origin, Origin) and type(revision) is int` 时透传（否则 0）；自身 `except Exception: pass`，audit 汇故障不改变结果（`test_capture_decline_audit_failure_never_changes_outcome` 钉住）。
- `connect_output.audit`（output.py:672–676）：`ref=="capture"` 分支只输出 `output capture=declined reason=<码>`，不记 revision、不记画面/提示/异常文本/行数/字节数。`watch_ref` 为 `secrets.token_hex(8)`（L322），不可能撞 `"capture"`；watch 事件日志格式未变。

### 合同红线

无 sleep、无按键/send-keys、无提示重放、无通用屏幕 diff、无原始输出直转、无守卫放宽；`_allowed`/`_valid`/`_matches`/`_prompt`/CAPACITY 语义与调用点顺序保持；读错误 fail-closed；`_prompt` 位置与异常暴露面与原版相同。

### 测试质量

- `test_capture_declines_emit_fixed_reason_codes`：17 个 subTest 覆盖全部 15 个代码出口，逐项断言「恰好一个 declined 码 + get/read 调用序列」，含 idle 单读 vs devin+working 两读、status_other×2、frame_status_other×2、`error` 兜底（get 返回非 PaneState）。
- `test_working_capture_variants`：8 个 subTest——queue_chrome×2（解析器回归）、torn_then_queued（synthetic hypothesis，重试生效）、torn_twice/two_queued_rows（fail-closed + `frame_unparsed`）、residue/shared_prefix（变体 b/c）、read_raises（不重试 + `read_failed`）。
- `test_issue24_second_prompt_during_working_turn_arms`：建模场景 arm→working→cancel→capture→armed。
- `test_working_devin_retries_one_torn_baseline_without_guessing`：torn→valid 恢复 arm + calls=get,read,read；持续 torn → None + 两读。
- `test_non_group_*` 补 6×`invalid_origin` 断言。

## 二、审核发现收敛核对

| 发现 | 级别 | 收敛证据 | 结论 |
|---|---|---|---|
| A-B1-001 测试名/定位越证据 | P1 | `live` 已从测试名移除；注释明示 "sanitized synthetic fixtures (no live frame)"；queue 两例降格 parser-only regression；torn 标 synthetic hypothesis；progress/findings 无"忠实现场/红→绿已钉住"现行表述（残留字样仅见于撤回标注语境） | 收敛 |
| A-B1-002 根因排序无证据 | P1 | findings F-001 改「候选集非穷尽 + 已排除/有旁证未排除/未知」三栏，补入守卫翻转/目标漂移/容量/prompt 候选；结论明示"无证据称最可能/已确认根因"；B2 表述=验证假设 (i) + 取证日志 | 收敛 |
| A-B1-003 三处评估过强 | P2 | "11 组=语义分组"；容量改"有旁证未排除"；秒级时间戳不能排除 2s 边界；注释非实测；非失败警告已落 progress | 收敛 |
| A-B2-001 progress 旧表述未清 | P1 | c6584fa 机械修正：B1 §3 测试名改现行名并标注"已被 audit-B1 撤回"、§5 结论标注作废指向 F-001、"11 个出口"改"语义条件组"；rework-1 段如实记录 | 收敛 |
| A-B2-002 范围级 diff-check 失败 | P2 | audit-B1.md:3-4 尾随空格已删；本复核独立复跑 `git diff --check 83c4302..HEAD` → 干净（exit 0） | 收敛 |

## 三、独立重跑（复核 worker 自跑，非引用）

```text
.venv/bin/python -c "import feishu_herdr_bridge;print(...__file__)"
→ /home/nash/work/lark-herdr-o24/feishu_herdr_bridge/__init__.py  （指向本 worktree，Python 3.12.3）

env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests
→ Ran 302 tests in 41.704s — OK
  （2 条依赖 DeprecationWarning + 末尾 1 条 ResourceWarning: unclosed event loop + 既有
   contract=static-not-live WARNING / closed cancelled INFO 样例——非失败，progress 已登记）

.venv/bin/python -m compileall -q feishu_herdr_bridge tests → 无输出（exit 0）

git diff --check                     → 干净（工作区，exit 0）
git diff --check 83c4302..HEAD       → 干净（rework 范围，exit 0）
git diff --check main...HEAD         → 干净（全 PR 范围，merge-base=04f1580，exit 0）

聚焦复跑（5 tests OK）：
  test_capture_declines_emit_fixed_reason_codes / test_capture_decline_audit_failure_never_changes_outcome
  test_working_devin_retries_one_torn_baseline_without_guessing
  test_issue24_second_prompt_during_working_turn_arms / test_working_capture_variants
```

## 四、变异点验证（施加 → 红 → 还原）

基线 `sha256(feishu_herdr_bridge/output.py)` = `e5fe64422be90d50da06e2fd7763cfc64b1b6b2e676f8f258531c7684071af03`

| # | 变异 | 施加态 hash | 结果 | 还原态 hash |
|---|------|------------|------|------------|
| M1 | `attempts = 2` → `1`（去掉重试） | `50df978d4ae3114a0b77c1e895847f944b13af1a4a4d8eafc775703fb2112af0` | **FAILED (failures=5)**：`test_working_devin_retries_one_torn_baseline_without_guessing`、`test_working_capture_variants[torn_then_queued_frame / torn_twice_during_queue_redraw / two_queued_rows]`、`test_capture_declines_emit_fixed_reason_codes[frame_unparsed_devin_working_two_reads]` | `e5fe6442…af03`（=基线，`git diff` 空） |
| M2 | `read_failed` 出口 `self._decline(...)` → `return None`（去一条拒绝码日志） | `ba5c10ccfd2e0a09dc6288f8bd1e19c71d2ba278f4326c077c679adb978d2e0a` | **FAILED (failures=2)**：`test_capture_declines_emit_fixed_reason_codes[read_failed]`、`test_working_capture_variants[read_raises]` | `e5fe6442…af03`（=基线，`git status` 对生产/测试文件干净） |

两个变异均为「改坏必红」验证：测试确实钉住了 (a) devin+working 两读上限语义与 (b) 拒绝码审计的每出口一条不变量。还原后聚焦 3 测试复跑 OK。

## 五、遗留说明（不阻断合入，供主控/用户知情）

1. **根因定位**：按 D-002 裁决，本 PR 表述为「验证假设 (i) 撕裂帧的最小修复 + capture 拒绝码取证日志」，非「Issue #24 根因已确认」。若部署后仍出现「未启用」，新日志 `output capture=declined reason=<码>` 将直接指出条件组——这是本 PR 的取证价值所在。
2. **D-003 未决（用户级）**：Devin 原生队列需 Enter 才放行排队提示；本 PR 修复的是 capture→arm 环节，不改变队列行为。B5 真实验收若观察到 P2 滞留队列，属 D-003 范畴而非本 PR 回归。
3. **PR 内含编排文档**：`docs/workspace/01-ISSUE24-*/**`（约 580 行）随分支提交，在 task_plan 允许路径内；若合入策略要求 PR 只含生产/测试改动，可由主控在合入时处理，不影响本复核结论。

## 总结论

**approved**。代码、测试、文档、验证证据与两份审核报告的整改要求一致；未发现新增阻断项。合入与部署（B5）由主控按 runbook §2.2 向用户申请维护窗口后执行，本复核不替代该授权。
