# audit-B6 — ISSUE24 / Issue #28 / PR #29 小审

审核者：gpt-5.6-sol 独立审核 worker（未参与实施）

审核对象：worktree `/home/nash/work/lark-herdr-o28`，分支 `fix/devin-working-frame-unparsed`，`main@1088e8b...db36cde`；提交 `8a9b476`、`db36cde`。审核期间仅写本报告，未修改 o28 的代码、测试、fixture 或账本。

## 结论先行

**changes-requested**。

生产改动的核心方向成立：三个新增状态栏尾部都是固定字面量分支，由 `fullmatch` 严格限定在 Devin chrome 的逻辑末行；未知尾部仍返回 `None`。用 PR 前的 `_DEVIN_STATUS` 对真实 alt-hint fixture 运行，`capture()` 得到 `None / frame_unparsed`，当前实现则首读成功并 arm，红→绿确实复现并关闭了 B5 的 live `frame_unparsed` 形态。

阻断 approved 的不是 parser 安全性，而是证据与结论准确性：三个公开 working fixture 虽未发现凭据或用户/绝对路径，却携带大量不必要的仓库相对路径、提交 SHA 与内部审核正文，不满足本单“无路径”及设计合同的公开 fixture 脱敏/最小化要求；F-003 又把一次 idle/done 未识别帧的影响夸大成“最终正文永远发不出”，忽略现有 5 次 transient 容忍与恢复路径。此外，“四条全部精确字面、无通配”不符合代码事实：三个新增尾部是字面量，既有 Context 尾部仍是受限数字正则。

## 1. `_DEVIN_STATUS`、末行位置与 fail-closed

`feishu_herdr_bridge/output.py:71-75` 的三个新增分支分别为：

```text
Press alt+t to cycle thinking levels
See usage and cost: /session-stats
Press Ctrl+L to clear the screen, Ctrl+Shift+L to redraw
```

它们在正则中只对 `+` 做必要转义，没有 `.*`、可选片段或兜底分支。独立探针确认三个完整尾部均匹配，而在 alt-hint 后追加 ` EXTRA` 不匹配。

既有 Context 分支是：

```text
Context: [0-9.]+[kKmM]? / [0-9.]+[kKmM]? tokens \([0-9]+%\)
```

因此它是边界明确的动态数字语法，不是“精确字面”。模型名前缀中也保留了 PR 前已有的 `.*?`。这没有给尾部增加宽松兜底，但 F-003/代码注释所谓“四条全部精确字面、无通配”并不准确，见 A-B6-003。

`_devin_frame()` 在可选且已知的 activity footer 被剥离后，对 `lines[chrome_end - 1]` 调用 `_DEVIN_STATUS.fullmatch()`；因此只接受逻辑 chrome 末行，不会在正文或任意中间行命中。规则/input/status 的相对位置检查未改。新增测试也明确断言 `Some other tail` 返回 `None`。PASS。

相对 main 的生产 diff 只扩充 `_DEVIN_STATUS`，没有改 capture、tick、守卫或读取次数；未引入 sleep、按键、提示重放、通用 diff、原始转发或守卫放宽。PASS。

## 2. 三个真实 working fixture

三个文件均保留了终端自然换行、spinner 时刻和 chrome 相对位置，且 context/running-tools/alt-hint 三帧形成一致的同一 working turn 序列。提交说明及测试注释将其登记为 `w1V:p5` 的 `sleep 30` 采样；没有发现人为改造 parser 所需结构以“恰好过测”的迹象。

但仓库中没有独立原始采样或采样时 SHA/receipt 可供逐字节比对，所以审核只能确认“内容形态一致且 provenance 有文字声明”，不能独立证明 verbatim。三个 fixture 的 SHA-256 为：

```text
31b1492ab1122b8cf58ff096c50ab32e3029fb71dfeaccb33fc57959a8a3fdb8  devin_working_live_alt_hint.txt
4314dbc3fe7289197e6ddc728d0bbe3315be79c1f4ad0aa86f55eea071e1d517  devin_working_live_context.txt
67ad9bf58fcd0b6540ba07a045d00d0c6770394e1c9ede517efd144896405e93  devin_working_live_running_tools.txt
```

敏感项扫描未发现 `/home/...`、`/Users/...`、token、密码、密钥或 bearer 等凭据；这一部分 PASS。可是三帧都包含 `docs/workspace/01-ISSUE24-.../orchestration/reviews/round2.md`、`output.py`、`test_output.py` 等路径，以及多枚提交 SHA 和完整内部审核摘要。它们不是凭据，也不是用户绝对路径，但仍是路径与无关内部内容，不符合“无路径”的字面门槛及设计 §7“公开 fixture 使用合成/脱敏数据”的最小暴露要求，见 A-B6-001。

## 3. RED → GREEN 复现

独立探针用当前真实 `devin_working_live_alt_hint.txt`，只把 `_DEVIN_STATUS` 暂时替换成 main 的旧正则；未改文件。结果：

```text
fixture= devin_working_live_alt_hint.txt
before_main_parser= None
after_pr29_status= working
after_pr29_real= True
```

进一步从 `Rig.capture()` 走真实入口：

```text
RED capture= None
RED calls= ['get', 'read', 'read']
RED declined= ['frame_unparsed']
GREEN capture= True
GREEN calls= ['get', 'read']
```

这准确覆盖 B5 记录的“devin+working 两次读均因 alt-hint 尾部无法解析而 `frame_unparsed`”路径，而不是仅构造一个 parser 单测。当前实现对同帧首读即建立 capture；PR #25 的两读上限和其他状态/agent 的读取策略未变。PASS（fixture 的公开证据最小化问题另见 A-B6-001）。

## 4. F-003 对 idle/done after 帧的影响面

“影响不只 working baseline”这一层推断成立：idle/done 时 `tick()` 会读取 after frame；若尾部未知，`_frame()` 返回 `None`，本轮无法提取正文。因此为两个已目击 idle/done 尾部增加严格白名单有实际价值。

但“命中未识别尾部后最终正文永远发不出”不成立。`output.py:593-606` 将该情形归为 `Extraction(None, "unrecognized")`；`unrecognized` 属于 transient reason，观察会容忍至多 `TRANSIENT_POLLS = 5` 次，期间下一帧若切回 Context 或其他已识别尾部即可恢复、稳定并发送。`RealDevinExtractionTests.test_transient_unrecognized_frame_keeps_polling_then_sends` 已直接证明恢复；只有连续超过容忍次数、到 deadline，或始终没有有效 after frame，才会停止正文并发固定 NOTICE。见 A-B6-002。

## 5. 独立复跑

在 `/home/nash/work/lark-herdr-o28` 使用其自身 `.venv`：

```text
env -u FEISHU_APP_ID -u FEISHU_APP_SECRET \
  .venv/bin/python -m unittest discover -s tests
Ran 305 tests in 45.122s
OK
```

输出含既有 DeprecationWarning、静态非 live 提示及末尾 ResourceWarning，未造成测试失败。

```text
.venv/bin/python -m compileall -q feishu_herdr_bridge tests
PASS（无输出）

git diff --check
PASS（无输出）

git diff --check main...HEAD
PASS（无输出）
```

定向复跑 parser/capture、未知尾部及 transient 恢复相关 5 项：

```text
Ran 5 tests in 0.051s
OK
```

审核后 o28 worktree 保持 clean，HEAD 为 `db36cdeb4861d74df68a701fcd950facd0f8a967`。

## 审核发现

### A-B6-001 — P1：三个公开 working fixture 未做到“无路径”与最小化脱敏

位置：`tests/fixtures/devin_working_live_alt_hint.txt:1-34`、`devin_working_live_context.txt:1-30`、`devin_working_live_running_tools.txt:1-30`。

fixture 没有凭据、用户路径或绝对路径，但包含仓库相对路径、提交 SHA、分支审计结论与测试计数；这些内容与复现 status-bar parser 无关。测试注释把“无 user/home path”写成已脱敏，弱于本单“无路径”及设计合同对公开 fixture 的要求。

关闭要求：把三个 fixture 缩到仍保持原始连续字面内容、足以覆盖 transcript/tool/spinner/chrome/status 的最小脱敏片段，去掉所有路径、SHA 和无关内部报告正文；记录裁剪规则与裁剪后 SHA。若主控认为“无路径”只指用户/绝对敏感路径，需要人明确裁决并修正文案，审核 worker 不代裁决。

### A-B6-002 — P1：F-003 与测试/提交说明夸大单次 idle/done 未识别帧的后果

位置：o24 `findings.md:77`；o28 `tests/test_output.py:1193-1195`；提交 `db36cde` message。

现有实现会把 `unrecognized` 当 transient 并允许后续有效帧恢复，不是一次命中便“最终正文永远发不出”。该表述会错误扩大风险面并掩盖真正失败条件。

关闭要求：将结论收敛为“本轮无法提取；若连续未识别超过 transient 容忍/直至 deadline，才不能发送正文并走 NOTICE”，并同步测试注释与当前可修改的权威 findings。无需新机制，范围内。

### A-B6-003 — P2：“四条全部精确字面、无通配”与实现不符

位置：o24 `findings.md:77`；o28 `feishu_herdr_bridge/output.py:69-75`。

三个新增提示尾部确为固定字面量；Context 尾部为受限数字正则，模型前缀还保留 PR 前已有的 `.*?`。安全边界仍是 `fullmatch + 末行 + 已知 chrome`，未发现宽松尾部兜底，但不能称四条都是精确字面且完全无通配。

关闭要求：把 findings/代码注释改为“三个新增尾部精确字面；Context 为既有受限数字格式；无新增宽泛兜底”。如果要求 Context 也变为固定完整字符串，会与动态 token 数冲突，应另交人裁决，不应机械实现。

## 发现表与总结论

| ID | 级别 | 结论 | 是否阻断 approved | 关闭方式 |
|---|---|---|---|---|
| A-B6-001 | P1 | fixture 无凭据/绝对路径，但带不必要的仓库路径、SHA 与内部报告正文；verbatim 无独立原件可逐字节复核 | 是 | 最小连续脱敏裁剪并记录 SHA；或由人明确“无路径”的口径 |
| A-B6-002 | P1 | F-003 错把可 transient 恢复的单次未识别 after 帧写成永久不能发送 | 是 | 修正为连续超限/deadline 的条件式结论 |
| A-B6-003 | P2 | 三个新增尾部是字面量，但 Context 是受限数字正则，“四条全部字面、无通配”失实 | 是 | 修正文档/注释；严格静态化 Context 若被要求则需人裁决 |

**总结论：changes-requested。** parser 改动、fail-closed、末行定位、读取/守卫边界和 live alt-hint 红→绿均通过；需先收敛公开 fixture 与 F-003/注释的证据口径。审核 worker 不修改代码、不替主控裁决。
