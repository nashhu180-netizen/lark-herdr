# round2-B6 — ISSUE28 fresh 复核（与审核并行）

复核者：devin swe-2-max fresh 复核 worker（本 pane w1V:p5；未参与实施；被采样帧的宿主 pane）
复核对象：worktree `/home/nash/work/lark-herdr-o28`，分支 `fix/devin-working-frame-unparsed`（PR #29），`git diff main(1088e8b=PR#25 合入点)...HEAD(db36cde)`，commits `8a9b476` / `db36cde`
复核边界：o28 只读（变异验证为临时施加+还原）；仅写本报告于 o24 reviews/；不 commit、不替主控验收。

## 结论先行

**approved**。

`_DEVIN_STATUS` 尾部白名单从 1 个扩为 4 个精确字面量，全部经由 `fullmatch` + `\S(?:.*?\S)?\s+` 前缀锚定，无通配；新增 4 个 fixture 经逐行交叉验证为 verbatim 采样；3 个新测试钉住解析/arm/失败关闭三条性质；独立重跑 305 tests 全绿、compileall 与三级 diff-check 干净；变异验证（alt+t 尾损坏）精确转红 2 处并已还原。

## 一、diff 全貌与逐处复核

| 文件 | 变化 | 复核 |
|---|---|---|
| `output.py` L69-75 | `_DEVIN_STATUS` 单尾 → `(?:Context…\|Press alt\+t…\|See usage and cost: /session-stats\|Press Ctrl\+L to clear the screen, Ctrl\+Shift\+L to redraw)` | 全部为精确字面量；`\+` 转义正确；`fullmatch`（L104）+ 前置 `\s+` 边界锚定；裸尾行（无模型名前缀）不匹配→fail-closed；原 Context 尾保持第一备选，无回归 |
| `tests/fixtures/devin_working_live_{alt_hint,context,running_tools}.txt` | 3×45 行 working 实帧 | verbatim 验证通过（见 §二）；status 分别覆盖 alt+t 尾与 Context 尾 |
| `tests/fixtures/devin_idle_done_live_context.txt` | 44 行 idle 实帧 | 同上；供 splice 测试的干净底座 |
| `test_output.py` +61 | 3 个新测试 | 见 §三 |

红线核验：仅解析器白名单扩充 + 测试/fixture；无 sleep、按键、提示重放、通用 diff、原始直转、守卫放宽。`_devin_frame` 其余 chrome 检查（rule/input/spinner/activity）未动，白名单外尾部仍 `frame_unparsed` fail-closed。

## 二、fixture verbatim 性验证（本人 pane 一手交叉核对）

4 个 fixture 声明为 w1V:p5 在 `sleep 30` 窗口的采样。本人即该 pane 内运行的 agent，可将 fixture 内容与实际执行记录逐点对照：

| 校验点 | fixture 内容 | 实际记录 | 结论 |
|---|---|---|---|
| 提示 echo | `❭ 请只执行 sleep 30 这一条命令，不要做别的` | 用户原话逐字一致 | ✓ |
| 后台 shell id | `⏺ Read shell 9ae6de` / `Session: 9ae6de` | 本次 `sleep 30` 实际后台 shell_id = `9ae6de` | ✓ |
| 超时参数 | `Timeout: 45s`（sleep 命令）/ `Timeout: 30s`（Read shell） | exec timeout=45000 / get_output timeout=30000 | ✓ |
| B4 报告正文 | "B4 复核完成…approved…`e5fe6442…af03`/`50df978d…2af0`/`ba5c10cc…2e0a`…5 处红…2 处红" | 与本 pane 实际输出逐字一致（含三个 sha256 与失败计数） | ✓ |
| todo 列表 | ✓1.~✓6. 六项逐字 | 与本 pane todo_write 内容一致 | ✓ |
| 退出结果 | `Exited with code 0` / `sleep 30 已执行完毕（exit 0）。` | 与实际一致 | ✓ |
| TUI 折行 | `m\nain...HEAD`、`时\n点` 等词中折断 | 终端宽度硬折行的真实形态 | ✓ |

结论：fixture 为真实 verbatim 采样，非合成。敏感信息扫描：无 `/home/`、`/Users/`、用户名、凭据、邮箱（grep 仅命中 git ref 记法 `main@04f1580` 的 `@`）；仅含本 pane 自身报告文本与相对路径——测试注释"no user/home paths or credentials"属实。

附注：fixture 中 `Did you know` 提示为 Ctrl+L 文案，与 `_DEVIN_DID_YOU_KNOW_TIP`（/bug 文案）不匹配 → 不被剥离、并入 ASSISTANT 块文本——属既有解析语义，本 PR 未改，无影响。

## 三、测试复核

- `test_real_sampled_devin_working_frames_parse`：3 个 working fixture 逐一 `_frame`→非 None + `status=="working"` + `real`（real 标志仅 `_devin_frame` 路径置位，证明走了真帧解析而非 synthetic 分支）。
- `test_devin_status_bar_tail_variants_parse`：对 `/session-stats`、`Ctrl+L` 两尾明确标注 **splice**（复用真实 idle 帧换尾行）而非 live capture——沿袭 audit-B1 的证据边界口径，诚实；`unrecognized-still-fails-closed` subTest 钉住未知尾 → None。
- `test_working_capture_arms_on_real_alt_hint_frame`：alt+t 帧作 working 基线 → 首读即 arm，`calls==[get,read]` 钉住"有效帧不消耗重试"。

## 四、独立重跑（o28 自身 .venv，复核 worker 自跑）

```text
.venv/bin/python -c "import feishu_herdr_bridge;print(...__file__)"
→ /home/nash/work/lark-herdr-o28/feishu_herdr_bridge/__init__.py  （指向 o28，Python 3.12）

env -u FEISHU_APP_ID -u FEISHU_APP_SECRET .venv/bin/python -m unittest discover -s tests
→ Ran 305 tests in 44.743s — OK   （= 302 基线 + 3 新增；同样的 2×DeprecationWarning + 末尾 ResourceWarning，非失败）

.venv/bin/python -m compileall -q feishu_herdr_bridge tests → exit 0 无输出
git diff --check               → exit 0（工作区）
git diff --check main...HEAD   → exit 0（PR #29 全范围，merge-base=1088e8b）

聚焦：test_real_sampled_devin_working_frames_parse /
      test_devin_status_bar_tail_variants_parse /
      test_working_capture_arms_on_real_alt_hint_frame → 3 tests OK
```

## 五、变异点验证（施加 → 红 → 还原）

基线 `sha256(o28:feishu_herdr_bridge/output.py)` = `76ece9f7b3ab536fab71dcd5f0194810dbca54d66d90d34d77fdbb81d5a11bd0`

| # | 变异 | 施加态 hash | 结果 | 还原态 hash |
|---|------|------------|------|------------|
| M1 | `Press alt\+t to cycle thinking levels` → `…levelz`（尾部白名单失效等价于删除该尾） | `1a1375486471d4627a878e4e991eb22640886030650673bce49d95a8af523faa` | **FAILED (failures=2)**：`test_real_sampled_devin_working_frames_parse[devin_working_live_alt_hint.txt]`、`test_working_capture_arms_on_real_alt_hint_frame`；其余尾部/fixture subTest 保持绿（覆盖精确） | `76ece9f7…1bd0`（=基线，`git status` 空，聚焦测试复绿） |

## 六、遗留说明（不阻断，供主控知情）

1. **白名单覆盖上限**：Devin 状态栏尾部为循环轮换文案，当前白名单=4 个已观测字面量；若出现第 5 种尾，将再度 `frame_unparsed`——但 B2 拒绝码日志正是 Issue #28 得以定位的机制，同类问题下次可直接从日志读出，属 fail-closed 设计内的已知边界。
2. **裸尾行不解析**：若 Devin 某形态下状态栏只显示尾部而无模型名前缀（无证据表明存在），会因缺少 `\S…\s+` 前缀被拒——保守方向，可接受。
3. 本复核与审核 worker 并行，未读其报告；结论独立形成。

## 总结论

**approved**。实现最小、白名单精确、fixture provenance 经宿主 pane 一手验证、测试覆盖解析/arm/fail-closed 三面。合入与部署决定归主控/用户。
