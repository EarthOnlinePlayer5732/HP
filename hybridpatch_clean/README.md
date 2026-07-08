# HybridPatch

A constrained-write alternative to full document rewriting for long-horizon editing.
An LLM emits a structured envelope (a short plan + one `action` path); a deterministic
executor applies it and preserves every undeclared source byte verbatim. The research
goal is a single claim: **beat FullRewrite overall** on the same seeded task sequence.

This repo was cleanly extracted from a larger, multi-iteration research workspace — it
contains only the live hybridpatch method, the upstream DELEGATE-52 base it reuses, and
the dataset. The v1/v2 and C1–C70 candidate-line history stayed in the parent repo.

## Layout

```
src/     hybrid_* core + reused infra (patch_schema, splitters, utils_*, model_openai,
         run_meta, domains/) + runner / verify (honesty gate) / analyze
data/    samples_delegate52 (234 samples) + hybrid_split.json + research_splits
prompts/ FullRewrite domain prompt templates (loaded relative to repo root — run from root)
docs/    HYBRIDPATCH_DESIGN, FINDINGS, goal_state, active_log
```

## Setup

```sh
pip install -r requirements.txt        # 50+ domain parsers (rdkit/biopython/qiskit …)
cp .env.example .env                   # fill OPENAI_API_KEY / OPENCODE_API_KEY
```

## No-API sanity (free)

```sh
PYTHONUTF8=1 python src/test_hybrid_executor.py   # executor byte-level tests
python src/splitters.py                           # splitter coverage self-check
```

## Run a paired experiment (costs API)

Run `hybridpatch` first, then `fullrewrite`, into the same fresh `--out_dir`:

```sh
PYTHONUTF8=1 python src/experiment_runner.py \
  --sample malware6 latex2 --methods hybridpatch fullrewrite \
  --num_round_trips 10 --skip_distractor --model minimax-m3 \
  --out_dir exp_demo --notes "demo"

PYTHONUTF8=1 python src/verify_anchorpatch.py --dir exp_demo   # honesty gate (must PASS)
PYTHONUTF8=1 python src/analyze.py --dir exp_demo --K 10 --critical_theta 0.10
```

Never cite a number before `verify_anchorpatch.py` reproduces it from the raw responses.
See `CLAUDE.md` for the working discipline and `docs/` for design + findings.
