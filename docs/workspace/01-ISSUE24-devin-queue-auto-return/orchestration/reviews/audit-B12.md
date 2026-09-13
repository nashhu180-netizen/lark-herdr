# audit-B12 — ISSUE24 / PR #33 回显锚点提取

审核者：gpt-5.6-sol 独立审核 worker（未参与实施）

审核对象：worktree `/home/nash/work/lark-herdr-o32`，分支 `fix/devin-anchor-extract`，范围 `main@b19780eb35c14715e3838f6c6e66dc5481cee41e...e6f9673c62b43566748211eea310ac3075938006`，单提交 `e6f9673`。审核期间仅写本报告；未修改代码、测试、fixture，未合并、未部署。

## 结论先行

**rework**。

真实 F-007 帧对的正向修复成立：working baseline 尚无 P2 用户回显，done after 中恰有一次 P2 回显及 `PONG-F`，当前提取结果为 `PONG-F`；基线已经出现该回显、after 缺失或出现多次、锚点后再出现用户块等路径仍拒绝。已知 Devin tip、tool block 和 chrome/status 也没有进入候选。

但补丁把合同 §4.2 第 2 条要求的 baseline/after 行级增量证明从共享 `_extract()` 完全删除，并明确让滚屏、截断、甚至重排历史都不再否决。独立反例证明：before 与 after 零 overlap 时，只要 after 恰好显示一次历史中的同文用户块，就会把它后面的旧正文作为新 candidate。这不是 fail-closed。其次，真实 Devin 回显匹配会删除全部空白，`alpha beta`、`alpha\nbeta` 均可被另一用户块 `❭ alphabeta` 冒充，唯一锚点本身并不唯一。

## 1. 锚点唯一性、折行与截断

`output.py:245-268` 的锚点条件为：

```python
want = "".join(prompt.split()) if after.real else prompt
anchors = [block for block in after.blocks
           if block.role == "USER" and block.text == want]
```

after 中无匹配锚点返回 `waiting_for_prompt`；匹配数量不是 1，或 before 中已有任一相同锚点，返回 `ambiguous_overlap`。锚点后出现任一其他 USER 返回 `ambiguous_prompt`。这些出口方向保守。

折行/截断实测：

```text
wrapped_echo  => 'RIGHT_BODY' candidate
truncated_echo => None waiting_for_prompt
```

即 Devin 已知的两空格续行折行可以匹配，截断回显不会误当完整锚点。可是实现用 `.split()` 后拼接，折叠了所有内部空格、换行和 Unicode whitespace；合同 `auto-pane-output-design.md:65` 明确要求“只移除已知 UI 包装和行尾填充后比较；不折叠正文内部空白”。反例：

```text
目标 prompt: alpha beta
after 用户块: ❭ alphabeta
结果: 'WRONG_BODY' candidate

目标 prompt: alpha\nbeta
after 用户块: ❭ alphabeta
结果: 'WRONG_BODY' candidate
```

不同提示因此发生归一化碰撞。锚点成为唯一增量证明后，这个既有近似比较从辅助核对升级成可直接错误归属正文的单点，见 A-B12-002。

## 2. fail-closed 合同

合同 `docs/auto-pane-output-design.md:64`（§4.2 第 2 条）只允许：

- after 以完整 baseline 内容开头；或
- baseline 尾部与 after 头部存在至少两行非空、在两帧中各唯一的重叠锚点。

该条同时明确：重排、清屏或重叠不足时放弃自动正文，不做宽松 diff 猜测。第 65 行随后要求用户回显必须出现在“已证明为新的区域”内；用户块核对不能替代第 64 行的区域增量证明。

e6f9673 删除 `_occurrences()` 及整段 prefix/overlap/partial-block 判断，并在共享 `_extract()` 中只保留“after 恰有一次回显、before 没有”。这不仅作用于 Devin working，而是作用于 idle/done baseline 及 synthetic codex/claude/devin 的全部提取。新测试 `test_scrolled_or_reordered_transcript_still_anchors_on_new_echo` 甚至将 reordered history 从原先 fail-closed 改为成功提取，与合同原文直接相反。

独立零 overlap 反例：

```text
before = unrelated current history
after  = one historical matching USER block + its old ASSISTANT body
zero_overlap_old_history => 'OLD_BODY_NEWLY_VISIBLE' candidate
```

两帧没有任何行级证据证明该用户块比 baseline 新；它可能只是 resize、清屏、重排或窗口移动后重新可见的旧轮次。当前实现仍返回旧正文。A-B12-001 阻断批准。

保守出口复跑均为绿：缺锚点、after 重复锚点、before 已有锚点、锚点后第二用户块、approval、未知 frame、持续 ambiguous 均不会发送原始内容。两次稳定发送、授权/revision/目标守卫及 capture 读取次数没有被本提交改动。但这些局部 fail-closed 不能抵消增量证明被全局移除。

## 3. 非助手正文泄漏

对已识别 Devin 语法，未发现本提交新增的直接 chrome 泄漏：

- `_devin_frame()` 仍在生成 blocks 前截掉 rule/input/status/activity chrome；
- Did-you-know tip 仍在 transcript 分块前按既有有界规则剔除；
- `_DEVIN_TOOL_HEAD` / `_DEVIN_TOOL_BODY` 仍生成 `TOOL` block；
- candidate 只连接锚点之后的 `ASSISTANT` blocks；
- approval 仍返回 `attention`，锚点后其他 USER 仍返回 `ambiguous_prompt`；
- `echo_only` 与观察端两次稳定机制未变。

真实 F-007 帧对的结果严格等于 `PONG-F`；没有混入 `Running/Ran command`、shell 输出、tip、input chrome 或 Context 状态栏。以下定向 5 项通过：

```text
test_real_working_baseline_anchors_on_late_echo ... ok
test_real_baseline_already_echoed_fails_closed ... ok
test_real_frames_tip_blocks_never_enter_body ... ok
test_unprovable_or_duplicate_echo_fails_closed ... ok
test_tool_approval_and_literal_body_markers_are_distinguished ... ok
Ran 5 tests in 0.006s
OK
```

结论：已知结构的内容分类 PASS；A-B12-001/002 是轮次归属泄漏，而非 chrome/tool 清洗本身失败。

## 4. fixture 来源

本提交新增：

```text
tests/fixtures/devin_tick_working_tool_baseline.txt
tests/fixtures/devin_tick_echoed_baseline.txt
tests/fixtures/devin_tick_pong_f_done.txt
```

三帧具有一致的 `Read shell 210b5e`、`sleep 30`、token 计数递增、working spinner、原生 queued 行、P2 从 queued 到 `❭` 回显再到 `PONG-F` 的连续变化，以及真实终端软折行。它们与 F-008 登记的 f_01/f_20/f_21~23 序列吻合，没有发现为了让 parser 过测而手工拼接 chrome 或正文的内部迹象。路径/凭据扫描未发现绝对用户路径、API key、secret、password、bearer、token 值、私钥或 SSH key。

SHA-256：

```text
c26fc510028752160323ce4c63564c98505244695d599b5e3b38d1972e1e9e11  devin_tick_working_tool_baseline.txt
ffca13f647f93d1a50b77dff4c779307d59c70b463b29f4cd769d6ea33c0609a  devin_tick_echoed_baseline.txt
2fa999db26be36b792dc5b8108058c6d97eb76cb70662aabf96e3d4233beb4a3  devin_tick_pong_f_done.txt
```

不过仓库中没有原始 23 帧采集文件、采样 receipt 或采样时 digest 可供独立逐字节比对；“verbatim”来源仍依赖施工方的 fixture 注释和 F-008 自述。形态证据支持真实采样，但独立审核无法证明“绝非手造”，见 A-B12-003。

## 5. 测试与静态验证

用户指定命令在本环境无法运行：

```text
python -m pytest tests/test_output.py -q
```

shell 中没有 `python` 可执行文件，命令 exit 1、无输出。进一步检查：

```text
.venv/bin/python -m pytest tests/test_output.py -q
/home/nash/work/lark-herdr-o32/.venv/bin/python: No module named pytest

python3 -m pytest --version
/usr/bin/python3: No module named pytest
```

因此 pytest gate 是 **NOT_RUN（环境缺依赖）**，不能记成测试红，也不能声称 pytest 通过。审核 worker 未获授权安装依赖。

使用项目现有 unittest runner 的等价模块级补充验证：

```text
.venv/bin/python -m unittest tests.test_output -q
Ran 100 tests in 1.883s
OK
```

另补跑仓库既有全量命令：

```text
env -u FEISHU_APP_ID -u FEISHU_APP_SECRET \
  .venv/bin/python -m unittest discover -s tests
Ran 313 tests in 42.930s
OK
```

与 F-008 登记的 313 tests 一致。输出只有既有依赖 DeprecationWarning、静态非 live WARNING/INFO 及末尾 unclosed event-loop ResourceWarning。

```text
.venv/bin/python -m compileall -q feishu_herdr_bridge tests
PASS（无输出）

git diff --check
PASS（无输出）

git diff --check b19780e...HEAD
PASS（无输出）
```

审核结束时 o32 worktree clean，HEAD 为 `e6f9673c62b43566748211eea310ac3075938006`。

## 审核发现

### A-B12-001 — P1：删除合同要求的 baseline/after 增量证明，并全局接受零 overlap/重排历史

位置：`feishu_herdr_bridge/output.py:240-268`；`tests/test_output.py:151-167`；合同 `docs/auto-pane-output-design.md:64-65`。

after 中唯一且 before 中不可见的同文 USER block 不能单独证明它是 baseline 之后的新轮次；合同明确要求先通过 prefix 或唯一 overlap 证明新区域，并明确拒绝重排、清屏和重叠不足。当前实现却会从零 overlap 的旧历史返回 `OLD_BODY_NEWLY_VISIBLE`，且改变作用于共享 `_extract()` 的全部 kind/status，不限 Devin+working。

关闭要求：恢复 §4.2 第 2 条的增量证明及非目标 kind/status 的原 fail-closed 行为。若要让“回显本身即唯一增量证明”取代该条，属于合同语义变更，须主控/用户先裁决；在当前 B12 实现范围内标记为**范围外**，不能由施工方自称“合同内”。

### A-B12-002 — P1：whitespace-free 回显比较可把不同提示碰撞成同一唯一锚点

位置：`feishu_herdr_bridge/output.py:153,245-251`；合同 `docs/auto-pane-output-design.md:63,65`。

`"".join(...split())` 同时删除 UI 折行和用户原始内部空白，使 `alpha beta`、`alpha\nbeta`、`alphabeta` 共用同一键。另一用户/旧历史中的 `❭ alphabeta` 可冒充当前提示并放出其后正文。截断虽然安全拒绝，但折行处理打穿了“完整用户块完全对应”与“不折叠正文内部空白”。

关闭要求：增加空格/换行碰撞负例并保证不同原提示不能共享锚点；若保留 whitespace-free 匹配，只能作为有独立增量证明后的辅助 UI 折行核对，不能单独承担轮次归属。

### A-B12-003 — P2：真实 fixture 形态可信，但缺独立 verbatim provenance

位置：三个新增 `tests/fixtures/devin_tick_*.txt`；`findings.md:108-115`。

三帧序列和自然终端细节支持真实采样，未见手工拼接迹象；但没有原始采集 receipt/digest 可供独立比对，所以只能确认“与 live 叙述一致”，不能确认“逐字节 verbatim、绝非手造”。

关闭要求：保留不入库的敏感原帧时，也应在允许的脱敏账本记录采样命令、时间、帧编号、脱敏/裁剪规则及原始/fixture digest 的对应关系；不得把原始敏感帧提交到仓库。无需修改 parser。

### A-B12-004 — P2：指定 pytest gate 无法执行

位置：o32 Python 环境。

`python` 不存在，`.venv` 与系统 `python3` 均未安装 pytest；指定命令无法产生测试计数。unittest 模块级 100 项和全量 313 项虽均通过，但不能冒充 pytest 结果。

关闭要求：由主控明确接受项目既有 unittest 命令作为本 gate 的等价替代，或在项目约定的开发依赖中提供可复现 pytest runner 后重跑；审核 worker 不在只读复核中安装依赖。

## 发现表与总结论

| ID | 级别 | 结论 | 是否阻断 approved | 关闭方式 |
|---|---|---|---|---|
| A-B12-001 | P1 | 合同规定的行级增量证明被全局删除；零 overlap/重排历史可放出旧正文 | 是 | 恢复合同证明；anchor-only 若保留须先裁决合同变更（范围外） |
| A-B12-002 | P1 | whitespace-free 唯一键会让不同提示碰撞并错误归属正文 | 是 | 增加碰撞负例；近似匹配不得单独作为增量证明 |
| A-B12-003 | P2 | fixture 形态支持 live 来源，但无独立 receipt/digest 证明 verbatim | 是 | 补脱敏 provenance 账，不提交敏感原帧 |
| A-B12-004 | P2 | 指定 pytest 命令因 runner 缺失而 NOT_RUN | 是 | 主控裁 unittest 等价，或提供可复现 pytest 环境后重跑 |

**总结论：rework。** F-007 正向样本和已知 chrome/tool/tip 清洗通过，但 anchor-only 实现越过合同规定的 baseline 增量证明，并存在可复现的空白碰撞误归属；指定 pytest gate 也尚无可运行环境。审核 worker 不修改代码、不合并、不部署，现停下等待主控。

## 增量审核 143026e

审核对象：PR #33 rework1，o32 worktree `e6f9673..143026e`。对照本报告 A-B12-001/002、`decisions.md` D-006 与合同 `auto-pane-output-design.md` §4.2 逐条复核。结论先行：**rework**；两条 P1 都只完成了部分反例修复，未达到 D-006 的关闭条件。

### 1. A-B12-001：部分修复，但未关闭（P1）

已恢复的部分：`output.py:254-273,321-330` 重新要求 prefix 或 overlap 边界；零 overlap、单行 overlap、重复 overlap 均返回 `ambiguous_overlap`。锚点前后检查与候选正文行不得复用 baseline 行也位于共享 `_extract()` 路径，新增 `test_unprovable_or_duplicate_echo_fails_closed` 的零 overlap 反例确实由父提交 RED 变为当前 GREEN。

仍不符合合同的部分：合同 §4.2 第 2 条（本文档行 64）只允许 `B` 的最后 `k` 行等于 `S` 的前 `k` 行，并明确规定“重排……时放弃”。当前 `_overlap_boundary()` 却在 after 的**任意位置**搜索 baseline 尾部（`output.py:258-273`），施工账 `findings.md:119` 也如实称其为“任意位置泛化”。`tests/test_output.py:151-160` 更将完整 baseline 历史倒序，仍断言返回正文。审核实跑同一输入：父提交与当前提交都返回 `candidate`；因此这不是合同要求的 fail-closed 恢复。

该放宽发生在共享 `_extract()`，并非只针对 F-007 的 Devin+working 特例。candidate 行“未在当前 baseline 可见”也不能证明它在历史上不存在，无法补足被重排快照破坏的时间顺序。D-006 已明确要求：若合同内修复使 F-007 再次拒绝，应保留未解决并交主控，不得为保正向场景改合同。当前真实 F-007 用例仍绿，但其实现基础包含这项未裁决的合同放宽；`tests/test_output.py:399-405` 的注释也仍写“echo anchor alone proves the round”，与 rework1 声称恢复行级证明不一致。

关闭要求不变：overlap 必须是 after 的前缀，重排必须 fail-closed；若要采用任意位置 overlap，须先取得合同语义变更裁决，不能以本次 rework 自行关闭 A-B12-001。

### 2. A-B12-002：简单碰撞已修，精确/不可区分边界未关闭（P1）

已修复的部分：`_devin_frame()` 不再用 `split()` 删除全部空白；单行 `alphabeta`、`alpha beta`、双空格等输入不再互相匹配。审核动态复跑还确认 `prompt="alpha\nbeta"` 对单行回显 `❭ alphabeta` 从父提交错误放出 `WRONG_BODY` 变为当前 `waiting_for_prompt`。

未关闭部分：`_echo_match()` 在每个 `\x00` 折行边界同时枚举 `""` 和 `" "`（`output.py:276-297`）。因此同一可见帧：

```text
❭ alpha
  beta
```

会同时匹配 `alphabeta` 和 `alpha beta`，两者均返回 `RIGHT_BODY`。这不是审核构造出的隐藏路径；新增测试 `tests/test_output.py:214-220` 正面断言两者都应成功，施工账 `findings.md:120` 也承认“两边都认”。该行为与 D-006“无法区分 UI 折行与原始换行时拒绝”及合同 §4.2 第 3 条“完整多行提示或换行方式无法核对……时拒绝”直接冲突。当前没有真实样本证明原始单换行必定产生额外空白屏幕行，因而不能把不可判定的可见折行猜成无空格或一个空格。

新增负例覆盖了单行空格数量碰撞，却没有把“同一折行画面对应不同原始空白/换行”钉为 fail-closed；相反，它将歧义接受编码成正向断言。关闭要求：一旦一个可见 USER block 可对应多个规范化提示形态，应拒绝该锚点；并补 D-006 指定的折行/原始换行歧义负例。

### 3. 两类 RED→GREEN 证据

审核通过只读动态加载父提交 `e6f9673:output.py`，对当前提交新增的两个测试方法执行同一套输入：

```text
parent-e6f9673: run=2 failures=7 errors=0 successful=False
current-143026e: run=2 failures=0 errors=0 successful=True
```

逐输入探针结果：

```text
zero_overlap_old_body: parent=(candidate, old body) current=(ambiguous_overlap, None)
space_collision:       parent=(candidate, WRONG_BODY) current=(waiting_for_prompt, None)
newline_collision:     parent=(candidate, WRONG_BODY) current=(waiting_for_prompt, None)
reordered_history:     parent=(candidate, old body) current=(candidate, old body)
UI wrap + space:       parent=(candidate, RIGHT_BODY) current=(candidate, RIGHT_BODY)
UI wrap + no space:    parent=(candidate, RIGHT_BODY) current=(candidate, RIGHT_BODY)
```

所以零 overlap 旧正文与单行空白碰撞确有 RED→GREEN 证据；但合同要求的重排 fail-closed 及不可区分折行拒绝没有 GREEN 证据，现有测试反而明确保留错误语义。

### 4. P2 补项

- **A-B12-003 provenance：已补齐。** 三个 fixture 首部均记录时间、pane `o24-review2 (w1V:p5)`、帧号、完整只读命令和 pre-header SHA-256。审核将注释剥除后与仍在 `/tmp/b12/` 的 f_14/f_20/f_21 原帧逐字节 `cmp`，三组均 `MATCH`，SHA 分别为 `c26fc510…`、`ffca13f6…`、`2fa999db…`。敏感词/绝对路径扫描无命中。该项关闭。
- **A-B12-004 pytest 环境说明：说明已补，gate 仍 NOT_RUN。** `findings.md:122` 已写明 o32 `.venv` 无 pytest，并列出替代的项目 unittest gate；本次复跑再次得到 `python: command not found` 与 `.venv/bin/python: No module named pytest`。这如实解释了环境，不能记作 pytest 通过；项目既有 runner 的模块级和全量结果见下。是否正式接受 unittest 等价仍归主控裁决，不影响本次两条 P1 的 rework 结论。

### 5. 本次验证

```text
python -m pytest tests/test_output.py -q
/bin/bash: python: command not found

.venv/bin/python -m pytest tests/test_output.py -q
/home/nash/work/lark-herdr-o32/.venv/bin/python: No module named pytest
```

指定 pytest：**NOT_RUN（环境缺 runner）**。

```text
.venv/bin/python -m unittest tests.test_output -q
Ran 101 tests in 1.644s
OK

env -u FEISHU_APP_ID -u FEISHU_APP_SECRET \
  .venv/bin/python -m unittest discover -s tests
Ran 314 tests in 45.553s
OK

.venv/bin/python -m unittest -v \
  tests.test_output.ExtractionTests.test_unprovable_or_duplicate_echo_fails_closed \
  tests.test_output.ExtractionTests.test_echo_match_preserves_interior_whitespace \
  tests.test_output.ExtractionTests.test_scrolled_tail_overlap_still_anchors_on_new_echo \
  tests.test_output.RealDevinExtractionTests.test_real_working_baseline_anchors_on_late_echo
Ran 4 tests in 0.010s
OK

.venv/bin/python -m compileall -q feishu_herdr_bridge tests
PASS（无输出）

git diff --check e6f9673..143026e
PASS（无输出）
```

全量测试仍含既有 DeprecationWarning、静态非 live WARNING/INFO 与末尾 unclosed event-loop ResourceWarning，exit 0。审核结束时 o32 HEAD=`143026e64a45304af37572d59ed3c122dd91123f`，worktree clean。

### 发现表与最终结论

| ID | 级别 | 增量结论 | 状态 |
|---|---|---|---|
| A-B12-001 | P1 | 零 overlap 已拒绝，但 after 任意位置 overlap 仍接受重排历史，越出 §4.2，且作用于共享提取路径 | 未关闭 |
| A-B12-002 | P1 | 单行空白碰撞已修；不可区分折行同时匹配有/无空格，且缺换行歧义 fail-closed 测试 | 未关闭 |
| A-B12-003 | P2 | fixture provenance 注释、原帧 digest 与逐字节对应均已补齐 | 已关闭 |
| A-B12-004 | P2 | pytest 缺失原因与替代命令已记录；指定 pytest 仍 NOT_RUN | 说明已补，等价 gate 待主控裁决 |

**最终结论：rework。** 143026e 修掉了零 overlap 与简单空白折叠反例，但同时明确保留了 D-006 禁止的重排历史接受和不可区分折行猜测；两条 P1 均未关闭。不改代码、不合并、不部署。
