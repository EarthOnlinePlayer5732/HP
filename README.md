# HybridPatch

HybridPatch is a structured editing protocol for applying compact, verifiable changes to multi-file contexts. This repository contains the current unversioned protocol, prompt builder, deterministic executor, validation gate, campaign runtime, provider adapter, and active DELEGATE-52 domain evaluators used by the paper implementation.

## Layout

- `src/hybrid_schema.py` — protocol schema and burden checks
- `src/hybrid_prompt.py` — primary and repair prompts
- `src/hybrid_executor.py` — deterministic patch execution
- `src/hybrid_gate.py` — reference-free validation and completion audit
- `src/relay_core.py` — HybridPatch and FullRewrite relay semantics
- `src/model_openai.py` — DeepSeek, OpenCode, MiniMax, Anthropic, Azure OpenAI, and generic OpenAI-compatible transports
- `src/run_campaign.py` — campaign entry point
- `src/domains/` — active domain evaluators

## Installation

Use Python 3.11 or later in a virtual environment:

```bash
python -m pip install -r requirements.txt
```

The dataset is intentionally not included. Pass an external DELEGATE-52-compatible samples directory with `--samples-root`. GNU make and Graphviz are additionally required by their corresponding domain evaluators.

## Campaign entry point

The paper campaign runner fixes the model to `deepseek-v4-flash` on DeepSeek's official OpenAI-compatible endpoint. It expects a clean Git commit and a key file outside the repository.

```bash
cd src
python -B run_campaign.py   --samples sample_id_1 sample_id_2   --samples-root /absolute/path/to/samples   --methods hybridpatch fullrewrite   --keys-file /absolute/path/to/keys.env   --key-labels KEY_1   --base-url https://api.deepseek.com   --out-dir /absolute/path/to/new-output
```

Use `python -B run_campaign.py --help` for all runtime options. Provider routing outside the fixed paper campaign is exposed by `model_openai.generate` and configured through the corresponding provider environment variables.
