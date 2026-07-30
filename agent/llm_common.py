"""
Cross-cutting plumbing shared by every LLM-calling path (agent.claude, agent.llm_api).

No dependency on either of those modules or on agent.runner — this is a leaf module,
so agent.claude and agent.llm_api can both import from here without any risk of a
circular import.
"""

import json
import threading
import time

from app.logging_setup import get_model_logger

# ---------------------------------------------------------------------------
# Progress events
# ---------------------------------------------------------------------------

# The web UI runs agent.runner as a subprocess and folds these events into its
# in-memory run state to drive the run drawer. Each event is one line on stdout,
# sentinel-prefixed so the parent can pick them out from ordinary log output.
PROGRESS_SENTINEL = "SCOUT_PROGRESS "


def emit(**event) -> None:
    """Emit one structured progress event for the web UI to parse.

    Written as a single sentinel-prefixed JSON line and flushed immediately so
    the parent process sees stage transitions live rather than at run end.
    """
    print(PROGRESS_SENTINEL + json.dumps(event), flush=True)


def emit_log(msg: str, level: str = "info", index: int | None = None) -> None:
    """Emit one line for the run drawer's scrolling event-log pane.

    ``level`` drives the log line's color in the UI ("info", "good", "drop",
    "head"); ``index`` optionally ties the line to a specific search group.
    The web UI timestamps each line on receipt (see app/main.py::_apply_event)
    rather than trusting a value from this subprocess, so no timestamp is sent.
    """
    emit(scope="log", msg=msg, level=level, index=index)


# ---------------------------------------------------------------------------
# Setup validation
# ---------------------------------------------------------------------------

class SetupError(Exception):
    """Raised when required user setup is missing, malformed, or unreachable.

    Raised both by agent.runner's check_setup (config/CLI/resume-file problems)
    and agent.llm_api's _verify_api_llm (api-endpoint problems) — neither is the
    other's dependency, so this lives here rather than forcing a circular import
    either direction. The CLI entry point turns this into a clean `sys.exit`; the
    web UI catches it and renders the message in the run drawer instead of
    launching the pipeline, so both callers share one exception type.
    """


# ---------------------------------------------------------------------------
# Model-interaction logging (opt-in via --log-model-calls)
# ---------------------------------------------------------------------------

_log_model_calls = False


def log_model_call(call_type: str, model: str, system_prompt: str,
                   user_message: str) -> None:
    """Append one Claude-call record to the model-interaction log, if enabled.

    Human-readable blocks (not JSON — escaped newlines would make the
    multi-KB markdown prompts unreadable): a header line with timestamp,
    pass name, and model, then the full system prompt and user message
    verbatim under labeled rules. A no-op unless the run was started with
    --log-model-calls.
    """
    if not _log_model_calls:
        return
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    get_model_logger().info(
        "=" * 78 + "\n"
        f"{ts} | {call_type} | {model}\n"
        + "-" * 30 + " system prompt " + "-" * 33 + "\n"
        f"{system_prompt}\n"
        + "-" * 30 + " user message " + "-" * 34 + "\n"
        f"{user_message}\n"
    )


# ---------------------------------------------------------------------------
# Token tracking
# ---------------------------------------------------------------------------

_tokens: dict = {
    "input": 0,
    "output": 0,
    "cache_read": 0,
    "cache_write": 0,
    "cost_usd": 0.0,
    "calls": 0,
}
_tokens_lock = threading.Lock()


def _add_usage(usage: dict, cost_usd: float) -> None:
    """Accumulate token counts from a claude subprocess result (thread-safe)."""
    with _tokens_lock:
        _tokens["input"] += usage.get("input_tokens", 0)
        _tokens["output"] += usage.get("output_tokens", 0)
        _tokens["cache_read"] += usage.get("cache_read_input_tokens", 0)
        _tokens["cache_write"] += usage.get("cache_creation_input_tokens", 0)
        _tokens["cost_usd"] += cost_usd
        _tokens["calls"] += 1


def print_token_summary() -> None:
    """Print accumulated token/cost totals."""
    t = _tokens
    total_input = t["input"] + t["cache_read"] + t["cache_write"]
    print("\n" + "=" * 55)
    print("  TOKEN USAGE SUMMARY")
    print("=" * 55)
    print(f"  API calls          : {t['calls']}")
    print(f"  Input tokens       : {t['input']:,}  (fresh)")
    print(f"  Cache read tokens  : {t['cache_read']:,}")
    print(f"  Cache write tokens : {t['cache_write']:,}")
    print(f"  Output tokens      : {t['output']:,}")
    print(f"  Total input equiv  : {total_input:,}")
    print(f"  Estimated cost     : ${t['cost_usd']:.4f}")
    print("=" * 55)
