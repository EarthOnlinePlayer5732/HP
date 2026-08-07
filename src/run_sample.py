"""One-process, one-sample worker for the HybridPatch campaign runtime."""
import argparse
import os
from pathlib import Path
import signal
import traceback

from hybrid_schema import PROTOCOL
from model_openai import (DEEPSEEK_OFFICIAL_BASE_URL,
                          model_runtime_config, normalize_openai_base_url,
                          openai_chat_completions_url)
from relay_core import (EvaluatorIncompleteError, PreservationViolationError,
                        RelayHooks, run_method)
from simple_api_recorder import SimpleApiRecorder
from simple_runtime_io import (LocalEvidenceError, commit_round_trip,
                               load_resume_state, load_task_plan, method_dir, read_json,
                               read_status, sample_dir, sample_lock, utc_now,
                               write_status)


EXIT_COMPLETE = 0
EXIT_API_INCOMPLETE = 10
EXIT_EVALUATOR_FAILED = 11
EXIT_PRESERVATION_INVALID = 12
EXIT_WORKER_FAILED = 13
EXIT_RATE_LIMIT_INCOMPLETE = 14
EXIT_INTERRUPTED = 130


class WorkerInterrupted(RuntimeError):
    pass


def _signal_handler(signum, _frame):
    raise WorkerInterrupted(f"worker received signal {signum}")


def _append_worker_log(path, message):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{utc_now()} {message}\n")
        handle.flush()


def _ordinary_rate_limit_exhaustion(exc):
    if getattr(exc, "_hybridpatch_key_unusable_reason", None):
        return None
    attempts = list(getattr(exc, "transport_attempts", None) or [])
    if not attempts or not isinstance(attempts[-1], dict):
        return None
    final = attempts[-1]
    if (final.get("status") != "retryable_error"
            or final.get("error_type") != "rate_limit"
            or final.get("http_status") != 429
            or final.get("rate_limit_budget_consumed") is not True
            or not isinstance(
                final.get("rate_limit_budget_attempt_index"), int)
            or isinstance(final.get("rate_limit_budget_attempt_index"), bool)
            or final.get("rate_limit_budget_attempt_index") < 1):
        return None
    return final


def _error_record(exc):
    rate_limit_attempt = _ordinary_rate_limit_exhaustion(exc)
    return {
        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": (str(exc) or repr(exc))[:4000],
        "traceback": "".join(traceback.format_exception(exc))[-12000:],
        "api_failure_path": getattr(
            exc, "_hybridpatch_simple_failure_path", None),
        "api_failure_journal_error": getattr(
            exc, "_hybridpatch_simple_failure_journal_error", None),
        "key_unusable_reason": getattr(
            exc, "_hybridpatch_key_unusable_reason", None),
        "http_status": getattr(exc, "status_code", None),
        "rate_limit_budget_exhausted": rate_limit_attempt is not None,
        "rate_limit_retry_after_seconds": (
            rate_limit_attempt.get("retry_after_seconds")
            if rate_limit_attempt is not None else None),
    }


def _is_api_exception(exc):
    return bool(
        getattr(exc, "_hybridpatch_simple_failure_path", None)
        or hasattr(exc, "transport_attempts")
        or type(exc).__module__.endswith("model_openai")
    )


def run_sample(out_dir, sample, key_label, *, generate_impl=None):
    out_dir = Path(out_dir).resolve()
    run = read_json(out_dir / "run.json")
    scientific = run["scientific"]
    execution = run.get("execution")
    if not isinstance(execution, dict):
        raise LocalEvidenceError("run.json execution configuration is invalid")
    base_url = normalize_openai_base_url(execution.get("base_url"))
    request_url = openai_chat_completions_url(base_url)
    if request_url != execution.get("request_url"):
        raise LocalEvidenceError("run.json execution request URL is invalid")
    os.environ["OPENAI_BASE_URL"] = base_url
    for name in (
            "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
            "OPENCODE_API_KEY", "OPENCODE_GO_API_KEY", "MINIMAX_API_KEY"):
        os.environ.pop(name, None)
    samples_root = Path(scientific["samples_root"])
    if not samples_root.is_absolute() or not samples_root.is_dir():
        raise LocalEvidenceError("run.json samples_root must be an existing absolute directory")
    methods = list(scientific["method_order_by_sample"][sample])
    task_plan, _plan_payload = load_task_plan(out_dir, sample)
    round_trips = int(scientific["round_trips"])
    seed = int(scientific["seed"])
    include_distractor = bool(scientific["include_distractor"])
    model = scientific["model"]
    max_tokens = scientific["max_tokens"]
    reasoning_effort = scientific["reasoning_effort"]
    runtime = model_runtime_config(
        model, max_tokens=max_tokens, reasoning_effort=reasoning_effort)
    if (scientific.get("protocol") != PROTOCOL
            or base_url != DEEPSEEK_OFFICIAL_BASE_URL
            or runtime.get("provider") != "deepseek_official"
            or scientific.get("provider") != "deepseek_official"
            or scientific.get("thinking_mode") != "enabled"
            or runtime.get("thinking_mode") != "enabled"
            or runtime.get("base_url") != base_url
            or runtime.get("request_url") != request_url
            or scientific.get("transport") != "openai_sdk_stream"
            or runtime.get("transport") != "openai_sdk_stream"):
        raise LocalEvidenceError(
            "worker execution route is not the frozen DeepSeek official stream")
    log_path = sample_dir(out_dir, sample) / "worker.log"

    with sample_lock(out_dir, sample):
        status = read_status(out_dir, sample, methods)
        status["attempt"] = int(status.get("attempt") or 0) + 1
        status.update(
            state="running", key_label=key_label,
            worker_pid=os.getpid(), started_at=utc_now(),
            finished_at=None, last_error=None)
        write_status(out_dir, sample, status)
        _append_worker_log(
            log_path, f"start sample={sample} key_label={key_label} "
                      f"attempt={status['attempt']}")

        try:
            for method in methods:
                method_path = method_dir(out_dir, sample, method)
                checkpoint, completed = load_resume_state(
                    out_dir, sample, method, round_trips, seed,
                    include_distractor, samples_root, task_plan=task_plan)
                if completed >= round_trips:
                    status["methods"][method] = "complete"
                    write_status(out_dir, sample, status)
                    continue
                status["methods"][method] = "running"
                write_status(out_dir, sample, status)
                recorder = SimpleApiRecorder(
                    method_path, sample, method, model,
                    generate_impl=generate_impl)

                def log_event(event):
                    _append_worker_log(
                        log_path,
                        f"commit method={method} rt={event['round_trip']} "
                        f"score={event.get('backward_score')}")

                hooks = RelayHooks(
                    commit_round_trip=lambda rows, ckpt, path=method_path:
                        commit_round_trip(path, rows, ckpt),
                    set_step=recorder.set_step,
                    log=log_event,
                )
                run_method(
                    method, sample, task_plan,
                    num_round_trips=round_trips, seed=seed,
                    include_distractor=include_distractor,
                    model=model, max_tokens=max_tokens,
                    generate_fn=recorder.generate, hooks=hooks,
                    resume_state=checkpoint,
                    reasoning_effort=reasoning_effort,
                    stop_on_preservation_violation=True,
                    samples_root=str(samples_root), validate_transport=False)
                status["methods"][method] = "complete"
                write_status(out_dir, sample, status)

            status.update(
                state="complete", finished_at=utc_now(), last_error=None)
            write_status(out_dir, sample, status)
            _append_worker_log(log_path, f"complete sample={sample}")
            return EXIT_COMPLETE
        except WorkerInterrupted as exc:
            for method, value in list(status["methods"].items()):
                if value == "running":
                    status["methods"][method] = "incomplete"
            status.update(
                state="pending", finished_at=utc_now(),
                last_error=_error_record(exc))
            write_status(out_dir, sample, status)
            _append_worker_log(log_path, f"interrupted sample={sample}")
            return EXIT_INTERRUPTED
        except EvaluatorIncompleteError as exc:
            status["methods"][exc.method] = "incomplete"
            status.update(
                state="evaluator_failed", finished_at=utc_now(),
                last_error=_error_record(exc))
            write_status(out_dir, sample, status)
            _append_worker_log(log_path, f"evaluator_failed sample={sample}")
            return EXIT_EVALUATOR_FAILED
        except PreservationViolationError as exc:
            status["methods"][exc.method] = "incomplete"
            status.update(
                state="preservation_invalid", finished_at=utc_now(),
                last_error=_error_record(exc))
            write_status(out_dir, sample, status)
            _append_worker_log(log_path, f"preservation_invalid sample={sample}")
            return EXIT_PRESERVATION_INVALID
        except Exception as exc:
            for method, value in list(status["methods"].items()):
                if value == "running":
                    status["methods"][method] = "incomplete"
            if _ordinary_rate_limit_exhaustion(exc) is not None:
                state = "rate_limit_incomplete"
            else:
                state = (
                    "api_incomplete" if _is_api_exception(exc)
                    else "worker_failed")
            status.update(
                state=state, finished_at=utc_now(),
                last_error=_error_record(exc))
            write_status(out_dir, sample, status)
            _append_worker_log(log_path, f"{state} sample={sample}")
            if state == "rate_limit_incomplete":
                return EXIT_RATE_LIMIT_INCOMPLETE
            return (
                EXIT_API_INCOMPLETE
                if state == "api_incomplete" else EXIT_WORKER_FAILED)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--key-label", required=True)
    args = parser.parse_args(argv)
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)
    try:
        return run_sample(args.out_dir, args.sample, args.key_label)
    except BaseException as exc:
        try:
            log_path = sample_dir(args.out_dir, args.sample) / "worker.log"
            _append_worker_log(
                log_path,
                "bootstrap_failed "
                + "".join(traceback.format_exception(exc))[-12000:].replace("\n", "\\n"))
        except Exception:
            pass
        if isinstance(exc, (KeyboardInterrupt, WorkerInterrupted)):
            return EXIT_INTERRUPTED
        return EXIT_WORKER_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
