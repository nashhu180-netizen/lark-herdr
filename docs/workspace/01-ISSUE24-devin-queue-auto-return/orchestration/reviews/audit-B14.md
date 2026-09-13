# audit-B14 — PR #35 / 29134f6

审核对象：`/home/nash/work/lark-herdr-o34`，分支 `fix/send-wait-idle`，范围 `main=b19780e..29134f6`。依据 `orchestration/decisions.md` D-005 与 `docs/auto-pane-output-design.md` §4.2；只读核代码与运行测试，未改代码、未合并、未部署。

**结论：rework。** 单条消息的 working→idle 路径、正常超时、绑定变化和基础 FIFO 顺序均有测试且通过；但同 pane 队列会在 HerdR 状态滞后的同一个 idle 窗口连续提交多条消息，发送前状态翻转也不会阻止提交，直接打穿 D-005“等 pane 空闲后再发送并截取空闲基线”的核心语义。另有超时终态写库失败仍出 definitive 回执、prompt 失败后 capture 残留两项边界问题。

## 1. 修改范围与 §4.2

范围 diff 只有：

```text
M feishu_herdr_bridge/__main__.py
M feishu_herdr_bridge/core.py
A tests/test_queue.py
```

`feishu_herdr_bridge/output.py` 与 `docs/auto-pane-output-design.md` 在两端的 Git blob 分别完全一致：

```text
output.py: 91f065c0de3ec43bcf105745a4e91391515c09ff
design:    f7eae238a2ddec6ae94afd1001ccf9e9e7d82c11
```

`git diff --exit-code b19780e..29134f6 -- feishu_herdr_bridge/output.py docs/auto-pane-output-design.md` 为 exit 0。因此 parser、§4.2 的行级增量/提示锚点/正文提取规则均为**零改动**；实现按 D-005 改走发送侧。没有按键、提示重放、通用 diff 或原始画面转发。

## 2. 正常队列路径

以下正常路径实现与测试成立：

- group prompt 到达时，目标状态非 `idle/done` 或该 pane 已有 pending，就在 `core.py:618-624` 入 per-pane deque；request 行保持 `processing`，不调用 prompt。
- `idle/done` 且无 pending 走原即时 capture→prompt→arm 路径。
- queue poll 在共享 `core._lock` 内重读 agent 与 binding（`core.py:181-253`），避免桥内新消息、换绑与本次 dequeue 交错。
- 每 pane 只检查 deque head；正常超时会写 `failed/queue_timeout`、发一次“pane 持续忙，未发送”回执并移除队首。
- 等待期间 busy poll 只做 `get_agent`，不 capture；实际 dequeue 时才执行一次 `cancel_current→capture→prompt→arm`。新增测试对此顺序有断言。

不过这些测试只证明“人工把 fake 状态按期切换”的顺序，不证明真实运行循环在状态滞后时仍一次只发一条，见 A-B14-001。

## 3. 审核发现

### A-B14-001 — P1：同一 idle 状态窗口会连续 drain 同 pane 队列，已知状态滞后下可重入 Devin 原生队列

位置：`feishu_herdr_bridge/core.py:255-284,618-628`；`tests/test_queue.py:157-185`；`findings.md` F-009 第 135 行。

`SendQueue.run()` 在一次 `poll()` 消费成功后立即 `continue`，不等下一 interval，也没有记录“该 pane 已提交一条、必须先观察到 busy 再等下一次 idle”的状态。首条 `_settle()` 又在 prompt 返回后立刻从 deque 移除。因此 HerdR `agent get` 仍报告旧 `idle` 时：

1. 下一条排队消息会在紧接着的 poll 中立即提交；
2. 若队列只剩一条，新的飞书消息在首条提交后到达，会因 `pending=False + status=idle` 绕过队列走即时路径。

这不是纯理论窗口。F-009 自己记录 `agent_status` 在本桥提交后约滞后 0.3 秒，但将其定性为“非排队漏洞”；代码没有任何 pane 级 in-flight/busy-edge 闸来吸收该已知窗口。现有 `test_same_pane_fifo_waits_for_prior_send` 在第一次 poll 后由测试代码手工 `set_status("working")`，恰好替实现补上了生产中并不可靠的状态跃迁；`test_late_message_never_jumps...` 也只证明 deque 中已有 pending 时不插队。

审核只读探针保持 fake 状态为 idle，得到：

```text
enqueue queued queued
poll1 True ['first'] pending True
poll2 True ['first', 'second'] pending False

queued-one queued
late-arrival submitted ['queued-first', 'arrived-in-stale-idle']
```

顺序虽然仍是 FIFO，但“同 pane FIFO”所需的逐轮串行不存在；第二条会回到 Devin 原生队列/并发输入风险，正是 D-005 要移出屏幕解析路径的问题。

### A-B14-002 — P1：idle 检查与 prompt 之间的状态翻转未 fail-closed，baseline 不保证为空闲画面

位置：`feishu_herdr_bridge/core.py:181-251`；`feishu_herdr_bridge/output.py:460-508`（未改，但 capture 允许 working baseline）。

`_submit()` 在共享 core lock 内只于 `core.py:193-203` 读取一次 pane status；这个锁只能阻止桥内前台操作，不能锁住 Devin/TUI 或外部终端。随后 `output.capture()` 自己会再次 get/read，但其合同允许 `idle/done/working` 三种 baseline；返回 capture 后 `_submit()` 不核对 baseline 状态，也不再 get，直接 `prompt()`。

审核在 capture 回调内把 pane 从 idle 翻为 working，仍观察到：

```text
flip-poll True
status-at-end working
submitted ['flip-during-capture']
calls [('cancel_current', ...), ('capture', ...), ('prompt', ...), ('arm', ...)]
```

所以“状态在发送瞬间翻转”会产生 working baseline 并照常提交，违反 D-005“等 pane 空闲后再发送并截取基线，使基线始终为空闲画面”。A-B14-001 的状态滞后是该 TOCTOU 的常见实例。

关闭要求：同 pane 提交后建立明确的 in-flight/状态边沿闸，至少观察到该提交进入 non-idle 并再次回到 idle/done 才允许下一条；capture 若看到 working 或无法证明发送点仍 idle，应取消本次 capture、保留队首等待，而不是提交。具体机制由施工方设计，但不得依赖测试手工切状态。

### A-B14-003 — P1：超时终态写库失败仍发送确定性回执并从队列删除

位置：`feishu_herdr_bridge/core.py:160-179,269-274`。

正常 SQLite 路径下 `test_queue_timeout_notifies_and_records_failure` 能证明 `failed/queue_timeout` 落库。异常路径中 `_finish()` 却吞掉所有存储错误，`_settle()` 随后仍记录 `closed/queue_timeout`、发送“未发送”确定性回执并 pop 队首。审核注入一次 `store.finish` 失败后得到：

```text
poll True pending False notices 1
row processing ''
audit_tail ('m-2', 1, 'closed', 'queue_timeout')
```

即用户与日志都看到已关闭，SQLite 却仍是 `processing`；同 message 重投只会得到 duplicate processing，直到进程重启 recovery 才变 `unknown/interrupted`。timeout poll 又没有持有 core lock，恰可与前台 SQLite 写并发，不能把写失败视为不可达。

关闭要求：只有终态成功持久化后才能发 definitive timeout 回执、记录 `closed` 并删除 entry；落库失败须保留可重试 settlement 或明确进入 unknown 路径，不能制造“回执已结束、账本未结束”的分裂状态。

### A-B14-004 — P2：prompt 失败或 arm 抛错后，deferred capture 没有统一清理

位置：`feishu_herdr_bridge/core.py:213-250`。

deferred capture 存在局部变量，不进入 `BridgeCore.execute()` 原有的 `_output_capture` finally。绑定二次核对失败会显式 `output.cancel(capture)`，但 `_prompt()` 的 `HerdrError`/通用异常分支均直接 settle 返回；`output.arm(capture)` 抛错也只吞异常。审核让 prompt 在 capture 后失败，调用序列为：

```text
[('cancel_current', 'chat-a'), ('capture', 'prompt-will-fail'),
 ('prompt', 'prompt-will-fail')]
row unknown invalid_output
```

没有 `cancel(capture)`。真实 OutputObserver 中该 observation 保持 `captured` 并占用 active/chat 槽，直到未来消息或停机才清理。它不会自行进入 watching，因此未发现重复发送；但这是等待路径新引入的生命周期残留，应统一 finally 清理未成功 arm 的 capture。

### A-B14-005 — P2：F-009 支持单条真实 pane 路径，但不是可独立复核的完整 receipt

位置：`findings.md:124-137`；临时文件 `/tmp/b14_now.txt`。

F-009 写明使用真实 `BridgeCore + HerdrAdapter + SendQueue + connect_output`，但飞书出站是 stub；所以记录中的 `[SEND→chat-b14]` 证明候选进入桩，不证明真实 Feishu POST。现存 `/tmp/b14_now.txt`（SHA-256 `ab4664b388e5483dc2c2d2ac63df32bd6d5073c075d6eabcb6316f9c32bd6333c`）可独立看到两轮 sleep、两次 `❭ 只回复 PONG-J` 和两次 `PONG-J`，支持 pane 侧真实执行；但文件没有排队回执、audit 时间序列或 SQLite 两条 terminal row，无法独立重建 F-009 第 130-132 行的 `queued→closed/submitted→armed→sent` 链。

因此 F-009 可作为**真实 pane + stub 出站的正向 smoke evidence**，不能作为并发/FIFO 或真实飞书 receipt 的完整验收。其唯一 queued item 也没有覆盖 A-B14-001 的多条积压与 0.3 秒 stale-idle 窗口。

## 4. 旧 watch、capture 与回执/日志风格

- enqueue 和 busy poll 不调用 capture，未发现重复 capture；dequeue 成功路径只 capture 一次。
- queued 等待期间旧 prompt 的 watch 保持有效，直到新 prompt 真正 dequeue 时 `cancel_current`。因此旧轮可能在 queue 线程前抢到共享锁并先返回自己的正文，也可能像 F-009 那样被 dequeue 取消；两者都不会把旧正文当作新 prompt 正文，但可见回执顺序存在调度差异。D-005 未规定 queued receipt 即取消旧轮，暂不单列阻断项。
- 回执文案明确区分“已排队”“pane 持续忙，未发送”“绑定/目标变化未发送”；均不含提示或异常文本。
- audit 形态 `send-queue message=<id> revision=<n> event=<fixed> reason=<snake_case>` 与既有机器码风格一致；HerdR error code 由 adapter 的固定集合产生，未发现画面/提示/异常 repr 泄漏。`enqueued/busy`、`closed/submitted|queue_timeout`、`poll/get_failed`、`lost/restart` 均为固定短码。

## 5. 测试与静态验证

```text
.venv/bin/python -m unittest tests.test_queue -v
Ran 12 tests in 3.151s
OK

env -u FEISHU_APP_ID -u FEISHU_APP_SECRET \
  .venv/bin/python -m unittest discover -s tests
Ran 322 tests in 51.940s
OK

.venv/bin/python -m compileall -q feishu_herdr_bridge tests
PASS（无输出）

git diff --check b19780e..29134f6
PASS（无输出）
```

全量计数与 `state.json`/F-009 声称的 322 一致。输出含既有两条依赖 DeprecationWarning、静态非 live WARNING/INFO 和末尾 unclosed event-loop ResourceWarning，exit 0。审核结束时 o34 HEAD=`29134f6bbe4dbaeb1e06006737aa235f3fa5d906`，worktree clean。

## 发现表

| ID | 级别 | 结论 | 状态 |
|---|---|---|---|
| A-B14-001 | P1 | 状态滞后时同一 idle 窗口连续 drain，多条消息未按 pane 工作轮次串行 | open，阻断 approved |
| A-B14-002 | P1 | capture/发送间状态翻转仍提交，不能保证 idle baseline | open，阻断 approved |
| A-B14-003 | P1 | timeout 落库失败仍 definitive 回执、closed audit 并删除队首 | open，阻断 approved |
| A-B14-004 | P2 | prompt/arm 异常后 deferred capture 未统一取消 | open |
| A-B14-005 | P2 | F-009 仅真实 pane + stub 出站，缺可复核 audit/DB/真实飞书 receipt | evidence gap |

**最终结论：rework。** §4.2 与 output.py 确为零改动，单条正常流程和 322 项回归均绿；但排队器尚未建立跨状态滞后窗口的 pane 级串行保证，且两个 fail-closed/持久化边界未收紧。不改代码、不合并、不部署。

---

## 增量审核 0403752

审核对象：`/home/nash/work/lark-herdr-o34`，增量范围 `29134f6bbe4dbaeb1e06006737aa235f3fa5d906..040375286e74ba2638292e67d722f5c187a00450`。本节只复核 A-B14-001…005 的关闭情况；未改代码、未合并、未部署。

**结论：approved。** 五条原发现均已按关闭要求落实，未发现新增 P0–P3。D-005 的发送侧方案仍未修改 `output.py` 或 §4.2 解析合同；增量 diff 仅为 `feishu_herdr_bridge/__main__.py`、`feishu_herdr_bridge/core.py`、`tests/test_queue.py`。

### A-B14-001 — closed：pane 级边沿闸不再依赖测试手工切 busy，且有有界兜底

- `core.py:138-154,215-230,693-704` 将 `_inflight` 纳入 `pending()`；即时或延迟 prompt 成功尝试后都调用 `mark_submitted()`。下一条 group prompt 因此不能利用 HerdR 的旧 `idle` 状态绕过队列。
- `_gate()` 在观察到 non-idle 时只撤销闸、不发送；后续再次读到 `idle/done` 才发送。`test_stale_idle_window_never_drains_the_queue` 在首条发送后始终保持 fake 状态为 idle，没有由测试手工补 working 边沿，仍证明上限内第二条不发送；`test_late_message_queues_while_submit_edge_is_unproven` 覆盖 stale-idle 窗口中新到消息不能走即时路径。
- 未观察到 busy 边沿时不会无限卡死：`queue_edge_cap` 默认 15 秒，配置只接受有限正数；到期仍以当次 `idle/done` 读数放行并记固定审计码 `edge/unobserved`。测试用 5 秒上限钉住“上限前拒绝、上限后放行”。这是有审计的有界状态滞后兜底，不是无条件跳过当前状态检查。
- 只读探针保持 pane 为 stale idle，实测：`poll1=True` 仅发 first；紧接 `poll2=False`；cap 前仍 `False`；cap 后才 `True` 发 second，audit 尾为 `edge/unobserved → closed/submitted`。

### A-B14-002 — closed：baseline 与发送点均重新证明 idle，失败路径取消 capture 并保留队首

- `_submit()` 在首次 idle/gate/binding 校验后 capture；若 `capture._baseline.status` 不是精确 `idle/done`，记录 `poll/baseline_busy` 并 fail-closed。capture 后、prompt 前再次 `_get()`；workspace 不匹配终结，状态变为 non-idle 则记录 `poll/status_changed`、保留队首，随后 finally 取消本次 capture；binding 也再次核对。
- 只读探针在 `capture()` 回调内把真实 fake 状态从 idle 翻为 working，实测 `poll=False`、submitted 为空、队首仍 pending，调用序列为 `cancel_current,capture,cancel`，audit 为 `poll/status_changed`。未依赖预排的状态脚本来制造结论。
- 仍存在外部 pane 在最后一次 get 与 prompt 系统调用之间翻转的不可原子化窄窗，但桥已在发送点前完成可用边界的最后一次重读；未见放松 D-005 或 §4.2。

### A-B14-003 — closed：未发送的 timeout 必须先持久化，才回执、closed 与出队

- `_finish()` 现在返回持久化成败；`_settle(..., sent=False)` 在失败时只记 `poll/settle_failed` 并返回 False，不发 definitive receipt、不记 closed、不 pop。下一轮 poll 可重试。
- 注入一次 `store.finish` 失败，实测 `poll=False`、pending=True、notices=0、SQLite 行仍 `processing`；恢复后下一次 `poll=True`，才变为 `failed/queue_timeout`、发一次回执并出队。
- 对已经尝试过 prompt 的 `sent=True` 分支仍坚持“不重放”，即便账本失败也消费一次并记 `poll/persist_failed`；这与发送是否发生不确定时禁止重放的原合同一致，不构成 timeout 终态顺序回退。

### A-B14-004 — closed：deferred capture 统一 finally 清理

- `_submit()` 以 `armed=False` 管理 observation；除 `output.arm()` 明确返回 truthy 外，所有返回/异常路径都会在 finally 调用 `output.cancel(capture)`，且清理异常不会改变 prompt 结果。
- 回归分别覆盖 prompt 抛 `HerdrError` 与 `arm()` 抛异常：前者调用序列结束于 `prompt,cancel`，后者结束于 `arm,cancel`；baseline busy、发送点翻转与二次 get/binding 失败也复用同一 finally，而非分散遗漏。

### A-B14-005 — closed：F-009a receipt 可独立重建 audit 与账本链，证据边界如实保留

- `findings.md:139-176` 已补 F-009a：列出可复跑命令、receipt 脚本 SHA-256、真实组件/桩边界、逐字输出、audit 顺序、SQLite requests 与 binding。它明确声明 fake pane 与 stub 飞书出站不证明真实 Feishu POST；真实 pane 侧仍引用 F-009 的 `w1V:p7` smoke，没有把合成边界冒充 live。
- 独立哈希核对与记录一致：脚本 `a236e710…`，stdout `f7535d07…`，`bridge.log` `ce9c0fe1…`，SQLite `ca9df381…`，fake calls `e3680f11…`。
- 只读打开 receipt SQLite，得到 `m-1 done/bound`、`m-2 done/submitted`、`m-3 done/submitted`；日志顺序为 queued prompt 的 `enqueued/busy → edge/observed → closed/submitted → output armed → attempted/body → closed/sent`。结合 F-009 的真实 pane 正向 smoke，已补齐原报告所缺的可复核 audit/DB/完整链，同时没有夸大真实飞书出站覆盖。

### 测试与静态验证

```text
.venv/bin/python -m unittest tests.test_queue -v
Ran 19 tests in 7.443s
OK

env -u FEISHU_APP_ID -u FEISHU_APP_SECRET \
  .venv/bin/python -m unittest discover -s tests
Ran 329 tests in 51.230s
OK

.venv/bin/python -m compileall -q feishu_herdr_bridge tests
PASS（无输出）

git diff --check 29134f6..0403752
PASS（无输出）

git diff --check b19780e..0403752
PASS（无输出）
```

全量输出仍含既有 lark SDK DeprecationWarning、静态非 live WARNING/INFO 与末尾 unclosed event-loop ResourceWarning，exit 0。测试后 o34 HEAD 精确为 `040375286e74ba2638292e67d722f5c187a00450`，worktree clean。

### 增量发现表

| ID | 原级别 | 增量结论 | 状态 |
|---|---|---|---|
| A-B14-001 | P1 | pane 级 in-flight 边沿闸覆盖 stale idle 与 late arrival；cap 前拒绝、cap 后有审计放行 | closed |
| A-B14-002 | P1 | baseline busy 与 capture 后状态翻转均不 prompt、保留队首并取消 capture | closed |
| A-B14-003 | P1 | timeout 持久化失败时不回执、不 closed、不出队；可重试落定 | closed |
| A-B14-004 | P2 | 所有未成功 armed 的 deferred capture 统一 finally 取消 | closed |
| A-B14-005 | P2 | F-009a 补齐可复核 audit/SQLite receipt，并如实限定 fake/stub 边界 | closed |
| 新发现 | — | 无新增 P0/P1/P2/P3 | none |

**增量最终结论：approved。** `0403752` 关闭 A-B14-001…005；19 项队列测试、329 项全量回归、compileall、两档 diff check 与三组独立故障探针均为 GREEN。不改代码、不合并、不部署。
