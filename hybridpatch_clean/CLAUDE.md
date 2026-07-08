# CLAUDE.md

Guidance for Claude Code when working in this repository.

# HybridPatch（受约束写入 vs 全局重写）

## 项目定性

长程文档编辑里，全局重写（FullRewrite）容易把未被要求改动的内容一起改坏。**HybridPatch** 用受约束写入替代无约束全文重写：LLM 输出一个结构化信封（计划单 + 一条 action 路径），确定性执行器应用它，未被声明的源字节由执行器逐字节保留。主线目标只有一个：**做出一个整体上优于全局重写的新方法**，与 FullRewrite 在同一 seeded 任务序列上配对比较。

本仓库是从一个多轮迭代的研究工作区（DELEGATE-52 fork + AnchorPatch v1/v2/C1-C70 历史）中**干净剥离**出来的，只含 hybridpatch 活代码 + 复用基建 + 数据。历史死代码与归档留在父仓库。

## 目录结构

```
src/      所有 Python：hybrid_* 核心 + 复用基建(patch_schema/splitters/utils_*/model_openai/run_meta/domains) + runner/verify/analyze
data/     samples_delegate52(234样本) + hybrid_split.json + research_splits
prompts/  FullRewrite 域提示模板（domains 以相对路径 prompts/ 加载，须从仓库根运行）
docs/     HYBRIDPATCH_DESIGN / FINDINGS / goal_state / active_log
```

所有命令**从仓库根目录运行**，指向 `src/`。Windows 必加 `PYTHONUTF8=1`。

## 三层管线（`src/`）

```
文档(bytes) → splitters.split_struct2 → Block 列表(block_id = "<filename>:<seq>")
            → hybrid_prompt.build_hybrid_prompt(全文 + 文件/块索引 + 4 路径 schema + 规则)
            → LLM 输出信封 JSON(+可选 [FILE BODIES] 围栏原文块) → extract_hybrid_json
            → hybrid_executor.apply_hybrid(确定性执行 + HybridExecLog 遥测)
            → {filename: content} → domains.get_domain(...).evaluate_context → RS
```

| 文件 | 职责 |
|---|---|
| `hybrid_schema.py` | 信封/路由/协议校验；`PROTOCOL_V1`/`PROTOCOL_V2`/`PROTOCOL_V3`/`rev_of`（版本门控） |
| `hybrid_index.py` | 确定性文件/块索引（block/fragment 表、块摘要） |
| `hybrid_prompt.py` | 提示构造 + `extract_hybrid_json`（信封 + `[FILE BODIES]` 围栏原文块解析） |
| `hybrid_executor.py` | `apply_hybrid`：确定性执行 4 路径 + 来源复制 + 字节保留 |
| `hybrid_gate.py` | reference-free 输出闸 + `audit_forward_completion` |
| `build_hybrid_split.py` | 从 research_splits 生成 `data/hybrid_split.json`（dev/val/test） |
| `experiment_runner.py` | HybridPatch/FR relay 循环；单次主调用 + 一次 repair（任何失败类都触发：JSON/schema/op 拒绝/gate，repair prompt 带拒因 + 文档 grounding）；禁静默 FR fallback；main 按样本隔离故障 |
| `verify_anchorpatch.py` | 诚实门：从 raw 响应独立重算每个 RS，非零退出 = 遥测漂移/造假 |
| `analyze.py` | RS@k 配对统计 + hybrid 遥测 → `<dir>/analysis/comparison.md` |

## 4 条 action 路径

| 任务形态 | 路径 | 说明 |
|---|---|---|
| 少量精确编辑 | `local_patch` | old_text/anchor_text 文件级匹配、须唯一或给 occurrence |
| 大量同型修改 | `bulk_patch` | 字面 replace_all / delete_lines_containing，禁正则 |
| 块搬运/分发 | `dsl_rules` | 仅块 copy/distribute，非通用转换；受 R-ENUM 上限 |
| 整文件生成/转换 | `bounded_rewrite` | 声明可写文件，内容走 `content` 或 `@body:` 引用 |

**文件体传输**：含反斜杠/引号/多行的文本（content/new_text **及** old_text/anchor_text）用 `@body:<name>` 哨兵引用，在 JSON 后的 `[FILE BODIES]` 段用变长围栏原文块承载——零 JSON 转义。

**hybridpatch/2 语义**（`rev_of` 按信封 protocol 门控；`hybridpatch/1` 走严格旧语义）：
- E1 空白无关匹配阶梯（精确→空白无关，可见字符仍精确、仍要求唯一）
- E2 文件级跨 block 匹配（span 映射回重叠块，字节保留不变）
- E3 distribute 局部覆盖（未指派块留在源文件）

**hybridpatch/3 语义**（当前生成默认；含全部 v2 语义）：
- local_patch 快照定位：所有 op 的 occurrence/唯一性一律按**步骤输入文档**解析（模型在 prompt 里看到的编号即最终编号，前序 op 不再使后续 occurrence 漂移），span 校验互不重叠后一次性应用（右到左）
- 重叠 span 显式拒绝（`overlapping_span`）；同位 insert 按 op 顺序组合

**核心不变量**：`preservation_violations` 必须恒为 0（未声明块被改 = 执行器 bug）。这是 HybridPatch 唯一稳健的字节级声明。

## 工作纪律

1. **执行器改动必跑回归**：改 `hybrid_executor.py`/`splitters.py`/`patch_schema.py` 后必跑 `PYTHONUTF8=1 python src/test_hybrid_executor.py` 与 `python src/splitters.py`，非零退出不得交付。
2. **诚实性**：任何实验结论必须出自真实运行产物（JSONL/checkpoint），报告前先过 `verify_anchorpatch.py` 诚实门。禁止根据预期编造或外推数字。
3. **新实验隔离输出**：跑新实验显式 `--out_dir` 指向新目录。
4. **逐轮分析**：每完成一次 relay 往返，分析本轮 LLM 实际表现（RS 下降/恢复都要归因），实事求是不写断言式判断；结论写入 `docs/FINDINGS.md`。
5. **版本门控**：改执行器语义走新 rev + 版本门控，旧归档 replay 须字节级复现。
6. **DeepSeek/MiniMax 无 JSON mode**：补丁解析依赖 `extract_hybrid_json` 鲁棒提取，勿改 `response_format`。

## 常用命令

```sh
# 无 API、零费用
PYTHONUTF8=1 python src/test_hybrid_executor.py   # 执行器字节级验证(全路径用例)
python src/splitters.py                           # 切分器覆盖自检(拼回=原文)

# 调 LLM（产生费用）——先 hybridpatch 后 fullrewrite，配对比较
PYTHONUTF8=1 python src/experiment_runner.py \
  --sample malware6 latex2 --methods hybridpatch fullrewrite \
  --num_round_trips 10 --skip_distractor --model minimax-m3 \
  --out_dir exp_<slug> --notes "<什么实验>"

# 诚实门 + 配对统计
PYTHONUTF8=1 python src/verify_anchorpatch.py --dir exp_<slug>
PYTHONUTF8=1 python src/analyze.py --dir exp_<slug> --K 10 --critical_theta 0.10
```

- 默认模型 `deepseek-v4-flash`（OpenAI 兼容端点，CNY）；`minimax-m3` 走 OpenCode Go Anthropic 兼容 `/messages`（USD），可开 `MINIMAX_THINKING=1` 推理。
- API 密钥放根 `.env`（见 `.env.example`）。
- `experiment_runner.py` 单进程、每次一个/多个样本；并行靠多开进程（每样本独立 checkpoint 幂等续跑）。

## 必读文档

| 文档 | 内容 |
|---|---|
| `docs/RESEARCH_JOURNAL.md` | 研究实录（活文档）：设计哲学、实现、完整 Bug 史（现象/根因/修复/教训）、实验轨迹；每个 campaign 跑完按其 §6 模板追加 |
| `docs/HYBRIDPATCH_DESIGN.md` | 设计原则、4 路径、系统结构 |
| `docs/FINDINGS.md` | 实验发现与根因（track 完成 + win 机制 + 契约对齐 + body-ref/thinking 修复） |
| `docs/goal_state.md` | 当前 goal/candidate 状态快照 |
| `docs/active_log.md` | before/after 迭代游标 |
