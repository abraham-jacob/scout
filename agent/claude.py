"""
Everything that talks to the Claude CLI: the Pass 1 browser scrape (run_claude)
and the headless Pass 2/3 claude-backend call (_run_claude_headless), plus the
subprocess plumbing and model constants only they need.
"""

import functools
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from agent.llm_common import _add_usage, emit_log, log_model_call

BASE_DIR = Path(__file__).parent.parent

# Pass 1 (browser scrape) and Pass 2 (description cleaning) both run on Haiku —
# cheap and mechanical. Pass 3 (enrichment/scoring) runs on Sonnet — the
# classification, summarization, and fit judgment are the quality-sensitive steps.
SCRAPER_MODEL = "claude-haiku-4-5-20251001"
CLEAN_MODEL   = "claude-haiku-4-5-20251001"
ENRICH_MODEL  = "claude-sonnet-4-6"

# Which Claude model each headless pass uses when backend == "claude". On the
# api backend both passes use the single configured [llm.api] model.
_PASS_CLAUDE_MODEL = {"clean": CLEAN_MODEL, "enrich": ENRICH_MODEL}

# The clean/enrich calls are structured extraction against an explicit rubric;
# extended thinking adds ~1.5K billed-but-invisible output tokens per call
# without improving them, so it is disabled for those subprocesses. The browser
# scrape keeps thinking — it is an agentic multi-step task.
_NO_THINKING_ENV = {**os.environ, "MAX_THINKING_TOKENS": "0"}

# Hard wall-clock cap on each claude subprocess (the browser scrape and each
# enrichment call). Past this we kill the subprocess so a runaway or stuck agent
# can't hang the run indefinitely.
SUBPROCESS_TIMEOUT_S = 240  # 4 minutes


# ---------------------------------------------------------------------------
# Cross-platform subprocess helpers
# ---------------------------------------------------------------------------

# Give each claude subprocess its own process group so the watchdog can kill the
# whole tree (the browser agent spawns children). POSIX uses a new session;
# Windows uses CREATE_NEW_PROCESS_GROUP — the nearest equivalent.
if os.name == "nt":
    _NEW_GROUP_KWARGS = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
else:
    _NEW_GROUP_KWARGS = {"start_new_session": True}


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Hard-kill a subprocess and every child it spawned, cross-platform.

    POSIX SIGKILLs the process group; Windows has no group-signal equivalent,
    so ``taskkill /T`` walks and kills the tree. Best-effort — losing a race
    with a process that already exited is fine.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@functools.lru_cache(maxsize=None)
def claude_executable() -> str:
    """Absolute path to the ``claude`` CLI, resolved once via PATH.

    Windows installs the CLI as a .cmd shim that ``subprocess`` can't find by
    bare name; ``shutil.which`` honors PATHEXT and returns the real path, which
    we hand to subprocess directly. Raises FileNotFoundError if it isn't
    installed / on PATH (validate_setup surfaces this as a clean startup error).
    """
    resolved = shutil.which("claude")
    if resolved is None:
        raise FileNotFoundError(
            "'claude' CLI not found on PATH. Install Claude Code and make sure "
            "the `claude` command is on your PATH, then re-run."
        )
    return resolved


# ---------------------------------------------------------------------------
# Headless call (Pass 2/3 claude backend)
# ---------------------------------------------------------------------------

def _run_claude_headless(model: str, system_prompt: str,
                         user_message: str) -> str | None:
    """Run one headless `claude --print --output-format json` call.

    The shared subprocess path for the clean and enrich passes: extended
    thinking off, dynamic system-prompt sections excluded, hard-capped at
    SUBPROCESS_TIMEOUT_S. Accumulates usage/cost into _tokens and returns the
    envelope's `result` text, or None on timeout / subprocess / parse failure.
    """
    cmd = [
        claude_executable(),
        "--print",
        "--model", model,
        "--exclude-dynamic-system-prompt-sections",
        "--system-prompt", system_prompt,
        "--output-format", "json",
        user_message,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_S, env=_NO_THINKING_ENV,
        )
        envelope = json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        print(f"  claude {model} call timed out (> {SUBPROCESS_TIMEOUT_S}s)",
              file=sys.stderr)
        return None
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        print(f"  claude {model} call failed: {exc}", file=sys.stderr)
        return None

    _add_usage(
        envelope.get("usage", {}),
        envelope.get("total_cost_usd", envelope.get("cost_usd", 0.0)),
    )
    return envelope.get("result", "")


# ---------------------------------------------------------------------------
# Pass 1 — browser scrape
# ---------------------------------------------------------------------------

def _build_scrape_cmd(system_prompt_file: Path, user_message: str) -> list[str]:
    """Read the scrape system prompt, log the call, and build the subprocess cmd."""
    system_prompt = system_prompt_file.read_text()
    log_model_call("scrape", SCRAPER_MODEL, system_prompt, user_message)
    return [
        claude_executable(),
        "--print",
        "--model", SCRAPER_MODEL,
        "--verbose",
        "--chrome",
        "--dangerously-skip-permissions",
        "--exclude-dynamic-system-prompt-sections",
        "--system-prompt", system_prompt,
        "--output-format", "stream-json",
        user_message,
    ]


def _spawn_scrape_subprocess(cmd: list[str]) -> subprocess.Popen:
    """Launch the browser-scrape subprocess in its own process group."""
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(BASE_DIR),
        **_NEW_GROUP_KWARGS,  # own process group so we can kill the whole tree
    )


def _run_scrape_watchdog(proc: subprocess.Popen) -> tuple[threading.Timer, threading.Event]:
    """Start the hard-kill-on-timeout guard for a scrape subprocess.

    Returns the started (not yet cancelled) timer and the event it sets, so
    the caller can check whether it fired and cancel it once the subprocess
    finishes normally.
    """
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        _kill_process_tree(proc)

    watchdog = threading.Timer(SUBPROCESS_TIMEOUT_S, _kill_on_timeout)
    watchdog.start()
    return watchdog, timed_out


def _stream_scrape_events(proc: subprocess.Popen) -> tuple[str, dict]:
    """Read and print the scrape subprocess's streamed JSON events live.

    Prints a formatted line per event type (assistant text/tool_use,
    tool_result, system) as they arrive; raw non-JSON lines are printed
    as-is. Returns (text_output, envelope) from the terminal "result" event.
    """
    text_output = ""
    envelope = {}
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(f"[raw] {line}", flush=True)
            continue

        event_type = event.get("type", "")

        if event_type == "assistant":
            # Print each content block as it arrives
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    print(f"[agent] {block['text'][:200]}", flush=True)
                elif block.get("type") == "tool_use":
                    print(f"[tool_use] {block.get('name')} — input: {str(block.get('input',''))[:150]}", flush=True)
        elif event_type == "tool_result":
            content = event.get("content", "")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
            print(f"[tool_result] {str(content)[:200]}", flush=True)
        elif event_type == "result":
            envelope = event
            text_output = event.get("result", "")
        elif event_type == "system":
            print(f"[system] {event.get('subtype','')} — {str(event)[:150]}", flush=True)

    return text_output, envelope


def _check_scrape_result(proc: subprocess.Popen, timed_out: threading.Event,
                         elapsed: float) -> None:
    """Raise on a watchdog kill, else report the exit code/stderr.

    Called after proc.wait() so proc.returncode and proc.stderr are final.
    """
    if timed_out.is_set():
        print(f"[ERROR] browser scrape exceeded {SUBPROCESS_TIMEOUT_S}s "
              f"({elapsed:.0f}s) — subprocess group killed.", file=sys.stderr)
        raise TimeoutError(
            f"browser scrape exceeded {SUBPROCESS_TIMEOUT_S // 60} min and was killed"
        )

    stderr_out = proc.stderr.read()
    if proc.returncode != 0:
        print(f"Scrape agent exited with error (code {proc.returncode}):\n{stderr_out}", file=sys.stderr)
    elif stderr_out.strip():
        print(f"stderr: {stderr_out[:300]}", file=sys.stderr)


def _report_scrape_usage(envelope: dict, elapsed: float) -> None:
    """Accumulate the scrape's usage/cost and print the summary line."""
    usage = envelope.get("usage", {})
    cost = envelope.get("total_cost_usd", envelope.get("cost_usd", 0.0))
    _add_usage(usage, cost)
    in_tok = usage.get("input_tokens", 0)
    out_tok = usage.get("output_tokens", 0)
    cache_r = usage.get("cache_read_input_tokens", 0)
    print(
        f"Scrape done in {elapsed:.0f}s — "
        f"in={in_tok:,} out={out_tok:,} cache_read={cache_r:,} cost=${cost:.4f}",
        flush=True,
    )


def run_claude(system_prompt_file: Path, user_message: str) -> str:
    """
    Invoke the browser scrape subprocess: `claude --print --chrome` on the
    scraper model with the given system prompt and user message. Streams each
    output event to stdout in real time. Token usage is accumulated into _tokens.
    """
    print("Starting browser scrape subprocess...", flush=True)
    t0 = time.monotonic()
    cmd = _build_scrape_cmd(system_prompt_file, user_message)
    proc = _spawn_scrape_subprocess(cmd)
    watchdog, timed_out = _run_scrape_watchdog(proc)
    text_output, envelope = _stream_scrape_events(proc)
    proc.wait()
    watchdog.cancel()
    elapsed = time.monotonic() - t0
    _check_scrape_result(proc, timed_out, elapsed)
    _report_scrape_usage(envelope, elapsed)
    return text_output
