# HybridPatch 研究实录

> **这是什么**：一份活文档（living document），按时间记录 HybridPatch 的设计哲学、实现思路、遇到的每一个 bug 和每一轮实验数据。新 campaign 跑完后按 §6 的模板追加。
> **诚实规则**：本文档只写通过 `verify_anchorpatch.py` 诚实门（从 raw LLM 响应独立重算每个 RS）的数字；门 FAIL 的数据必须注明原因与处置，不得作为方法证据引用。

---

## 1. 问题与设计哲学

### 1.1 源问题

DELEGATE-52 基准揭示的现象：让 LLM 对专业文档做往返中继编辑（forward 编辑 → backward 撤销，链式 10+ 轮），**全局重写（FullRewrite）会把没有要求改动的内容一起改坏**——文档被静默污染，重建分数（RS）逐轮衰减甚至断崖。

### 1.2 为什么选补丁作为入口

补丁天然适合限制写入范围：模型只声明要改什么，其余内容不经过模型的"手"。这是对抗静默污染的结构性手段，而非提示工程手段。

### 1.3 旧补丁路线的教训（AnchorPatch v1/v2/C1–C70）

前身 AnchorPatch 经历了 v1 → v2 → v2.1 → v2.2 → C1–C70 共七十多个候选迭代，最终结论：**逐块、逐槽、逐操作枚举的大协议本身成为模型的认知负担**。模型要同时完成任务理解、全局规划、协议编写、多文件协调、格式维护——这个负担在不少任务里接近甚至超过全文重写，RS 上 AP≈FR 统计不可区分。协议救不了协议。

### 1.4 四条一级设计原则

由上述教训直接推出：

| 原则 | 含义 |
|---|---|
| **减负优先** | 任何补丁协议都必须减少模型负担；大枚举协议退出主路径 |
| **写入有界** | 模型只能修改声明过的文件和区域；应保留内容由执行器复制 |
| **路径显式** | 不同任务形态走不同路径；有界重写是一条显式路径，不是耻辱的 fallback |
| **失败显式** | 无效不修改、空输出、漏文件、只读被改，都直接记失败——禁止任何静默兜底 |

### 1.5 唯一研究主张

**整体上优于 FullRewrite**，在同一 seeded 任务序列上配对比较。不做分域挑选，不做 cherry-picking；输就是输，赢要能被独立重算复现。

---

## 2. 方法设计与实现

### 2.1 三层管线

```
文档(bytes) → splitters.split_struct2 → Block 列表(block_id = "<filename>:<seq>")
            → hybrid_prompt.build_hybrid_prompt(全文 + 文件/块索引 + 4 路径 schema + 规则)
            → LLM 输出信封 JSON(+可选 [FILE BODIES] 围栏原文块) → extract_hybrid_json
            → hybrid_executor.apply_hybrid(确定性执行 + HybridExecLog 遥测)
            → {filename: content} → 领域评估器 → RS
```

### 2.2 信封与 4 条 action 路径

模型输出一个信封：`{protocol, plan, action}`。`plan` 是计划单（task_family / writable_files / readonly_files / target_files / obligations），`action` 是**一条**路径的最小结构——模型只输出当前路径需要的东西，不输出超量协议。

| 任务形态 | 路径 | 说明 |
|---|---|---|
| 少量精确编辑 | `local_patch` | old_text/anchor_text 文件级匹配，须唯一或给 occurrence |
| 大量同型修改 | `bulk_patch` | 字面 replace_all / delete_lines_containing，禁正则 |
| 块搬运/分发 | `dsl_rules` | 仅块 copy/distribute，受 R-ENUM 上限，非通用转换 |
| 整文件生成/转换 | `bounded_rewrite` | 声明可写文件，内容走 `content` 或 `@body:` 引用 |

（原设计稿是 5 条路径——"规则组装/受限转换程序"独立成路；实现时并入 `dsl_rules`+`bounded_rewrite`，通用转换一律走有界重写。）

### 2.3 文件体传输（@body: + [FILE BODIES]）

含反斜杠/引号/多行的文本**不嵌 JSON 字符串**（那是二次转义地狱，见 §4.1），而是在字段里放 `@body:<name>` 哨兵，在 JSON 之后的 `[FILE BODIES]` 段用变长围栏原文块承载。写侧（content/new_text）与匹配侧（old_text/anchor_text/text）都支持。

### 2.4 执行层：确定性 + 字节保留

执行器按信封确定性地复制、替换、搬运、组装。**核心不变量：`preservation_violations` 恒为 0**——未被任何 op 声明的块若被改动即执行器 bug。这是 HybridPatch 唯一稳健的字节级声明，任何执行器改动不得破坏。

### 2.5 验证层：失败显式

reference-free 输出闸（`hybrid_gate`）：目标文件齐全、只读不动、有效修改审计等。任何 op 拒绝 / route violation / gate 失败 → 该步**保持原上下文**（kept-context）并显式打失败标记；**禁止静默 FullRewrite fallback**。宁可诚实 no-op，不做暗改。

### 2.6 repair：单次、有界、可重放

主调用失败后至多一次 repair 调用，携带具体错误；两次尝试用确定性 key 择优。选择规则版本化，诚实门 replay 时重演同一规则——repair 不是黑箱。

### 2.7 版本门控哲学

执行器语义改动必须走新 protocol rev（`rev_of` 按**信封自带的 protocol 字段**分支），旧归档在新代码下必须**字节级复现**。这使得：旧实验永远可重放审计、语义演化有明确的断代边界、"当前代码能否复现旧数据"成为每次改动的硬回归。

| 协议 rev | 语义 |
|---|---|
| `hybridpatch/1` | 严格块局部匹配（初版，冻结） |
| `hybridpatch/2` | E1 空白无关匹配阶梯 + E2 文件级跨块匹配 + E3 distribute 局部覆盖（冻结） |
| `hybridpatch/3` | v2 全部语义 + local_patch 快照定位：occurrence/唯一性按步骤输入文档解析、span 互不重叠校验、右到左一次性应用、同位 insert 按 op 序组合（当前生成默认） |

### 2.8 诚实门

`verify_anchorpatch.py` 从存储的 raw 响应出发，独立重放整条 relay（含 repair 择优规则），重算每个 backward RS 与存储值逐一比对，非零退出 = 遥测漂移或造假。**任何数字先过门再引用**——这条纪律在本项目里不止一次拦下了"看起来能用其实不能用"的数据（见 §5.2）。

---

## 3. 基础设施要点

- **模型**：minimax-m3 经 OpenCode Go Anthropic 兼容 `/messages` 端点；`MINIMAX_THINKING=1` 开 extended thinking（budget 不设人为上限）；SSE 流式调用（见 §4.4）。
- **实验循环**：AP 与 FR 共享同一 seeded forward 序列（task_plan 落盘复用）→ 同任务配对比较；每往返原子提交（JSONL + checkpoint）→ 幂等断点续跑；runner `main()` 按样本隔离故障（见 §4.5）。
- **遥测**：每步落 route / protocol_rev / op 接受率 / 拒因明细 / gate 错误 / repair 记录 / 字节来源比（copied vs generated）/ preservation_violations。归因分析全靠它。

---

## 4. Bug 史（现象 → 根因 → 修复 → 教训）

按发现顺序。每一条都改变了设计或纪律。

### 4.1 P0：JSON 转义摧毁 backslash-dense 内容

- **现象**：malware（YARA 规则）、latex 等反斜杠密集域，补丁内容嵌 JSON 字符串后二次转义损坏，op 匹配失败或写出坏内容。
- **根因**：把原文当 JSON 字符串运输，转义层数随嵌套增长。
- **修复**：`[FILE BODIES]` 围栏传输 + `@body:` 哨兵——原文零转义，JSON 里只放引用。
- **教训**：**运输层永远不要让载荷经过第二次编码**。协议设计首先要为"最难运输的内容"设计。

### 4.2 执行器 vs prompt 契约错位（33% kept-context）

- **现象**：早期 dev 轮 33% 的步 kept-context no-op，op_accept_rate 0.783。
- **根因**：prompt 教模型"文件级定位、空白宽容"，执行器实际"块级定位、字节严格"——模型按契约 A 写，执行器按契约 B 判。
- **修复**：`hybridpatch/2` 执行器优先对齐——E1 空白无关匹配阶梯（可见字符仍精确）、E2 文件级跨块匹配（span 映射回重叠块）、E3 distribute 未指派块留在源文件。
- **教训**：**协议契约只有一份，以执行器为准**；prompt 描述的行为必须是执行器真实实现的行为。

### 4.3 match-侧 body-ref 未解析

- **现象**：malware6 RT4 forward ecr=0，op 拒因 `not_found matches:0`，但 old_text 是 `@body:op4_old`。
- **根因**：§4.1 的传输修复只在**写侧**解析 `@body:`，匹配侧（old_text/anchor_text）把哨兵当字面文本去 find。
- **修复**：匹配侧同样解析；引用悬空拒因改为 `body_ref_not_found`（不再误报 not_found）。
- **教训**：新机制引入的**接缝**要枚举全部消费点；一个字段族（"文本"）在协议里出现几次，解析就要覆盖几次。

### 4.4 thinking + 非流式 + Cloudflare 120s = 524 结构性死亡

- **现象**：thinking 开启后长调用反复 HTTP 524，重试 10 次全部无效，两臂进程先后死亡（exp_20260704_hybridv2think2 因此不完整）。
- **根因**：非流式请求的首字节要等全部生成完成；Cloudflare 在源站前有 120s Proxy Read Timeout——单次生成稳定超 120s 时，**每次重试都撞同一堵墙**，客户端 watchdog/重试预算全部无关。
- **修复**：minimax 路径改 SSE 流式（`stream:true`，逐事件重组完整消息）；字节持续流动使 CF 读超时不再触发，socket timeout 顺带变为块间停滞守卫。
- **教训**：**分辨"瞬态错误"与"结构性错误"**——前者值得重试，后者重试只是烧钱。修错误前先确认它属于哪类。

### 4.5 runner 无样本隔离

- **现象**：上述 524 死亡发生在第 3 个样本时，第 4、5 个样本从未开始。
- **修复**：`main()` 对单样本 relay 包 try/except，失败记录后继续下一样本；checkpoint 幂等续跑兜底。
- **教训**：**故障爆炸半径要与故障单元一致**——一个样本的死不该赔上整个 campaign。

### 4.6 protocol_version 遥测标签硬编码

- **现象**：44 行 envelope 实际全部声明 `hybridpatch/2`，遥测却全标 `hybridpatch/1`。
- **根因**：runner 写遥测时硬编码了标签，没读执行器返回的真实 `protocol_rev`。
- **修复**：从 exec log 读真实执行 rev。
- **教训**：**遥测必须来自执行现场**，不能来自书写者的假设——否则归因分析在错误的地图上进行。

### 4.7 occurrence 索引漂移（→ hybridpatch/3）

- **现象**：exp_20260706_hybridthink5 中 docker6 两步共 5 个 op 拒于 `occurrence_out_of_range`，且 docker6 是唯一败给 FR 的样本。
- **根因**：任务要求"把 4 处 X 全改成 Y"，模型按**原文档**编号发 occurrence=1..4；执行器逐 op 对**变异中**的文档解析——每替换一处剩余匹配数递减，occ=3 时只剩 2 处 → 拒绝。更隐蔽的是被接受的 op 也发生了无害错位（恰好替换文本相同才没造成伤害）。模型的理解（快照编号）是自然语义，执行器行为是 footgun。
- **修复**：`hybridpatch/3` 快照语义——所有 op 的匹配一律按步骤输入文档解析，span 互不重叠校验后一次性应用；prompt 同时加路由引导（同 old_text 多处替换本该走 `bulk_patch replace_all`）。
- **教训**：**声明式协议的引用系统必须锚定在双方共同可见的状态上**（模型看到的 prompt 快照），而不是单方内部的中间状态。

### 4.8 repair 触发盲区

- **现象**：think5 的 9 个失败步中 6 个（全部 op_rejected/gate 类）`repair_attempted=False`。
- **根因**：repair 触发条件只有 invalid_json/schema；且旧 repair prompt 说 "Output ONLY the corrected JSON"，实际禁止了 `[FILE BODIES]` 补发。
- **修复**：任何失败类都触发（op 拒绝/route violation/空输出/gate 失败——均为可精确描述的错误）；repair prompt 携带 [TASK] + [EDITABLE DOCUMENTS] grounding，明确允许 bodies。
- **教训**：**给"可精确描述的失败"以修复机会**是廉价的；但 repair 必须有界（单次）且可重放（确定性择优），否则会稀释"单次调用≈FR"的公平性。

### 4.9 模型侧失败形态（非执行器 bug，但要设计承接）

think5 归因中剩余的失败：声明了 `@body:X` 却完全不写 [FILE BODIES]（协议违规）；max_tokens 64000 被 thinking + 大文档烧满导致截断；引用不存在的 stale old_text；歧义 delete 不带 occurrence。前两类可被 §4.8 的扩展 repair 承接；后两类是模型能力边界，执行器正确拒绝即是正确行为。

---

## 5. 实验轨迹

### 5.1 p0smoke（exp_20260704_hybridp0smoke）——转义修复后基线

`hybridpatch/1`，无 thinking，max_tokens 20000。诚实门 **PASS**（100 backward RS）。

| RS@10 | HybridPatch | FullRewrite |
|---|---|---|
| malware6 | 0.951 | 0.892 |
| latex2 | 0.685 | 0.372 |
| mathlean2 | 0.876 | 0.862 |
| foodmenu6 | **0.434** | 0.819 |
| docker6 | **0.647** | 0.922 |

转义 bug 确认修复（malware6 翻正），但 foodmenu6/docker6 大输，整体均值 AP 0.719 vs FR 0.773——**此阶段 HybridPatch 整体是输的**。

### 5.2 think2（exp_20260704_hybridv2think2）——诊断归档，数据不可作方法证据

`hybridpatch/2` + thinking 首次尝试。三重问题：两臂死于 524（§4.4）致 campaign 不完整；run 中改码致单目录混两个 code fingerprint；诚实门 **FAIL**（2 行 hybrid 预存漂移——run 后落地的 match-侧 body-ref 修复改变了 replay 结果，属可解释漂移而非造假）。**处置**：定性为诊断归档；产出了 §4.4/§4.5/§4.6 三个修复。
**教训**：campaign 进行中绝不改代码；一个目录一个代码版本。

### 5.3 think5（exp_20260706_hybridthink5）——首个完整验证的胜利

`hybridpatch/2` + thinking + SSE 流式 + 样本隔离，max_tokens 64000，5 样本 × 10RT × 双臂，单一 code fingerprint。诚实门 **PASS**（100/100 backward RS）。

**配对统计**：backward RS AP **0.946** vs FR **0.571**，Δ**+0.375**（n=50，t=6.36，p<0.001，Cohen d=0.90）；ECR 条件化 Δ+0.340。CriticalFailure@10：AP 2/45 vs FR 4/45。

| RS@10 | HybridPatch | FullRewrite |
|---|---|---|
| malware6 | 0.990 | 1.000 |
| latex2 | 0.741 | 0.424 |
| mathlean2 | 0.900 | 0.140 |
| foodmenu6 | **0.983** | 0.025 |
| docker6 | 0.684 | **0.945** |

- **foodmenu6 从 0.434 翻到 0.983**（p0 阶段最大败点翻盘）；docker6 仍败，且其全部失败步正是 §4.7 的 occurrence bug。
- 路由份额：bounded_rewrite 52%、local_patch 38%、bulk_patch 7%、失败 3%。
- 成本：AP tokens 2.39M vs FR 1.18M（约 2×）。
- `preservation_violations` 全程 0。
- 9 个 kept-context 步的完整归因见 §4.7–§4.9 与 FINDINGS §215。
- 纵向对比注意：与 p0smoke 同 seed 同 task plan，但混杂 thinking/max_tokens/协议三因素，单样本差异不可单独归因。

### 5.4 下一步（待跑）

`hybridpatch/3` + 扩展 repair 的 fresh smoke（同 5 样本）。观察点：docker6 的 occurrence 救回、repair 使用率/救回率、bounded 份额变化、grounded repair 的 token 成本。

---

## 6. 追加约定（怎么继续写这份文档）

每个新 campaign 跑完后，在 §5 追加一小节，模板：

```markdown
### 5.N <slug>（exp_<YYYYMMDD>_<slug>）—— 一句话定性

协议/模型/条件。诚实门 **PASS|FAIL**（数量；FAIL 必须写原因与处置）。

**配对统计**：…（Δ / n / t / d）
| RS@10 | HybridPatch | FullRewrite |（分样本表）

- 关键变化与归因（新 bug 记入 §4 编号条目，此处引用）
- 路由份额 / 成本 / preservation_violations
- 与上一轮的纵向对比及混杂因素声明
```

新 bug 记入 §4（现象/根因/修复/教训四段式），协议语义变化更新 §2.7 的版本表。数字先过诚实门，详细流水交叉引用 `docs/FINDINGS.md` 的编号条目。
