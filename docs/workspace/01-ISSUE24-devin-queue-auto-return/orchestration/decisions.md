# decisions — ISSUE24 待决条目

> 格式：`## D-00N 标题（状态）` + 背景 / 选项 / 执行者倾向 / 裁决。决策 pane 只裁实现层小决策；方向性（改合同、扩允许路径、部署、降档复核）写「升级用户」。

## D-001 B2 范围：只合 PR #25，还是同时补 capture 拒绝原因审计日志（待裁）

**背景**：B1（findings F-001）证明两次 live 失败均为新提示 `capture()` 返回 None，但离线无法区分三类根因：(i) 撕裂帧（PR #25 覆盖）；(ii) 持续性未识别真实帧形态；(iii) get/read 边界异常或状态/kind 检测异常。区分需要 capture 时刻的现场证据。现有审计日志只在 Observation 创建后才有事件（armed/closed），capture 返回 None 时**没有任何日志**，所以 live 一旦再失败仍然无证据。

**选项**：
- a) B2 只按 PR #25 推进（撕裂假设），部署后若再失败再回来加诊断。
- b) B2 = PR #25 + 在 `capture()` 每个返回 None 的出口发一条审计日志 `output capture=declined reason=<机器码>`（例如 `invalid_origin / not_allowed / same_message / capacity / get_failed / status_<x> / kind_mismatch / read_failed / frame_unparsed / frame_status_<x>`），**只记机器码，不记画面、提示或异常文本**（与现有 `_emit` 同口径，见 output.py L638 的 logger 用法与设计 §"不把正文写日志"）。加对应单测断言 reason 码。
- c) b 之外再把未解析帧的行数/字节数记进日志（仍不记内容）。

**执行者倾向**：未表态；主控倾向 b（成本一个函数级改动，能把下一次 live 失败变成可定位证据；c 的行数信息价值低且逼近内容边界）。

**裁决**：选择 b：B2 在 PR #25 候选补丁基础上补齐 `capture()` 返回 None 的拒绝原因审计日志及对应 reason 码单测；仅使用固定白名单机器码（未知 status/kind 归一为固定码，不拼接外部值），不记画面、提示、异常文本或行数/字节数，日志失败不得改变 capture/提示提交行为，保留现有守卫与读取次数。理由：F-001 尚不能锁定 live 唯一根因，而现有 `_emit` 依赖 Observation、覆盖不到 capture 拒绝出口，补原因码可在既有设计的日志合同与允许文件内缩小后续排障范围。此裁决仅确定 B2 实现范围，不构成 PR 合入、部署或复核降档授权。

## D-002 audit-B1 changes-requested 后是否继续 B2（主控裁：继续，整改并入 B2）

**背景**：audit-B1 给出 P1×2 + P2×1：现有 RED 是 synthetic 假设帧，不能称为 Issue #24 现场复现；三类根因非穷尽、"最可能"无判别证据；11 条出口的几处概率评估过强。审核 worker 把"缺少 live capture 时是否按假设 (i) 进入 B2"交主控裁。

**裁决（主控，2026-09-13）**：继续 B2，理由：D-001 已选 b —— B2 同时补 capture 拒绝原因码日志，这正是为"离线不能定根因"准备的现场取证手段；PR #25 对假设 (i) 的实现正确性审核未否定。audit-B1 三条发现作为 B2 整改项并入：测试改名去掉 `live` 与"忠实现场"措辞、torn 例标 synthetic hypothesis、queue 两例降格为解析器/时序回归；findings F-001 改为"候选集（非穷尽）+ 已排除/有旁证/未知"三栏，撤回"最可能/红→绿已钉住"；progress 补记非失败警告。B2 表述为"验证 PR #25 对假设 (i) 的最小修复 + 取证日志"，不称根因已确认。

## D-003 Devin 原生队列是否由桥接代按 Enter（升级用户·待裁）

**背景**：live 观察（本编排自身的执行 pane 复现）：Devin 在 working 中收到第二条提示时放入原生队列并显示 `Press Enter to send queued messages now`，不按 Enter 不发送。桥接 79c9b5a / a65191d 刻意"保留原生队列不动"，所以即使 PR #25 + B2 让观察成功 arm，第二条消息仍可能一直排队，飞书得不到回复。

**选项**：a) 保持现状（队列留给人按 Enter，飞书回执提示"已排队，需在终端确认"）；b) 观察到精确的 queued 行形态后由桥接代发一次 Enter（需改 auto-pane-output-design 合同与"不发按键"铁律，属方向性）；c) 不排队：Pane working 时桥接拒收第二条文本并回执"Agent 忙，稍后再发"。

**主控倾向**：先在 B5 真实验收中确认队列行为是否真的需要 Enter（本编排看到的是 herdr `agent prompt` 形态，与桥接调用路径相同），再由用户裁 a/b/c。
