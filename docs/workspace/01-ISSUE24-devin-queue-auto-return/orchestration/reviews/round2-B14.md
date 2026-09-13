# Round-2 复核报告 — B14 / PR #35（发送侧等空闲再发）

- 复核人：devin swe-2-max（fresh 视角，未参与实施，未读审核结论先行）
- 对象：`/home/nash/work/lark-herdr-o34`（分支 `fix/…`，PR #35）
- 范围：`git diff b19780e..29134f6`，commit `29134f6`（feat: queue group prompts until the bound pane is idle）
- 纪律：未改生产/测试代码、未 commit、未合并、未部署；probe pane 用后已关闭

## 结论先行

**approved**

派发三项验证全部实测通过：F-009 端到端打通（忙时排队→空闲才发→PONG-K 自动回传）；三连发严格 FIFO；`queue_timeout=5` 超时回执与落定正确。322 tests OK。

---

## 一、diff 复核（+553/−11：`core.py` +253、`__main__.py` +46、新 `test_queue.py` +265）

**`SendQueue`**（core.py 新增 ~240 行）：每 pane 内存 FIFO。
- `enqueue`：在共享操作锁内被调用，请求行保持 `processing`（capture/arm 复用未变的 request-phase 守卫），audit `enqueued/busy`。
- `poll()`：每 pane 只驱动队头一次；超时先行判定 → `queue_timeout` + 回执「pane 持续忙，未发送。」；否则 `_submit`。
- `_submit()`：非阻塞抢 `core._lock`（前台操作优先，本轮放弃下轮再来）→ **锁内重读 pane 状态**（空闲检查与发送之间不可插入前台 prompt，B14 不变量）→ workspace 不符 `_invalidate_quietly`+`workspace_mismatch`；绑定四元组复查（revision/session/workspace/pane）→ `binding_changed`；群聊分支 `cancel_current`+`capture`（**基线取自空闲点**）→ 再次绑定复查（capture 引入的读耗时窗口）→ `herdr.prompt` → `finish done/submitted` → `arm`（arm 时行已 submitted，与即时路径的 phase 语义一致）。HerdrError→`unknown|failed`+`exc.code`；其他异常→`unknown/internal_error`；每条 settle 发固定码回执。
- `_settle` 仅在队首仍是本 entry 时 popleft；空队列清理。`stop()` 清队列 + audit `lost/restart`（内存丢失有记录，行保持 processing 由启动恢复补 `interrupted`）。
- `run()`：runtime 所有线程，`Event.wait(interval)` 可中断、非忙轮询；异常→`request_stop`（不打异常文本、不重放 poll）。

**入口分流**（execute 内 prompt 路径）：`send_queue` 存在且 `chat_type=="group"` 且（`agent.status∉{idle,done}` **或** `send_queue.pending(pane)`）→ enqueue + `Reply(done,queued,「已排队…」)`；`queued` 在 `_record` 前提前返回，行留 `processing` 由队列事后 settle。`pending()` 分支保证迟到消息永远排在已有队列之后——**FIFO 不依赖状态探测时机**。私聊不走队列（本就无 baseline capture，即时路径不变）。

**`__main__.py` 接线**：独立 `queue_adapter`（第二 HerdrAdapter，同一 ManagedRunner——CLI 子进程串行由 runner 单进程锁保证）；`queue_interval`/`queue_timeout` 入配置白名单且与其他 timeout 同法校验（有限正数）；queue 线程由 `start_output()` 拉起（daemon、命名 `send-queue`）；关停顺序 `stopping→request_stop→reap→join(≤3s)→stop()`，超时抛 RuntimeError 同既有线程一致。

**红线核验**：无按键/无重放/无通用 diff；队列失败全部固定码；`_emit`/`_finish`/`_settle`/notice-send 全部吞异常（审计失败不影响投递结果，方向正确——宁可丢日志不可误动提交）；`poll`/`run` 无 sleep 忙等。

## 二、真实 F-009 复现（自建 probe pane `w1V:p8`，非 w1V:p5/p2）

> 执行留下的 `b14-probe`（w1V:p7）验证后已关闭（findings 如实记录），按派发备选路径自建：`herdr tab create --workspace w1V --label b14-review-probe` → `w1V:t8`/`w1V:p8` → `pane run w1V:p8 "devin --model swe-2-medium --permission-mode dangerous"`（执行记录里的 `devin swe-2-medium` 实为本版 CLI 的 `--model` 旗标形式）。
>
> 驱动 = 真实 `BridgeCore` + `HerdrAdapter`(kpi-agg) + `SendQueue` + `connect_output`，仅 send 出站与 `queue.poll()/output.tick()` 由复核脚本桩收/手动泵（与 runtime 线程同一契约）。驱动脚本：`/tmp/b14_driver.py` 等，会话临时文件，未入库。
>
> 前置：Devin 启动 banner 在 80 行窗内时 `_devin_frame` 不解析（执行已记录的介质边界）——先直发 `seq 1 90` 把 banner 顶出窗口（实测 `frame=('idle',39,2)`）再开桥验证。

**Phase A — F-009 主场景**：
```
/bind w1V w1V:p8          → done bound
请只执行 sleep 25…         → done submitted + watch armed（即时路径不变）
+1.5s status=working
只回复 PONG-K             → done queued「已排队，等 pane 空闲后发送」；pending=True；audit enqueued/busy；无 prompt 调用
~25s pane 转 idle 瞬间    → 旧 watch closed/cancelled → queue closed/submitted → [PROMPT→w1V:p8] '只回复 PONG-K'（此刻才发出）→ watch armed
随后                      → attempted reason=body → [SEND→chat-b14r] '主控 Pane w1V:p8\nPONG-K' → closed/sent
```
**第二条消息确在 pane 空闲后才发出（`prompt_calls` 时间戳=t+~25s），回传 `PONG-K` 成功。**

**Phase B — FIFO 三连发**（忙时连发 `PONG-K1/K2/K3`，均 `queued`）：
- 提交顺序实测 `['只回复 PONG-K1','只回复 PONG-K2','只回复 PONG-K3']`——严格 FIFO（各在 pane 空闲点发出，间隔≈微回合时长 3s）。
- 观察（既有语义非本 PR 引入）：K1/K2 的 watch 被下一次延迟 capture 的 `cancel_current` 正当取消，仅最后一条 `PONG-K3` 回传——「最新回复拥有 watch」与执行记录如实标注一致；三条均 `done/submitted` 落定。

**Phase C — 超时回执**（`queue_timeout=5.0, interval=0.5`）：
- 忙时发 `只回复 PONG-KT` → `queued`；约 5s 后：`audit closed/queue_timeout` + `[SEND] '[w1V / w1V:p8] pane 持续忙，未发送。'`；`prompt_calls=[]`（从未发出）；请求行 `failed/queue_timeout`。

## 三、独立重跑（o34 `.venv`，指向本 worktree已验证）

| 项 | 结果 |
|---|---|
| `unittest discover -s tests` | **322 tests OK**（47.3s；基线 309 + 本 PR `test_queue.py` 13 例） |
| `compileall feishu_herdr_bridge tests` | 干净 |
| `git diff --check`（工作区 + `b19780e..29134f6`） | 双干净 |
| `git status` | 干净（复核未留改动） |

派发命令里 "全量 pytest"：o34 venv 无 pytest（运行态环境），沿用项目 canonical `unittest discover`（同 B12 已注明口径）。

## 四、审查发现与残差

**测试覆盖核对**（`test_queue.py` 13 例）：idle/done 即时、working/blocked 排队、空闲后延迟发送、timeout 回执+落定、同 pane FIFO、迟到消息不插队、私聊即时路径、绑定变更丢弃+回执、capture→arm 次序、poll 失败重试至超时、stop 丢队列+restart 审计、配置值校验——与本 PR 每个公开语义一一对应，无缺测面。

**残差/观察（均不阻断，如实记录）**：

1. **状态探测滞后窗**（执行同样如实记录）：`agent_status` 翻转滞后 TUI ~0.3s——恰在 prompt1 提交后极短窗内到达的消息会走即时路径发往正在启动的回合。缓解因素：`pending()` 使后续消息一律排队；且即便即时路径遇上 working 帧，B12 系修复后的锚点提取对 working 基线已可存活。属探测介质固有边界，非本 PR 可消除。
2. **中间回复被取代**：队列连续提交时前序 watch 被 `cancel_current` 取消——Phase B 实测仅末条回传。既有「一 chat 一 watch、最新拥有回复」语义，未由本 PR 改变。
3. **`get_failed` 重试日志频率**：pane get 持续失败时每 poll 一条 `poll/get_failed`（2s 间隔×timeout）——固定码无内容泄漏，量级可接受。
4. `_submit` 第二次绑定复查未含 `herdr_session`（第一次含）——session 变化必然伴随 revision 变化，无实际逃逸路径，记录备查。

**P 级问题**：无。

## 五、结论

`approved`。SendQueue 把「忙时到达的群提示」从 Devin 原生队列前移到桥内 FIFO：基线 capture 被推迟到 pane 可证的空闲点（锁内重读状态，B14 不变量成立），与 §4.2 的增量证明链无缝衔接；超时/绑定变更/workspace 漂移/CLI 失败各有固定码回执与落定；重启丢失显式记录。live F-009（PONG-K）、FIFO、5s 超时三条派发场景全部实测通过；322 tests OK。probe tab `w1V:t8` 已关闭。

---

## 增量复核 0403752（rework1：busy 边门 + 发送点重证 + 先持久化落定）

- 范围：`29134f6..0403752`（PR #35 第二个提交，audit rework1）
- 复核人：同上（devin swe-2-max）
- 环境：上轮 probe `w1V:p8`/`t8` 已关闭，本轮同法新建 `w1V:p9`/`t9`（`b14r1-review-probe`，devin swe-2-medium dangerous）；验证后已关闭

### 改动复核（+329/−82）

1. **in-flight 边门**（消掉我上轮残差①）：`mark_submitted` 在每次发送后置 `_inflight[pane]=now`（队列提交与即时路径 `execute` 内 `herdr.prompt` 之后都置）。`_gate`：观测到非空闲→清 inflight + `edge/observed` 并放行后续等待；空闲但边未证且 <`edge_cap`（默认 15s，入配置白名单校验）→ 拒发等下轮；超时未观测→`edge/unobserved` 放行（有审计，不静默）。`pending()` 把 inflight 窗计入占用——`agent_status` 滞后窗内到达的消息改走排队路径，即时路径在窗内不再逃逸。
2. **发送点二次重证**：capture 的 baseline 本身必须 `idle/done`（`_baseline.status` 检查，busy/未证→`baseline_busy` 保队首）；capture 之后、prompt 之前重读 `get_agent`+绑定（`status_changed`/`binding_changed` 各归其位），封住 capture→send 翻转窗。
3. **先持久化落定**：`_finish` 返回 bool；`_settle` 先写终态行再回执/出队——未发送条目持久化失败→`settle_failed` 保队重试；已发送条目（`sent=True`）恰好消费一次、持久化失败加 `persist_failed` 审计。超时落定也须行先持久才算 definitve。
4. **capture 清理**：`armed` 标志 + finally 统一 cancel——prompt 失败、`arm` 返 False/抛异常均不留活 capture。

### 派发验证（全部真实 pane w1V:p9 实测）

**正向 F-009**（sleep 25 → 紧接 `只回复 PONG-L`）：
```
prompt1 → submitted + watch armed；+1.5s status=working
prompt2 → queued（pending=True，无 prompt 调用）
pane done 后 → audit edge/observed → closed/submitted → [PROMPT→w1V:p9] '只回复 PONG-L'
        → watch armed → attempted/body → [SEND] '主控 Pane w1V:p9\nPONG-L' → closed/sent
```
status_log：`working→done`（t≈121.0）与 prompt 调用（t=120.7）吻合；返工后正向路径无回退。

**三连发逐轮串行**（忙时连发 L1/L2/L3，全 `queued`）：
- audit 流 `edge/observed → closed/submitted` ×3——每次提交都先观测到前一轮的 busy 边回到 idle 才放行：L1@184.1→pane working@184.9→done→L2@189.1→working@189.7→L3@192.2→done@196.3→**PONG-L3 回传**。
- 与 rework1 前相比：上轮 K1→K2 间隔 ~3s 仅靠"轮询恰见 idle"；本轮每次提交之间有完整的 working→done 观测环（audit 可证），不再是快照巧合。
- L1/L2 回复被后续 capture 的 `cancel_current` 正当取消，仅 L3 回传——既有最新回复语义不变。

### 独立重跑（0403752 下）

| 项 | 结果 |
|---|---|
| `unittest discover -s tests` | **329 tests OK**（48.3s；322+7 新用例：`stale_idle_window_never_drains`、`late_message_queues_while_submit_edge_unproven`、`working_baseline_keeps_head`、`status_flip_after_capture_keeps_head`、`timeout_settle_stays_queued_until_persisted`、`prompt_failure/arm_failure` capture 清理） |
| `compileall` / `git diff --check`（工作区+`29134f6..0403752`） / `git status` | 全干净 |

### 增量结论

**approved**。rework1 把「pane 空闲」从轮询快照升级成可证明的状态机：每次发送武装边门，下一发必须观测到完整的 busy→idle 环（滞后窗被 `pending()` 一并封死——我首轮报告的残差①被消除）；capture 基线与发送点双重重证杜绝 busy 帧 baseline；落定先持久化使超时/失败回执不再先于事实。审计码集扩展（`edge/observed|unobserved`、`baseline_busy`、`status_changed`、`settle_failed`、`persist_failed`）仍全固定码无内容。残差（不阻断）：边门最坏为排队头加 ≤`edge_cap`(15s) 延迟——方向保守；`edge/unobserved` 有审计可追溯。P 级问题：无。
