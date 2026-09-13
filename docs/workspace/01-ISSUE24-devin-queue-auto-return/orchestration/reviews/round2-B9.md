# round2-B9 — ISSUE30 fresh 复核（与审核并行）

复核者：devin swe-2-max fresh 复核 worker（本 pane w1V:p5；未参与实施；fixture 采样宿主）
复核对象：worktree `/home/nash/work/lark-herdr-o30`，分支 `fix/devin-tick-extract-deadline`（PR #31），`git diff main(cf52ca8)...HEAD(6f47673)`，commits `970714d` + `6f47673`
复核边界：o30 只读（变异为临时施加+还原）；仅写本报告于 o24 reviews/；不 commit、不替主控验收。

## 结论先行

**approved**（范围 = 970714d + 6f47673；含一条已验证的边界事实、一条覆盖粒度说明与一条并发 WIP 提示，均不阻断——见 §五/§六/§七）。

生产改动把 Did-you-know tip 剔除从「转录尾部精确两行匹配」改为「全转录结构扫描：精确 header ` ✱ Did you know` + 紧随的 `  `（≥2 空格）起始行」，3 个新增 fixture 经一手交叉验证为 verbatim 采样，独立重跑 308 tests 全绿、compileall/双级 diff-check 干净，变异验证完成并还原。

## 一、diff 复核

| 文件 | 变化 | 复核 |
|---|---|---|
| `output.py` L88-92, 127-133 | `_DEVIN_DID_YOU_KNOW_TIP` 精确对 → `_DEVIN_DID_YOU_KNOW` header 字面量；剔除点从「仅尾部两行」→ 扫描全转录，命中 header 即删并连删后续 `startswith("  ")` 行 | 见下方边界分析 |
| `tests/fixtures/devin_tick_{queue_working,done_baseline,marker_echo_done}.txt` | 3×45 行实帧 | verbatim 验证通过（§二） |
| `test_output.py` +77/-14 | 合成 tip 测试改写 + 3 个实帧测试 | §三 |

### 核心问题：是否误吞两空格缩进正文行（派发重点）

机制事实（实证，构造帧直测 `_devin_frame`）：

- tip header **紧贴** `  real content`（2 空格行、无空行）→ 该行**会被吞**（机制确实吃 ≥2 空格 flush 行）；
- 同一行与 tip 之间有**空行** → 保留（空行不 `startswith("  ")`，终止吞噬）。

是否构成真实误吞，取决于真实版式：

1. **全部 7 个采样实帧**（o28 的 4 个 + o30 的 3 个）中 tip 均为 `header(1sp) / body(3sp) / 空行 / 下一元素`——空行恒在下方，吞噬总在空行处终止。
2. **真实转录语法中没有以 ≥2 空格起始的块首行**：`❭`(0sp)、` ⏺`/` ○`/` text`/` │`/` └`(1sp)。深缩进只作块内续行存在；要被吞需 tip header 嵌进块内部——该版式未被观测且本身违反块语法（如 `❭` echo 续行 `  \S` 之上必有 `❭` 行，`❭` 不是 `  ` 起始，构成硬终止）。
3. **失败方向保守**：若假想中的紧贴版式出现，被吞行从解析帧缺失 → 抽取走 `unchanged`/`ambiguous_overlap`/无候选 → 不发送（漏发），而非把 tip 文本发给用户（本次修的 Issue #30 实害方向）。严苛度上「误吞致漏」显著优于「漏剔致错发」。
4. 旧的精确文本对已被证伪：Issue #30 即 `Use Shift+Tab…` 文案 tip 漏进 live 自动回传并实发——结构规则修复的是已观测缺陷。

判定：在已观测真实版式上不误吞；残差风险为未观测的「tip 紧贴深缩进内容」假想版式，后果保守。不阻断。

## 二、fixture verbatim 验证（宿主 pane 一手核对）

| 校验点 | fixture | 实际记录 | 结论 |
|---|---|---|---|
| shell id | `fe53e8`（queue_working L16-18）、`0404dc`（done_baseline/marker_echo L19-21） | 本 pane 第 2、3 次 `sleep 30` 的实际后台 shell id | ✓ |
| 排队形态 | `── 1 queued ── ↑ edit · ↵ send now ──` + `○ 回复标记 PONG-D，收到后只回复这一行`（queue_working L40-41） | 第 3 次 sleep 期间 PONG-D 提示真实排入原生队列 | ✓ |
| 标记 echo | `❭ ISSUE24 验收标记 D` + ` ISSUE24 验收标记 D`（marker_echo L35-37，逐字相同） | 本 pane 实际回复即标记原文 | ✓ |
| 提示 echo | `❭ 请只执行 sleep 30…`、`❭ 回复标记 PONG-D…` | 用户原话逐字 | ✓ |
| 上屏正文 | B6 报告尾部（hash `1a137548…3faa`/`76ece9f7…1bd0`、「2 处红」等） | 与本 pane 实际输出逐字一致，含 TUI 硬折行 | ✓ |
| 敏感信息 | 无 `/home/`、用户名、凭据（仅相对路径与 git ref） | — | ✓ |

## 三、测试复核

- `test_did_you_know_tip_blocks_drop_by_structure_not_text`：3 个 tip 文案（/bug 旧样、Shift+Tab 实漏样、@-mention 样）各置合成帧不同位置，断言 body 只剩 `P1_LIVE_OK`——「不枚举文案」的性质被钉住。旧 near_match 断言（异文 tip 体须保留）按新语义正确移除。
- `test_real_frames_verbatim_reply_declines_echo_only`：done_baseline vs marker_echo + 标记 prompt → `echo_only`。钉住 F-005：逐字回显的应答按合同拒发。
- `test_real_frames_tip_blocks_never_enter_body`：3 实帧解析后 rows 与 blocks 双查无 "Did you know" 残留。
- `test_real_frames_scrolled_transcript_fails_closed_overlap`：queue_working vs done_baseline（滚窗错位）→ `ambiguous_overlap`。钉住 F-004 candidate b 为实：滚动破坏对齐时 fail-closed 不猜。

## 四、独立重跑（o30 自身 .venv，复核 worker 自跑）

```text
.venv/bin/python -c "import feishu_herdr_bridge;print(__file__)" → /home/nash/work/lark-herdr-o30/…（指向 o30）

env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests
→ Ran 308 tests in 41.801s — OK   （=305+3；同样的 2×DeprecationWarning + 末尾 ResourceWarning，非失败）

.venv/bin/python -m compileall -q feishu_herdr_bridge tests → exit 0
git diff --check             → exit 0（工作区）
git diff --check main...HEAD → exit 0（PR #31 全范围，merge-base=cf52ca8）

聚焦：test_did_you_know_tip_blocks_drop_by_structure_not_text /
      test_real_frames_verbatim_reply_declines_echo_only /
      test_real_frames_tip_blocks_never_enter_body /
      test_real_frames_scrolled_transcript_fails_closed_overlap → 4 tests OK
```

## 五、补：commit 级覆盖核对（主控要求复核范围 = 970714d + 6f47673）

- `970714d`：生产修复（结构剔除）+ 3 个 tick fixture + 主体测试（+207/−15）——§一至§三全部覆盖。
- `6f47673`：纯测试 commit（test_output.py +15/−3）：注释 Issue 编号 `#28→#30` 更正；sample 2 的 tip 文案从 `Press Ctrl+L…` 换成**真实泄漏的 Shift+Tab 文案**（`Use Shift+Tab to cycle permission modes, and /plan and /ask to switch profiles`，与 `devin_tick_done_baseline.txt:33`、`devin_tick_marker_echo_done.txt:26` 逐字一致）；新增 sample 3 `Type @ to mention files and add them as context`（标注为 Devin CLI 上目击的另一 verbatim 文案）。三条 verbatim 样本的可核性：/bug 与 Shift+Tab 均在实帧中原样出现，`Type @` 为声称目击（合成断言，与 splice 同口径）。
- 复核时 `git diff main...HEAD` 即含本 commit 终态，前文测试复核与 308 全绿计数均已覆盖。

**并发 WIP 提示（范围外，供主控知情）**：复核期间 o30 工作区出现未提交改动（`M output.py`/`M test_output.py`），方向为收紧本报告 §一记录的误吞边界——header 须位于转录首行或空行之后、续行上限 `_DEVIN_TIP_CONT_MAX = 2`、续行须非空，并新增 `test_did_you_know_boundary_never_eats_assistant_indents` 负例（非空行后的 header 行不剔、超过两行的缩进保留、无 header 的缩进正文不动）。该 WIP 不在 `main...HEAD` 范围内、未经复核；若其提交入 PR，本 approved 不自动延伸（方向更严，但建议复核其「非空行后的 tip 不剔→文案可留存」的取舍）。我的变异施加/还原均已在 WIP 落地前完成，未污染其改动。

## 六、变异点验证（施加 → 红 → 还原）

基线 `sha256(o30:feishu_herdr_bridge/output.py)` = `b1ae6402f439672d8fcba56223a54db61e939d29efc60eff9a2e685d3b6d06e5`

| # | 变异 | 施加态 hash | 结果 | 还原态 hash |
|---|------|------------|------|------------|
| M1 | header 字面量 `" ✱ Did you know"`→`"…knoW"`（等效禁用剔除） | `78398781bdf01c984ea9c667a70a027306048d20c36fca4882c355445263b35c` | **FAILED (failures=5)**：`tip_blocks_never_enter_body`×3 fixture、`drop_by_structure_not_text`（tip 体漏进 body）、`verbatim_reply_declines_echo_only`（基线含未剔 tip → 对齐破坏级联） | `b1ae6402…d06e5`（=基线，`git status` 空，25 tests 复绿） |
| M2（探索性） | 续行条件 `startswith("  ")`→`startswith(" ")`（放宽一阶） | `bd5a765a0de7570bada333d7fc74f47efacd67702a4b0a6e36e68146ded56843` | **全量 308 tests 仍 OK**——证明在已观测版式上真正终止吞噬的是空行分隔，缩进深度精度本身未被测试单独钉住 | `b1ae6402…d06e5`（=基线） |

M2 说明（非缺陷、如实记录）：测试钉住的是「tip 被剔除」，而 `"  "` 与 `" "` 的边界差只在「tip 紧贴 1 空格正文行且无空行」这一未观测版式下才可分辨；该边界靠空行分隔兜底。若未来观测到 tip 与内容无空行分隔的版式，需重新评估续行条件（届时的失败方向仍是漏发而非错发）。

## 七、遗留说明（不阻断）

1. 上述假想版式残差风险：tip header 若与 ≥2 空格内容行无空行紧贴，内容会被吞→漏发。已观测 7 帧均空行分隔；后果保守。复核期间工作区已出现针对该边界的未提交收紧 WIP（§五末），方向一致。
2. 滚动截半的 tip（header 滚出可视窗、body 残留）会以普通 assistant 行留存——既有的部分帧歧义，非本 PR 新增面。
3. 本复核与审核 worker 并行，未读其报告；结论独立形成。

## 总结论（首轮，至 6f47673）

**approved**。结构剔除在全部真实采样上正确且方向保守；「不枚举文案」修复了已观测的 Issue #30 泄漏实害；测试钉住剔除/echo_only/ambiguous_overlap 三条合同性质。合入与部署决定归主控/用户。

---

## 增量复核 1511ea2（B10，rework-2）

主控指出：上轮 approved 漏掉了 audit-B9 **A-B9-001** 的阻断定性——「tip 剔除无条件吞两空格正文行」按派发口径应为 changes-requested（我在 §一实证过该机制可吞 flush 行但判为未观测版式，定级偏轻）。本轮范围更新为 `main(cf52ca8)...HEAD(1511ea2)`。

### rework-2 变更复核（`git show 1511ea2`）

`output.py` L89-97、134-141：

```python
if (transcript[i] == _DEVIN_DID_YOU_KNOW
        and (i == 0 or blank(transcript[i - 1]))):   # 新增：须处可证明 UI 边界
    del transcript[i]
    taken = 0
    while (i < len(transcript) and taken < _DEVIN_TIP_CONT_MAX   # 新增：上限 2
           and transcript[i].startswith("  ") and transcript[i].strip()):  # 新增：续行须非空
        del transcript[i]
        taken += 1
```

逐条核对：

- **边界条件** `i == 0 or blank(transcript[i-1])`：header 只在转录首行或空行后成立——真实语法中块首行均为 0/1 空格（`❭`/` ⏺`/` text`/` │`），故边界后紧贴的 ≥2 空格行在已观测版式中只可能是 tip 续行。检查用的是删除前的 `transcript[i-1]`（删除只去 ≥i 的行），语义正确。
- **`_DEVIN_TIP_CONT_MAX = 2`**：实采样 tip 均恰 1 行续行，cap=2 容纳一行换行宽度下的两行 tip 体；超过即保留（>2 行 tip 的残行会留存为正文——见残差说明）。
- **`transcript[i].strip()` 非空要求**：纯空白行不消耗 cap 且终止吞噬，防止空白续行占额。
- **新增负例测试** `test_did_you_know_boundary_never_eats_assistant_indents`：case(1) 非空行后 header+缩进全保留（恢复 PR #23 near-match 精神）、case(2) cap=2 后 KEEP_THREE/FOUR 保留、case(3) 无 header 缩进正文不动——A-B9-001 关闭要求的两类反例齐备。

### A-B9-001 / A-B9-002 收敛核对

- **A-B9-001 收敛**：审核的反例形态（header 紧贴非空行 + 两空格正文）现判定为非边界不剔除，正文保留；负向断言已恢复并扩为三例。
- **A-B9-002 收敛**：o24 `findings.md:94` 已改引「§4.2 第 4 条（第 66 行）」（工作区账本，随其自身提交流程）。

### 派发实证：含两空格缩进代码/列表的助手正文完整保留

用 o30 `.venv` 直测 `extract_new_text`（before=`devin_screen(DEVIN_HISTORY)`，after 经 `round()` 构造）：

| 构造 | 结果 |
|---|---|
| A：`P1_LIVE_OK` + `  - item one` / `    def nested():` / `      pass` / `  - item two`（无 tip） | `candidate`，body 含全部 5 行，缩进层级完整（`def`/`pass` 的 4/6 空格经 `[1:]` 剥一层后为 3/5 空格保留） |
| B：`P1_LIVE_OK` + 空行 + 真 tip（header+3sp body）+ 空行 + `  - keep a`/`    keep_code()`/`  - keep b` | `candidate`，tip 行消失（仅余空位行），keep 三行全保留 |
| C：audit 原反例 `P1_LIVE_OK` + ` ✱ Did you know`（前一行非空）+ `  KEEP_TWO_SPACE_BODY` | `candidate`，header 行与两空格正文行**均保留**在 body 中——A-B9-001 的误吞路径已封死 |

### 变异点验证（本轮新做）

基线 `sha256(o30:output.py)` = `af6bc183650f0026cd1b5880b2997a32ff15e186c20376d699d726e9bb5964c4`

| # | 变异 | 施加态 hash | 结果 | 还原态 hash |
|---|------|------------|------|------------|
| M1 | `_DEVIN_TIP_CONT_MAX` 2→99 | `9e33eeaa0d404d64eb4adbbf74a99862a8444511ec23390dbd42b2830465ef0c` | **FAILED (failures=1)**：`test_did_you_know_boundary_never_eats_assistant_indents`（case 2：KEEP_THREE/FOUR 被吞） | `af6bc183…64c4`（=基线） |
| M2 | 去掉 `and (i == 0 or blank(transcript[i-1]))` 边界条件 | `81d384c09e787451aa13f0cf35cab2fbda6b754849483ce5b49c3fc96c9d70d3` | **FAILED (failures=1)**：同测试（case 1：非边界 header 被剔、正文缩进行被吞） | `af6bc183…64c4`（=基线，`git status` 空） |

两条保护（边界、上限）各被新反例测试精确钉住，破坏任一即红。

### 独立重跑（本轮，HEAD=1511ea2）

```text
env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests
→ Ran 309 tests in 41.300s — OK   （=308+1 新边界测试；同样的非失败警告）

.venv/bin/python -m compileall -q feishu_herdr_bridge tests → exit 0
git diff --check             → exit 0（工作区）
git diff --check main...HEAD → exit 0（PR #31 全范围含 1511ea2）
```

### 残差说明（不阻断）

- 若 Devin 某版式产出 >2 行的 tip 续行，第 3 行起留存为正文（cap 取舍）；实采样均 1 行，cap=2 留一行余量。
- 对称面：非空行后的 header 形行现一律保留（case 1 语义）——若真 tip 出现「前一行非空」版式将漏剔留存，为未观测版式，方向保守可接受。

### 增量结论

**approved**（`main...1511ea2` 全范围）。A-B9-001/002 均收敛：剔除被约束到可证明的 UI 边界（首行或空行后）且续行封顶，正文缩进形态（代码块/列表/两空格行）三种位置实证完整保留，负向覆盖恢复并加强。
