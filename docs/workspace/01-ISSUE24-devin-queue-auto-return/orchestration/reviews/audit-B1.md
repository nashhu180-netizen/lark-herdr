# audit-B1 — ISSUE24 根因确认小审

审核者：gpt-5.6-sol 独立审核 worker（未参与实施）
审核对象：`83c4302977ca8750e0cc86fd975f53f8a18041d5` + 未提交 `tests/test_output.py` B1 diff + `findings.md` F-001 + `progress.md`
审核边界：只读代码；仅写本报告。

## 结论先行

**changes-requested**。

`capture()` 的控制流枚举大体覆盖了可见的拒绝点，新增测试也保持了当前合同的 fail-closed 行为；但它们没有用真实的 Issue #24 capture 时刻帧复现失败。当前 RED 是一个人工构造的“队列区域半重绘”假设帧；两个 queue 子例又与 `capture → prompt` 的实际时序不相容。因此，现有证据只证明“若首读恰好是不可解析帧、次读恢复，则 PR #25 有效”，不能证明 Issue #24 的现场根因就是该情形，也不能据此称其为“最可能”或“红→绿已钉住”。F-001 的三类结论还漏掉/提前排除了若干未被现场日志排除的出口。

## 审核发现

### A-B1-001 — P1：所谓 live RED fixture 没有复现 Issue #24 现场

位置：`tests/test_output.py:1016-1045, 1068-1113`；`findings.md:45-70`；`progress.md:18-20, 28`。

- `torn_queue` 是测试内新造的“queue rule 已画、queued row 未画”半帧，仓内 `tests/fixtures/` 没有对应 capture，也没有日志或脱敏原帧证明真实 Devin 曾产生该序列。它是合理假设，但不是现场 fixture。
- `queue_this_prompt` 在本轮基线捕获时不可能出现：`core.py:374-376` 明确先 `capture`、后 `herdr.prompt(P2)`。测试自己也承认这个顺序，却仍把 P2 已入队帧列作 `test_live_issue24_*` 的现场形态。
- `queue_prior_prompt` 同样没有还原已描述的 P1→P2 场景。P1 已经进入 working 并拥有 W1；没有证据表明 P1 此刻仍是 native queue 中的待发送项。该帧可以做解析器回归，但不能作为此次基线现场证据。
- `devin_screen()` / `devin_queued_screen()` 是“脱敏模型/样板生成器”，不是 Issue #24 两次失败时保存的原始帧；`tests/fixtures/pane_output.json` 还明确标为 `synthetic/not-live`。因此“3 个忠实真实形态帧”和“该场景红→绿已钉住”的表述越过了证据边界。

要求：把 queue 两例降格为解析器/时序假设回归，把 torn 例明确标为 synthetic hypothesis；删除 `live`/“忠实现场复现”/“红→绿已钉住”等超证据声明。若没有经授权取得的 capture 时刻脱敏帧，B1 应按 task_plan 允许的结论记录为“离线不可复现，需要 live 帧”，而不是把假设 RED 当成 Issue #24 RED。取得新的 capture 原因遥测或新 live 帧若需新增机制，标记为**范围外**并交主控裁决。

### A-B1-002 — P1：三类根因不是穷尽划分，“最可能”缺少判别证据

位置：`findings.md:31-43, 63-72`；`progress.md:28`。

F-001 自己枚举了初始/中途 `_allowed` 失败、容量/提示拒绝、目标不匹配等路径，但最终写成 live 的 None “必然”属于：(i) 一次撕裂、(ii) 持续未识别帧、(iii) get/read 异常或 status/kind 异常。该三分没有容纳：

- binding/授权/request phase/停止守卫在各复查点变化；
- 第二次 `get` 得到 workspace/pane/kind 目标不匹配（不等同于 status/kind 检测异常）；
- 容量拒绝或 `_prompt()` 拒绝。

这些候选可以基于调用锁、同一进程日志、消息属性被评为低概率，但现有日志没有记录 capture 的 reason，也没有逐项给出足以“排除”的证据。“所有已知样板能解析”也不能给一次性撕裂相对于持续未知布局、2 秒边界失败或状态识别异常排序，所以“最可能”“最简解释”只能标为假设，不能标为根因结论。

要求：三类改为非穷尽候选集，或逐项补齐其余出口并区分“已排除 / 有旁证但未排除 / 未知”；撤回无概率证据的排序。B2 只能表述为“验证 PR #25 对假设 (i) 的最小修复”，不能表述为已经确认 Issue #24 根因。

### A-B1-003 — P2：11 条枚举的结构基本完整，但评估口径需校正

位置：`findings.md:27-43`。

对 `main@04f1580` 单读版 `capture()`，表格已覆盖 `_valid/_allowed`、重复 message、容量/提示、get、目标/status、read/frame 及最终守卫等主要 None 路径；这部分没有发现决定性漏项。但以下评估不是代码或日志能直接证明的事实：

- “容量 1”应写成“现场只见同群一条 active watch，未见全进程 active 计数证据”；
- “cancelled→POST 约 1 秒偏向快速失败”受 journald 秒级时间戳和随后 prompt/record/send 链路影响，不能有效排除 2 秒边界异常；
- output.py 中“same signal the agent detector uses”的注释不是 `agent get` 在该时刻返回何种 status/kind 的实测证据。

要求：保留枚举，降低上述评估确定性，并明确 11 是“语义条件组”而非 11 个独立 `return None` 语句。

## 合同、范围与 fail-closed 核验

| 核验项 | 结果 | 证据/说明 |
|---|---|---|
| B1 未提交代码范围 | PASS | 仅 `tests/test_output.py`；工作区文档位于 task_plan 允许路径。 |
| 禁止 sleep/按键/提示重放/通用 diff/原始转发/放宽守卫 | PASS | B1 diff 仅测试；PR #25 生产 diff 只在 devin+working 基线读取上限从 1 次变 2 次，没有这些机制。 |
| 读错误 fail-closed | PASS | `read_raises` 返回 None；全量既有边界测试通过。 |
| 两帧无效 fail-closed | PASS | `torn_twice_during_queue_redraw` 与原 `still torn` 用例均返回 None。 |
| 目标不匹配/守卫撤销 fail-closed | PASS | 生产守卫未改；既有相关测试随全量 300 tests 通过。 |
| fixture 忠实复现 Issue #24 | FAIL | 见 A-B1-001。 |
| 根因结论证据充分 | FAIL | 见 A-B1-001 / A-B1-002。 |

## 独立复跑

```text
.venv/bin/python -m unittest tests.test_output -k issue24 -v
Ran 2 tests in 0.051s
OK

env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests
Ran 300 tests in 41.963s
OK
```

另跑：

- `.venv/bin/python -m compileall -q feishu_herdr_bridge tests`：PASS，无输出。
- `git diff --check`：PASS，无输出。
- 测试计数与 `progress.md` 一致；全量出现两条依赖弃用警告与末尾一个 unclosed event loop `ResourceWarning`，不影响本次 exit 0，但 progress 未记这些非失败警告。

## 发现表与总结论

| ID | 级别 | 结论 | 是否阻断 B1 approved | 关闭方式 |
|---|---|---|---|---|
| A-B1-001 | P1 | synthetic/时序不相容帧被当作 Issue #24 live RED | 是 | 修正测试命名/定位与 findings/progress；无 live 帧则如实记录离线不可复现 |
| A-B1-002 | P1 | 三类候选非穷尽且“最可能”无判别证据 | 是 | 补全候选或逐项证明排除，撤回根因确认式表述 |
| A-B1-003 | P2 | 11 条出口覆盖基本完整，但三处概率评估过强 | 否（随上两项一并修） | 降低确定性并注明是语义条件组 |

**总结论：changes-requested。** 本结论不否定 PR #25 对“一次无效 working 基线、下一读恢复”这一假设的实现正确性；它只拒绝把当前 synthetic GREEN/RED 证据升级为 Issue #24 现场根因已确认。是否在缺少 live capture 的情况下按该假设进入 B2，由主控裁决；审核 worker不代裁决。
