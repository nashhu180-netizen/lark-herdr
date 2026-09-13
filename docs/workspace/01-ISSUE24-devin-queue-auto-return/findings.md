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
