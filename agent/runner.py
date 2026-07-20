"""
Scout agent runner.

Three Claude passes, orchestrated here:

Pass 1 — Browser scrape (Haiku, system_prompt.md)
    A browser subprocess navigates LinkedIn and pulls EVERY job on page 1 into
    a Downloads/scout_<run_id>.json blob download via the Voyager API, which the
    runner reads directly from the Downloads folder. It does no filtering and no
    description cleaning — the browser agent is already complex (privacy-filter
    handoff, blob-download, virtualized cards) and we deliberately keep it
    mechanical. description_raw is stored as-is from the API.

Pass 2 — Description cleaning (Haiku, parallel, clean_prompt.md)
    One headless Haiku call per surviving job strips EEO boilerplate, benefits
    copy, and generic company marketing from description_raw, producing
    description_clean. Runs after the deterministic filters so we never clean
    jobs we're going to drop anyway. Cheaper Haiku input tokens here save the
    more expensive Sonnet input tokens in Pass 3.

Pass 3 — Per-job enrichment (Sonnet, parallel, enrichment_prompt.md)
    One headless Sonnet call per job classifies it into one of the configured
    role types (profiles/config.toml [[roles]]) or Other, writes a 2–4 sentence
    description_summary, tags it, and scores it against the candidate's profiles.
    Uses description_clean so the model sees only the signal, not the noise.
    Jobs classified Other (or that fail to enrich) are dropped; the rest are saved.

Passes 2 and 3 are the two "headless" passes and run on a configurable backend
(profiles/config.toml [llm] backend): the default "claude" shells out to the
`claude` CLI, while "local" routes both through run_headless() to a local
OpenAI-compatible server (e.g. Ollama). Pass 1 always runs on Claude — it drives
the browser and is agentic, which a local text model can't do.

Usage:
    python -m agent.runner                 # scrapes every profiles/config.toml [[linkedin_searches]] entry
    python -m agent.runner --url <url>     # scrape one ad-hoc URL, ignoring config
"""

import argparse
import functools
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from app.config import load_config, load_roles
from app.database import init_db
from app.logging_setup import get_model_logger, setup_logging
from agent.tools import create_scrape_run, save_jobs, get_existing_job_ids

BASE_DIR = Path(__file__).parent.parent
PROMPT_DIR = BASE_DIR / "agent"
SYSTEM_PROMPT_FILE = PROMPT_DIR / "system_prompt.md"
CLEAN_PROMPT_FILE  = PROMPT_DIR / "clean_prompt.md"
ENRICH_PROMPT_FILE = PROMPT_DIR / "enrichment_prompt.md"

# Personal match-scoring artifacts (git-ignored; see profiles/README.md).
# Scoring activates only when resume.md and every profile file referenced by
# the roles config exist. Per-role profile files come from profiles/config.toml
# (see app/config.py); a role may omit its profile and score on resume alone.
PROFILES_DIR = BASE_DIR / "profiles"
RESUME_FILE = PROFILES_DIR / "resume.md"
CRITERIA_FILE = PROFILES_DIR / "criteria.md"

# Pass 1 (browser scrape) and Pass 2 (description cleaning) both run on Haiku —
# cheap and mechanical. Pass 3 (enrichment/scoring) runs on Sonnet — the
# classification, summarization, and fit judgment are the quality-sensitive steps.
SCRAPER_MODEL = "claude-haiku-4-5-20251001"
CLEAN_MODEL   = "claude-haiku-4-5-20251001"
ENRICH_MODEL  = "claude-sonnet-4-6"

# The Pass 2/Pass 3 worker-pool width is configurable per backend via
# [llm] max_workers (config.max_workers). It's a knob because the right value
# depends on the active backend: a Claude run trades wall-clock against
# duplicate prompt-cache writes of the shared system prompt, while a local
# server is bounded by its own VRAM/throughput (a 16GB box may only manage 1).

# The clean/enrich calls are structured extraction against an explicit rubric;
# extended thinking adds ~1.5K billed-but-invisible output tokens per call
# without improving them, so it is disabled for those subprocesses. The browser
# scrape keeps thinking — it is an agentic multi-step task.
_NO_THINKING_ENV = {**os.environ, "MAX_THINKING_TOKENS": "0"}

# Hard wall-clock cap on each claude subprocess (the browser scrape and each
# enrichment call). Past this we kill the subprocess so a runaway or stuck agent
# can't hang the run indefinitely.
SUBPROCESS_TIMEOUT_S = 240  # 4 minutes

# Where the agent hands off the downloaded job batch: the browser saves it to
# the Downloads folder and the runner reads it straight from there — no shell,
# no move to /tmp (see load_downloaded_jobs). This is what makes the handoff
# work identically on Windows/macOS/Linux.

# How long load_downloaded_jobs waits for the blob download to land before
# giving up. Chrome writes a .crdownload temp first and renames to the final
# name on completion, so seeing the final name means the write finished. This
# poll replaces the wait-loop the sub-agent used to run in bash.
DOWNLOAD_WAIT_S = 15
DOWNLOAD_POLL_S = 0.5


def download_dir() -> Path:
    """Directory the browser saves the scrape blob into (from config).

    Defaults to the OS Downloads folder (``~/Downloads``, correct on
    Windows/macOS/Linux) and is overridable via [scrape] download_dir in
    profiles/config.toml. ``~`` is expanded on every call so the resolved path
    tracks config changes without a module reload.
    """
    return Path(load_config().download_dir).expanduser()

# ---------------------------------------------------------------------------
# Progress events
# ---------------------------------------------------------------------------

# The web UI runs this module as a subprocess and folds these events into its
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_downloaded_jobs(run_id: str) -> dict | None:
    """Poll the Downloads folder for the run's blob, read it, and remove it.

    The scrape sub-agent writes scout_<run_id>.json to the Downloads folder via
    a browser blob download and returns only a status line — the descriptions
    never come back through the extension (the privacy filter blocks large
    javascript_tool returns). We poll for the file (cross-platform, no shell —
    this replaces the agent's old bash wait-loop + move to /tmp), parse it, and
    delete it so the folder doesn't accumulate run files. A parse failure is
    retried until the deadline in case we caught Chrome mid-write. Returns None
    if the file never appears within DOWNLOAD_WAIT_S or can't be parsed.
    """
    path = download_dir() / f"scout_{run_id}.json"
    deadline = time.monotonic() + DOWNLOAD_WAIT_S
    while True:
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                data = None  # possibly caught mid-write; retry until the deadline
            if data is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
                return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(DOWNLOAD_POLL_S)


def _file_job_to_record(job_id: str, obj: dict) -> dict:
    """Map a downloaded window.__jobs entry to the save_jobs schema.

    description_clean is added by Pass 2 (clean_one); role_type,
    description_summary, tags, and scores are added by Pass 3 (enrich_one).
    """
    return {
        "job_id": job_id,
        "title": obj.get("title"),
        "company": obj.get("company"),
        "location": obj.get("location"),
        "linkedin_url": f"https://www.linkedin.com/jobs/view/{job_id}",
        "apply_platform": obj.get("apply_platform", "other"),
        "apply_url": obj.get("apply_url"),
        "salary_range": obj.get("salary_range"),
        "description_raw": obj.get("description_raw"),
    }


def _extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating stray prose around it."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}


# ---------------------------------------------------------------------------
# Headless-pass backend dispatch (Pass 2 clean + Pass 3 enrich)
# ---------------------------------------------------------------------------

# Which Claude model each headless pass uses when backend == "claude". On the
# local backend both passes use the single configured [llm.local] model.
_PASS_CLAUDE_MODEL = {"clean": CLEAN_MODEL, "enrich": ENRICH_MODEL}


def run_headless(pass_name: str, system_prompt: str, user_message: str) -> str | None:
    """Run one headless structured call for Pass 2/3 on the configured backend.

    Dispatches to Claude (a `claude --print` subprocess) or a local
    OpenAI-compatible server (e.g. Ollama) according to [llm] backend in the
    config. pass_name is "clean" or "enrich". Handles model-call logging and
    token/cost accounting internally and returns the raw model result text (the
    JSON blob the caller parses with _extract_json), or None on any failure so
    the caller can fall back gracefully. Pass 1 (the browser scrape) does not go
    through here — it always runs on Claude via run_claude.
    """
    config = load_config()
    if config.llm_backend == "local":
        model = config.local_model
        log_model_call(pass_name, model, system_prompt, user_message)
        return _run_local_llm(config, pass_name, model, system_prompt,
                              user_message)
    model = _PASS_CLAUDE_MODEL[pass_name]
    log_model_call(pass_name, model, system_prompt, user_message)
    return _run_claude_headless(model, system_prompt, user_message)


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


# A non-streaming request gives Ollama nothing to write until generation is
# fully done, so a client-side timeout goes completely unnoticed server-side
# — the server keeps computing a response nobody will read, and the next
# request queues up behind it, cascading timeouts across the whole batch
# (observed in practice). Streaming means the server is writing bytes
# continuously, so a dead client is noticed on its next write instead of
# after the full (possibly abandoned) generation completes. It has a second
# benefit: httpx's read timeout applies per-chunk on a streamed response, not
# once for the whole reply, so a slow-but-progressing generation no longer
# trips a false-positive timeout the way one all-or-nothing deadline did —
# only a genuine stall (no new chunk within config.local_timeout) does.
# LOCAL_STREAM_RETRIES/LOCAL_STREAM_RETRY_DELAY_S retry a stalled/dropped
# stream a few times, pausing between attempts so an already-abandoned
# generation has a chance to actually finish draining server-side before the
# next attempt piles on top of it.
LOCAL_STREAM_RETRIES = 3
LOCAL_STREAM_RETRY_DELAY_S = 10


def _run_local_llm(config, pass_name: str, model: str, system_prompt: str,
                   user_message: str) -> str | None:
    """POST one streamed chat-completion to the configured OpenAI-compatible server.

    Talks to config.local_base_url (e.g. an Ollama server's /v1 endpoint),
    asking for JSON output. Temperature is NOT forced — the server/model default
    applies unless the per-pass param table sets one. That optional table
    ([llm.local.<pass_name>], e.g. temperature or GPT-OSS's reasoning_effort) is
    merged over the JSON-mode baseline — so a user can raise the effort for enrich
    and drop it for clean — but the model/messages/stream/stream_options fields
    the pipeline owns are re-asserted afterward so a stray config key can't
    clobber them.

    Streams the response (see LOCAL_STREAM_RETRIES above for why) and
    reassembles the answer from each chunk's delta.content. Reasoning/thinking
    tokens (confirmed via a live test against Ollama) arrive as a separate
    delta.reasoning field and are never mixed into delta.content, so they're
    simply skipped rather than needing to be stripped out of the final text.
    stream_options.include_usage=true (also confirmed supported) makes the
    server send one final chunk with empty choices and a populated usage
    field just before [DONE]; that's mapped into the token tracker at zero
    cost (local inference is free to us).

    Retries up to LOCAL_STREAM_RETRIES times, LOCAL_STREAM_RETRY_DELAY_S apart,
    on a connection error or a stream stall — this composes with
    _retry_local_failures' own single batch-level retry pass, so a call can
    exhaust its retries here and still get one more shot there. Returns the
    assistant message text, or None if every attempt fails so the caller
    falls back gracefully. _extract_json still tolerates stray prose if the
    server ignores the JSON-mode request.

    Every failure (connection error, stream stall, or a stream that ends
    with reasoning chunks but no content — the model reasoning itself out of
    budget without ever producing an answer) is also emit_log'd, not just
    printed to stderr — this subprocess's stderr is piped into an in-memory
    buffer by app/main.py and only ever surfaced (truncated) if the whole
    run fails, so a per-job failure that falls back gracefully would
    otherwise be invisible anywhere the user can see it. The event log's
    "log" scope isn't tied to a specific search's group (see emit_log), so
    no index needs threading through here.
    """
    url = config.local_base_url.rstrip("/") + "/chat/completions"
    headers = {}
    if config.local_api_key:
        headers["Authorization"] = f"Bearer {config.local_api_key}"
    pass_params = (config.local_clean_params if pass_name == "clean"
                   else config.local_enrich_params)
    payload = {
        "response_format": {"type": "json_object"},
        **pass_params,
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    for attempt in range(1, LOCAL_STREAM_RETRIES + 1):
        attempt_t0 = time.monotonic()
        reasoning_chunks = 0
        try:
            content_parts: list[str] = []
            usage: dict = {}
            with httpx.stream("POST", url, json=payload, headers=headers,
                              timeout=config.local_timeout) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk = line[len("data: "):]
                    if chunk == "[DONE]":
                        break
                    event = json.loads(chunk)
                    choices = event.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        if delta.get("content"):
                            content_parts.append(delta["content"])
                        elif delta.get("reasoning"):
                            reasoning_chunks += 1
                    elif "usage" in event:
                        usage = event["usage"] or {}
            content = "".join(content_parts)
            if not content:
                attempt_elapsed = time.monotonic() - attempt_t0
                raise ValueError(
                    f"stream ended with no content after {attempt_elapsed:.0f}s "
                    f"({reasoning_chunks} reasoning chunk(s), 0 content chunks)"
                )
        except httpx.HTTPError as exc:
            attempt_elapsed = time.monotonic() - attempt_t0
            msg = (f"local LLM call failed (attempt {attempt}/{LOCAL_STREAM_RETRIES}, "
                   f"{attempt_elapsed:.0f}s, {reasoning_chunks} reasoning chunk(s) "
                   f"before failure): {exc}")
            print(f"  {msg}", file=sys.stderr)
            emit_log(msg, level="warn")
            if attempt < LOCAL_STREAM_RETRIES:
                time.sleep(LOCAL_STREAM_RETRY_DELAY_S)
                continue
            return None
        except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError) as exc:
            msg = f"local LLM returned an unexpected response (attempt {attempt}): {exc}"
            print(f"  {msg}", file=sys.stderr)
            emit_log(msg, level="warn")
            return None

        _add_usage(
            {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
            0.0,
        )
        return content
    return None


# ---------------------------------------------------------------------------
# Pass 1 — browser scrape (Haiku)
# ---------------------------------------------------------------------------

def run_claude(system_prompt_file: Path, user_message: str) -> str:
    """
    Invoke the browser scrape subprocess: `claude --print --chrome` on the
    scraper model with the given system prompt and user message. Streams each
    output event to stdout in real time. Token usage is accumulated into _tokens.
    """
    print("Starting browser scrape subprocess...", flush=True)
    t0 = time.monotonic()

    system_prompt = system_prompt_file.read_text()
    log_model_call("scrape", SCRAPER_MODEL, system_prompt, user_message)

    cmd = [
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

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(BASE_DIR),
        **_NEW_GROUP_KWARGS,  # own process group so we can kill the whole tree
    )

    # Guardrail: hard-kill the whole subprocess group if it runs past the cap so
    # a stuck or runaway browser agent can't hang the run indefinitely.
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        _kill_process_tree(proc)

    watchdog = threading.Timer(SUBPROCESS_TIMEOUT_S, _kill_on_timeout)
    watchdog.start()

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

    proc.wait()
    watchdog.cancel()
    elapsed = time.monotonic() - t0

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
    return text_output


# ---------------------------------------------------------------------------
# Pass 2 — description cleaning (Haiku, parallel)
# ---------------------------------------------------------------------------

def clean_one(job: dict) -> dict | None:
    """Strip EEO boilerplate from one job's raw description.

    Fires a single headless call (clean_prompt.md) on the configured backend
    (run_headless) that returns JSON with `description_clean` (boilerplate
    stripped). Returns None on failure — the caller falls back gracefully so
    enrichment always has something to work with.
    """
    desc = job.get("description_raw") or ""
    if not desc:
        return None

    result = run_headless("clean", CLEAN_PROMPT_FILE.read_text(), desc)
    if result is None:
        print(f"  clean failed for {job.get('job_id')} — falling back to raw",
              file=sys.stderr)
        return None

    parsed = _extract_json(result)
    clean = (parsed.get("description_clean") or "").strip()
    return {"description_clean": clean or None}


def _timed_clean_one(job: dict) -> tuple[dict | None, float]:
    """Run clean_one, timing only its own execution (not queue-wait behind
    other jobs in the pool) — used to report an honest per-call duration in
    the run drawer's event log, since submission order alone would include
    however long a job sat waiting for a free worker.
    """
    t0 = time.monotonic()
    result = clean_one(job)
    return result, time.monotonic() - t0


def _retry_local_failures(jobs: list[dict], results: list, is_failure,
                          one_fn, max_workers: int, label: str,
                          index: int = 1) -> None:
    """Retry once, in place, the subset of `results` that `is_failure` flags.

    Local-only: the local backend is the flaky one (occasional generation
    stalls/timeouts on the local server — observed and documented during
    tuning, not a Claude API issue), so this is called only when
    config.llm_backend == "local". Re-runs `one_fn` on just the failed jobs'
    subset (parallel, same max_workers) and overwrites their slot in `results`
    with whatever the retry returns — success or a repeat failure, exactly one
    extra attempt, not a retry loop. A quiet no-op when nothing failed.

    Surfaces the retry pass in the run drawer's event log (``index`` ties the
    lines to the right per-email group) so a run that recovered a stalled call
    reads honestly instead of looking like every job cleaned first try.
    """
    failed_idx = [i for i, r in enumerate(results) if is_failure(r)]
    if not failed_idx:
        return
    print(f"  retrying {len(failed_idx)} failed {label} call(s)...", flush=True)
    emit_log(f"Retrying {len(failed_idx)} failed {label} call(s)…",
             level="head", index=index)
    retry_jobs = [jobs[i] for i in failed_idx]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        retry_results = list(pool.map(one_fn, retry_jobs))
    for i, res in zip(failed_idx, retry_results):
        results[i] = res
    recovered = sum(1 for i in failed_idx if not is_failure(results[i]))
    emit_log(f"Retry: {recovered}/{len(failed_idx)} {label} recovered",
             level="good" if recovered == len(failed_idx) else "warn", index=index)


def clean_jobs(jobs: list[dict], index: int = 1) -> None:
    """Clean descriptions in-place (parallel Haiku calls).

    Sets description_clean on each job. On the local backend, a job whose
    clean_one call fails gets one retry pass (_retry_local_failures) before
    falling back — the local server's occasional stalls are usually transient,
    so a second attempt often succeeds. Still falls back to description_raw if
    the retry also fails, so enrichment always has something to work with.

    Runs the pool via submit()/as_completed() rather than pool.map() so a
    "N of M" progress event can be emitted as each call finishes, driving the
    run drawer's live count — ``index`` ties those events to the right
    per-email group. Each event-log line also reports that job's own call
    duration (via _timed_clean_one), so slow-vs-fast variance is visible
    without needing --log-model-calls (which only records the request, not
    timing or the response).
    """
    print(f"Cleaning {len(jobs)} descriptions (parallel calls)...", flush=True)
    emit_log(f"Cleaning {len(jobs)} descriptions…", level="head", index=index)
    t0 = time.monotonic()
    config = load_config()
    results: list[dict | None] = [None] * len(jobs)
    done = 0
    with ThreadPoolExecutor(max_workers=config.max_workers) as pool:
        futures = {pool.submit(_timed_clean_one, job): i for i, job in enumerate(jobs)}
        for future in as_completed(futures):
            i = futures[future]
            results[i], call_elapsed = future.result()
            done += 1
            emit(scope="search", index=index, key="clean", status="active",
                 stat=f"{done} of {len(jobs)}")
            label = f"{jobs[i].get('title') or '?'} @ {jobs[i].get('company') or '?'}"
            if results[i] is None:
                emit_log(f"clean failed · {label} ({done}/{len(jobs)}) · {call_elapsed:.0f}s",
                         level="warn", index=index)
            else:
                emit_log(f"✓ cleaned {label} ({done}/{len(jobs)}) · {call_elapsed:.0f}s",
                         level="info", index=index)
    if config.llm_backend == "local":
        _retry_local_failures(jobs, results, lambda r: r is None, clean_one,
                              config.max_workers, "clean", index)
    for job, result in zip(jobs, results):
        job["description_clean"] = (
            (result or {}).get("description_clean") or job.get("description_raw") or ""
        )
    elapsed = time.monotonic() - t0
    print(f"Cleaning done in {elapsed:.0f}s", flush=True)
    emit_log(f"Cleaning done · {len(jobs)}/{len(jobs)} ({elapsed:.0f}s)",
             level="good", index=index)


# ---------------------------------------------------------------------------
# Pass 3 — per-job enrichment (Sonnet, parallel)
# ---------------------------------------------------------------------------

MAX_TAGS = 10

_enrich_system_prompt_cache: str | None = None


class SetupError(Exception):
    """Raised when required user setup is missing, malformed, or unreachable.

    The CLI entry point turns this into a clean `sys.exit`; the web UI catches
    it and renders the message in the run drawer instead of launching the
    pipeline, so both callers share one set of checks (check_setup).
    """


def check_setup() -> None:
    """Validate required user setup, raising SetupError on the first problem.

    The shared check for both entry points (CLI validate_setup and the web UI's
    Run button) so a broken setup is caught before any work: the full config
    must load (≥1 role, ≥1 linkedin_searches entry), the `claude` CLI must be
    on PATH (Pass 1 shells out to it), profiles/resume.md must exist (every
    kept job is scored against it), any profile file a role references must
    exist, and — on the local backend — the server must be reachable and
    serving the configured model.
    """
    try:
        config = load_config()
    except ValueError as exc:
        raise SetupError(f"Config error: {exc}")
    roles = config.roles
    try:
        claude_executable()
    except FileNotFoundError as exc:
        raise SetupError(f"Setup error: {exc}")
    if not RESUME_FILE.exists():
        raise SetupError(
            "profiles/resume.md is required — every kept job is scored "
            "against it. Add your resume as markdown, then re-run. "
            "See profiles/README.md."
        )
    missing = [role.profile for role in roles
               if role.profile and not (PROFILES_DIR / role.profile).exists()]
    if missing:
        raise SetupError(
            "Config error: profiles/config.toml references profile file(s) "
            f"that don't exist: {', '.join(missing)}. Create them or remove "
            "the 'profile' key(s) to score those roles on the resume alone."
        )

    if config.llm_backend == "local":
        _verify_local_llm(config)


def validate_setup() -> None:
    """CLI wrapper around check_setup that exits cleanly on any setup failure.

    Called at pipeline start (agent.runner main) so a broken setup errors
    immediately with guidance instead of failing mid-run. The web UI calls
    check_setup directly and renders the SetupError rather than exiting.
    """
    try:
        check_setup()
    except SetupError as exc:
        sys.exit(str(exc))


def _verify_local_llm(config) -> None:
    """Verify the local-LLM server is reachable and serving the configured model.

    Probes the OpenAI-compatible /models endpoint with a short timeout and
    raises SetupError if the server can't be reached (wrong host / down), if the
    response isn't an OpenAI-compatible model list, or if the list doesn't
    include [llm.local] model — so a misconfigured backend fails at startup,
    before Pass 1, instead of failing every clean/enrich call mid-run. Only
    called when the [llm] backend is "local".
    """
    url = config.local_base_url.rstrip("/") + "/models"
    headers = {}
    if config.local_api_key:
        headers["Authorization"] = f"Bearer {config.local_api_key}"
    try:
        resp = httpx.get(url, headers=headers, timeout=5.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise SetupError(
            f"Setup error: local LLM server at {config.local_base_url} is "
            f"unreachable ({exc}). Is it running and reachable from this "
            "machine? Check [llm.local] base_url in profiles/config.toml, or "
            'set [llm] backend = "claude" to use the Claude API instead.'
        )
    try:
        data = resp.json()
        available = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
    except (ValueError, AttributeError, TypeError) as exc:
        raise SetupError(
            f"Setup error: local LLM server at {config.local_base_url} returned "
            f"an unexpected /models response ({exc}). Is base_url pointing at an "
            "OpenAI-compatible endpoint (usually one ending in /v1)?"
        )
    if config.local_model not in available:
        listed = ", ".join(sorted(available)) or "none"
        raise SetupError(
            f"Setup error: local LLM server at {config.local_base_url} does not "
            f"serve a model with the exact id {config.local_model!r} (it serves: "
            f"{listed}). [llm.local] model must match one of those ids exactly, "
            'including any tag — e.g. "scout-enrich:latest", not "scout-enrich". '
            "Copy the id from your server's model list (for Ollama, `ollama "
            f"list`), or pull it if it's missing (e.g. `ollama pull "
            f"{config.local_model}`)."
        )


# The run-start warm-up absorbs the one-time cold model load. It gets its own
# timeout and retry budget, independent of the (deliberately tight) per-call
# [llm.local] timeout: WARMUP_TIMEOUT_S per attempt, WARMUP_ATTEMPTS attempts.
# Loading a model into memory has been observed at ~1 min, so a ~1 min per-attempt
# cap plus a couple of retries recovers a server that crashed on the first
# request — without making a wedged server hang the run for many minutes (the
# earlier 5-min-per-attempt cap did exactly that).
WARMUP_TIMEOUT_S = 60
WARMUP_ATTEMPTS = 3

# _warm_local_llm's max_tokens=1 ping only forces the model weights into VRAM
# — it doesn't exercise prefill/KV-cache cost for a real-sized prompt (real
# descriptions run 5-13 KB, per this module's docstring), which is where the
# very first real clean call was observed to time out on a cold local server.
# _warm_up_clean_pass follows it with one real clean_one() call against a
# similarly-sized synthetic description, retrying WARMUP_CLEAN_RETRIES times
# with a WARMUP_CLEAN_RETRY_DELAY_S pause between attempts. Unlike the ping
# warm-up, failure here is fatal (see _warm_up_clean_pass) — if the model
# can't process a real-sized prompt after every retry, every real clean/
# enrich call in the run would likely also fail, so we abort before spending
# a browser scrape on a run that can't finish.
WARMUP_CLEAN_RETRIES = 3
WARMUP_CLEAN_RETRY_DELAY_S = 5
_WARMUP_FAKE_DESCRIPTION = "We are looking for a Senior Software Engineer to join our team. " * 80


def _warm_local_llm(config) -> None:
    """Fire one tiny generation so the local model loads before the timed passes.

    The setup check (_verify_local_llm) only lists /models — it runs no
    inference, so the first real clean call is otherwise where the model loads
    into VRAM and warms its compute graph. That one-time cost can be minutes and
    can even exceed the per-call timeout, making the first job time out and fall
    back to its raw description (a silent quality loss). Sending a throwaway
    max_tokens=1 completion here moves that cost to run start — before Pass 1 —
    and retries it (WARMUP_ATTEMPTS attempts, WARMUP_TIMEOUT_S each), so a
    first-request server hiccup (an observed failure mode: the first request
    stalls or crashes the server) is absorbed here instead of costing a real
    job. Failures are non-fatal: the real clean/enrich calls still retry and
    fall back, so a warm-up problem never aborts the run. Only called on the
    local backend.
    """
    url = config.local_base_url.rstrip("/") + "/chat/completions"
    headers = {}
    if config.local_api_key:
        headers["Authorization"] = f"Bearer {config.local_api_key}"
    payload = {
        "model": config.local_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    emit_log("Warming local model…", level="head")
    t0 = time.monotonic()
    for attempt in range(1, WARMUP_ATTEMPTS + 1):
        try:
            resp = httpx.post(url, json=payload, headers=headers,
                              timeout=WARMUP_TIMEOUT_S)
            resp.raise_for_status()
            resp.json()
            emit_log(f"Local model ready ({time.monotonic() - t0:.0f}s)",
                     level="good")
            return
        except (httpx.HTTPError, ValueError) as exc:
            print(f"  local model warm-up attempt {attempt} failed: {exc}",
                  file=sys.stderr)
    emit_log("Local model warm-up failed — continuing (calls will retry)",
             level="warn")


def _warm_up_clean_pass(config) -> None:
    """Run one real clean_one() call against a realistically-sized fake job.

    See the WARMUP_CLEAN_* constants above for why this exists on top of
    _warm_local_llm. Retries WARMUP_CLEAN_RETRIES times with a
    WARMUP_CLEAN_RETRY_DELAY_S pause between attempts; if every attempt
    fails, aborts the whole run (sys.exit(1)) rather than proceeding to a
    browser scrape whose clean/enrich passes would likely all fail the same
    way. Local backend only.
    """
    fake_job = {"job_id": "warmup", "description_raw": _WARMUP_FAKE_DESCRIPTION}
    for attempt in range(1, WARMUP_CLEAN_RETRIES + 1):
        emit_log(f"Warm-up clean pass (attempt {attempt}/{WARMUP_CLEAN_RETRIES})…",
                 level="head")
        if clean_one(fake_job) is not None:
            emit_log("Warm-up clean pass succeeded", level="good")
            return
        if attempt < WARMUP_CLEAN_RETRIES:
            time.sleep(WARMUP_CLEAN_RETRY_DELAY_S)

    msg = (f"Local model failed to clean a realistically-sized warm-up job "
           f"after {WARMUP_CLEAN_RETRIES} attempts — aborting before Pass 1. "
           f"Check the local server at {config.local_base_url} (model "
           f"{config.local_model!r}) is healthy and [llm.local] timeout is "
           f"generous enough for a full-size prompt.")
    print(f"ERROR: {msg}", file=sys.stderr)
    logging.getLogger("scout").error(msg)
    sys.exit(1)


def scoring_enabled() -> bool:
    """True when resume.md and every role-referenced profile file exist.

    Roles without a profile file don't block scoring — they are scored against
    the resume alone (see profiles/README.md).
    """
    if not RESUME_FILE.exists():
        return False
    return all(
        (PROFILES_DIR / role.profile).exists()
        for role in load_roles() if role.profile
    )


def build_enrich_system_prompt() -> str:
    """Assemble the enrichment system prompt, cached for the process lifetime.

    Reads enrichment_prompt.md (classification + summary + tags + scoring
    instructions in one file) and injects the configured role types into its
    {{ROLE_DEFINITIONS}} / {{ROLE_ENUM}} placeholders. When scoring is enabled,
    resume/profiles/criteria are appended. The result is identical for every
    job in a run, which is what lets the Anthropic prompt cache absorb the
    resume and profiles almost for free.
    """
    global _enrich_system_prompt_cache
    if _enrich_system_prompt_cache is not None:
        return _enrich_system_prompt_cache

    roles = load_roles()
    parts = [ENRICH_PROMPT_FILE.read_text()]
    if scoring_enabled():
        parts.append("# Resume\n\n" + RESUME_FILE.read_text())
        for role in roles:
            if role.profile:
                parts.append(f"# {role.name} Profile\n\n"
                             + (PROFILES_DIR / role.profile).read_text())
        if CRITERIA_FILE.exists():
            parts.append("# Criteria\n\n" + CRITERIA_FILE.read_text())

    definitions = "\n\n".join(
        f"**`{role.name}`** — {role.definition}" for role in roles
    )
    enum = " | ".join(f'"{role.name}"' for role in roles) + ' | "Other"'
    prompt = "\n\n---\n\n".join(parts)
    prompt = prompt.replace("{{ROLE_DEFINITIONS}}", definitions)
    prompt = prompt.replace("{{ROLE_ENUM}}", enum)

    _enrich_system_prompt_cache = prompt
    return _enrich_system_prompt_cache


def _clean_score(raw) -> float | None:
    """Validate a model-produced score: numeric, clamped to 0–100, else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0.0, min(100.0, float(raw)))


def compute_match_score(fit_score: float | None,
                        criteria_score: float | None,
                        dealbreakers: list[str]) -> float | None:
    """Derive the final match score from the stored subscores.

    fit_weight/criteria_weight weighted sum (profiles/config.toml [scoring]);
    falls back to pure fit when there is no criteria score (no criteria.md);
    capped at dealbreaker_cap when any dealbreaker was hit. The raw subscores
    are stored alongside, so the weights can be changed later and every final
    score recomputed without any LLM calls (backfill_scores.py --recompute).
    Returns None when there is no fit score at all.
    """
    if fit_score is None:
        return None
    config = load_config()
    if criteria_score is None:
        score = fit_score
    else:
        score = (config.fit_weight * fit_score
                 + config.criteria_weight * criteria_score)
    if dealbreakers:
        score = min(score, config.dealbreaker_cap)
    return round(score, 1)


def _clean_tags(raw) -> list[str]:
    """Validate a model-produced tag list: strings only, stripped, deduped
    (case-insensitive, first occurrence wins), hard-capped at MAX_TAGS.

    Returns [] for anything that isn't a list — a bad tags field never drops
    a job.
    """
    if not isinstance(raw, list):
        return []
    tags, seen = [], set()
    for tag in raw:
        if not isinstance(tag, str):
            continue
        tag = tag.strip()
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        tags.append(tag)
        if len(tags) == MAX_TAGS:
            break
    return tags


def _normalize_role(raw) -> str | None:
    """Map a model-produced role_type onto a configured role name, or None.

    Case-insensitive match against the configured role names; "Other" passes
    through as the canonical drop bucket. Anything unrecognized returns None
    so the job is dropped downstream instead of saving a role the UI has no
    filter or color for.
    """
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if raw.lower() == "other":
        return "Other"
    for role in load_roles():
        if role.name.lower() == raw.lower():
            return role.name
    return None


_ENRICH_FAILURE = {
    "role_type": None, "description_summary": None, "tags": [],
    "fit_score": None, "criteria_score": None, "dealbreakers": [],
    "match_reason": None, "match_score": None,
}


def enrich_one(job: dict) -> dict:
    """Classify, summarize, tag, and score one job against the candidate's profiles.

    Fires a single headless call on the configured backend (run_headless) with
    enrichment_prompt.md (plus resume/profiles/criteria when scoring is enabled)
    and the job's title + cleaned description. Returns role_type / description_summary / tags
    plus the scoring fields (fit_score, criteria_score, dealbreakers, match_reason,
    and the derived match_score — all None/[] when scoring is disabled); on any
    failure returns role_type=None so the job is dropped downstream.
    """
    title = job.get("title") or ""
    desc = job.get("description_clean") or job.get("description_raw") or ""
    user_message = f"Job title: {title}\n\nJob description:\n{desc}"

    result = run_headless("enrich", build_enrich_system_prompt(), user_message)
    if result is None:
        print(f"  enrich failed for {job.get('job_id')} — dropping job",
              file=sys.stderr)
        return dict(_ENRICH_FAILURE)

    parsed = _extract_json(result)
    fit_score = _clean_score(parsed.get("fit_score"))
    criteria_score = _clean_score(parsed.get("criteria_score"))
    dealbreakers = _clean_tags(parsed.get("dealbreakers"))
    return {
        "role_type": _normalize_role(parsed.get("role_type")),
        "description_summary": (parsed.get("description_summary") or "").strip() or None,
        "tags": _clean_tags(parsed.get("tags")),
        "fit_score": fit_score,
        "criteria_score": criteria_score,
        "dealbreakers": dealbreakers,
        "match_reason": parsed.get("match_reason"),
        "match_score": compute_match_score(fit_score, criteria_score, dealbreakers),
    }


def _emit_enrich_progress(index: int, done: int, total: int) -> None:
    """Emit the "N of M" live-count event for one completed enrich_one call."""
    emit(scope="search", index=index, key="enrich", status="active",
         stat=f"{done} of {total}")


def _log_enrich_outcome(job: dict, res: dict, index: int) -> None:
    """Emit one event-log line describing a single job's enrichment outcome.

    Distinguishes the three outcomes honestly: a scored keep, a genuine "Other"
    drop, and an outright call failure (res == _ENRICH_FAILURE) — the last logs
    as a warning rather than masquerading as an "Other" classification, since on
    the local backend it may still be recovered by the retry pass.
    """
    label = f"{job.get('title') or '?'} @ {job.get('company') or '?'}"
    if res == _ENRICH_FAILURE:
        emit_log(f"enrich failed · {label}", level="warn", index=index)
        return
    role_type = res.get("role_type")
    if role_type and role_type != "Other":
        score = res.get("match_score")
        score_txt = f" — {score}/100" if score is not None else ""
        emit_log(f"✓ {label}{score_txt}", level="good", index=index)
    else:
        emit_log(f"✗ {label} — dropped (Other)", level="drop", index=index)


def enrich_jobs(jobs: list[dict], index: int = 1) -> None:
    """Enrich each job in-place with role_type, summary, tags, and match scores.

    One headless Sonnet call per job, run in parallel. On the local backend, a
    job whose enrich_one call fails outright gets one retry pass
    (_retry_local_failures) before its result is applied — the local server's
    occasional stalls are usually transient, so a second attempt often
    succeeds instead of the job being dropped for nothing.

    Runs the pool via submit()/as_completed() rather than pool.map() so a
    "N of M" progress event and a per-job outcome log line can be emitted as
    each call finishes, driving the run drawer's live count and event log —
    ``index`` ties those events to the right per-email group.
    """
    print(f"Enriching {len(jobs)} jobs (parallel calls, "
          f"scoring {'on' if scoring_enabled() else 'off'})...", flush=True)
    emit_log(f"Enriching {len(jobs)} jobs…", level="head", index=index)
    t0 = time.monotonic()
    config = load_config()
    max_workers = config.max_workers
    results: list[dict] = [None] * len(jobs)
    done = 0
    if config.llm_backend == "local":
        # The local backend has no Anthropic prompt cache to warm, so the
        # serial-first-call + sleep below would just add latency. Run the whole
        # batch straight through the pool.
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(enrich_one, job): i for i, job in enumerate(jobs)}
            for future in as_completed(futures):
                i = futures[future]
                results[i] = future.result()
                done += 1
                _emit_enrich_progress(index, done, len(jobs))
                _log_enrich_outcome(jobs[i], results[i], index)
        _retry_local_failures(jobs, results, lambda r: r == _ENRICH_FAILURE,
                              enrich_one, max_workers, "enrich", index)
    else:
        # Warm the Anthropic prompt cache with one serial call, then pause
        # briefly before firing the parallel wave. Parallel calls that start
        # simultaneously all miss the cache and each pays the cache WRITE for the
        # large shared system prompt (resume + profiles). The sleep gives the
        # cache write time to propagate so the parallel batch reads instead of
        # re-writing.
        results[0] = enrich_one(jobs[0])
        done += 1
        _emit_enrich_progress(index, done, len(jobs))
        _log_enrich_outcome(jobs[0], results[0], index)
        if len(jobs) > 1:
            time.sleep(2)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(enrich_one, job): i
                           for i, job in enumerate(jobs[1:], start=1)}
                for future in as_completed(futures):
                    i = futures[future]
                    results[i] = future.result()
                    done += 1
                    _emit_enrich_progress(index, done, len(jobs))
                    _log_enrich_outcome(jobs[i], results[i], index)
    for job, res in zip(jobs, results):
        job["role_type"] = res.get("role_type")
        job["description_summary"] = res.get("description_summary")
        job["tags"] = res.get("tags") or []
        job["fit_score"] = res.get("fit_score")
        job["criteria_score"] = res.get("criteria_score")
        job["dealbreakers"] = res.get("dealbreakers") or []
        job["match_reason"] = res.get("match_reason")
        job["match_score"] = res.get("match_score")
    elapsed = time.monotonic() - t0
    kept = sum(1 for r in results if r.get("role_type") and r.get("role_type") != "Other")
    print(f"Enrichment done in {elapsed:.0f}s", flush=True)
    emit_log(f"Enrich done · {kept} kept, {len(jobs) - kept} dropped ({elapsed:.0f}s)",
             level="good", index=index)


# ---------------------------------------------------------------------------
# Scrape orchestration
# ---------------------------------------------------------------------------

def apply_deterministic_filters(all_jobs: dict, existing_ids: set) -> list[dict]:
    """Drop jobs we already know to exclude, cheaply, before the LLM step.

    Excludes: scrape-error entries, jobs already in the DB, jobs already
    applied to, closed listings (jobState != "LISTED"), jobs with no company
    name (can't be exclude-checked, repost-matched, or acted on), and
    companies in the config's [filters] exclude_companies. Returns the
    surviving job records (save_jobs schema) to be enriched.
    """
    excluded = {c.lower() for c in load_config().exclude_companies}
    survivors: list[dict] = []
    for job_id, obj in all_jobs.items():
        if not obj or "error" in obj:
            continue
        if job_id in existing_ids:
            continue
        if obj.get("applied") is True:
            continue
        if obj.get("jobState") not in (None, "LISTED"):
            continue
        company = (obj.get("company") or "").lower().strip()
        if not company or company in excluded:
            continue
        survivors.append(_file_job_to_record(job_id, obj))
    return survivors


def run_scrape(url: str, scrape_run_id: str, index: int = 1) -> list[dict]:
    """Scrape → deterministic filter → enrich → keep only configured role types.

    ``index`` is the 1-based position of this email in the run, used to route
    progress events to the right per-email group in the UI drawer.
    """
    user_message = f"""Run Scout for this LinkedIn job alert.

LinkedIn URL: {url}
Scrape run ID: {scrape_run_id}

Follow the system prompt exactly. Scrape every job on page 1 into the download file.
"""

    emit(scope="search", index=index, key="scrape", status="active")
    emit_log("Scraping LinkedIn…", level="head", index=index)
    run_claude(SYSTEM_PROMPT_FILE, user_message)

    all_jobs = load_downloaded_jobs(scrape_run_id)
    scraped = len(all_jobs) if all_jobs else 0
    emit(scope="search", index=index, key="scrape", status="done", stat=f"{scraped} scraped")
    emit_log(f"Scraped {scraped} jobs", level="good", index=index)

    if all_jobs is None:
        msg = (
            f"No downloaded job file for run {scrape_run_id} appeared in "
            f"{download_dir()} within {DOWNLOAD_WAIT_S}s. Nothing to save.\n"
            f"  Most likely: Chrome is set to ask where to save each file, so "
            f"the blob download opened a 'Save As…' dialog instead of writing "
            f"the file (this also freezes the browser agent). Turn OFF Settings "
            f"→ Downloads → 'Ask where to save each file before downloading', "
            f"then re-run.\n"
            f"  If your Chrome download folder isn't {download_dir()}, set "
            f"[scrape] download_dir in profiles/config.toml."
        )
        print(f"WARNING: {msg}", file=sys.stderr)
        logging.getLogger("scout").warning(msg)
        emit(scope="search", index=index, key="filter", status="done", stat="0 of 0 kept")
        emit(scope="search", index=index, key="enrich", status="done", stat="0 kept")
        return []

    # Deterministic pre-filters — cheap, and done BEFORE enrichment so we never
    # spend a Sonnet call on a job we're going to drop anyway.
    emit(scope="search", index=index, key="filter", status="active")
    existing = set(get_existing_job_ids())
    survivors = apply_deterministic_filters(all_jobs, existing)
    emit(scope="search", index=index, key="filter", status="done",
         stat=f"{len(survivors)} of {len(all_jobs)} kept")
    emit_log(f"Filter: {len(survivors)} of {len(all_jobs)} kept",
             level="info", index=index)

    print(f"{len(all_jobs)} scraped; {len(survivors)} survive deterministic "
          f"filters (already-in-DB / applied / closed / excluded companies).")
    if not survivors:
        emit(scope="search", index=index, key="enrich", status="done", stat="0 kept")
        return []

    # Description cleaning: strip EEO boilerplate / benefits tail before Sonnet.
    emit(scope="search", index=index, key="clean", status="active")
    clean_jobs(survivors, index)
    emit(scope="search", index=index, key="clean", status="done",
         stat=f"{len(survivors)} cleaned")

    # Per-job enrichment: role_type (configured roles / Other) + tags + scoring.
    emit(scope="search", index=index, key="enrich", status="active")
    enrich_jobs(survivors, index)

    # Keep only the configured role types; drop Other (and any that failed to enrich).
    role_names = {role.name for role in load_roles()}
    kept = [j for j in survivors if j.get("role_type") in role_names]
    emit(scope="search", index=index, key="enrich", status="done",
         stat=f"{len(kept)} kept")
    print(f"Enriched {len(survivors)}; kept {len(kept)} "
          f"({'/'.join(sorted(role_names))}), "
          f"dropped {len(survivors) - len(kept)} Other/failed.")
    return kept


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_url(
    url: str,
    search_name: str = "Manual run",
    index: int = 1,
    total: int = 1,
) -> bool:
    """Create a scrape run, scrape + enrich + filter, and save results.

    ``index``/``total`` position this search within the run for UI progress.
    Returns True if the search was fully processed (even if 0 jobs were
    saved), False if the run was aborted (e.g. the scrape timed out).
    """
    print(f"\nURL  : {url[:80]}...")

    run_id = create_scrape_run(
        search_name=search_name,
        linkedin_url=url,
        role_type=None,  # a run no longer has a single role; role is per-job
    )
    print(f"Run  : {run_id}")

    try:
        jobs = run_scrape(url, run_id, index)
    except TimeoutError as exc:
        emit(scope="search", index=index, key="scrape", status="error", stat="timed out")
        print(f"\nERROR: {exc}. Run aborted — nothing saved.", file=sys.stderr)
        logging.getLogger("scout").error("Scrape run %s aborted: %s", run_id, exc)
        print_token_summary()
        return False

    emit(scope="search", index=index, key="save", status="active")
    if not jobs:
        emit(scope="search", index=index, key="save", status="done", stat="0 saved")
        print("No jobs to save.")
        logging.getLogger("scout").info("Scrape run %s: no jobs to save", run_id)
        print_token_summary()
        return True

    result = save_jobs(run_id, jobs)
    emit(scope="search", index=index, key="save", status="done",
         stat=f"{result['saved']} saved, {result['reposts_detected']} reposts")
    emit_log(f"Saved {result['saved']} jobs · {result['reposts_detected']} reposts",
             level="good", index=index)
    print(f"\nSave result: {result}")
    logging.getLogger("scout").info(
        "Scrape run %s: %d saved, %d reposts", run_id,
        result.get("saved", 0), result.get("reposts_detected", 0))
    print_token_summary()
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point."""
    global _log_model_calls

    parser = argparse.ArgumentParser(description="Scout job agent runner")
    parser.add_argument("--url", help="Scrape one ad-hoc LinkedIn URL, ignoring config")
    parser.add_argument("--log-model-calls", action="store_true",
                        help="Log every Claude call (model, system prompt, user "
                             "message) to model_calls.log in the configured log dir")
    args = parser.parse_args()

    validate_setup()
    log = setup_logging()
    _log_model_calls = args.log_model_calls
    init_db()
    log.info("Run started (source=%s, model call logging %s)",
             "manual URL" if args.url else "config",
             "on" if _log_model_calls else "off")

    # Tell the drawer which backend/models are driving this run. Pass 1 (the
    # browser scrape) always runs on Claude even when Pass 2/3 are local.
    config = load_config()
    is_local = config.llm_backend == "local"
    models = {
        "scrape": SCRAPER_MODEL,
        "clean": config.local_model if is_local else CLEAN_MODEL,
        "enrich": config.local_model if is_local else ENRICH_MODEL,
    }
    emit(scope="meta", backend=config.llm_backend, models=models)
    emit_log(f"Run started · backend={config.llm_backend}", level="head")

    # Load/warm the local model now (before Pass 1) rather than letting the
    # first clean call eat the multi-minute cold start — see _warm_local_llm.
    # _warm_up_clean_pass follows with a real, realistically-sized clean call
    # so a server that can't handle full-size prompts fails here, not silently
    # mid-run — see its docstring.
    if is_local:
        _warm_local_llm(config)
        _warm_up_clean_pass(config)

    emit(scope="global", key="start", status="done")

    if args.url:
        process_url(url=args.url, index=1, total=1)
        log.info("Run finished (1 URL)")
        emit(scope="run", status="done")
        return

    searches = config.linkedin_searches
    for i, search in enumerate(searches, 1):
        emit_log(f"Search {i}/{len(searches)}: {search.name}", level="head", index=i)
        process_url(url=search.url, search_name=search.name, index=i, total=len(searches))

    log.info("Run finished (%d search%s)", len(searches), "es" if len(searches) != 1 else "")
    emit(scope="run", status="done")


if __name__ == "__main__":
    main()
