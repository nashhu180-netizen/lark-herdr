# audit-B9 — ISSUE24 / Issue #30 / PR #31 小审

审核者：gpt-5.6-sol 独立审核 worker（未参与实施）

审核对象：worktree `/home/nash/work/lark-herdr-o30`，分支 `fix/devin-tick-extract-deadline`，更新后的范围 `main@cf52ca8...HEAD@6f47673`，包含 `970714d` 与补测提交 `6f47673`。审核期间仅写本报告，未修改 o30 的代码、测试、fixture 或账本。

## 结论先行

**changes-requested**。

`echo_only` 保留确有合同依据，但原文位于 `docs/auto-pane-output-design.md` §4.2 第 66 行，不是 F-006 所写的 §5。三份新 live fixture 未发现路径或凭据，全量 308 项测试也全部通过。

阻断点在 tip 结构规则：当前实现精确识别 ` ✱ Did you know` 标题，却会无条件删除它后面所有连续以两个空格开头的行。该形态无法区分 tip 续行和助手正文中的缩进行，独立反例已证明正文会被静默吞掉。PR #23 原测试恰好钉住“同标题 + 非已知 tip 的缩进助手文本必须保留”，970714d 删除了这个负向断言；6f47673 只增加三条正向 tip 文本，没有恢复边界覆盖。

## 1. `echo_only` 的合同来源

PASS，行为应保留；但章节引用需修正。

权威合同原文位于 `docs/auto-pane-output-design.md:66`（§4.2“有界、正向识别的提取规则”，第 4 条）：

> 只取该新用户块之后、已识别的 Agent/CLI 正文块；用户块、工具执行面板、审批 UI、输入提示、banner 和 chrome 不纳入候选。没有明确正文边界就不自动发送。候选只是提示回显或与规范化后的提示完全相同，也不发送。

`output.py:274-277` 在候选为空或 `body == prompt` 时返回 `Extraction(None, "echo_only")`，并未被本 PR 放宽。真实 `devin_tick_done_baseline.txt → devin_tick_marker_echo_done.txt` 回放得到 `body=None, reason=echo_only`，与上述“不发送”合同一致。

F-006 `findings.md:94` 将该原文标成“§5”不准确：§5 从设计文档第 81 行开始，内容是 revision 守卫与发送边界，并没有 echo-only 条款。见 A-B9-002。

## 2. tip 块结构及反例

`output.py:127-134` 的实现为：

```python
if transcript[i] == " ✱ Did you know":
    del transcript[i]
    while i < len(transcript) and transcript[i].startswith("  "):
        del transcript[i]
```

标题必须整行精确相等，标题本身不会模糊命中；普通下一行只有一个 UI 前导空格时不会被删除，遇到空行也会停止。因此正向上确实是“标题行 + 紧随的更缩进行”。

问题是“以两个空格起首”也是合法助手正文缩进形态。设计合同 `auto-pane-output-design.md:63` 明确要求“保留正文缩进、内部空行”，并要求只在已识别 UI 区域移除 chrome；当前规则仅凭缩进便把行认成 tip 续行，缺少能证明其属于 UI 块的结束/内容边界。

独立无文件改动反例：在正常回答后放置精确 tip 标题，再紧跟助手正文缩进行：

```text
 P1_LIVE_OK
 ✱ Did you know
  KEEP_THIS_ASSISTANT_INDENT
```

当前结果：

```text
title_then_two_space_body => 'P1_LIVE_OK' candidate
```

`KEEP_THIS_ASSISTANT_INDENT` 被静默删除。对照结果：一个空格起首的下一正文行会保留；标题后先有空行再出现两空格正文也会保留。这证明误吞边界正是“标题后的连续两空格行”，而非泛化猜测。

## 3. 三个 verbatim fixture

检查范围：

```text
tests/fixtures/devin_tick_done_baseline.txt
tests/fixtures/devin_tick_marker_echo_done.txt
tests/fixtures/devin_tick_queue_working.txt
```

三帧均保持终端换行、tool 区、用户块、tip、chrome 和状态栏的自然布局，彼此内容连续一致；没有为了让 tip parser 过测而剪接结构的可见迹象。仓库内只有采样声明，没有独立原始帧 receipt 可做逐字节 provenance 对照，因此“verbatim”只能确认到“内容形态与登记来源一致”，不能独立重建采集链。

路径/凭据扫描 PASS：未发现 `/home/...`、`/Users/...`、盘符路径、仓库路径、文件名、API key、secret、password、bearer、token 值、私钥或 SSH key。`fe53e8`、`0404dc` 是画面中的短期 shell session 标识，不是路径或认证凭据。

三个文件 SHA-256：

```text
614a63bb1735b37d6a2dfe7447595fdf4682310f497edf69f0bc5f5a7b924915  devin_tick_done_baseline.txt
cb2e47c87e2803695202cba6b0e1519147305c01e8153a0c9024cbd13235efd3  devin_tick_marker_echo_done.txt
c0ed80556f2d3746b76f42a38a6d29a9de81316c5873b30095a4bd545bd8a00a  devin_tick_queue_working.txt
```

## 4. PR #23 旧测试覆盖对照

FAIL，覆盖被降低。

PR #23 提交 `b6de85e` 的 `test_exact_did_you_know_tip_after_answer_is_not_returned` 同时包含：

1. 正向：精确 `/bug` tip 两行应剔除；
2. 负向：相同 ` ✱ Did you know` 标题后若是 `   Arbitrary assistant text must remain visible`，该文本必须保留。

970714d 将测试改名并保留/扩充正向 tip 样本，但删除了第 2 条负向断言。用旧反例原样运行当前 HEAD：

```text
old_pr23_near_match => 'P1_LIVE_OK' candidate
```

旧断言要求正文包含 `Arbitrary assistant text must remain visible`，当前实际完全丢失该行。

补测提交 6f47673 只在同一测试方法中补入 `/bug`、Shift+Tab、Type-@ 三条被认定为 tip 的正向文本，未增加任何“相同标题但助手缩进行必须保留”的负向边界。因此它提升了已知 tip 文本覆盖，却没有修复 PR #23 覆盖降级或生产误吞。见 A-B9-001。

## 5. 独立复跑

在 `/home/nash/work/lark-herdr-o30`、HEAD `6f476736f249543d39f075d4ea0aedb5c1322a4e` 使用该 worktree 自己的 `.venv`：

```text
env -u FEISHU_APP_ID -u FEISHU_APP_SECRET \
  .venv/bin/python -m unittest discover -s tests
Ran 308 tests in 42.560s
OK
```

输出含既有两条依赖 DeprecationWarning、静态非 live WARNING/INFO 及末尾 unclosed event-loop ResourceWarning，未造成测试失败。

```text
.venv/bin/python -m compileall -q feishu_herdr_bridge tests
PASS（无输出）

git diff --check
PASS（无输出）

git diff --check main...HEAD
PASS（无输出）
```

审核结束时 o30 worktree clean，分支相对 `origin/main` ahead 2。

## 审核发现

### A-B9-001 — P1：tip 结构剔除会静默吞掉合法缩进助手正文，且 6f47673 未补回负向边界

位置：`feishu_herdr_bridge/output.py:127-134`；`tests/test_output.py:199-233`；PR #23 旧测试 `b6de85e:tests/test_output.py:199-216`。

标题精确匹配之后，任何连续以两个空格开头的行都会被删除；实现没有证据区分 tip 续行与助手正文缩进。动态反例已复现正文丢失，并直接违反设计文档第 63 行保留正文缩进的要求。970714d 删除了旧 near-match 保留断言；6f47673 仅增加正向样本。

关闭要求：恢复 PR #23 的负向断言，并增加至少“两空格正文紧邻标题”和“多行缩进正文”的反例；实现必须只在可证明的 tip UI 边界内删除，否则 fail-closed，不能静默删正文。无需新机制，范围内。

### A-B9-002 — P2：F-006 把 echo-only 合同原文错引为 §5

位置：o24 `findings.md:94`；合同实际位置 `docs/auto-pane-output-design.md:66`（§4.2 第 4 条）。

`echo_only` 结论本身正确，但 §5 是 revision/发送守卫，错误章节会误导后续审计。

关闭要求：把 F-006 的“§5”机械更正为“§4.2 第 4 条（第 66 行）”。无需新机制，范围内。

## 发现表与总结论

| ID | 级别 | 结论 | 是否阻断 approved | 关闭方式 |
|---|---|---|---|---|
| A-B9-001 | P1 | 标题后连续两空格行会被无条件删除，旧负向覆盖被改掉，6f47673 仍只有正向样本 | 是 | 恢复负向测试并收紧到可证明的 tip UI 边界 |
| A-B9-002 | P2 | echo-only 确为合同要求，但原文在 §4.2 第 66 行而非 §5 | 是 | 修正 F-006 章节引用 |

**总结论：changes-requested。** `echo_only`、三份 fixture 脱敏、全量测试与静态检查均通过；tip 结构剔除存在可复现的助手正文丢失，且 PR #23 原负向覆盖被降低。审核 worker 不修改代码、不替主控裁决。

---

## 复审 1511ea2

复审对象：rework-2 提交 `1511ea29062106a3681d5a06a4895641e6d8afa9`，PR #31，o30 worktree；范围更新为 `main@cf52ca8...HEAD@1511ea2`。本节只复核 A-B9-001 / A-B9-002。

### 复审结论先行

**需人裁决**。

- A-B9-002：**CLOSED**。F-006 已准确改为 `auto-pane-output-design.md` “§4.2 第 4 条（第 66 行）”。
- A-B9-001：**PARTIAL / OPEN**。边界实现和新增三组反例均按 rework-2 自述通过；但 PR #23 的原负向断言没有等价恢复，原输入在 1511ea2 上仍丢失正文。更关键的是，原断言与本次指定的新结构规则对同一输入要求相反，无法仅靠现有结构信号同时满足，须主控/用户明确哪条语义优先。

### A-B9-001 边界实现

`feishu_herdr_bridge/output.py:132-143` 实际约束如下：

1. 标题必须整行精确等于 ` ✱ Did you know`；
2. 标题必须位于 transcript 帧首，或上一行 `blank()`；
3. 只删除紧随标题、非空且以至少两个空格起首的续行；
4. 遇首个空行立即停止；即使没有空行，也由 `_DEVIN_TIP_CONT_MAX = 2` 封顶。

因此本单要求的“精确标题 + 上一行空行/帧首 + 到首个空行 + 最多两行”静态核验为 PASS。三个真实 fixture 中的一行 tip continuation 仍能正确剔除。

新增 `test_did_you_know_boundary_never_eats_assistant_indents` 为绿，并覆盖：

- 标题紧邻两空格以上正文、且标题上一行非空：标题与正文均保留；
- 没有 tip 标题的多行缩进正文：三行均保留；
- 合法边界后的连续缩进行超过两行：只删除前两行，第三、第四行保留，从而钉住两行封顶。

定向复跑：

```text
test_did_you_know_boundary_never_eats_assistant_indents ... ok
test_did_you_know_tip_blocks_drop_by_structure_not_text ... ok
Ran 2 tests in 0.004s
OK
```

独立反例探针也确认上述三类当前均为绿：

```text
adjacent_indent_nonblank_prev => 'P1_LIVE_OK\n✱ Did you know\n KEEP_ADJACENT' candidate
multiline_plain_indent => 'P1_LIVE_OK\n KEEP_ONE\n KEEP_TWO\n KEEP_THREE' candidate
valid_tip_over_cap => 'P1_LIVE_OK\n\n KEEP_THREE' candidate
valid_tip_blank_boundary => 'P1_LIVE_OK\n\n\n KEEP_AFTER_BLANK' candidate
```

但 PR #23 原负向断言的输入是：上一正文后有空行，再出现精确标题和一条缩进的任意助手文本：

```text
 P1_LIVE_OK

 ✱ Did you know
   Arbitrary assistant text must remain visible
```

该输入同时满足 1511ea2 的全部 tip 结构条件，因此当前实现仍将任意助手文本当作第一条 tip continuation 删除：

```text
pr23_exact_negative => 'P1_LIVE_OK' candidate
```

代码中的新测试只称恢复了 “PR #23 near-match spirit”，并没有恢复原断言或其等价行为。因此“PR #23 负向断言是否恢复”的严格答案是 **否**。

这不是再补一个同类测试便能机械关闭：对“前空行 + 精确标题 + 一条缩进行”这个完全相同的可见结构，新规则要求剔除，PR #23 原断言要求保留。现有帧没有 role 标签或其他边界信号可区分两者。主控/用户需要二选一：

1. 裁决新的有界结构规则取代 PR #23 原 near-match 语义，并明确撤回“恢复原断言”的要求；或
2. 保留 PR #23 原语义，允许改用已知 tip 内容白名单或引入另一个可证明 UI 身份的信号。

审核 worker 不替主控选择。若选择第 2 项且需要新增 UI 身份机制，标记为**范围外**。

### A-B9-002 引用修正

o24 `findings.md:94` 当前为：

```text
auto-pane-output-design.md §4.2 第 4 条（第 66 行）
```

与合同实际位置一致。A-B9-002 **CLOSED**。

### 全量复跑

在 `/home/nash/work/lark-herdr-o30`、HEAD `1511ea29062106a3681d5a06a4895641e6d8afa9` 使用其 `.venv`：

```text
env -u FEISHU_APP_ID -u FEISHU_APP_SECRET \
  .venv/bin/python -m unittest discover -s tests
Ran 309 tests in 42.374s
OK
```

输出仍只有既有依赖 DeprecationWarning、静态非 live WARNING/INFO 与末尾 unclosed event-loop ResourceWarning。

```text
.venv/bin/python -m compileall -q feishu_herdr_bridge tests
PASS（无输出）

git diff --check
PASS（无输出）

git diff --check main...HEAD
PASS（无输出）
```

o30 worktree clean，分支相对 `origin/main` ahead 3。

### 复审发现表与最终结论

| 原 ID | 原级别 | 1511ea2 状态 | 复审结论 | 后续 |
|---|---|---|---|---|
| A-B9-001 | P1 | PARTIAL / OPEN | 新边界与新增反例通过，但 PR #23 原负向断言仍失败；两项要求对同一结构冲突 | 主控/用户裁决新结构是否正式取代旧语义；若保留旧语义则决定白名单或范围外新信号 |
| A-B9-002 | P2 | CLOSED | F-006 已改为 §4.2 第 4 条（第 66 行） | 无 |

**复审最终结论：需人裁决。** 1511ea2 的边界实现、三组新增反例、全量测试和静态检查均通过；但不能据此声称 PR #23 的原负向断言已恢复。审核 worker 不修改代码、不替主控裁决。

---

## 终审 0dc06cc

终审对象：D-004 裁决后的提交 `0dc06cc891d44ff59ac77ddd5d4145fa5026f346`，PR #31，o30 worktree；范围 `main@cf52ca8...HEAD@0dc06cc`。本节只核 D-004 对 A-B9-001 的收敛、A-B9-002 已关闭状态及三条全量验证。

### 终审结论先行

**approved**。

D-004 已明确裁决“结构规则优先”，撤销 PR #23 原 near-match 的保留语义；0dc06cc 用原输入钉住“合法 UI 边界处标题及一条缩进续行整块剔除”，测试名和紧邻说明明确引用 D-004。1511ea2 的三类反例保持全绿，F-006 仍正确引用 §4.2。PR #31 当前描述也有独立的 D-004 语义变更段，明确说明旧断言为何改写、两行封顶及反例承担的新保证。

### D-004 落地核验

权威裁决：o24 `orchestration/decisions.md:32-37`。其中要求结构规则优先、把 PR #23 原输入改写为整块剔除、测试改名/说明语义变化、三类边界反例继续承担“不误吞”保证，并在 PR 描述说明本裁决。

0dc06cc 新增：

```python
def test_tip_header_at_boundary_drops_following_indent_line(self):
    # Semantic change per decisions D-004 (structure rule wins) ...
```

测试名直接表达新预期；虽然说明采用行内注释而非 Python 三引号 `__doc__`，但紧邻测试、明确写出 `decisions D-004`、旧 PR #23 输入和结构规则优先，追溯性完整。原输入现断言只返回 `P1_LIVE_OK`，与 D-004 一致。PASS。

`test_did_you_know_boundary_never_eats_assistant_indents` 的三组边界反例未被 0dc06cc 改弱：

1. 标题上一行非空时，标题和紧邻的两空格以上正文均保留；
2. 合法 tip 边界后的缩进行超过两行时，只剔除前两行，第三、第四行保留；
3. 没有 tip 标题的多行缩进助手正文全部保留。

独立定向复跑新语义、三反例及三个已知 tip 样本：

```text
test_tip_header_at_boundary_drops_following_indent_line ... ok
test_did_you_know_boundary_never_eats_assistant_indents ... ok
test_did_you_know_tip_blocks_drop_by_structure_not_text ... ok
Ran 3 tests in 0.002s
OK
```

另以反例探针复核“tip 续行后的首个空行即停止”，空行后的缩进正文仍保留；1511ea2 的精确标题、帧首/前空行、首空行停止、最多两行四重边界均未被 0dc06cc 改动。PASS。

### PR #31 描述

2026-09-13 终审通过 GitHub Pull Request API 读取 PR #31；远端 head 为 `0dc06cc891d44ff59ac77ddd5d4145fa5026f346`、base 为 `cf52ca8a3f6b555aa51c814ccea6dc0581be5cca`。

PR 描述已追加 `## D-004 语义变更说明（rework-3）`，明确：

- “tip 结构规则优先”；
- PR #23 原负向断言与结构规则冲突，现改写为整块剔除；
- UI 边界为帧首/空行之后，续行封顶两行；
- 原保证由标题不在边界、超过封顶和无标题多行缩进等反例承担；
- 新测试名及 D-004 追溯关系。

D-004 对 PR 描述的落账要求满足。PASS。PR 页面：`https://github.com/nashhu180-netizen/lark-herdr/pull/31`。

非阻断说明：PR 描述前部的旧“验证”段仍写 308 tests，本终审实际 HEAD 为 310 tests；本节以下方独立复跑为准。这不影响 D-004 语义说明是否已落地，也不重开 A-B9-001/002。

### A-B9-002 保持关闭

o24 `findings.md:94` 仍为 `auto-pane-output-design.md §4.2 第 4 条（第 66 行）`，与合同实际位置一致。A-B9-002 保持 **CLOSED**。

### 全量复跑

在 `/home/nash/work/lark-herdr-o30`、HEAD `0dc06cc891d44ff59ac77ddd5d4145fa5026f346` 使用其 `.venv`：

```text
env -u FEISHU_APP_ID -u FEISHU_APP_SECRET \
  .venv/bin/python -m unittest discover -s tests
Ran 310 tests in 40.837s
OK
```

输出只有既有依赖 DeprecationWarning、静态非 live WARNING/INFO 与末尾 unclosed event-loop ResourceWarning，未造成失败。

```text
.venv/bin/python -m compileall -q feishu_herdr_bridge tests
PASS（无输出）

git diff --check
PASS（无输出）

git diff --check main...HEAD
PASS（无输出）
```

终审结束时 o30 worktree clean，分支相对 `origin/main` ahead 4。

### 终审发现表与最终结论

| 原 ID | 0dc06cc 状态 | 终审依据 |
|---|---|---|
| A-B9-001（P1） | CLOSED | D-004 已裁结构规则优先；原输入改写为新语义，测试名/说明可追溯，三类反例继续为绿，PR 描述已说明语义变更 |
| A-B9-002（P2） | CLOSED | F-006 保持正确引用 §4.2 第 4 条（第 66 行） |

**终审最终结论：approved。** 0dc06cc 按 D-004 落地，A-B9-001 与 A-B9-002 均关闭；全量 310 项、compileall 与两级 diff-check 全部通过。审核 worker 不修改代码、不替主控执行合入或部署裁决。
