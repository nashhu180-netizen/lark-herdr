# Round-2 复核报告 — B12 / PR #33（Devin 回显锚点提取）

- 复核人：devin swe-2-max（fresh 视角，未参与实施，未读审核结论先行）
- 对象：`/home/nash/work/lark-herdr-o32`（分支 `fix/devin-anchor-extract`）
- 范围：`git diff main(b19780e)..HEAD(e6f9673)`，commit `e6f9673`（fix: anchor tick extraction on the new user-block echo）
- 复核时间：本轮会话内一次性完成；纪律遵守：未改生产/测试代码、未 commit、未合并、未部署

## 结论先行

**approved**

派发三项验证全部实测通过：真实 F-007 复现返回 `PONG-G`（非 `ambiguous_overlap`）；双锚点/折行/回显不符等反向用例均 fail-closed；全量套件 313 tests OK。

---

## 一、diff 复核（e6f9673，+114/−67）

生产改动仅在 `output.py` 的 `_extract`（+测试 + fixtures）：

| 旧语义（行级尾部对齐） | 新语义（回显锚点） |
|---|---|
| `_occurrences` 求 `before.rows` 后缀与 `after.rows` 前缀的最长唯一重叠 k | 删除 `_occurrences`；锚点 = after 中 `role=="USER"` 且规范化文本 == 提示的块 |
| k 无唯一命中 → `ambiguous_overlap` | 锚点 0 个 → `waiting_for_prompt`；≠1 个或已在 baseline → `ambiguous_overlap` |
| added = 衔接点之后行集 | added = `block.start >= anchor.end` 的块集（文档序） |
| 无 USER/ASSISTANT 残留校验同上 | 锚点后 USER → `ambiguous_prompt`；APPROVAL → `attention`；`body == prompt` → `echo_only` |

**正确性论证（独立推导，非沿用实施者口径）**：Devin 转录为时间序单调追加。锚点（本提示的 `❭` echo）在 after 中唯一且 baseline 中不存在 ⟹ 该 echo 块即本轮提交行为的可证明投影（§4.2 第 3 条"新用户块+增量证明"），其后块必然晚于本轮提交时刻，归属无歧义。before 内容仅用于证明锚点不在基线（防止把上一轮相同 echo 当锚），不再参与对齐——滚动、工具行变形（`○ Running`→`⏺ Ran`）、chrome 增减均不影响锚点成立性，这正是 F-007 的结构性死因。

**规范化匹配**：真实帧 `want = "".join(prompt.split())`，USER 块文本同法去全部空白后等值比较——使锚点对 TUI 硬折行免疫（见 §三 R2a/R2b 的精确边界）。

**tick 语义联动**（本轮重读 tick 循环确认）：`waiting_for_prompt`/`unchanged`/`echo_only`/`not_ready` 为静默续看（排队提示的 echo 晚到正走此路）；`ambiguous_overlap` 走 transient 预算（≤5 次后 NOTICE 一次）；`ambiguous_prompt`/`attention`/`deadline` 立即 NOTICE。deadline/MAX_POLLS 兜底不变。排队提示不再被旧路径的即时 NOTICE 杀死——属于对 F-007 同族问题的正向修正。

**文档化语义收窄**（实施者如实记入 F-008，我复核属实）：①同文提示重发且旧回显仍在可视窗 → `ambiguous_overlap`（fail-closed，宁可不发）；②baseline 截点与锚点之间的游离 ASSISTANT 块不再否决本轮正文（旧 `ambiguous_prompt` 分支并入锚点后 USER 否决）；③回显未渲染时他轮答案先达 → `waiting_for_prompt` 静默等而非即时 NOTICE。①为收窄、③为放宽但均朝可验证方向，未发现合同违反。

## 二、真实 F-007 复现（派发指定 pane：w1V:p2 / o24-exec / Devin）

> 已按更正使用空闲的 o24-exec pane `w1V:p2`（非本 pane w1V:p5）。`agent get` 确认 `agent=devin, agent_status=done`（空闲）。

**操作序列**（逐条 `herdr --session kpi-agg`）：
1. `agent prompt w1V:p2 '请只执行 sleep 25 这一条命令，不要做别的'` → `agent_prompted`
2. `agent prompt w1V:p2 '只回复 PONG-G'` → `agent_prompted`（紧接发出）
3. ~2s 后 `agent read w1V:p2 --source visible --lines 80 --format text` → **baseline 帧**（存 `/tmp/b12_baseline.txt`）
4. 轮询至 `❭ 只回复 PONG-G` echo 出现且 `── 1 queued` 消失且独立行 ` PONG-G` 出现 → **after 帧**（存 `/tmp/b12_after.txt`）

**baseline 帧实测形态**（F-007 关键要素全部在场）：
- `❭ 请只执行 sleep 25 这一条命令，不要做别的`（P1 echo）
- `○ Running command / │ $ sleep 25`（**进行中工具行**——F-007 的行级锚点死因）
- `⠋⠀ Typing · 2s` / `── 1 queued ──` / `○ 只回复 PONG-G`（P2 在原生队列）
- `❭ Press Enter to send queued messages now` + `Press alt+t to cycle thinking levels` 尾

**after 帧实测形态**：
- `⏺ Ran command / $ sleep 25 / Exited with code 0`（工具行**已变形**，与 baseline 行级不对齐）
- `  sleep 25 已执行完毕（exit 0），未做其他操作。`（P1 答复）
- `❭ 只回复 PONG-G`（P2 echo，已从队列被拾取）
- ` PONG-G`（P2 答复）+ ` ✱ Did you know / Use /help…`（第 5 种 tip 文案，被 PR#31 结构剔除覆盖）+ idle composer

**修复代码实测**（o32 `.venv` 直跑）：
```
baseline: status=working real=True
after   : status=idle    real=True
extract_new_text('devin', baseline, after, '只回复 PONG-G')
→ body='PONG-G'  reason='candidate'        ✓ 非 ambiguous_overlap
```

帧间行级差异确认旧代码必死：before 尾 `○ Running command`（1 工具行）vs after 同位 `⏺ Ran command`+`Exited`（2 行变形扩行），窗口滚动使 `before.rows` 后缀与 `after.rows` 前缀无唯一 k——正是 live F-007 形态。

## 三、反向用例（在真实 after/baseline 帧上做受控变体）

| 用例 | 构造 | 结果 | 判定 |
|---|---|---|---|
| R1 锚点×2 | after 内插入第二个 `❭ 只回复 PONG-G` 块 | `ambiguous_overlap` | ✓ fail-closed |
| R2a 折行锚点（规范化等值） | echo 改 `❭ 只回复 PONG-\n  G` | `candidate 'PONG-G'` | 合同内：去空白规范化后仍唯一等值=可证明为本轮 echo，非 fail-open（见下） |
| R2b 折行锚点（尾部滚出/截断） | echo 改 `❭ 只回复 PONG-`（G 不存） | `waiting_for_prompt` | ✓ fail-closed |
| R3 回显不符 | echo 改 `❭ 只回复 PONG-H` | `waiting_for_prompt` | ✓ fail-closed |
| R0b 基线已含回显 | baseline 转录区插入 echo+答复块 | `ambiguous_overlap` | ✓ fail-closed |
| R4 锚点后又一 USER 块 | 答复后插入 `❭ 再来一条别的` | `ambiguous_prompt` | ✓ fail-closed |
| echo_only | 答复行改为与提示同文 ` 只回复 PONG-G` | `echo_only`（抑制） | ✓ |
| APPROVAL | 真实帧 APPROVAL 块不可构造（合成帧专属解析路径；真实审批走 `state.status`→`attention`，tick 层） | — | 由套件覆盖（313 全绿内含） |

**R2a 说明**（派发要求"锚点被折行必须 fail-closed"的精确边界）：规范化比较（双方去全部空白）使"折行但非空白字符逐字相同"的 echo 仍成立锚点——此时它确实是本提示唯一 echo，归属可证明，返回 candidate 是**正确的容忍**，不是放宽；真正危险的折行（文本不等值/截断/他块）全部落入 `waiting_for_prompt`/`ambiguous_overlap`。R2b+R3 即为该方向的实证。

## 四、独立重跑（o32 `.venv`，指向本 worktree已验证）

| 项 | 命令 | 结果 |
|---|---|---|
| 全量套件 | `.venv/bin/python -m unittest discover -s tests` | **Ran 313 tests, OK**（46.4s）|
| 字节码 | `.venv/bin/python -m compileall -q feishu_herdr_bridge tests` | exit 0 干净 |
| 空白检查 | `git diff --check`（工作区）+ `git diff --check main...HEAD` | 双干净 |
| 工作区 | `git status --short` | 干净（复核未留任何改动） |

**派发命令偏差说明**：派发指定 `python -m pytest -q`；o32 `.venv` 为运行态环境（`pip list` 实测无 pytest），系统 python3 亦无 pytest 模块。项目套件为 `unittest.TestCase` 形态（pyproject 无 pytest 配置），`unittest discover` 是仓库一贯的全量入口，覆盖同一 313 用例——以此等价执行并如实标注。

**变异点说明**：本轮派发验证项为「真实复现+反向用例+全量套件」，未列变异要求，且明确"不改代码"——故未施加变异。判绿证据以上述真实帧实证为准（强于合成变异）。

## 五、风险与遗留说明（不阻断）

1. **锚点可视窗依赖**：答复过长致 echo 滚出 80 行窗口时锚点缺失 → `waiting_for_prompt` 轮询至 deadline → NOTICE。与旧码同向失败且严格更优（旧码还需行对齐存活）。
2. **空白-only 提示变体**：`只回复 PONG-G` 与 `只回复PONG-G` 规范化等值——同发且皆可见 → `ambiguous_overlap`；仅新者可见 → 归属正确。方向仍 fail-closed。
3. **P 级问题**：无。

## 六、结论

`approved`。e6f9673 用「提示自身 USER echo 唯一性」替代行级滚动对齐，语义上把"增量证明"锚定在不可被滚动/工具行变形破坏的时间序不变量上；真实 F-007（w1V:p2，sleep 25 + 排队 PONG-G）实测返回 `PONG-G`；全部反向用例 fail-closed；313 tests OK；compileall/diff-check/工作区干净。两处文档化语义收窄（F-008）均朝可验证方向，未发现合同违反。

---

## 增量复核 143026e（rework1：恢复行级增量证明 + 逐字空白 echo 匹配）

- 范围：`e6f9673..143026e`（PR #33 第二个提交，audit-B12 rework1）
- 复核人：同上（devin swe-2-max）；复核点按主控转达的 decisions D-006 执行

### 改动复核（+149/−29）

1. **`_devin_frame` echo 表示重写**：USER 块内部空白逐字保留——`❭` 标记与两空格续行槽为 UI chrome 剔除；折行界记 `\x00`（至多代表一个被吞空格）；块内空行记真实 `\n`。`alpha beta`（真空格）与 `alphabeta` 从此不碰撞。
2. **`_echo_match`**：按 `\x00` 界回放折行，每界枚举 `{"" , " "}` 两种可能；分段间 `\n` 逐字。复杂度有界（分段候选 >64 退化为无界拼接、总候选 >256 早停判 membership）。
3. **`_overlap_boundary`（rule-2 证明恢复）**：baseline 尾部 run（须含 ≥2 个非空 `| ` 行）在 old/new 中各恰出现一次 → 返回 new 中 run 末偏移为 boundary；`new[:len(old)]==old` 前缀情形直接 boundary=len(old)。锚点 `start < boundary` 或 boundary 不可证 → `ambiguous_overlap`。**锚点只定位轮次，行级证明负责定序**——e6f9673 缺失的第二重证明回来了。
4. **候选体新增校验**：added 中 ASSISTANT 块的非空 `| ` 行若已存在于 baseline 行集 → `ambiguous_overlap`（防旧行浮出被当答复）。方向保守。
5. Fixture 补 provenance 头（pane/frame id/采样时刻/sha256 台账）；`_live_fixture` 加载器剥离 `#` 头。抽验 `devin_tick_working_tool_baseline.txt`：`tail -n +4 | sha256sum` = `c26fc510…e11` 与头声明一致，台账如实。

### 派发反例验证（o32 `.venv` 直测，全部手工构造真实帧）

**C1 零重叠 / 历史同文块 → fail-closed：**

| 用例 | 构造 | 结果 |
|---|---|---|
| C1a | before/after 转录零共享行，after 含 `❭ 只回复 PONG-G`+答复（e6f9673 下会误提取 `PONG-G`，因锚点唯一且 baseline 缺席） | `ambiguous_overlap` ✓ |
| C1b | baseline 已含同文 echo，after 中 echo×2 | `ambiguous_overlap` ✓ |
| C1c | 仅头部重叠、baseline 尾未重现（中间/头部重叠不算定序证据） | `ambiguous_overlap` ✓ |

**C2 空白碰撞 → 拒绝（`_echo_match` 逐字语义）：**

| prompt | echo 形态 | 结果 |
|---|---|---|
| `alphabeta` | `alpha beta`（未折行真空格） | `waiting_for_prompt` ✓ |
| `alpha beta` | `alphabeta` | `waiting_for_prompt` ✓ |
| `alpha\nbeta` | `alpha beta`（单行） | `waiting_for_prompt` ✓ |
| `alpha\nbeta` | `alpha\n\nbeta`（块内空行形态） | `waiting_for_prompt` ✓ |
| `alphabeta` | `alpha\x00beta`（真实折行） | `candidate`（折行界∈{无字符}成立）|
| `alpha beta` | `alpha\x00beta`（真实折行） | `candidate`（折行界∈{一空格}成立）|

折行 echo 对两种 prompt 均锚定属**本质歧义的正确容忍**：屏上 `alpha↵  beta` 无法区分 `alphabeta`/`alpha beta` 的折行——安全性由「唯一锚点 + 落在证新区」双重证明承载，而非靠猜；碰撞用例（非折行的逐字差异）全部拒绝。

### 真实 F-007 复跑（w1V:p2，PONG-H 轮次）

- 序列：`prompt '请只执行 sleep 25 这一条命令，不要做别的'` → 紧接 `prompt '只回复 PONG-H'` → 2s 后读 baseline → 轮询至 echo 出现+队列消失+独立 ` PONG-H` 行
- baseline：working，`❭ sleep 25` echo + `⠓ Typing` + `── 1 queued ── ○ 只回复 PONG-H` + alt+t 尾（存 `/tmp/b12r_baseline.txt`）
- after：idle，`⏺ Ran command/Exited 0` 变形工具块 + `❭ 只回复 PONG-H` echo + ` PONG-H`（存 `/tmp/b12r_after.txt`）
- **rework1 提取：`body='PONG-H' reason='candidate'`** —— F-007 未因 rule-2 恢复而回退（baseline 尾 `state.json…停下，等主控派单。`+空行+`❭ sleep 25` 在 after 唯一重现 → boundary 证成，锚点居证新区内）
- 另：rework1 代码对上一轮 PONG-G 真实帧对重放，仍 `candidate 'PONG-G'`

### 独立重跑（143026e 下）

| 项 | 结果 |
|---|---|
| `unittest discover -s tests` | **314 tests OK**（46.7s；+1 为新增 `test_echo_match_preserves_interior_whitespace`，另有反例测试改写） |
| `compileall` | 干净 |
| `git diff --check`（工作区 + `e6f9673..143026e`） | 双干净 |
| `git status` | 干净 |

### 增量结论

**approved**。rework1 把 e6f9673 缺失的定序证明补回且未回退 F-007：锚点负责定位轮次、`_overlap_boundary` 负责证明 after 确在 baseline 之后、候选行须为新——三重证明叠加后，零重叠/历史同文/空白碰撞路径全部 fail-closed。残差（不阻断，如实记录）：若 baseline 尾部整体变形且滚出（如尾行为进行中工具行且 after 中全部变形），boundary 不可证 → `ambiguous_overlap` → transient 预算后 NOTICE；这是合同正确的取舍（宁可拒发不可错发），两次实测中 baseline 尾恰为 `❭` P1 echo（Typing 期捕获）故证成。P 级问题：无。
