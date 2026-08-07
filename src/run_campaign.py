"""Single entry point for new HybridPatch campaigns and same-command resume."""
import argparse
from collections import defaultdict, deque
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from domains import get_domain
from hybrid_schema import PROTOCOL
from model_openai import (DEEPSEEK_OFFICIAL_BASE_URL,
                          model_runtime_config, normalize_openai_base_url,
                          openai_chat_completions_url)
from simple_runtime_io import (DISPATCH_SCHEMA, RUN_SCHEMA, append_jsonl_owned,
                               all_sample_ids, campaign_lock,
                               create_or_validate_run, git_identity, load_keys,
                               lock_is_held, method_orders, prepare_task_plans,
                               read_json, read_status, utc_now,
                               validate_samples)
from utils_env import load_sample
from verify_campaign import write_quick_summary


_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_WORKER = _HERE / "run_sample.py"
_MODEL = "deepseek-v4-flash"
_REASONING_EFFORT = "high"
_MAX_TOKENS = 131072
_RATE_LIMIT_REQUEUE_COOLDOWN_SECONDS = 5.0


def _dispatch_event(out_dir, event, **fields):
    append_jsonl_owned(Path(out_dir) / "dispatch.jsonl", {
        "schema": DISPATCH_SCHEMA, "created_at": utc_now(),
        "event": event, **fields,
    })


def _configured_concurrency(args):
    """Return the per-invocation total worker ceiling."""
    return int(args.slots_per_key) * len(args.key_labels)


def _reduced_concurrency(current):
    """Apply one rate-limit batch reduction, including the terminal 1 -> 0."""
    current = int(current)
    if current <= 1:
        return 0
    return max(1, math.floor(current * 0.75))


def _rate_limit_cooldown_seconds(status):
    last_error = status.get("last_error") if isinstance(status, dict) else None
    retry_after = (
        last_error.get("rate_limit_retry_after_seconds")
        if isinstance(last_error, dict) else None
    )
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        return max(
            _RATE_LIMIT_REQUEUE_COOLDOWN_SECONDS,
            min(float(retry_after), 300.0),
        )
    return _RATE_LIMIT_REQUEUE_COOLDOWN_SECONDS


def _validate_evaluators(samples, samples_root):
    seen = set()
    for sample in samples:
        loaded, _folder, _states = load_sample(
            sample, samples_folder=str(samples_root) + os.sep)
        sample_type = loaded["sample_type"]
        if sample_type not in seen:
            get_domain(sample_type)
            seen.add(sample_type)


def _base_scientific(samples, orders, args, commit, tree_state, base_url,
                     request_url, samples_root):
    if base_url != DEEPSEEK_OFFICIAL_BASE_URL:
        raise RuntimeError(
            "HybridPatch campaigns require DeepSeek official base URL "
            f"{DEEPSEEK_OFFICIAL_BASE_URL}")
    os.environ["OPENAI_BASE_URL"] = base_url
    runtime = model_runtime_config(
        _MODEL, max_tokens=_MAX_TOKENS,
        reasoning_effort=_REASONING_EFFORT)
    if (runtime.get("provider") != "deepseek_official"
            or runtime.get("transport") != "openai_sdk_stream"
            or runtime.get("base_url") != base_url
            or runtime.get("request_url") != request_url):
        raise RuntimeError(
            "DeepSeek official runtime is not the official compact stream")
    return {
        "samples": list(samples),
        "method_order_by_sample": orders,
        "method_set": sorted(set(args.methods)),
        "round_trips": args.round_trips,
        "seed": args.seed,
        "model": _MODEL,
        "stream": True,
        "stream_options": {"include_usage": True},
        "thinking_mode": runtime.get("thinking_mode"),
        "reasoning_effort": _REASONING_EFFORT,
        "max_tokens": _MAX_TOKENS,
        "include_distractor": True,
        "protocol": PROTOCOL,
        "prompt_builder": "hybrid_prompt.build_hybrid_prompt",
        "executor": "hybrid_executor.apply_hybrid",
        "provider": runtime.get("provider"),
        "transport": runtime.get("transport"),
        "run_git_commit": commit,
        "git_tree_state": tree_state,
        "samples_root": str(samples_root),
    }


def _reject_existing_mismatch(out_dir, base):
    path = Path(out_dir) / "run.json"
    if not path.exists():
        return None
    run = read_json(path)
    if run.get("schema") != RUN_SCHEMA:
        raise RuntimeError("out_dir is not a HybridPatch campaign")
    prior_scientific = dict(run.get("scientific") or {})
    prior = dict(prior_scientific)
    prior.pop("task_plans", None)
    if prior != base:
        raise RuntimeError(
            "existing run.json scientific configuration differs; use a new out_dir")
    return prior_scientific


def _initial_queue(out_dir, samples, orders):
    pending, external = deque(), {}
    terminal = {}
    for sample in samples:
        try:
            status = read_status(out_dir, sample, orders[sample])
        except Exception:
            terminal[sample] = "worker_failed"
            continue
        state = status["state"]
        lock_path = Path(out_dir) / "locks" / f"{sample}.lock"
        held = lock_is_held(lock_path)
        if state == "complete":
            terminal[sample] = state
        elif held:
            external[sample] = status.get("key_label")
        elif state in {
                "pending", "running", "api_incomplete",
                "rate_limit_incomplete"}:
            pending.append(sample)
        else:
            terminal[sample] = state
    return pending, external, terminal


def _worker_env(key, key_label, base_url):
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        "OPENAI_API_KEY": key, "OPENAI_BASE_URL": base_url,
    })
    for name in (
            "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
            "OPENCODE_API_KEY", "OPENCODE_GO_API_KEY", "MINIMAX_API_KEY"):
        env.pop(name, None)
    return env


def _launch_worker(out_dir, sample, key_label, key, base_url):
    command = [
        sys.executable, "-u", "-B", str(_WORKER),
        "--out-dir", str(Path(out_dir).resolve()),
        "--sample", sample, "--key-label", key_label,
    ]
    log_path = Path(out_dir) / "samples" / sample / "worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8", newline="\n")
    try:
        return subprocess.Popen(
            command, cwd=str(_ROOT),
            env=_worker_env(key, key_label, base_url),
            stdin=subprocess.DEVNULL, stdout=log_handle,
            stderr=subprocess.STDOUT)
    finally:
        log_handle.close()


def _wait_and_stop(children, grace_seconds):
    for item in children.values():
        if item["process"].poll() is None:
            item["process"].terminate()
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if all(item["process"].poll() is not None for item in children.values()):
            break
        time.sleep(0.1)
    for item in children.values():
        if item["process"].poll() is None:
            item["process"].kill()
    for item in children.values():
        try:
            item["process"].wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def run_campaign(args):
    out_dir = Path(args.out_dir).resolve()
    samples_root = Path(args.samples_root).resolve()
    if not samples_root.is_dir():
        raise RuntimeError(f"samples root is not a directory: {samples_root}")
    base_url = normalize_openai_base_url(args.base_url)
    request_url = openai_chat_completions_url(base_url)
    samples = (
        all_sample_ids(samples_root) if args.all
        else validate_samples(args.samples, samples_root))
    if args.all and len(samples) != 234:
        raise RuntimeError(f"--all requires exactly 234 samples, found {len(samples)}")
    orders = method_orders(samples, args.methods)
    keys = load_keys(args.keys_file, args.key_labels)
    _validate_evaluators(samples, samples_root)
    commit, tree_state = git_identity(_ROOT)
    base = _base_scientific(
        samples, orders, args, commit, tree_state, base_url, request_url,
        samples_root)

    out_dir.mkdir(parents=True, exist_ok=True)
    with campaign_lock(out_dir):
        run_exists = (out_dir / "run.json").exists()
        if not run_exists:
            foreign = [
                path.name for path in out_dir.iterdir()
                if path.name not in {".campaign.lock", "task_plans"}]
            if foreign:
                raise RuntimeError(
                    "fresh simplified runtime out_dir is not empty: "
                    + ", ".join(sorted(foreign)[:10]))
            plans_dir = out_dir / "task_plans"
            if plans_dir.is_dir():
                expected = {f"{sample}.json" for sample in samples}
                unexpected = [
                    path.name for path in plans_dir.iterdir()
                    if not path.name.startswith(".") and path.name not in expected]
                if unexpected:
                    raise RuntimeError(
                        "incomplete fresh initialization contains unexpected task plans: "
                        + ", ".join(sorted(unexpected)[:10]))
        prior_scientific = _reject_existing_mismatch(out_dir, base)
        task_plans = prepare_task_plans(
            out_dir, samples, args.round_trips, args.seed, samples_root,
            allow_create=not run_exists)
        if prior_scientific is not None:
            if prior_scientific.get("task_plans") != task_plans:
                raise RuntimeError("existing task-plan records differ")
            scientific = prior_scientific
        else:
            scientific = {**base, "task_plans": task_plans}
        execution = {
            "keys_file": str(Path(args.keys_file).resolve()),
            "key_labels": list(args.key_labels),
            "slots_per_key": args.slots_per_key,
            "log_level": args.log_level,
            "base_url": base_url,
            "request_url": request_url,
        }
        _run, created = create_or_validate_run(out_dir, scientific, execution)
        invocation = uuid.uuid4().hex
        maximum_concurrency = _configured_concurrency(args)
        effective_concurrency = maximum_concurrency
        _dispatch_event(
            out_dir, "campaign_started", invocation_id=invocation,
            new_run=created, sample_count=len(samples),
            key_labels=list(args.key_labels), slots_per_key=args.slots_per_key,
            maximum_concurrency=maximum_concurrency,
            effective_concurrency=effective_concurrency)

        pending, external, terminal = _initial_queue(out_dir, samples, orders)
        running = {}
        cooling = {}
        quarantined = {}
        rate_limit_stop = False
        per_key = defaultdict(int)
        for label in external.values():
            if label in keys:
                per_key[label] += 1
        interrupted = {"signum": None}
        prior_handlers = {}

        def request_stop(signum, _frame):
            if interrupted["signum"] is None:
                interrupted["signum"] = signum

        def start_rate_limit_cooldown(sample, status, key_label, source):
            cooldown_seconds = _rate_limit_cooldown_seconds(status)
            cooling[sample] = time.monotonic() + cooldown_seconds
            _dispatch_event(
                out_dir, "sample_rate_limit_cooldown_started",
                invocation_id=invocation, sample=sample,
                key_label=key_label, source=source,
                cooldown_seconds=cooldown_seconds)

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                signum = getattr(signal, name)
                prior_handlers[signum] = signal.signal(signum, request_stop)

        try:
            while pending or running or external or cooling:
                if interrupted["signum"] is not None:
                    break
                if rate_limit_stop and not running and not external:
                    break

                if not rate_limit_stop:
                    now = time.monotonic()
                    for sample, ready_at in list(cooling.items()):
                        if now < ready_at:
                            continue
                        del cooling[sample]
                        pending.append(sample)
                        _dispatch_event(
                            out_dir, "sample_rate_limit_requeued",
                            invocation_id=invocation, sample=sample)

                rate_limited_batch = []
                completed_batch = []

                for sample in list(external):
                    lock_path = out_dir / "locks" / f"{sample}.lock"
                    if lock_is_held(lock_path):
                        continue
                    label = external.pop(sample)
                    if label in keys:
                        per_key[label] = max(0, per_key[label] - 1)
                    try:
                        status = read_status(out_dir, sample, orders[sample])
                    except Exception:
                        terminal[sample] = "worker_failed"
                        continue
                    if status["state"] == "complete":
                        terminal[sample] = "complete"
                        completed_batch.append(sample)
                    elif status["state"] == "rate_limit_incomplete":
                        start_rate_limit_cooldown(
                            sample, status, label, "external_worker")
                        rate_limited_batch.append(sample)
                    elif status["state"] in {"pending", "running", "api_incomplete"}:
                        key_reason = ((status.get("last_error") or {})
                                      .get("key_unusable_reason"))
                        if (status["state"] == "api_incomplete" and key_reason
                                and label in keys and label not in quarantined):
                            quarantined[label] = key_reason
                            _dispatch_event(
                                out_dir, "key_quarantined",
                                invocation_id=invocation, key_label=label,
                                reason=key_reason, sample=sample,
                                source="external_worker")
                        pending.append(sample)
                    else:
                        terminal[sample] = status["state"]

                for sample, item in list(running.items()):
                    code = item["process"].poll()
                    if code is None:
                        continue
                    del running[sample]
                    per_key[item["key_label"]] -= 1
                    try:
                        status = read_status(out_dir, sample, orders[sample])
                        state = status["state"]
                    except Exception as exc:
                        status = {"last_error": None}
                        state = "worker_failed"
                        _dispatch_event(
                            out_dir, "worker_status_invalid",
                            invocation_id=invocation, sample=sample,
                            key_label=item["key_label"],
                            error_type=(
                                f"{type(exc).__module__}."
                                f"{type(exc).__qualname__}"),
                            error_message=(str(exc) or repr(exc))[:2000])
                    _dispatch_event(
                        out_dir, "worker_exited", invocation_id=invocation,
                        sample=sample, key_label=item["key_label"],
                        pid=item["process"].pid, returncode=code,
                        sample_state=state)
                    key_reason = ((status.get("last_error") or {})
                                  .get("key_unusable_reason"))
                    if state == "running":
                        # A normal worker always publishes a terminal sample
                        # state before exiting.  A lock-free stale running state
                        # therefore means external process loss; retry locally.
                        pending.append(sample)
                        _dispatch_event(
                            out_dir, "worker_process_lost",
                            invocation_id=invocation, sample=sample,
                            key_label=item["key_label"], returncode=code)
                    elif state == "rate_limit_incomplete":
                        start_rate_limit_cooldown(
                            sample, status, item["key_label"], "worker")
                        rate_limited_batch.append(sample)
                    elif state == "api_incomplete" and key_reason:
                        label = item["key_label"]
                        if label not in quarantined:
                            quarantined[label] = key_reason
                            _dispatch_event(
                                out_dir, "key_quarantined",
                                invocation_id=invocation, key_label=label,
                                reason=key_reason, sample=sample)
                        pending.append(sample)
                    else:
                        terminal[sample] = state
                        if state == "complete":
                            completed_batch.append(sample)

                active_count = len(running) + len(external)
                if rate_limited_batch:
                    previous = effective_concurrency
                    effective_concurrency = _reduced_concurrency(previous)
                    _dispatch_event(
                        out_dir, "effective_concurrency_changed",
                        invocation_id=invocation,
                        reason="rate_limit_budget_exhausted_batch",
                        previous_effective_concurrency=previous,
                        effective_concurrency=effective_concurrency,
                        maximum_concurrency=maximum_concurrency,
                        exhausted_sample_count=len(rate_limited_batch),
                        exhausted_samples=sorted(rate_limited_batch),
                        active_worker_count=active_count)
                    if effective_concurrency == 0:
                        rate_limit_stop = True
                        _dispatch_event(
                            out_dir, "campaign_rate_limit_stop_requested",
                            invocation_id=invocation,
                            reason="persistent_http_429_at_concurrency_one",
                            exhausted_samples=sorted(rate_limited_batch),
                            active_worker_count=active_count,
                            pending_count=len(pending),
                            cooling_count=len(cooling))
                elif (completed_batch and not rate_limit_stop
                      and effective_concurrency < maximum_concurrency
                      and active_count <= effective_concurrency):
                    previous = effective_concurrency
                    effective_concurrency += 1
                    _dispatch_event(
                        out_dir, "effective_concurrency_changed",
                        invocation_id=invocation,
                        reason="stable_completed_worker_batch",
                        previous_effective_concurrency=previous,
                        effective_concurrency=effective_concurrency,
                        maximum_concurrency=maximum_concurrency,
                        completed_sample_count=len(completed_batch),
                        completed_samples=sorted(completed_batch),
                        active_worker_count=active_count)

                healthy = [
                    label for label in args.key_labels
                    if label not in quarantined]
                launched = True
                while (pending and healthy and launched and not rate_limit_stop
                       and len(running) + len(external)
                       < effective_concurrency):
                    launched = False
                    for label in healthy:
                        if not pending:
                            break
                        if (len(running) + len(external)
                                >= effective_concurrency):
                            break
                        if per_key[label] >= args.slots_per_key:
                            continue
                        sample = pending.popleft()
                        try:
                            process = _launch_worker(
                                out_dir, sample, label, keys[label], base_url)
                        except Exception as exc:
                            terminal[sample] = "worker_failed"
                            _dispatch_event(
                                out_dir, "worker_launch_failed",
                                invocation_id=invocation, sample=sample,
                                key_label=label,
                                error_type=(
                                    f"{type(exc).__module__}."
                                    f"{type(exc).__qualname__}"),
                                error_message=(str(exc) or repr(exc))[:2000])
                            launched = True
                            continue
                        running[sample] = {
                            "process": process, "key_label": label}
                        per_key[label] += 1
                        _dispatch_event(
                            out_dir, "worker_started",
                            invocation_id=invocation, sample=sample,
                            key_label=label, pid=process.pid)
                        launched = True

                if rate_limit_stop and not running and not external:
                    break
                if pending and not healthy and not running and not external:
                    break
                if pending or running or external or cooling:
                    sleep_seconds = 0.2
                    if cooling and not pending and not running and not external:
                        sleep_seconds = min(
                            sleep_seconds,
                            max(0.0, min(cooling.values()) - time.monotonic()),
                        )
                    time.sleep(sleep_seconds)
        finally:
            for signum, handler in prior_handlers.items():
                signal.signal(signum, handler)

        if interrupted["signum"] is not None:
            _dispatch_event(
                out_dir, "campaign_interrupt_observed", invocation_id=invocation,
                signal=interrupted["signum"], pending_count=len(pending),
                running_count=len(running), external_count=len(external))
            _wait_and_stop(running, args.grace_seconds)
            _dispatch_event(
                out_dir, "campaign_interrupted", invocation_id=invocation,
                signal=interrupted["signum"], pending_count=len(pending),
                running_count=len(running), external_count=len(external))
            write_quick_summary(
                out_dir, "interrupted", skip_samples=set(external))
            return 130 if interrupted["signum"] == signal.SIGINT else 143

        summary_hint = (
            "rate_limited_incomplete" if rate_limit_stop else "incomplete")
        summary = write_quick_summary(out_dir, summary_hint)
        if (not rate_limit_stop
                and all(item["complete"] for item in summary["samples"].values())):
            summary = write_quick_summary(out_dir)
            state, code = "complete", 0
        elif rate_limit_stop:
            state, code = "rate_limited_incomplete", 2
        else:
            state, code = "incomplete", 2
        _dispatch_event(
            out_dir, "campaign_finished", invocation_id=invocation,
            campaign_state=state, pending_count=len(pending),
            cooling_count=len(cooling), quarantined_keys=sorted(quarantined),
            maximum_concurrency=maximum_concurrency,
            effective_concurrency=effective_concurrency,
            stop_reason=(
                "persistent_http_429" if rate_limit_stop else None))
        return code


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--samples", nargs="+")
    selection.add_argument("--all", action="store_true")
    parser.add_argument(
        "--methods", nargs="+", required=True,
        choices=("hybridpatch", "fullrewrite"))
    parser.add_argument("--round-trips", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--samples-root", required=True)
    parser.add_argument("--keys-file", required=True)
    parser.add_argument("--key-labels", nargs="+", required=True)
    parser.add_argument("--slots-per-key", type=int, default=10)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--log-level", choices=("normal", "quiet"), default="normal")
    parser.add_argument("--grace-seconds", type=float, default=20.0)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.round_trips < 1:
        parser.error("--round-trips must be positive")
    if args.slots_per_key < 1:
        parser.error("--slots-per-key must be positive")
    if args.grace_seconds < 0:
        parser.error("--grace-seconds must be non-negative")
    try:
        return run_campaign(args)
    except Exception as exc:
        print(f"run_campaign: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
