# findings — ISSUE24

| ID | 级别 | 发现 | 证据 | 状态 |
|----|------|------|------|------|
| F-001 | P0 | B1 根因候选集（非穷尽）；B2 验证 PR #25 对假设 (i) 的最小修复并补取证日志 | 本文件 + progress.md | done |

## F-001 B1 根因确认（执行 worker；按 audit-B1 changes-requested 整改）

### 日志现场事实（`journalctl --user -u feishu-herdr-bridge`，秒级粒度）

```
00:01:50 watch=c6d1… event=armed  reason=submitted
00:02:04 watch=c6d1… event=closed reason=cancelled
00:02:05 POST（P2 回执 = Issue #24 的"本次自动回传未启用"）
00:03:27 POST（无 watch 事件 —— 应为 /read 诊断 或 又一条 capture→None 的提示）
00:04:22 watch=fd17… event=armed  reason=submitted
00:04:35 watch=fd17… event=closed reason=cancelled
00:04:35 POST（P4 回执，同样"未启用"）
```

可证的推断：
- 两次失败均为：新提示取消旧 watch 后，其 `capture()` 返回 `None`（无新 watch 的 armed/closed 事件 → 连 `Observation` 都未创建；若创建后 arm 失败会多一条新 watch_ref 的 closed）。
- `cancelled` 由新 prompt 的 `_capture_output`→`_cancel_output` 触发（core.py 调用顺序），非 `/read`——P2/P4 为普通文本且拿到"未启用"回执。
- Issue #24 自述 pane 当时 working（P2 进了原生队列）。

### capture() 返回 None 的语义条件组（main@04f1580 单读版；11 组 = 语义分组，非 11 条独立 return）

| # | 组 | 覆盖的出口 | 判定 | 依据与不确定度 |
|---|----|-----------|------|--------------|
| 1 | invalid_origin | `_valid`：session≠kpi-agg / kind∉KINDS / chat_type≠group / revision≤0 / 标识符非法 | **有旁证未排除** | session/revision/chat_type/标识符有强旁证（同群、绑定 revision=1 前后一致）；**kind 由 `agent get` 实时检测，capture 时刻返回值无日志**——检测为 unknown 时此组即触发 |
| 2 | not_allowed（入口） | `_stopping` / `current()` 假 / `request_phase`≠processing | **有旁证未排除** | 服务未停（后续 POST 照出）、同群前后 armed 证明守卫曾过；但两次复查点之间是否翻转无日志 |
| 3 | same_message | 旧 watch 的 message_id 相同 | **已排除** | 两条不同消息必然不同 message_id |
| 4 | capacity | `_active`≥16 | **有旁证未排除** | 同群只见一条 active watch；**全进程 `_active` 计数无日志证据**，不能凭现场断言"容量 1" |
| 5 | prompt_invalid | `_prompt` 拒绝（控制字符/空白/超长） | **有旁证未排除** | 报告中的提示为普通短文本且进了原生队列；但 `_prompt` 规则是否触发无直接日志 |
| 6 | get_failed | `reader.get_agent` 抛异常（≤2s 边界/CLI 错/decode 失败） | **未知** | 无捕获点日志；journald 秒级时间戳 + 后续 prompt/record/send 链路耗时，**不能用 cancelled→POST 同秒来排除 2s 边界失败** |
| 7 | kind_mismatch | `_matches`：agent get 报的 workspace/pane/kind 与冻结目标不符 | **未知** | 与"kind 检测异常"不同——目标本身漂移（重绑/迁移）或 get 误报均可能，无日志区分 |
| 8 | status_other | `state.status`∉{idle,done,working} | **未知** | output.py 注释"same signal the agent detector uses"是作者意图非实测；queue 显示期 `agent get` 实测返回什么 status/kind 无证据 |
| 9 | not_allowed（get 后复查） | 同 #2，时点在 get 返回后 | **有旁证未排除** | 同 #2 |
| 10 | read_failed | `reader.read_agent` 抛异常 | **未知** | 同 #6 |
| 11 | frame_unparsed / frame_status_other | 读回帧 `_frame`→None（撕裂帧或持续未识别的真实形态）；或解析成功但 status∉允许集 | frame_unparsed **有旁证未排除**；frame_status_other **已排除**（devin 实帧解析器只产 idle/working，synthetic 标记帧不可能出现在真 pane） | 已知采样形态单读即解析 → live None 若落此组，只能是采样外形态（撕裂或未建模），**无法在二者间排序** |

另：`not_allowed` 在入口、get 后、read loop 内、loop 后共四处代码位置，同一语义条件组同一码；未列于表的兜底 `except` 对应 B2 新增的 `error` 码。

### B1 测试证据（synthetic fixtures，均非 live capture 帧）

| 用例 | 性质 | 结果 |
|------|------|------|
| `test_issue24_second_prompt_during_working_turn_arms` | Issue #24 建模场景：idle arm → working → 取消 → 用 /read 描述形态的 working 帧 capture | armed |
| `test_working_capture_variants` queue_chrome_prior/new_prompt | **解析器回归**（采样 queue chrome 可解析为 working 基线）；新提示在 capture 时刻不可能在队列里（capture 先于提交）→ 仅证明解析能力 | armed |
| torn_then_queued_frame | **synthetic hypothesis**：首读撕裂次读完整 → PR #25 重试路径生效（该形态在单读版上会失败） | armed |
| torn_twice / two_queued_rows / read_raises | 合同要求的 fail-closed | None + `frame_unparsed`/`frame_unparsed`/`read_failed` |
| cancelled_watch_leaves_no_residue / shared_prefix_prompt | 变体 (b)/(c) 排除 | armed |

### 结论

- **候选集非穷尽但覆盖已知出口**；离线证据只能做到上表三栏分类，不能在候选间排序——"撕裂帧"是 Issue #24 正文与 PR #25 的假设 (i)，有建模测试支持其可修复性，**但无证据称其为"最可能"或"已确认根因"**。
- B2 的定位：**验证 PR #25 对假设 (i) 的最小修复 + 为 capture 全部拒绝出口补固定码取证日志**（`output capture=declined reason=<码>`），使下一次 live 失败可直接从日志读出条件组。
- 若需进一步区分，唯一路径是 live capture 时刻的帧/状态遥测——已由 B2 的拒绝码日志部分覆盖；更细的帧采样属合同禁止项，需要时交主控/用户裁决。

## F-002 B5 真实验收（2026-09-13 12:14，dh-relay 群 → w15:p1 devin）

- 12:14:47 第一条（sleep 25）`armed reason=submitted`；12:15:10 第二条到达：旧 watch `closed reason=cancelled`，随后 **`capture=declined reason=frame_unparsed`** → 飞书回执"本次自动回传未启用"。
- Pane 事后画面：两条提示都被 Devin 正常执行并回答（"ISSUE24 验收标记 B"），**没有出现 Press Enter 队列提示** → D-003 的"需代按 Enter"假设在本次不成立。
- 结论：live 根因是 F-001 的 (ii) **持续性未识别的真实 working 帧**（两次基线读都 `_frame()→None`），不是撕裂帧；PR #25 的重试对此无效。取证日志（D-001 b）起了作用。

## F-003 B6 状态栏尾部变体清单（取证档案，Issue #28 / PR #29）

Devin 状态栏末行尾部随状态循环切换；`_frame()` 对未列入白名单的尾部一律 fail-closed（`frame_unparsed`）。已观察到的 verbatim 尾部：

| 尾部（verbatim，模型名后） | 观测场景 | 来源 |
|---|---|---|
| `Context: NN / NNNk tokens (NN%)` | working / idle / done 常态 | w1V:p5 采样（12/18 帧） |
| `Press alt+t to cycle thinking levels` | working 期间高频（8/10 visible 帧） | w1V:p5 采样 |
| `See usage and cost: /session-stats` | idle（刚启动） | o24-monitor 目击（verbatim） |
| `Press Ctrl+L to clear the screen, Ctrl+Shift+L to redraw` | done 完成瞬间 | o24-review2 目击（verbatim）；本次采样窗口未自然复现 |

影响面：不只 working 基线——**idle/done 的 after 帧命中未识别尾部同样 `_frame()→None`，会导致 tick 侧 `extract_new_text` 拿不到 after 帧、最终正文永远发不出**（与 capture 侧 `frame_unparsed` 同源）。PR #29 已将四条全部以精确字面并列加入 `_DEVIN_STATUS` 白名单（无通配、末行位置检查不变）；未观察到的尾部继续 fail-closed 并由 `capture=declined reason=frame_unparsed` 取证。

## F-004 第二次真实验收（2026-09-13 12:48，dh-relay 群 → w15:p1，live=cf52ca8）

- 12:48:19 P1(sleep 25) armed；12:49:11 P2 到达：旧 watch cancelled，**P2 成功 armed（frame_unparsed 已消失，PR #29 生效）**。
- 12:51:11 P2 watch `attempted reason=deadline` → `closed reason=sent`：两分钟内没提取到可发送正文，只发了"本次自动回传已停止"。
- Pane 事后画面：P2 提示为 `ISSUE24 验收标记 C`，Devin 回复**与提示逐字相同** `ISSUE24 验收标记 C`；状态栏尾部为 Context 变体。
- 待查：tick 侧 `extract_new_text` 为何在 working→idle 后始终拿不到候选——疑点 (a) 回复文本与提示完全相同被当作回显过滤；(b) before 基线是 P1 working 帧（含 P1 transcript），after 帧 transcript 滚动导致对齐失败；(c) after 帧 `_frame()` 仍有未识别行（如 `10 subagents · ↓ select` 活动行）。

## F-005 PONG-D 对照验证（2026-09-13 12:55，同群同 Pane，idle 状态单条）

- 12:55:56 armed → 12:56:07 `attempted reason=body` → `closed reason=sent`，飞书收到正文 `PONG-D`。
- 结论：F-004 的 deadline 根因锁定为 **(a) 回复文本与提示逐字相同被当作回显过滤**（回复不同于提示即正常回传）。属测试构造边界，但应有测试钉住并在文档写明。
- 新发现（P3）：回传正文里夹带了 Devin 的 `✱ Did you know / Use Shift+Tab to cycle permission modes, and /plan and /ask to switch profiles` 提示。PR #23 只精确排除了一条 tip 文本，其它 tip 会漏进正文。需要按 tip 块结构（`✱ Did you know` 标题行 + 紧随缩进行）整体排除，而不是逐条枚举文本。

## F-006 B9 deadline 根因判定与处置（Issue #30 / 新 PR，o30 worktree）

- **(a) 回复==提示 → echo_only：合同内行为，保留过滤**。`auto-pane-output-design.md` §4.2 第 4 条（第 66 行）明写"候选只是提示回显或与规范化后的提示完全相同，也不发送"。真实帧复现（w1V:p5，`ISSUE24 验收标记 D` → 回复逐字相同）：before=f_08 done 基线 + after=f_15 done 帧 → `extract_new_text` 返回 `echo_only`，与 live deadline→"已停止"一致。F-005 对照（PONG-D 回复≠提示 → 正常回传）进一步钉死。处置：测试钉住 + 本文档说明；**标记协议类验收必须使用"回复≠提示"的提示**。放行 verbatim 回复需要合同修订（位置上可区分——回复块在已核验新用户块之后），属主控/用户裁决，不在本批。
- **(b) 滚屏对齐失败：真实存在且 fail-closed**。f_01（working+队列）与 f_08（done）相隔多轮，80 行可见窗滚屏 → 基线非 after 前缀且无唯一锚点 → `ambiguous_overlap` → 不发送。属合同保守性，测试已钉住。
- **(c) after 帧未识别行**：未复现——`10 subagents · ↓ select` 活动行已被 `_DEVIN_ACTIVITY` 末行剥离覆盖，四条状态栏尾部（PR #29）均已白名单。
- **P3 tip 污染修复**：`_DEVIN_DID_YOU_KNOW_TIP` 固定文本配对（仅 `/bug` 一条、仅末尾位置）→ 结构性剔除（` ✱ Did you know` 标题行须恰匹配且上一行为空行/帧首；续行只吃紧随的 `  ` 起首非空行、封顶 2 行；越界保留——rework-2 按 A-B9-001 收紧到可证明 UI 边界，防误吞助手缩进正文）。真实帧证实污染路径：tip 与相邻 ASSISTANT 块合并进正文（如 `PONG-D\n\n✱ Did you know\n  Use Shift+Tab…`）。测试样本覆盖：b6de85e 那条（/bug）、本次泄漏那条（Shift+Tab）、Type @ 变体三条 verbatim 全钉住；旧测试的"同 header 异续行保留"语义按本批修订为同样剔除。

## F-007 第三次真实验收（2026-09-13 13:38，dh-relay 群 → w15:p1，live=b19780e）

- 13:38:56 P1(sleep 25) armed；13:39:18 P2(PONG-E) 到达：旧 watch cancelled，P2 armed（基线 = P1 working 帧）。
- 13:40:02 `attempted reason=ambiguous_overlap` → `closed reason=sent`（只发"已停止"）。Pane 里 Devin 已正确回复 `PONG-E`，无 tip 残留。
- 根因 = F-004 候选 (b)：before 基线是 P1 仍在 working 的帧（含 spinner/工具块进行中），after 帧里 P1 的工具块展开 + "完成。" + P2 用户块 + 回复；before/after 的 transcript 对齐判定为歧义 → fail-closed。这正是 Issue #24 的目标场景（working 中追加提示），当前实现对它结构性失败。
- 需要的能力：以 P2 的用户块（`❭ ` + 规范化 P2 提示）作为唯一锚点，候选 = 锚点之后到 chrome 前的助手正文；锚点在 after 帧出现且在 before 帧不存在即为"新用户块 + 增量"证明，不再要求 before 尾部逐行对齐。

## F-008 B12 修复：回显锚点提取（PR #33）

- **复现**（w1V:p5，`sleep 30` + working 中发 `收到后只回复 PONG-F 这四个字符`，采 23 帧）：before=f_01~f_19 working 帧、f_20 working（转录已含 P2 回显）、f_21~f_23 idle（`❭ P2回显` + ` PONG-F`）。
- **歧义步定位**：旧 `_extract` 要求 `old[-k:] == new[:k]` 滚动衔接锚点。f_09~f_19 基线全部 k=0 → `ambiguous_overlap`——working 期间可见窗继续滚动，after 帧头部早于 before 尾部（衔接点滚出窗口）；且 before 尾的 `○ Running command` 进行中工具行在 after 中变形为 `⏺ Ran command` 并扩行，行级锚点结构性不存活。k=2 时唯一命中也落在展开后的 TOOL 块内部 → 同一 ambiguous 出口。
- **修法（合同内）**：以提示自身的用户块回显 `❭ <规范化提示>` 为唯一锚点——after 中恰一次、before 中不存在即构成 §4.2 第 3 条的"新用户块+增量证明"；候选 = 锚点之后、chrome 之前的 ASSISTANT 块（tip 结构剔除/echo_only/两次稳定均不变）。不再要求 before 尾部逐行对齐；锚点缺/多/已在基线 → `ambiguous_overlap`；锚点后出现另一用户块 → `ambiguous_prompt`；APPROVAL → `attention`。
- **语义变更（如实记录）**：
  1. 同一提示被重发两次（after 中两个相同回显）→ 旧行对齐可区分、新语义 fail-closed `ambiguous_overlap`（合同本来就要求拒绝"旧回显冒充候选"）；
  2. 基线与回显之间的游离 ASSISTANT 块（上一轮迟到输出）→ 旧 `mine!=0` 判 `ambiguous_prompt`，新语义视为旧区排除、不否决锚点后正文；
  3. 提示回显尚未渲染而上一轮答案先出现 → 旧 `ambiguous_prompt` 立即 NOTICE 会杀死 watch，新语义 `waiting_for_prompt` 静默等待——排队提示本来就要等 Devin 拾取才回显，这是修正。
- **验证**：真实帧 f_01~f_19 全部基线 → `candidate 'PONG-F'`（旧实现 f_09~f_19 全 ambiguous_overlap）；f_20 基线 → `ambiguous_overlap`（fail-closed 钉住）。`unittest discover` **313 tests OK**；compileall / diff-check 干净。

### F-008a rework-1（audit-B12 A-B12-001/002/003/004 整改）

- **行级增量证明恢复（A-B12-001）**：锚点降为"定位"——`before` 全前缀或 `before` 尾部在 `after` 中唯一再现（≥2 非空行、两帧各唯一、允许滚屏偏移）仍是新区域证明的硬前提；回显锚点必须落在证明点之后；候选正文非空行不得在 `before` 出现。零 overlap + 历史同文回显 → `ambiguous_overlap`（审核反例已钉为测试）。重排历史且尾部 run 仍唯一的场景仍提取（echo 不在 before + 正文行不在 before 双保险），如实记录为对合同规则(b)的"任意位置"泛化。
- **空白碰撞修复（A-B12-002）**：回显不再 `split()` 折叠——剥 `❭`/`  ` 槽位后按片段保留全部内部空白；折行边界每处至多补一个被吞空格；空行=真实空行段。单行 `alphabeta` 回显不再匹配 `alpha beta` 提示（冒充用例已钉）。残差如实记录：恰在折行边界处差一个空格的两个提示在屏幕上不可区分（同一像素形态），匹配两边都认——该歧义是介质固有的。
- **fixture provenance（A-B12-003）**：三个新 fixture 头部加 `#` 注释（pane/时间/读命令/帧号/sha256）；`_live_fixture` 剥前导注释行。采样账本：2026-09-13 13:5x，`herdr agent read w1V:p5 --source visible --lines 80 --format text`，o24-review2 授权 pane；f_14=`devin_tick_working_tool_baseline.txt`(sha c26fc510…)、f_20=`devin_tick_echoed_baseline.txt`(sha ffca13f6…)、f_21=`devin_tick_pong_f_done.txt`(sha 2fa999db…)；原始 23 帧在 /tmp/b12/（会话临时目录，不入库——含轮次上下文文本，无凭据）。
- **pytest gate（A-B12-004）**：环境无 pytest（`.venv/bin/python -m pytest` → No module）；以项目既有 `unittest discover` 为 gate——实际命令与环境已写入 PR #33 描述供审核复跑。

## F-009 B14 发送侧等空闲再发——真实验证通过（PR #35，o34 worktree，live=29134f6）

- **环境**：一次性授权 pane `w1V:p7`（tab `b14-probe`/`w1V:t7`，`herdr tab create --workspace w1V`，devin `swe-2-medium --permission-mode dangerous`）；非 w1V:p2。驱动 = 真实 `BridgeCore` + `HerdrAdapter`（session kpi-agg）+ `SendQueue` + `connect_output`，send 出站用桩收集。验证后 tab 已关闭。
- **序列**（第三次运行，前两次因下方记录的独立原因未回传）：
  1. `/bind w1V w1V:p7` → `bound`（假群 `chat-b14`）
  2. `请只执行 sleep 30 这一条命令，不要做别的` → `submitted` + `watch armed reason=submitted`（idle 即时路径不变）
  3. 0.3s 后 `agent_status=working` 确认，发 `只回复 PONG-J` → **`queued`，回执「已排队，等 pane 空闲后发送」**；audit `enqueued/busy`；无 `agent prompt` CLI 调用
  4. pane working ~30s 期间 prompt2 一直未发出；pane 转 done 的瞬间：prompt1 watch `closed reason=cancelled`（被延迟 capture 正当取消，单 watch/chat 不变量）→ queue `closed/submitted`（**此时才调用 herdr prompt**）→ prompt2 watch `armed reason=submitted`（延迟 capture 在 idle 点成功取到基线）
  5. pane working→done 一轮后：`attempted reason=body` → **`[SEND→chat-b14] '主控 Pane w1V:p7\nPONG-J'`** → `closed reason=sent`
- **结论**：Issue #24 目标场景端到端打通——working 中到达的群提示排队、pane 空闲后才发送、capture 基线取自空闲点、正文 `PONG-J` 自动回传。`output.py` 与 §4.2 未动。SQLite：两条 prompt 均 `done/submitted`。
- **伴随观察（非 B14 缺陷，如实记录）**：
  - `agent_status` 滞后于 TUI 约 ≥0.3s——prompt1 提交后立刻查仍可能见 `idle`；到达时刻恰好赶在状态翻转前的消息仍会走即时路径。这是探测介质的固有窗口，非排队漏洞（FIFO 由 `pending()` 兜底）。
  - Devin 启动 banner/logo/Trust 行在 80 行可见窗内时 `_frame()` 返回 `frame_unparsed`（前两次运行 capture 因此 declined）；转录增多滚出窗口后即解析正常——属 parser 覆盖面边界，按 D-005 不在本批处理。
  - prompt1 自己的答复（`Done — sleep 30 completed…`）未回传：prompt2 的延迟 capture 先一步取消了它的 watch。符合"最新一条拥有最终回复"的既有语义。

### F-009a rework1 完整 receipt（commit `0403752`，可独立复核）

- **命令**：`/home/nash/work/lark-herdr-o34/.venv/bin/python /home/nash/work/lark-herdr-o24/docs/workspace/01-ISSUE24-devin-queue-auto-return/orchestration/receipts/b14_rework1_receipt.py <outdir>`（脚本 SHA-256 `a236e7107c6759bc80de0c62b1dd7006b88e62661fe7764aa1bed4d6c475d9f4`；假时钟定死，无 sleep，输出除日志墙钟外逐字节确定）
- **对象**：真实 `BridgeCore` + `Store`（真 SQLite 文件）+ `SendQueue`（真 audit logger）+ `OutputObserver`（`tick()` 手泵，与 runtime 线程同一契约）；pane 侧为 `tests/fake_herdr.py` 子进程 fake（devin kind，屏幕= `tests.test_output.devin_screen` 合成帧），飞书出站为记录桩。**口径声明：本条证明 rework 后 `queued→edge/observed→closed/submitted→armed→attempted/body→closed/sent` 链与 audit/SQLite 落定；真实 pane 侧执行仍以上文 w1V:p7 正向 smoke 为准，stub 出站不证明真实飞书 POST。**
- **逐字输出**（`0403752` 工作树上执行）：

```text
wall=1751500001 handle '/bind workspace-a pa' → done/bound '[workspace-a / pane-a] 绑定成功；后续文本发往此 Pane。'
wall=1751500002 handle '请只执行 sleep 30 这一条命令，' → done/submitted '[workspace-a / pane-a] 已提交，尚未确认任务完成。'
wall=1751500003 handle '只回复 PONG-K' → done/queued '[workspace-a / pane-a] 已排队，等 pane 空闲后发送。'
mono=5003 poll() → False (pane working: busy edge observed, head kept)
mono=5006 poll() → True (gate open: send, arm)
mono=5009 tick() → True (first stable candidate)
mono=5012 tick() → True (stability met: deliver)

--- receipts (stubbed Feishu outbound) ---
wall=1751500003 SEND→chat-b14r '主控 Pane pane-a\nPONG-K'

--- send-queue + output audit lines (bridge.log) ---
2026-09-13T15:46:39 feishu_herdr_bridge.output output watch=49ece190bfb150e1 revision=1 event=armed reason=submitted
2026-09-13T15:46:40 feishu_herdr_bridge.core send-queue message=m-3 revision=1 event=enqueued reason=busy
2026-09-13T15:46:40 feishu_herdr_bridge.core send-queue message=m-3 revision=1 event=edge reason=observed
2026-09-13T15:46:40 feishu_herdr_bridge.output output watch=49ece190bfb150e1 revision=1 event=closed reason=cancelled
2026-09-13T15:46:40 feishu_herdr_bridge.core send-queue message=m-3 revision=1 event=closed reason=submitted
2026-09-13T15:46:40 feishu_herdr_bridge.output output watch=a70dd36a349b2741 revision=1 event=armed reason=submitted
2026-09-13T15:46:40 feishu_herdr_bridge.output output watch=a70dd36a349b2741 revision=1 event=attempted reason=body
2026-09-13T15:46:40 feishu_herdr_bridge.output output watch=a70dd36a349b2741 revision=1 event=closed reason=sent

--- SQLite requests (receipt.sqlite3) ---
('m-1', 'bind', 'done', 'bound', 1751500001.0)
('m-2', 'prompt', 'done', 'submitted', 1751500002.0)
('m-3', 'prompt', 'done', 'submitted', 1751500003.0)

--- SQLite bindings ---
('chat-b14r', 'workspace-a', 'pane-a', 1, 1)
```

- **rework1 覆盖新增**：in-flight 边沿闸（每次发送后须观察到 non-idle 再回到 idle/done 才放行下一条，`queue_edge_cap` 默认 15s 超时按 non-idle 放行并记 `edge/unobserved`）；发送点 fail-closed（capture baseline 非 idle/done 或 capture→prompt 间翻转 → 取消 capture、队首保留）；超时终态先落库再回执/出队（落库失败保留可重试）；deferred capture 统一 finally 清理。回归：`unittest discover` 329 tests OK（test_queue.py 19 例，新增 stale-idle 窗口/edge-observed/baseline_busy/status_changed/settle_failed/prompt失败取消/arm抛错取消用例）。

## F-010 Issue #24 目标场景 live 验收通过（2026-09-13 16:39，main=780db5a，PR #35 已部署）

用户在飞书群（chat oc_b101…，绑定 w15:p1K rev3）先发 sleep 25，紧接发"收到后只回复 PONG-M 这六个字符"。桥日志：16:39:09 `send-queue enqueued reason=busy` → `edge observed` → 16:39:35 `closed reason=submitted` + `watch armed` → 16:39:44 `attempted reason=body` → `closed reason=sent`。飞书收到"主控 Pane w15:p1K PONG-M"。requests 表两条均 done/submitted。

前一轮 16:35 失败（capture=declined reason=frame_unparsed）为 pane 被 w15:p1 上的另一编排插话并打断 sleep 所致（画面处于 Canceled 过渡态），非桥缺陷，已由本轮排除。
