# audit-B2 — ISSUE24 B2 小审

审核者：gpt-5.6-sol 独立审核 worker（未参与实施）

审核范围：`83c4302977ca8750e0cc86fd975f53f8a18041d5..b7951910051f3fbb3b3b5a06e2b47edf3659a446`（PR #25 已推送范围），并核对 D-001、D-002、F-001、progress 与 audit-B1。审核期间仅写本报告；未触碰代码、账本或并行中的 `decisions.md/state.json` WIP。

## 结论先行

**changes-requested**。

生产实现通过 D-001 的主要合同核验：`capture()` 的 15 个显式拒绝出口各发恰好一个固定字面量 reason；日志只输出 `output capture=declined reason=<固定码>`，不携带画面、提示、异常文本、行数或字节数；审计异常被吞掉；守卫短路次序和读取次数与 83c4302 保持等价。PR #25 的重试也仍严格限定为 devin+working 且至多两读。

阻断点有两个：D-002 要求撤回的旧过强表述仍留在 `progress.md` B1 段，而 B2 段反称已经删除；此外针对本次提交范围执行 `git diff --check 83c4302..HEAD` 实际失败，与 progress 的“干净”记录不一致。

## D-001 实现核验

### reason 出口与白名单

`feishu_herdr_bridge/output.py:441-489` 内所有显式 None 出口均改为 `_decline(origin, <字面量>)`，闭集为：

```text
invalid_origin
not_allowed
same_message
capacity
prompt_invalid
get_failed
kind_mismatch
status_other
read_failed
frame_unparsed
frame_status_other
error
```

其中 `not_allowed` 复用于入口、get 后、read loop 内与 loop 后四个守卫时点；同一语义条件使用同一码。workspace/pane/kind 的 `_matches` 失败统一为固定 `kind_mismatch`，status 外部值统一为 `status_other`；get/read/兜底异常只归一成固定码，没有拼接异常对象。

测试 `test_capture_declines_emit_fixed_reason_codes` 覆盖 15 个代码出口（含四个 `not_allowed` 时点、idle 单读/Devin working 两读、synthetic frame status），逐项断言恰好一个 reason 和 get/read 调用序列。PASS。

### 日志最小化与失败隔离

- `_decline()` 只向 audit 传固定 `ref="capture"`、revision、固定 event `declined` 与调用点字面量 reason；其自身吞掉 audit 异常。
- `connect_output.audit()` 对 capture 分支只格式化 `event/reason`；revision 也不进入该日志。没有画面、提示、异常文本、行数或字节数参数。
- `test_capture_decline_audit_failure_never_changes_outcome` 证明 audit 抛异常时 decline 仍返回 None，随后正常 capture/arm 不受损；既有 `test_capture_failure_does_not_fail_or_repeat_prompt` 证明 baseline read 失败仍只提示自动回传未启用，原 prompt 只提交一次。两者结合覆盖 D-001 的 capture 与提示提交失败隔离。PASS。

### 守卫与读取次数等价

与 83c4302 对照：拆分后的判断保持原短路顺序；`_matches/status/_allowed` 的先后未变；无效 frame 后仍先复查 `_allowed`；最终 `before is None` 或 status 不允许时仍不会额外调用末尾 `_allowed`。get 固定一次；非 devin 或非 working 固定一读；devin+working 仅首帧无效时第二读，读异常立即 fail-closed、不重试。PASS。

## PR #25 重试边界

`output.py:467-478` 仍为：

```python
attempts = 2 if origin.kind == "devin" and state.status == "working" else 1
```

循环没有 sleep、按键、提示重放、通用屏幕 diff、原始转发或守卫放宽。定向测试同时证明 devin+working 两帧无效为 `frame_unparsed`，读异常只有一次 read，其他 kind/status 不增加读取。PASS。

## audit-B1 / D-002 整改核验

- A-B1-001 测试代码：PASS。测试已去掉 `live` 命名；开头明确“sanitized synthetic fixtures / no live frame”；queue 例降格为 parser-only regression；torn 例明确 synthetic hypothesis。
- A-B1-002 findings：PASS。F-001 明示候选集非穷尽，表格使用“已排除 / 有旁证未排除 / 未知”，补入守卫、容量、prompt、目标 mismatch 等候选，并撤回候选排序。
- A-B1-003 findings 与 B2 progress 新段：PASS。11 条明确为语义分组，原三处概率判断降级，并补记非失败警告。
- D-002 对 progress 整体口径：**FAIL**，见 A-B2-001。

## 审核发现

### A-B2-001 — P1：progress 仍保留被 D-002 撤回的 B1 事实声明

位置：`progress.md:18-20, 28`，与 `progress.md:32-36`、`decisions.md:22` 冲突。

旧 B1 段仍写：

- 已不存在的 `test_live_issue24_*` 测试名；
- “3 个忠实真实形态帧”；
- torn 用例是 live 单读 RED；
- “撕裂假设与现场一致且为最简解释”。

这些正是 audit-B1 与 D-002 要求撤回的超证据表述。B2 段随后声称已“删除”并“统一表述”，造成同一权威进度账自相矛盾，也会让后续 reviewer/合入者误判根因证据已经成立。

关闭要求：机械修正 B1 历史段，使测试名和证据性质与当前代码/F-001 一致；保留历史发生事实可以，但必须明确是旧结论且已被 audit-B1 撤回，不能继续作为当前有效证据。无需新机制，范围内。

### A-B2-002 — P2：PR 范围级 diff-check 失败，progress 的“干净”证据不可复现

位置：`orchestration/reviews/audit-B1.md:3-4`；`progress.md:49`。

独立运行：

```text
git diff --check 83c4302..HEAD
docs/workspace/01-ISSUE24-devin-queue-auto-return/orchestration/reviews/audit-B1.md:3: trailing whitespace.
docs/workspace/01-ISSUE24-devin-queue-auto-return/orchestration/reviews/audit-B1.md:4: trailing whitespace.
```

裸 `git diff --check` 只检查当前未提交 diff，因此在实现已提交后不能证明 PR 范围干净。本次审核范围是 `83c4302..HEAD`，范围级命令才可复现该 gate。

关闭要求：移除两处尾随空格，复跑 `git diff --check 83c4302..HEAD` 并更新 progress 的实际命令/结果。无需新机制，范围内。

## 独立复跑

```text
env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests
Ran 302 tests in 42.848s
OK
```

与 progress 的 302/OK 一致；耗时差异属于正常运行波动。输出也复现了 progress 已登记的两条依赖 DeprecationWarning、静态合同 WARNING/既有 INFO，以及末尾 unclosed event loop ResourceWarning。

```text
.venv/bin/python -m unittest \
  tests.test_output.ObserverTests.test_capture_declines_emit_fixed_reason_codes \
  tests.test_output.ObserverTests.test_capture_decline_audit_failure_never_changes_outcome \
  tests.test_output.ObserverTests.test_working_devin_retries_one_torn_baseline_without_guessing \
  tests.test_output.ObserverTests.test_issue24_second_prompt_during_working_turn_arms \
  tests.test_output.ObserverTests.test_working_capture_variants -v
Ran 5 tests in 0.067s
OK
```

- `.venv/bin/python -m compileall -q feishu_herdr_bridge tests`：PASS，无输出。
- 裸 `git diff --check`：当前 WIP diff 无 whitespace 报错。
- `git diff --check 83c4302..HEAD`：FAIL，见 A-B2-002。

## 发现表与总结论

| ID | 级别 | 结论 | 是否阻断 approved | 关闭方式 |
|---|---|---|---|---|
| A-B2-001 | P1 | progress 保留已撤回的 live/最简根因表述并与 B2 段冲突 | 是 | 机械修正 progress 旧段，统一到 D-002/F-001 口径 |
| A-B2-002 | P2 | PR 范围级 diff-check 因 audit-B1 两处尾随空格失败 | 是 | 去尾随空格，复跑范围级 diff-check 并如实落账 |

**总结论：changes-requested。** 功能代码与测试通过 D-001、重试边界和 fail-closed 核验；本次不需要方向裁决，只需在现有允许路径内完成两项文档/验证证据整改。审核 worker不修改代码或替主控裁决。
