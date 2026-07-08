# 03 · 初步实验结果预览

> ⚠️ **全部为初步数字**：实验计划重跑，论文最终数字以后续交付的实验数据包为准。本文只用于 (a) 第一章"贡献点"里的一句话概括；(b) 让写作者对结果量级有体感。每个数字都出自真实运行产物，来源路径已标注。

## 1. 实验设置（一段话版）

模型 MiniMax-M3（开 extended thinking），20 个样本（覆盖 20 个领域：会计/电路/Docker/EDI 报文/字体/菜单/GPS 轨迹/地标/LaTeX/Makefile/YARA/Lean 数学/分子/乐谱/蛋白质/量子电路/卫星轨道/剧本/星表/翻译 PO 文件），每样本 10 个 round trip，HybridPatch 与 FullRewrite 在同一 seeded 任务序列上配对，共 200 个配对 backward RS 观测/臂。

## 2. 主结果（初步）

| 口径 | HybridPatch | FullRewrite | Δ (HP−FR) | 统计 |
|---|---|---|---|---|
| 全 20 样本，200 对 | **0.910** | 0.769 | **+0.141** | t=5.43，p<0.001，Cohen d=0.38 |

- CriticalFailure@10（断崖式失败步计数）：HP 5/180 vs FR 8/180。
- 更早一轮 5 样本小规模实验（think5 campaign）：HP 0.946 vs FR 0.571，Δ+0.375（n=50，d=0.90），诚实门 100/100 通过——小样本上差距更大，因为该批样本 FR 崩溃更多。
- **核心不变量**：preservation_violations 全程 **0**（两轮 campaign、600+ 步执行，无一未声明字节被改）。

来源：`exp_20260708_hybridv3dev20full/analysis/comparison.md`（重跑 rewind 后由 analyze.py 重新生成）；think5 数字见 `docs/RESEARCH_JOURNAL.md` §5.3。

## 3. 方法内部遥测（初步，供"披露指标"叙述）

| 指标 | 值 |
|---|---|
| 路由份额 | bounded_rewrite 74.2%，local_patch 18.0%，bulk_patch 7.8%（400 步） |
| repair 使用率 / 成功率 | 5.8% 触发，触发后 16/16 被采纳 |
| 闸失败（kept-context 诚实 no-op） | 5/400 步（1.2%） |
| 字节来源 | 模型生成字节占比均值 0.716（其余由执行器从源文档复制） |

注意：bounded_rewrite 份额高是本批任务形态（大量格式转换/拆分类 forward 任务）所致，属于如实披露，不是回避点——HP 的优势主要来自"backward 恢复时的受约束写入 + 失败显式"，而非"从不重写"。

## 4. 已知的效度事项（论文 threats 章将来展开，此处备忘）

1. **提供方空响应病理**：MiniMax-M3 + thinking 偶发只输出思考块、正文为空（发生率两臂相同，各 ~1.5%/调用）。HP 因 repair + kept-context 架构零污染；FR 无护栏，空响应会毒化后续所有轮次。我们对 FR 被空响应污染的样本做了有纪律的重跑（只重跑空响应锚定的链，重跑次数披露），并逐字节比对了上游官方 baseline 源码确认这不是本实现的偏差。
2. **调用预算不对称**：HP 失败时有第二次 repair 调用（使用率 5.8%），FR 恒单次。
3. think5 与 dev20 两轮之间协议版本、max_tokens、样本集均有变化，纵向不可直接比。

来源：`docs/FINDINGS.md`（rewind 重跑结果 + 上游对齐审计两节）。
