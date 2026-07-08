# HybridPatch — Design Findings

> Extracted from the parent workspace's ANCHORPATCH_DESIGN_AND_FINDINGS.md (§212-215). Only the hybridpatch track; v1/v2/C1-C70 history stayed in the parent repo.

## 212. HybridPatch vs FullRewrite track completed through distractor test

> 记录日期：2026-07-03。该段归档新的 `hybridpatch vs fullrewrite` track；C71/C30+ AP-pure 线未继续，v1/v2.1/v2.2 冻结路径未作为新证据修改。未编辑 raw JSONL、checkpoint、frozen task plan、sample/reference、evaluator 或 scoring semantics。

Scope and implementation:

- Canonical method id: `hybridpatch`; aliases and silent FR fallback are forbidden.
- Split artifact: `analysis/hybrid_split.json`, materialized from existing `dev20_20260625` and `holdout_reserve_20260625` as `dev=20`, `val=20`, `test=20`, `unused_reserve=20`.
- New HybridPatch surface: schema/index/prompt/executor/gate/test modules under `anchorpatch/`, top-level `experiment_runner.py` branch, honesty replay in `verify_anchorpatch.py`, hybrid telemetry in `analyze.py`, and run metadata fingerprint coverage for hybrid modules.
- Repair policy: one JSON/schema-only protocol repair is allowed; semantic/gate failures do not repair and carry the unchanged editable workspace forward with failure telemetry.
- CriticalFailure theta was calibrated on dev-mini and frozen at `0.10`.

Replay-passed results:

- Dev-mini out_dir: `anchorpatch/exp_20260703_hybridsmoke`, `minimax-m3`, 10 samples, `10RT`, no distractor. Replay PASS with `200` backward RS reproduced. RS@10: `hybridpatch=0.811`, `fullrewrite=0.728`; CriticalFailure@10: `5/90` vs `6/90`; bounded rewrite share `51.5%`.
- Val out_dir: `anchorpatch/exp_20260703_hybridval`, `minimax-m3`, val20, `10RT`, no distractor. Replay PASS with `400` backward RS reproduced. RS@10: `hybridpatch=0.603`, `fullrewrite=0.474`; CriticalFailure@10: `17/180` vs `18/180`; bounded rewrite share `70.8%`.
- Test out_dir: `anchorpatch/exp_20260703_hybridtest`, `minimax-m3`, test20, `10RT`, distractor enabled. Replay PASS with `400` backward RS reproduced. RS@10: `hybridpatch=0.584`, `fullrewrite=0.418`; paired backward mean delta `+0.181` over `200` pairs; CriticalFailure@10: `17/180` vs `23/180`; bounded rewrite share `76.8%`.

Operational notes:

- The distractor test hit one M3 5-hour quota stop during hybrid batch2 and resumed from runner checkpoints after reset.
- A verifier-only distractor replay bug was fixed after test completion: replay now passes editable-only context into each step, matching runner behavior. This did not change raw results, task plans, samples, evaluator code, prompt/protocol, runner, or executor.
- `analyze.py` report labeling was fixed offline to derive distractor status from rows and to label hybrid-only reports as `HybridPatch vs FullRewrite`.

Interpretation:

- The frozen track completed all requested gates with replay-passed dev-mini, val, and distractor test metrics.
- The evidence is for the declared hybrid method, not AP-pure capability. High bounded-rewrite share is a required identity-dilution disclosure and remains the main caveat.
- The test result is favorable to `hybridpatch` on this split, but should be reported with route share, no-op/effective-modification audit, repair rate, gate failure rate, and the verifier maintenance note.

## 213. HybridPatch test attribution: win mechanism, self-inflicted transport bug, honesty risks

> 记录日期：2026-07-04。本段是对 `anchorpatch/exp_20260703_hybridtest`（只读归档，未编辑 raw JSONL/checkpoint/task_plan/sample/reference/evaluator/scoring）的逐样本归因，两条独立子代理调查、主 Agent 复核证据链。事实性描述，未定处如实标注。

### 赢的机制（RS@10 `0.584` vs `0.418`，配对 backward Δ `+0.181`，ECR 条件化 169 对仍 `+0.135`）

优势主要来自 FullRewrite 的灾难放大机制被有界写入规避，而非补丁路径的正贡献：

- **FR max_tokens 截断 → 文件丢失/改名/工作区清空（不可逆）**：`translation2` RT3 forward `finish_reason=max_tokens`（completion 20000），围栏未闭合，`parse_context_string` 把目标 `ja.po` 解析成名为 `po` 的文件、内容截断到 3 条目；之后每轮 `ja.po` 恒 ~900B/3 条目（`ref_entry_count=48`），RS@10 归 0。`weather1` RT3 forward 同样 max_tokens，围栏未闭合导致解析为空、整个可编辑工作区被清空（`docs/fullrewrite/weather1/rt03_fwd_metar_table_imperial/` 只剩 `_step.json`），模型随后在 raw 里明确表示"看不到 bulletin.txt"，RS@10 归 0。HybridPatch 在这两样本走 bounded_rewrite，forward completion 仅 4k–5k，从未触顶，最终文档字节级完好。
- **FR 静默内容污染（无报错、逐轮携带）**：`protein3` RT6 一次往返把 PDB 记录复制错类（ATOM/HETATM 92/8 → 120/1、serial 不连续），全文重写永不自愈，0.99→0.063。HybridPatch 走 bounded_rewrite 20/20、结构保持 92/8。
- **验证门 + 失败保留上下文 = 优雅降级 vs 灾难替换**：FR 一次坏输出=文档被垃圾整体替换；hybrid 一次坏输出=保留上一轮好文档（kept_context，`bytes_changed=False`）。这是 CriticalFailure@10 `17/180` vs `23/180` 的机制解释。
- **公平性核查通过**：distractor 每步用原始内容 `merge_distractor` 回覆盖、target_filenames 排除 distractor、错误行两方法同样映射 RS `0.0`（非负惩罚）。FR 崩溃是全局重写范式的真实属性，非 harness 冤枉。模型在 raw 里正确将 distractor 识别为参考材料、未泄漏进目标文档。

### 自伤 bug（a 类，本轮最重要单点发现）

- **malware3 全 10 轮 RS 0.000**，同样本 FR ~0.97–1.0。根因：hybrid 协议把整文件塞进信封 JSON 的 `action.files[].content` 字符串，模型在嵌套 JSON 里**少转义一层反斜杠**（`\Global` 应为 `\\Global`），落盘 YARA 文件带非法转义序列 `\G`，评估器每轮报 `Invalid escape sequence: \G, at line 93`。
- **验证门未拦截**（`ecr_pass=true`、`failure_reason=null`、gate 零报错）——broken-but-syntactically-plausible 文件被写盘、逐轮 0 分。FR 用围栏纯文本传输、无嵌套 JSON 双重转义陷阱，同样本正常。
- 该缺陷暴露所有反斜杠密集格式（YARA/正则/Windows 路径/LaTeX）。与 v1 时代 escape 匹配问题（本文档 §4）同族。malware3 一样本约拖掉 headline 0.05。

### 非方法信号（复核确认）

- `genealogy6` 双方全 0：任务特性（GEDCOM→Mermaid 不可逆 forward + 逐 ID 精确覆盖率评分），`evaluation.error=None`、`individual_coverage≈0`、两方法重建出完全不同的人物集，非方法差异、非评估器崩溃。
- `circuit1` FR `1.000@1→0.149@5→0.812@10`：真实 RS 波动，无异常轮（backward 行全 `error=None`、`noop_forward=false`）。hybrid 自身 RT9-10 跌到 0.127 与 local_patch `op_rejected` 后 backward 保留 forward 态有关（**保留上下文在 backward 方向=没还原=低分**，kept-context 是双刃剑）。

### 补丁路径的真实贡献（近零）

- test 路由份额 bounded_rewrite `307/400 (76.8%)`、生成字节占比 `0.797`、块存活率 `0.196`——当前方法真实身份是"带验证门的受限范围重写"，不是补丁方法。
- forward no-op 来源：`local_patch` 68/95 route steps no-op（锚点/字面量找不到，与 C30–C70 整条 AP-pure span-production 失败同源）、`dsl_rules` 9/9 全灭（模型发明不支持的规则或超 R-ENUM）、`bulk_patch` 5/10。dsl_rules 使用率 2%、成功率 0%，为负资产。

### 报告主张前必须披露的诚实性风险

- **no-op 通胀**：合并 50 样本 RS@10 `0.637 vs 0.502`；若 forward no-op 轮强制记 0，合并变 `0.487 vs 0.502`（反超为负），仅 distractor test 在严格审计下保持正向 `0.490 vs 0.418`。可辩护主张范围收窄为"distractor 长程设定下"。
- **max_tokens 依赖**：FR 两个崩溃样本均撞 20000 上限；需 cap 敏感性消融堵"提高 cap FR 就不崩"的口。
- **val/test 混杂**：val 无 distractor、test 有 distractor，且样本集不同，"distractor 放大优势"目前无法因果归因。
- **成本反向**：hybrid 总 token `11.5M` vs FR `7.1M`（+61%，主要在 prompt 侧索引/块表），"减负"原则在成本维度不成立。

### 改进方向（一切改动=新 dev 迭代，冻结 val/test 链已关闭，归档不动）

- P0 传输层修复（已实现，无 API 验证 PASS）：文件体从嵌套 JSON `content` 字符串移到围栏 `[FILE BODIES]` 段（复用 `utils_context` 变长围栏），信封内用 `@body:<name>` 哨兵引用，执行器/replay 解引用为零转义字面体；内联 content 仍兼容旧归档；悬空 `@body:` 引用被拒、绝不写成哨兵字面串。改动落 `hybrid_schema/hybrid_executor/hybrid_prompt/experiment_runner/verify_anchorpatch/test_hybrid_executor`。回归：`test_hybrid_executor.py` 10/10（含 malware3 式 YARA `\Global\PIPE` 字节级往返、悬空引用拒绝、内联兼容）、`test_executor.py` PASS=236、`splitters.py` 字节级、`py_compile` 全绿、100 行旧归档 back-compat 复解析零误产 body。预计单独值 ~0.05 headline，待 dev smoke 确认。
- P1 归因消融：scoped-FR-only 臂（强制 bounded_rewrite）判定补丁路径当前是否零贡献；FR cap 敏感性量化截断悬崖占比。
- P2 补丁路径重修：片段 id 寻址替代字面锚点（攻 72% 锚点失败、绕开模型 span 生产短板、提高复制比例压低身份稀释）；砍 dsl_rules。
- P4 证据加固：第二 seed/第二模型复验；补无 distractor test 臂或有 distractor val 臂解混杂；`unused_reserve=20` 作改进版 sealed holdout；审计调整版 RS（no-op forward 记 0）升为共同主指标。

## 214. HybridPatch 执行器 vs Prompt 契约不一致排查 + hybridpatch/2 执行器优先对齐

> 记录日期：2026-07-04。人工发现 + 逐 op 代码核对（prompt/executor/schema/gate/index/runner repair）。改动全部走版本门控，旧 `hybridpatch/1` 归档字节级复现（诚实门四目录仍 PASS）。未编辑 raw JSONL/checkpoint/frozen task plan/sample/reference/evaluator/scoring。

### 病灶

P0 传输修复后，P0 冒烟（malware6/latex2/mathlean2/foodmenu6/docker6）转义 bug 已消除（malware6 RS@10 `0.951`），但整体配对 Δ `-0.037`：`local_patch` 43%、op_accept_rate `0.783`、gate 失败 33%、kept-context 33%、forward no-op `13/50`。根因是**我们同时在教 LLM 一套契约、却用另一套执行**：

| 机制 | prompt 暗示 | 执行器实际(v1) | 后果 |
|---|---|---|---|
| local_patch 匹配 | `file+old_text` 即可 | 逐 block 内 `find`、跨 block 不命中、须唯一(否则 not_unique/not_found)、无空白无关回退 | 大量 op 被拒→kept-context no-op |
| occurrence | 示例无 | schema 支持但 prompt 未教 | 多处命中无法消歧 |
| block 索引 | 展示 coarse+medium+fine 三尺度 | 只解析 coarse `file:seq`；M/F id 一律 not_found | 引用细尺度 id 100% 拒绝 |
| dsl distribute | 只说 assignments/discard | 要求覆盖全部 block(missing→violation) | 局部搬运→gate fail(dsl 9/9 挂) |
| dsl 能力 | "dsl_transform/规则"暗示通用转换 | 只支持块 copy/distribute | 发明不支持规则→schema_error |
| repair | 仅修 JSON/schema | op_rejected/route_violation 不触发 repair | 语义失败无救援→硬 no-op |

### 决策：执行器优先（"减负优先"第一原则）

判据修正——教模型更多规则=加负担；执行器确定性消解歧义=减负担。凡执行器能确定性且安全消解的，执行器吃下；prompt 只做"删陷阱/删过度承诺"(本身是减负) 与承载唯一不可消解歧义(occurrence)。

**执行器改（hybridpatch/2，版本门控）：**
- **E1 空白无关匹配阶梯**：移植 v1 `_match_span` 的"精确→空白无关"语义到新 hybrid 匹配器（不改冻结 executor.py），可见字符仍精确、仅空白模糊、仍要求唯一。
- **E2 文件级跨 block 匹配**：拼接文件文本找 old_text（可跨 struct2 块边界），再把 span 映射回仅重叠的块（未触碰块字节级不变，`preservation_violations=0`）。让现实契约=prompt 已暗示的"file+old_text 就够"，模型无需知道 block 存在。
- **E3 distribute 局部覆盖**：未指派块留在源文件（copy-forward 已保留），不再 missing→violation；保留 duplicate 检查。

**Prompt 改（全是"减信息"）：** 块表限 coarse-only（除陷阱 + 砍块表 token）、撤 dsl 宣传（明确只做块搬运、内容转换走 bounded_rewrite）、加一句 occurrence 规则+示例（唯一不可消解歧义）。

**版本门控**：`apply_hybrid` 按信封自带 `protocol` 分支（`rev_of`）；`hybridpatch/1`→严格 v1 语义（旧归档字节级复现），`hybridpatch/2`→上述放松。schema/extract 双版接受，生成端只发 v2。

**repair（M8）不动**：E1/E2 落地后 op-reject 预期大降，先看残留再定，避免第二次实质调用稀释"单次调用≈FR"公平性。

### 附带：MiniMax-M3 开推理

`model_openai.py` 加 `_minimax_thinking_config`（env `MINIMAX_THINKING` 门控、budget/max_tokens 不设人为上限、thinking 时强制 temperature=1）。thinking 计入 output tokens（成本/计价已含）；`_anthropic_text` 只收 text 块，thinking 不进 `raw_llm_response`，诚实门 replay 不受影响。默认关闭，旧行为不变；API 请求形状未验证，需 1 次 live 探针。

### 验证

`test_hybrid_executor.py` PASS 14/14（E1 空白无关、E2 跨 block 且 v1 拒/v2 收对照、occurrence 消歧、distribute 局部覆盖 + v1 missing-block 回归）；`test_executor.py` PASS=236；`splitters.py` 字节级；`py_compile` 全绿；四个旧归档诚实门 PASS（200/400/400/100 backward RS 字节级复现）。

## 215. exp_20260706_hybridthink5：SSE 流式修复后首个全程配对 campaign，HybridPatch 显著胜出（诚实门 PASS）

**背景**：exp_20260704_hybridv2think2 两臂均死于 524 重试耗尽——非流式请求 + thinking 使首字节时间超过 Cloudflare 120s Proxy Read Timeout，单次生成稳定超窗时重试结构性无效；且 run 中改码造成单目录混两个 code fingerprint、诚实门 FAIL（2 行可解释预存漂移，机制见 active log 2026-07-04 条目）。修复：`model_openai.py` minimax 路径改 SSE 流式（`stream:true` + `_read_sse_stream` 重组完整消息，字节持续流动使 CF 读超时不再触发；urllib socket timeout 顺带变为块间停滞守卫）；runner `main()` 每样本 try/except 隔离，单样本终态失败不再拖死后续样本。

**结果**（exp_20260706_hybridthink5：5 样本 × 10RT × 双臂，minimax-m3 + thinking + max_tokens 64000，单一 code fingerprint，诚实门 PASS 100/100 backward RS）：

- 配对 backward RS：AP 0.946 vs FR 0.571，Δ+0.375（n=50，t=6.36，p<0.001，d=0.90）；ECR 条件化 Δ+0.340（n=44）。CriticalFailure@10：AP 2/45 vs FR 4/45。
- RS@10 分域（AP vs FR）：foodmenu 0.983/0.025、mathlean 0.900/0.140、latex 0.741/0.424、malware 0.990/1.000、docker 0.684/0.945。与 p0smoke（hybridpatch/1、无 thinking、同 seed 同 task plan）纵向对比：foodmenu6 0.434→0.983 翻盘；docker6 0.647→0.684 仍败给 FR。注意纵向对比混杂 thinking/max_tokens/协议三因素，不可单独归因。
- 成本：AP tokens 2.39M vs FR 1.18M（约 2×）。preservation_violations 全程 0。
- 路由份额：bounded_rewrite 52%、local_patch 38%、bulk_patch 7%、失败 3%。

**9 个 kept-context 步骤归因**（9/100 步；no-op forward 5/50 与此重合）：

1. **occurrence 索引漂移**（5 op，docker6 RT3fwd/RT9bwd；执行器/协议语义缺陷 → §216 修复）：模型按步骤输入文档给 occurrence=1..4，执行器逐 op 对变异中文档解析，每替换一处剩余匹配递减，occ=3 时只剩 2 处 → occurrence_out_of_range；被接受的 op 实际也发生无害错位（恰好同替换文本）。docker6 是唯一败样本且其全部失败步为此 bug。
2. **@body 供给失败**（3 op）：latex2 RT2 声明 @body:bibtex.bib 但完全未写 [FILE BODIES]（end_turn、8.3k tokens，模型协议违规）；foodmenu6 RT6 finish=max_tokens（64000 烧满，[FILE BODIES] 截断）。
3. **纯模型错误**（2 op）：latex2 RT5 stale old_text（body 已正确解析后仍 matches=0——顺带验证 match-侧 body-ref 修复生效）；mathlean2 RT2 歧义 delete 不带 occurrence（按协议正确拒绝）。
4. **invalid_json ×3**（repair 尝试未救回）。

盲区：repair 触发器仅盯 invalid_json/schema，6/9 失败步（全部 op_rejected/gate 类）未获 repair 机会 → §216。

## 216. hybridpatch/3：occurrence 快照语义 + repair 触发扩展（版本门控，旧归档字节级复现）

**协议语义**（`rev_of` 按信封 protocol 门控；生成端默认升 `hybridpatch/3`，/1 与 /2 归档语义冻结）：

- local_patch 快照定位（`_run_local_v3`）：所有 op 的 occurrence/唯一性一律按**步骤输入文档**解析（模型在 prompt 里看到的编号即最终编号）；span 两两校验互不重叠（违者显式拒绝 `overlapping_span`）后右到左一次性应用；同位 insert 按 op 顺序组合。匹配阶梯（E1 空白无关、E2 文件级跨块）与唯一性规则沿用 v2。
- E3 distribute missing-block violation 改为仅 v1 记违规（v2/v3 共享放松）。

**prompt**：occurrence 快照语义说明（前序 op 不再漂移编号）+ 路由引导：同 old_text 多处/全部替换走 bulk_patch replace_all（docker6 案例任务本应走 bulk）。

**repair**：`need_repair` 从 invalid_json/schema 扩为任何失败类（op 拒绝/route violation/空输出/gate 失败——均为可精确描述错误）；repair prompt 泛化并携带 [TASK] + [EDITABLE DOCUMENTS] grounding（锚点级修复不再靠记忆猜），且明确允许补发 [FILE BODIES]（旧措辞 "Output ONLY the corrected JSON" 实际禁止 bodies 补发）。verify 的 replay 语义不变（只重放存储的两次尝试 + 确定性择优，触发条件不参与 replay）。

**验证**：test_hybrid_executor 21/21（新增 4 v3 用例：docker6 场景复刻含 v2 行为锁定、快照不受前序 op 影响、overlap 拒绝、同位 insert 组合）；splitters 字节级；py_compile 全绿；诚实门 replay PASS：exp_20260706_hybridthink5（100，/2 envelope）与 exp_20260704_hybridp0smoke（100，/1 envelope）在新代码下字节级复现。已同步移植 hybridpatch_clean（hybrid 四文件哈希核对后整体覆盖 + model_openai SSE + runner repair/隔离三处编辑），clean 侧 21/21 + splitters + think5 临时拷贝 replay PASS。

**未验证**：v3 + 扩展 repair 尚无 live 实验数据；occurrence 修复对 docker6 的实际效果、扩展 repair 的净收益（额外调用成本 vs 救回率）待下一轮 fresh out_dir campaign。

## 217. exp_20260708_hybridv3dev20full：hybridpatch/3 首个全量 dev20 campaign（诚实门 1 处评估器非确定性、headline 胜出大半来自 FR 提供方伪影）

> 记录日期：2026-07-08。out_dir `exp_20260708_hybridv3dev20full`，`minimax-m3`（`MINIMAX_THINKING=1`），dev20 全 20 样本 × 双方法 × 10RT × no distractor，40/40 checkpoint 完成。两条独立子代理归因（FR 轨迹异常 / HP no-op），主 Agent 逐条源码复核。只读归档，未编辑 raw JSONL/checkpoint/task_plan/sample/evaluator/scoring。

### headline 与去伪影分解（本轮最重要单点）

- analyze 配对：RS@10 `hybridpatch=0.905` vs `fullrewrite`（见 comparison.md），200 对 backward mean `HP=0.910 vs FR=0.685`，Δ`+0.225`（t=7.55，p<0.001，d=0.53）；CriticalFailure@10 `5/180` vs `10/180`。
- **但 +0.225 大半是 MiniMax-M3 提供方伪影，非方法效应**：剔除 6 个被"thinking-runaway 空返回"污染的 FR 样本（foodmenu6/geotrack3/mathlean2/protein1/satellite6/translation4）后，14 样本 Δ 塌到 **+0.051**（HP 0.900 vs FR 0.848）；被污染 6 样本上 Δ 高达 +0.632（HP 0.934 vs FR 0.302）。即约 ¾ 的 headline 优势来自 FR 链被单次空/截断响应毒化，而非补丁路径的正贡献——与 §213 test 归因（赢的机制主要是 FR 灾难放大被规避）同族。

### FR 轨迹异常（10 个，全部归因，证据链见子代理报告）

- **empty_poison（提供方 thinking-runaway，6 个）**：MiniMax-M3 输出巨型 `thinking` 块饿死 `text`，四级形态——`stop=max_tokens`+542k thinking+0 text（foodmenu6 RT6）；`stop=None`+`output_tokens=0`+thinking+0 text（geotrack3 RT10、mathlean2 RT3、protein1 RT2、satellite6 RT3）；`stop=end_turn`+384k thinking+365B 前言桩（translation4 RT1）。runner FR 路径无空响应护栏（`gen_real=parse_context_string(raw)` 后逐轮 carry），一次空/桩即永久毒化后续全链（protein1/foodmenu6 各拖 8-9 轮归零）。geotrack3 是干净对照：空只在末轮 RT10，仅 RT10 崩（RS 0.824→0.117），RT1-9 不受影响——证明是空响应而非渐进能力衰退。
- **provider 截断（1 个）**：makefile4 RT5 fwd SSE 流中断，text 截断到 6274B（非空，scan 不标记）、envelope 全零、缺 `platforms.txt`→RT5 bwd 幻觉出假 Makefile→归零。
- **capability_collapse（1 个）**：edifact6 RT8 多文件拆分把 2 条 invoice 消息丢了 1 条（items 19→6）+ 改错文件名（`1801464167.edi` vs `invoice.edi`）→0.24，逐轮携带。与已知结论（AP/FR 均弱于多文件拆分/格式转换）一致。
- **恢复（2 个，均真实非造假）**：landmarks1 RT1→RT2（0.0→1.0）是评估器假阴性——RT1 全 30 条数据都在但用了非规范 KML 字段名大写/Point 坐标，评估器解析不到；RT2 恰好用规范小写+lon/lat SimpleData→满分。quantum4 RT1→RT2（context_mismatch→0.744→0.879）是真实自纠——RT1 只发了 3 文件中的 1 个（vqe.qasm），RT2+ 补齐 3 文件，逐轮结构收敛。

### HP no-op（5 个 kept-context，全部 `repair used=False`，源码复核确认）

step 语义为**全或无**：任一 op 拒绝即 `gate_pass=False`→整步保留上一轮文档（`bytes_changed=False`），部分成功的 op 被丢弃。

- **HP `bulk_patch` 执行器过严（2 个，最可行动）**：docker6 RT3、fonteng3 RT1——被拒锚点在整文件里**恰好唯一存在**但**跨 struct2 block 边界**。源码复核证实：`local_patch` 的 `_collect_local_matches_v2` 重建整文件文本作文件级搜索（可跨块），而 `bulk_patch` 的 `_find_matches` 逐块 `for bid ... edited[bid]` 搜索，跨块 needle 永远匹配不到。两例各有 2/5 个同信封的合法 op（COPY 重命名、逐 token 改名）因这一个跨块 op 拒绝被整体丢弃。→ 同文件修法：跨块多行 needle 路由到 local_patch，或给 `_run_bulk` 文件级匹配器。
- **LLM 失败（3 个，不同类）**：docker6 RT9（空 op 列表+repair 逐字回吐输入，文档本已在目标态，gate 正确拒 `effective_noop`）；latex2 RT5（幻觉出源文本里不存在的段末换行，锚点作为写的确实不存在；次生：`_unescape_ws` 把 `\r`→CR 破坏 `\ref`/`\rho` 反斜杠 token，使空白容错阶梯对 LaTeX 失效）；mathlean2 RT2（`@body:` 哨兵里嵌了 `\n\n` 致 `body_ref_not_found`，且 repair 调用返回空 completion——tokens 全耗在 thinking 通道）。

### 诚实门

- `verify_anchorpatch.py` 报 1 处 MISMATCH：`hybridpatch/quantum4 RT9 stored=0.9850 recomputed=1.0000`（差在 `qubit_decl_accuracy` 0.85 vs 1.0）。源码复核：`score_qubit_decls` 纯确定性，差异来自 `domain_quantum.parse_context` 的 AST+regex 回退双路径对相同字节解析出不同 qubit_decl 集（环境/解析器版本敏感，非 RNG、非遥测漂移）；**recompute 得分更高**，故 stored 0.985 若有偏也是保守低估、非造假。属已知类评估器非确定性（与父仓库 §DESIGN circuit2 stored/recomputed 预存差值同族）。**其余 39 样本全部字节级复现 PASS**。

### 待办（未验证）

- occurrence 快照修复（§216）对 docker6 的实际效果本轮仍未生效：docker6 RT3 的失败是 bulk_patch 跨块匹配缺口，不是 occurrence 漂移——这是 §216 prompt 路由引导之外的独立执行器缺口。
- FR 6 个空返回是否稳定复现（thinking-runaway 是采样随机，重跑可能不再空）需从对应 RT 起 rewind 重跑验证；makefile4/translation4 的截断与前言桩形态 scan 不标记（raw 非空），需单独判断是否重跑。

### rewind 重跑结果（2026-07-08 下午，5 个纯空返回样本 rewind 后重跑完成）

- **空返回不是位置稳定复现的，是随机提供方事件**：3/5 样本重跑后彻底干净（foodmenu6 RT6+ 恢复 0.987→0.859、geotrack3 RT10 恢复 0.824、mathlean2 RT3+ 恢复 0.928→0.836）；2/5 在**不同调用点**又随机撞上新空（protein1 原空 RT2 fwd→新空 RT2 bwd；satellite6 原空 RT3 bwd→新空 RT9 fwd，RT3-8 已恢复 0.955）。
- **全 campaign 空返回率按 api_calls.jsonl 统计：HP 7/440=1.6% vs FR 7/482=1.5%，两方法均等**。差异在承接架构：HP 的 7 个空——5 个被 repair 第二次调用救回（含 foodmenu6 RT5 bwd 0.999、makefile4 RT5 bwd 0.930）、1 个 repair 自身空→kept-context 优雅降级（mathlean2 RT2）、1 个无害；**零样本链被毒化**。FR 无护栏，每个空直接毒化剩余全链。空返回韧性是 HP 真实的架构优势（repair 第二调用 + kept-context），但需披露调用预算不对称（HP 失败时有第二次调用，FR 恒一次）。
- 重跑后配对：Δ 从 +0.225 收敛到 **+0.141**（200 对，t=5.43，p<0.001，d=0.38）；剔除仍污染的 3 样本（protein1/satellite6/translation4）后 Δ=+0.048（n=17）。FR 臂如需完全去伪影还差 protein1（from_rt 2）与 satellite6（from_rt 9）二次 rewind。

### 上游 baseline 对齐审计（2026-07-09，对照 github.com/microsoft/DELEGATE52 主分支源码）

重跑验证之外的第二条证据链：逐文件核对我们的 FR 实现与官方 baseline，确认空返回毒化不是本仓库实现错误。

- **字节级一致**（仅 CRLF 行尾差异）：`utils_context.py`（stringify/parse_context_string/is_context_complete）、`utils_env.py`（shuffle_context/merge_distractor/load_sample）、`prompts/domain_documents.txt`（FR 提示模板）。
- **relay 语义一致**：上游 `run_relay.py` 对每步输出执行 `shuffle_context(merge_distractor(parse_context_string(llm_response), distractor))` 后无条件传入下一步——空响应解析为空 context 照样传播毒化全链；仅 `llm_response is None`（fiction 长度校验路径，本数据集不触发）才 abort。我们 runner 的循环与之逐行同构，FR 无 keep-context、无 retry-on-empty，行为忠实。
- **model 层同语义类**：上游 generate() 只对**异常**retry（max_retries=3，domain 层调用给 10）；HTTP 200 + 空 content 不是异常，原样返回。我们同样只对异常/超时/配额 retry（LLM_MAX_RETRIES=3 + 配额/transient 等待预算），空 content 原样返回；`_anthropic_text` 提取无 bug（已核对原始 provider_response 确实只有 thinking block、无 text block）。temperature 默认均 1.0。
- **有意偏差（已披露）**：① max_tokens 上游默认 20000、我们 uncapped（M3 262144 上限）——thinking 模型必需，上游 20000 对 M3 反而几乎每步截断；② seeded task_plan 替代上游全局 random（两臂共享同一 plan，配对公平性更强）；③ 模型家族：上游 GPT 系无 thinking-only 空响应病理，该病理来自 minimax-m3 + OpenCode Go 端点，非代码差异。
- **发现并修复一处真实缺口**：runner `_evaluate` 缺上游 `validate_wildcard_context` 分支（wildcard target 态下生成文件不得有杂散文件，否则 `wildcard_mismatch`）。回溯审计整个 campaign：仅 6 个 forward 行会改判，且**完美对称**（HP/FR 各 3 个、同样本同轮次：mathlean2 RT7 的 `Derangements_*.lean` 未落子目录、translation4 RT5/8 的杂散 `assembly.txt`），backward RS 与配对统计零影响。已按上游语义补齐（`experiment_runner.py::_evaluate`），21 执行器测试回归通过。
- **结论**：FR 空返回与毒化链均为 authentic baseline 行为 + provider 病理，非本仓库实现错误；对齐审计与 rewind 重跑（3/5 恢复、2/5 异位复发）两条证据链相互印证。
