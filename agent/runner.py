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

Usage:
    python -m agent.runner                 # reads Gmail for URLs
    python -m agent.runner --url <url>     # specific URL
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.auth.exceptions import RefreshError

from app.config import load_config, load_roles
from app.database import init_db
from app.gmail import get_job_alert_emails, mark_email_read
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

# Width of the Pass 2/Pass 3 worker pools. Narrow on purpose: fewer
# simultaneous first-wave calls means fewer duplicate cache writes of the
# shared system prompt, at some cost in wall-clock time.
MAX_WORKERS = 2

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

    Fires a single headless Haiku call (clean_prompt.md) that returns JSON
    with `description_clean` (boilerplate stripped). Returns None on failure —
    the caller falls back gracefully so enrichment always has something to work with.
    """
    desc = job.get("description_raw") or ""
    if not desc:
        return None

    system_prompt = CLEAN_PROMPT_FILE.read_text()
    log_model_call("clean", CLEAN_MODEL, system_prompt, desc)

    cmd = [
        claude_executable(),
        "--print",
        "--model", CLEAN_MODEL,
        "--exclude-dynamic-system-prompt-sections",
        "--system-prompt", system_prompt,
        "--output-format", "json",
        desc,
    ]

    try:
        proc = subprocess.run(
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_S, env=_NO_THINKING_ENV,
        )
        envelope = json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        print(f"  clean TIMEOUT for {job.get('job_id')} — falling back to raw",
              file=sys.stderr)
        return None
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        print(f"  clean failed for {job.get('job_id')}: {exc}", file=sys.stderr)
        return None

    _add_usage(
        envelope.get("usage", {}),
        envelope.get("total_cost_usd", envelope.get("cost_usd", 0.0)),
    )
    parsed = _extract_json(envelope.get("result", ""))
    clean = (parsed.get("description_clean") or "").strip()
    return {"description_clean": clean or None}


def clean_jobs(jobs: list[dict]) -> None:
    """Clean descriptions in-place (parallel Haiku calls).

    Sets description_clean on each job. Falls back to description_raw when a
    call fails so enrichment always has something to work with.
    """
    print(f"Cleaning {len(jobs)} descriptions (parallel Haiku calls)...", flush=True)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(clean_one, jobs))
    for job, result in zip(jobs, results):
        job["description_clean"] = (
            (result or {}).get("description_clean") or job.get("description_raw") or ""
        )
    print(f"Cleaning done in {time.monotonic() - t0:.0f}s", flush=True)


# ---------------------------------------------------------------------------
# Pass 3 — per-job enrichment (Sonnet, parallel)
# ---------------------------------------------------------------------------

MAX_TAGS = 10

_enrich_system_prompt_cache: str | None = None


def validate_setup() -> None:
    """Fail fast with guidance when the required user setup is missing.

    Called at pipeline start so a broken setup errors immediately instead of
    mid-run: the roles config must load (≥1 role), the `claude` CLI must be on
    PATH (every pass shells out to it), profiles/resume.md must exist (every
    kept job is scored against it), and any profile file a role references must
    exist.
    """
    try:
        roles = load_roles()
    except ValueError as exc:
        sys.exit(f"Config error: {exc}")
    try:
        claude_executable()
    except FileNotFoundError as exc:
        sys.exit(f"Setup error: {exc}")
    if not RESUME_FILE.exists():
        sys.exit(
            "profiles/resume.md is required — every kept job is scored "
            "against it. Add your resume as markdown, then re-run. "
            "See profiles/README.md."
        )
    missing = [role.profile for role in roles
               if role.profile and not (PROFILES_DIR / role.profile).exists()]
    if missing:
        sys.exit(
            "Config error: profiles/config.toml references profile file(s) "
            f"that don't exist: {', '.join(missing)}. Create them or remove "
            "the 'profile' key(s) to score those roles on the resume alone."
        )


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

    Fires a single headless Sonnet call (enrichment_prompt.md, plus
    resume/profiles/criteria when scoring is enabled) with the job's
    title + cleaned description. Returns role_type / description_summary / tags
    plus the scoring fields (fit_score, criteria_score, dealbreakers, match_reason,
    and the derived match_score — all None/[] when scoring is disabled); on any
    failure returns role_type=None so the job is dropped downstream.
    """
    title = job.get("title") or ""
    desc = job.get("description_clean") or job.get("description_raw") or ""
    user_message = f"Job title: {title}\n\nJob description:\n{desc}"

    system_prompt = build_enrich_system_prompt()
    log_model_call("enrich", ENRICH_MODEL, system_prompt, user_message)

    cmd = [
        claude_executable(),
        "--print",
        "--model", ENRICH_MODEL,
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
        print(f"  enrich TIMEOUT (> {SUBPROCESS_TIMEOUT_S}s) for "
              f"{job.get('job_id')} — killed, dropping job", file=sys.stderr)
        return dict(_ENRICH_FAILURE)
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        print(f"  enrich failed for {job.get('job_id')}: {exc}", file=sys.stderr)
        return dict(_ENRICH_FAILURE)

    _add_usage(
        envelope.get("usage", {}),
        envelope.get("total_cost_usd", envelope.get("cost_usd", 0.0)),
    )
    parsed = _extract_json(envelope.get("result", ""))
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


def enrich_jobs(jobs: list[dict]) -> None:
    """Enrich each job in-place with role_type, summary, tags, and match scores.

    One headless Sonnet call per job, run in parallel.
    """
    print(f"Enriching {len(jobs)} jobs (parallel Sonnet calls, "
          f"scoring {'on' if scoring_enabled() else 'off'})...", flush=True)
    t0 = time.monotonic()
    # Warm the Anthropic prompt cache with one serial call, then pause briefly
    # before firing the parallel wave. Parallel calls that start simultaneously
    # all miss the cache and each pays the cache WRITE for the large shared
    # system prompt (resume + profiles). The sleep gives the cache write time
    # to propagate so the parallel batch reads instead of re-writing.
    results = [enrich_one(jobs[0])]
    if len(jobs) > 1:
        time.sleep(2)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            results += list(pool.map(enrich_one, jobs[1:]))
    for job, res in zip(jobs, results):
        job["role_type"] = res.get("role_type")
        job["description_summary"] = res.get("description_summary")
        job["tags"] = res.get("tags") or []
        job["fit_score"] = res.get("fit_score")
        job["criteria_score"] = res.get("criteria_score")
        job["dealbreakers"] = res.get("dealbreakers") or []
        job["match_reason"] = res.get("match_reason")
        job["match_score"] = res.get("match_score")
    print(f"Enrichment done in {time.monotonic() - t0:.0f}s", flush=True)


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

    emit(scope="email", index=index, key="scrape", status="active")
    run_claude(SYSTEM_PROMPT_FILE, user_message)

    all_jobs = load_downloaded_jobs(scrape_run_id)
    scraped = len(all_jobs) if all_jobs else 0
    emit(scope="email", index=index, key="scrape", status="done", stat=f"{scraped} scraped")

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
        emit(scope="email", index=index, key="filter", status="done", stat="0 of 0 kept")
        emit(scope="email", index=index, key="enrich", status="done", stat="0 kept")
        return []

    # Deterministic pre-filters — cheap, and done BEFORE enrichment so we never
    # spend a Sonnet call on a job we're going to drop anyway.
    emit(scope="email", index=index, key="filter", status="active")
    existing = set(get_existing_job_ids())
    survivors = apply_deterministic_filters(all_jobs, existing)
    emit(scope="email", index=index, key="filter", status="done",
         stat=f"{len(survivors)} of {len(all_jobs)} kept")

    print(f"{len(all_jobs)} scraped; {len(survivors)} survive deterministic "
          f"filters (already-in-DB / applied / closed / excluded companies).")
    if not survivors:
        emit(scope="email", index=index, key="enrich", status="done", stat="0 kept")
        return []

    # Description cleaning: strip EEO boilerplate / benefits tail before Sonnet.
    emit(scope="email", index=index, key="clean", status="active")
    clean_jobs(survivors)
    emit(scope="email", index=index, key="clean", status="done",
         stat=f"{len(survivors)} cleaned")

    # Per-job enrichment: role_type (configured roles / Other) + tags + scoring.
    emit(scope="email", index=index, key="enrich", status="active")
    enrich_jobs(survivors)

    # Keep only the configured role types; drop Other (and any that failed to enrich).
    role_names = {role.name for role in load_roles()}
    kept = [j for j in survivors if j.get("role_type") in role_names]
    emit(scope="email", index=index, key="enrich", status="done",
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
    email_subject: str = "Manual run",
    email_date: str = "",
    index: int = 1,
    total: int = 1,
) -> bool:
    """Create a scrape run, scrape + enrich + filter, and save results.

    ``index``/``total`` position this email within the run for UI progress.
    Returns True if the email was fully processed (even if 0 jobs were saved),
    False if the run was aborted (e.g. the scrape timed out) so the caller can
    leave the source email unread for a later retry.
    """
    print(f"\nURL  : {url[:80]}...")

    run_id = create_scrape_run(
        email_subject=email_subject,
        email_date=email_date,
        linkedin_url=url,
        role_type=None,  # a run no longer has a single role; role is per-job
    )
    print(f"Run  : {run_id}")

    try:
        jobs = run_scrape(url, run_id, index)
    except TimeoutError as exc:
        emit(scope="email", index=index, key="scrape", status="error", stat="timed out")
        print(f"\nERROR: {exc}. Run aborted — nothing saved.", file=sys.stderr)
        logging.getLogger("scout").error("Scrape run %s aborted: %s", run_id, exc)
        print_token_summary()
        return False

    emit(scope="email", index=index, key="save", status="active")
    if not jobs:
        emit(scope="email", index=index, key="save", status="done", stat="0 saved")
        print("No jobs to save.")
        logging.getLogger("scout").info("Scrape run %s: no jobs to save", run_id)
        print_token_summary()
        return True

    result = save_jobs(run_id, jobs)
    emit(scope="email", index=index, key="save", status="done",
         stat=f"{result['saved']} saved, {result['reposts_detected']} reposts")
    print(f"\nSave result: {result}")
    logging.getLogger("scout").info(
        "Scrape run %s: %d saved, %d reposts", run_id,
        result.get("saved", 0), result.get("reposts_detected", 0))
    print_token_summary()
    return True


def process_email(email: dict, index: int = 1, total: int = 1) -> None:
    """Run a full scrape for a single Gmail alert email."""
    url = email.get("see_all_jobs_url")
    if not url:
        print(f"No URL found in email: {email.get('subject')}")
        emit(scope="email", index=index, key="scrape", status="error", stat="no URL")
        return

    print(f"\nSubject : {email.get('subject')}")
    processed = process_url(
        url=url,
        email_subject=email.get("subject", ""),
        email_date=email.get("date", ""),
        index=index,
        total=total,
    )

    # Only clear the email from the unread queue once its jobs are in the DB, so
    # an aborted run leaves it to be picked up again next time.
    message_id = email.get("message_id")
    if processed and message_id:
        try:
            mark_email_read(message_id)
        except Exception as exc:  # marking read must never fail the run
            print(f"  WARN: could not mark email {message_id} read: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point."""
    global _log_model_calls

    parser = argparse.ArgumentParser(description="Scout job agent runner")
    parser.add_argument("--url", help="Scrape a specific LinkedIn URL (skips Gmail)")
    parser.add_argument("--max-emails", type=int, default=5, help="Max Gmail emails to process")
    parser.add_argument("--log-model-calls", action="store_true",
                        help="Log every Claude call (model, system prompt, user "
                             "message) to model_calls.log in the configured log dir")
    args = parser.parse_args()

    validate_setup()
    log = setup_logging()
    _log_model_calls = args.log_model_calls
    init_db()
    log.info("Run started (source=%s, model call logging %s)",
             "manual URL" if args.url else "gmail",
             "on" if _log_model_calls else "off")
    emit(scope="global", key="start", status="done")

    if args.url:
        emit(scope="global", key="gmail", status="skipped", stat="manual URL")
        process_url(url=args.url, index=1, total=1)
        log.info("Run finished (1 URL)")
        emit(scope="run", status="done")
        return

    emit(scope="global", key="gmail", status="active")
    try:
        emails = get_job_alert_emails(max_results=args.max_emails)
    except RefreshError:
        emit(scope="global", key="gmail", status="error", stat="auth expired",
             auth_required=True)
        log.error("Gmail authentication expired — reauth required")
        print("Gmail token has expired or been revoked. "
              "Use the Reauthenticate button in the run drawer.", file=sys.stderr)
        sys.exit(1)
    if not emails:
        emit(scope="global", key="gmail", status="done", stat="0 emails", emails=[])
        print("No unread job alert emails found.")
        log.info("Run finished (no unread job alert emails)")
        emit(scope="run", status="done")
        return

    subjects = [(e.get("subject") or "(no subject)")[:80] for e in emails]
    emit(scope="global", key="gmail", status="done",
         stat=f"{len(emails)} email{'s' if len(emails) != 1 else ''}", emails=subjects)

    for i, email in enumerate(emails, 1):
        process_email(email, index=i, total=len(emails))

    log.info("Run finished (%d email%s)", len(emails), "s" if len(emails) != 1 else "")
    emit(scope="run", status="done")


if __name__ == "__main__":
    main()
