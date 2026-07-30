"""
Scout agent runner.

Three Claude passes, orchestrated here:

Pass 1 — Browser scrape (Haiku, scrape_prompt.md / scrape_single_prompt.md)
    A browser subprocess navigates LinkedIn and pulls EVERY job on page 1 into
    a Downloads/scout_<run_id>.json blob download via the Voyager API, which the
    runner reads directly from the Downloads folder. It does no filtering and no
    description cleaning — the browser agent is already complex (privacy-filter
    handoff, blob-download, virtualized cards) and we deliberately keep it
    mechanical. description_raw is stored as-is from the API.

Pass 2 — Description cleaning (Haiku, parallel, clean_prompt.md)
    description_raw is split in Python (agent/units.py) into numbered
    sentence/bullet units; one headless Haiku call per surviving job is sent
    only the numbered skeleton and names unit-ranges to drop (EEO, benefits,
    culture, etc.) rather than rewriting anything, and the survivors are
    stitched back into description_clean. The model can only mark text for
    removal, never alter kept text. Runs after the deterministic filters so
    we never clean jobs we're going to drop anyway. Cheaper Haiku input
    tokens here save the more expensive Sonnet input tokens in Pass 3.

Pass 3 — Per-job enrichment (Sonnet, parallel, enrichment_prompt.md)
    One headless Sonnet call per job classifies it into one of the configured
    role types (profiles/config.toml [[roles]]) or Other, writes a 2–4 sentence
    description_summary, tags it, and scores it against the candidate's profiles.
    Uses description_clean so the model sees only the signal, not the noise.
    Jobs classified Other (or that fail to enrich) are dropped; the rest are saved.

Passes 2 and 3 are the two "headless" passes and run on a configurable backend
(profiles/config.toml [llm] backend): the default "claude" shells out to the
`claude` CLI, while "api" routes both through run_headless() to any
OpenAI-compatible endpoint (e.g. Ollama, local or remote). Pass 1 always runs on
Claude — it drives the browser and is agentic, which a text-completion model
can't do.

Usage:
    python -m agent.runner                 # scrapes every profiles/config.toml [[linkedin_searches]] entry
    python -m agent.runner --url <url>     # scrape one ad-hoc URL, ignoring config
"""

import argparse
import functools
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import load_config, load_roles
from app.database import init_db
from app.logging_setup import setup_logging
import agent.llm_common as llm_common
from agent.claude import CLEAN_MODEL, ENRICH_MODEL, SCRAPER_MODEL, claude_executable, run_claude
from agent.llm import run_headless
from agent.llm_api import _verify_api_llm, _warm_api_llm
from agent.llm_common import SetupError, emit, emit_log, print_token_summary
from agent.step_keys import StepKey
from agent.tools import create_scrape_run, save_jobs, get_existing_job_ids
from agent.units import (
    parse_drop_response,
    render_units,
    split_into_units,
    stitch_units,
)

BASE_DIR = Path(__file__).parent.parent
PROMPT_DIR = BASE_DIR / "agent"
SCRAPE_PROMPT_FILE = PROMPT_DIR / "scrape_prompt.md"
SCRAPE_SINGLE_PROMPT_FILE = PROMPT_DIR / "scrape_single_prompt.md"
CLEAN_PROMPT_FILE  = PROMPT_DIR / "clean_prompt.md"
ENRICH_PROMPT_FILE = PROMPT_DIR / "enrichment_prompt.md"

# Personal match-scoring artifacts (git-ignored; see profiles/README.md).
# Scoring activates only when resume.md and every profile file referenced by
# the roles config exist. Per-role profile files come from profiles/config.toml
# (see app/config.py); a role may omit its profile and score on resume alone.
PROFILES_DIR = BASE_DIR / "profiles"
RESUME_FILE = PROFILES_DIR / "resume.md"
CRITERIA_FILE = PROFILES_DIR / "criteria.md"

# The Pass 2/Pass 3 worker-pool width is configurable per backend via
# [llm] max_workers (config.max_workers). It's a knob because the right value
# depends on the active backend: a Claude run trades wall-clock against
# duplicate prompt-cache writes of the shared system prompt, while an api-backend
# server is bounded by its own VRAM/throughput (a 16GB box may only manage 1).

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


@functools.lru_cache(maxsize=None)
def _load_clean_prompt() -> str:
    """Read and cache clean_prompt.md for the process lifetime.

    clean_one is called once per job across a MAX_WORKERS-wide thread pool
    (clean_jobs); without caching, every job would re-read the same static
    file from disk. lru_cache is thread-safe, so the first caller reads it
    and the rest just get the cached string back.
    """
    return CLEAN_PROMPT_FILE.read_text()


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
# Pass 2 — description cleaning (Haiku, parallel)
# ---------------------------------------------------------------------------

def clean_one(job: dict) -> dict | None:
    """Strip boilerplate units from one job's raw description.

    Splits the raw text into numbered sentence/bullet units (agent.units),
    sends the rendered numbered text to a single headless call
    (clean_prompt.md) on the configured backend asking which unit ranges to
    drop, and stitches the survivors back into description_clean. Returns
    None on any failure — including a malformed drop response — so the
    caller (clean_jobs / _warm_up_clean_pass) falls back to description_raw
    exactly as before; every caller of this function (clean_jobs,
    _warm_up_clean_pass, and the eval scripts under scripts/) depends on this
    dict|None contract, so it must never change shape.
    """
    desc = job.get("description_raw") or ""
    if not desc:
        return None

    units = split_into_units(desc)
    if not units:
        return None

    rendered = render_units(units)
    result = run_headless("clean", _load_clean_prompt(), rendered)
    if result is None:
        print(f"  clean failed for {job.get('job_id')} — falling back to raw",
              file=sys.stderr)
        return None

    parsed = _extract_json(result)
    drop = parse_drop_response(parsed, len(units))
    if drop is None:
        print(f"  clean returned an unparseable drop response for "
              f"{job.get('job_id')} — falling back to raw", file=sys.stderr)
        return None

    clean = stitch_units(units, drop).strip()
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


# Delay before firing a batch's one retry pass, keyed by backend (see
# _retry_failures). 0 on api preserves that backend's original behavior
# unchanged — its failures are usually already-transient stream stalls, so
# an immediate retry is fine. 5s on claude gives a rate-limited or otherwise
# hiccuping call room to clear, since a claude subprocess failure (timeout,
# malformed response) is more often a hard failure than the api backend's
# flakiness, so retrying instantly is less likely to help.
API_RETRY_DELAY_S = 0
CLAUDE_RETRY_DELAY_S = 5


def _retry_failures(jobs: list[dict], results: list, is_failure,
                    one_fn, max_workers: int, label: str,
                    retry_delay: float, index: int = 1) -> None:
    """Retry once, in place, the subset of `results` that `is_failure` flags.

    Shared by clean_jobs and enrich_jobs, on both backends. Waits
    `retry_delay` seconds (see API_RETRY_DELAY_S/CLAUDE_RETRY_DELAY_S) before
    firing the retry pool, then re-runs `one_fn` on just the failed jobs'
    subset (parallel, same max_workers) and overwrites their slot in `results`
    with whatever the retry returns — success or a repeat failure, exactly one
    extra attempt, not a retry loop. A quiet no-op when nothing failed.

    Surfaces the retry pass in the run drawer's event log (``index`` ties the
    lines to the right search) so a run that recovered a stalled call
    reads honestly instead of looking like every job succeeded first try.
    """
    failed_idx = [i for i, r in enumerate(results) if is_failure(r)]
    if not failed_idx:
        return
    print(f"  retrying {len(failed_idx)} failed {label} call(s)...", flush=True)
    emit_log(f"Retrying {len(failed_idx)} failed {label} call(s)…",
             level="head", index=index)
    if retry_delay:
        time.sleep(retry_delay)
    retry_jobs = [jobs[i] for i in failed_idx]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        retry_results = list(pool.map(one_fn, retry_jobs))
    for i, res in zip(failed_idx, retry_results):
        results[i] = res
    recovered = sum(1 for i in failed_idx if not is_failure(results[i]))
    emit_log(f"Retry: {recovered}/{len(failed_idx)} {label} recovered",
             level="good" if recovered == len(failed_idx) else "warn", index=index)


def _emit_progress(index: int, done: int, total: int, step_key: StepKey) -> None:
    """Emit the "N of M" live-count event for one completed clean/enrich call.

    Shared by clean_jobs and enrich_jobs — step_key picks which run-drawer
    step (StepKey.CLEAN or StepKey.ENRICH) the event updates.
    """
    emit(scope="search", index=index, key=step_key, status="active",
         stat=f"{done} of {total}")


def clean_jobs(jobs: list[dict], index: int = 1) -> None:
    """Clean descriptions in-place (parallel Haiku calls).

    Sets description_clean on each job. Every job whose clean_one call fails
    gets one retry pass (_retry_failures) before falling back — the retry
    often recovers a transient failure, but still falls back to
    description_raw if it also fails, so enrichment always has something to
    work with.

    Runs the pool via submit()/as_completed() rather than pool.map() so a
    "N of M" progress event can be emitted as each call finishes, driving the
    run drawer's live count — ``index`` ties those events to the right
    search. Each event-log line also reports that job's own call
    duration (via _timed_clean_one), so slow-vs-fast variance is visible
    without needing --log-model-calls (which only records the request, not
    timing or the response).

    On the claude backend, warms the Anthropic prompt cache with one serial
    call before the parallel wave — see enrich_jobs's docstring for why;
    unified here so both passes behave identically regardless of how large
    each one's system prompt is. The api backend has no such cache, so it
    skips straight to the full-batch pool.
    """
    print(f"Cleaning {len(jobs)} descriptions (parallel calls)...", flush=True)
    emit_log(f"Cleaning {len(jobs)} descriptions…", level="head", index=index)
    t0 = time.monotonic()
    config = load_config()
    max_workers = config.max_workers
    results: list[dict | None] = [None] * len(jobs)
    done = 0

    def _apply_result(i: int, result: dict | None, call_elapsed: float) -> None:
        nonlocal done
        results[i] = result
        done += 1
        _emit_progress(index, done, len(jobs), StepKey.CLEAN)
        label = f"{jobs[i].get('title') or '?'} @ {jobs[i].get('company') or '?'}"
        if result is None:
            emit_log(f"clean failed · {label} ({done}/{len(jobs)}) · {call_elapsed:.0f}s",
                     level="warn", index=index)
        else:
            emit_log(f"✓ cleaned {label} ({done}/{len(jobs)}) · {call_elapsed:.0f}s",
                     level="info", index=index)

    if config.llm_backend == "api":
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_timed_clean_one, job): i for i, job in enumerate(jobs)}
            for future in as_completed(futures):
                i = futures[future]
                result, call_elapsed = future.result()
                _apply_result(i, result, call_elapsed)
    else:
        result, call_elapsed = _timed_clean_one(jobs[0])
        _apply_result(0, result, call_elapsed)
        if len(jobs) > 1:
            time.sleep(2)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_timed_clean_one, job): i
                           for i, job in enumerate(jobs[1:], start=1)}
                for future in as_completed(futures):
                    i = futures[future]
                    result, call_elapsed = future.result()
                    _apply_result(i, result, call_elapsed)

    retry_delay = API_RETRY_DELAY_S if config.llm_backend == "api" else CLAUDE_RETRY_DELAY_S
    _retry_failures(jobs, results, lambda r: r is None, clean_one,
                    max_workers, "clean", retry_delay, index)

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


def check_setup() -> None:
    """Validate required user setup, raising SetupError on the first problem.

    The shared check for both entry points (CLI validate_setup and the web UI's
    Run button) so a broken setup is caught before any work: the full config
    must load (≥1 role, ≥1 linkedin_searches entry), the `claude` CLI must be
    on PATH (Pass 1 shells out to it), profiles/resume.md must exist (every
    kept job is scored against it), any profile file a role references must
    exist, and — on the api backend — the endpoint must be reachable and
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

    if config.llm_backend == "api":
        _verify_api_llm(config)


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


# _warm_api_llm's max_tokens=1 ping only forces the model weights into VRAM
# — it doesn't exercise prefill/KV-cache cost for a real-sized prompt (real
# descriptions run 5-13 KB, per this module's docstring), which is where the
# very first real clean call was observed to time out on a cold api-backend
# server. _warm_up_clean_pass follows it with one real clean_one() call against a
# similarly-sized synthetic description, retrying WARMUP_CLEAN_RETRIES times
# with a WARMUP_CLEAN_RETRY_DELAY_S pause between attempts. Unlike the ping
# warm-up, failure here is fatal (see below) — if the model can't process a
# real-sized prompt after every retry, every real clean/enrich call in the run
# would likely also fail, so we abort before spending a browser scrape on a
# run that can't finish.
WARMUP_CLEAN_RETRIES = 3
WARMUP_CLEAN_RETRY_DELAY_S = 5
_WARMUP_FAKE_DESCRIPTION = "We are looking for a Senior Software Engineer to join our team. " * 80


def _warm_up_clean_pass(config) -> None:
    """Run one real clean_one() call against a realistically-sized fake job.

    See the WARMUP_CLEAN_* constants above for why this exists on top of
    _warm_api_llm. Retries WARMUP_CLEAN_RETRIES times with a
    WARMUP_CLEAN_RETRY_DELAY_S pause between attempts; if every attempt
    fails, aborts the whole run (sys.exit(1)) rather than proceeding to a
    browser scrape whose clean/enrich passes would likely all fail the same
    way. Api backend only.
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

    msg = (f"Model failed to clean a realistically-sized warm-up job "
           f"after {WARMUP_CLEAN_RETRIES} attempts — aborting before Pass 1. "
           f"Check the API endpoint at {config.api_base_url} (model "
           f"{config.api_model!r}) is healthy and [llm.api] timeout is "
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


def _timed_enrich_one(job: dict) -> tuple[dict, float]:
    """Run enrich_one, timing only its own execution (not queue-wait behind
    other jobs in the pool) — mirrors _timed_clean_one so both passes report
    an honest per-call duration in the run drawer's event log.
    """
    t0 = time.monotonic()
    result = enrich_one(job)
    return result, time.monotonic() - t0


def _log_enrich_outcome(job: dict, res: dict, call_elapsed: float, index: int) -> None:
    """Emit one event-log line describing a single job's enrichment outcome.

    Distinguishes the three outcomes honestly: a scored keep, a genuine "Other"
    drop, and an outright call failure (res == _ENRICH_FAILURE) — the last logs
    as a warning rather than masquerading as an "Other" classification, since it
    may still be recovered by the retry pass. Reports the call's own duration,
    same as clean's per-job log lines.
    """
    label = f"{job.get('title') or '?'} @ {job.get('company') or '?'}"
    if res == _ENRICH_FAILURE:
        emit_log(f"enrich failed · {label} · {call_elapsed:.0f}s", level="warn", index=index)
        return
    role_type = res.get("role_type")
    if role_type and role_type != "Other":
        score = res.get("match_score")
        score_txt = f" — {score}/100" if score is not None else ""
        emit_log(f"✓ {label}{score_txt} · {call_elapsed:.0f}s", level="good", index=index)
    else:
        emit_log(f"✗ {label} — dropped (Other) · {call_elapsed:.0f}s", level="drop", index=index)


def enrich_jobs(jobs: list[dict], index: int = 1) -> None:
    """Enrich each job in-place with role_type, summary, tags, and match scores.

    One headless Sonnet call per job, run in parallel. Every job whose
    enrich_one call fails outright gets one retry pass (_retry_failures)
    before its result is applied.

    Runs the pool via submit()/as_completed() rather than pool.map() so a
    "N of M" progress event and a per-job outcome log line (with call
    duration, via _timed_enrich_one) can be emitted as each call finishes,
    driving the run drawer's live count and event log — ``index`` ties those
    events to the right search.

    On the claude backend, warms the Anthropic prompt cache with one serial
    call before the parallel wave: parallel calls that start simultaneously
    would all miss the cache and each pay the cache WRITE for the large
    shared system prompt (resume + profiles). The sleep gives the cache
    write time to propagate so the parallel batch reads instead of
    re-writing. The api backend has no such cache, so it skips straight to
    the full-batch pool.
    """
    print(f"Enriching {len(jobs)} jobs (parallel calls, "
          f"scoring {'on' if scoring_enabled() else 'off'})...", flush=True)
    emit_log(f"Enriching {len(jobs)} jobs…", level="head", index=index)
    t0 = time.monotonic()
    config = load_config()
    max_workers = config.max_workers
    results: list[dict] = [None] * len(jobs)
    done = 0

    def _apply_result(i: int, result: dict, call_elapsed: float) -> None:
        nonlocal done
        results[i] = result
        done += 1
        _emit_progress(index, done, len(jobs), StepKey.ENRICH)
        _log_enrich_outcome(jobs[i], result, call_elapsed, index)

    if config.llm_backend == "api":
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_timed_enrich_one, job): i for i, job in enumerate(jobs)}
            for future in as_completed(futures):
                i = futures[future]
                result, call_elapsed = future.result()
                _apply_result(i, result, call_elapsed)
    else:
        result, call_elapsed = _timed_enrich_one(jobs[0])
        _apply_result(0, result, call_elapsed)
        if len(jobs) > 1:
            time.sleep(2)
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_timed_enrich_one, job): i
                           for i, job in enumerate(jobs[1:], start=1)}
                for future in as_completed(futures):
                    i = futures[future]
                    result, call_elapsed = future.result()
                    _apply_result(i, result, call_elapsed)

    retry_delay = API_RETRY_DELAY_S if config.llm_backend == "api" else CLAUDE_RETRY_DELAY_S
    _retry_failures(jobs, results, lambda r: r == _ENRICH_FAILURE, enrich_one,
                    max_workers, "enrich", retry_delay, index)

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


# Matches a bare LinkedIn job-view URL (https://www.linkedin.com/jobs/view/<id>/…)
# so run_scrape/process_url/main() can route to the single-job Pass 1 variant
# instead of the search-results scrape.
JOB_VIEW_RE = re.compile(r"/jobs/view/(\d+)")
BARE_JOB_ID_RE = re.compile(r"^\d+$")


def extract_single_job_id(url: str) -> str | None:
    """Return the job id if ``url`` is a LinkedIn job-view URL, else None.

    Used to detect a single-job scan (as opposed to a search-results scrape)
    from an ad-hoc ``--url``/UI-submitted URL — see scrape_single_prompt.md.
    Expects ``url`` to already be a full URL; a bare job id should be passed
    through resolve_scan_url first.
    """
    match = JOB_VIEW_RE.search(url)
    return match.group(1) if match else None


def resolve_scan_url(raw: str) -> str:
    """Expand a bare LinkedIn job id into its canonical job-view URL.

    Lets a user paste just the numeric id (e.g. copied out of a Slack
    message) into the smart-paste bar instead of a full URL — the frontend
    sends the raw digits straight through rather than duplicating this URL
    construction in JS. Anything that isn't all-digits (a full URL, or an
    empty string for the default config-driven run) passes through
    unchanged. Called once, at the top of both entry points (the web route
    and this module's CLI ``main()``), so every downstream consumer of
    ``url`` — labeling, extract_single_job_id, run_scrape's navigation — only
    ever sees a real URL.
    """
    raw = raw.strip()
    return f"https://www.linkedin.com/jobs/view/{raw}/" if BARE_JOB_ID_RE.match(raw) else raw


def run_scrape(url: str, scrape_run_id: str, index: int = 1,
              job_id: str | None = None) -> list[dict]:
    """Scrape → deterministic filter → enrich → keep only configured role types.

    ``index`` is the 1-based position of this search in the run, used to route
    progress events to the right search group in the UI drawer. When
    ``job_id`` is set, ``url`` is a single LinkedIn job-view URL and Pass 1
    fetches just that one job (scrape_single_prompt.md) instead of scraping a
    search-results page (scrape_prompt.md); everything downstream (filters,
    clean, enrich) is unchanged since both variants write the same
    ``{job_id: {...}}`` shape to the download file.
    """
    if job_id:
        prompt_file = SCRAPE_SINGLE_PROMPT_FILE
        user_message = f"""Scan this single LinkedIn job posting.

LinkedIn URL: {url}
Job ID: {job_id}
Scrape run ID: {scrape_run_id}

Follow the system prompt exactly. Fetch just this one job into the download file.
"""
    else:
        prompt_file = SCRAPE_PROMPT_FILE
        user_message = f"""Run Scout for this LinkedIn job alert.

LinkedIn URL: {url}
Scrape run ID: {scrape_run_id}

Follow the system prompt exactly. Scrape every job on page 1 into the download file.
"""

    emit(scope="search", index=index, key=StepKey.SCRAPE, status="active")
    emit_log("Scraping LinkedIn…", level="head", index=index)
    run_claude(prompt_file, user_message)

    all_jobs = load_downloaded_jobs(scrape_run_id)
    scraped = len(all_jobs) if all_jobs else 0
    emit(scope="search", index=index, key=StepKey.SCRAPE, status="done", stat=f"{scraped} scraped")
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
        emit(scope="search", index=index, key=StepKey.FILTER, status="done", stat="0 of 0 kept")
        emit(scope="search", index=index, key=StepKey.ENRICH, status="done", stat="0 kept")
        return []

    if job_id:
        job_entry = all_jobs.get(job_id)
        if not job_entry or "error" in job_entry:
            reason = job_entry.get("error") if job_entry else "no data returned"
            msg = (f"Job {job_id} could not be scanned — it doesn't exist, has "
                   f"been removed, or isn't accessible ({reason}). Nothing to save.")
            print(f"WARNING: {msg}", file=sys.stderr)
            logging.getLogger("scout").warning(msg)
            emit(scope="search", index=index, key=StepKey.FILTER, status="done", stat="invalid job")
            emit(scope="search", index=index, key=StepKey.ENRICH, status="done", stat="0 kept")
            emit_log(msg, level="warn", index=index)
            return []

    # Deterministic pre-filters — cheap, and done BEFORE enrichment so we never
    # spend a Sonnet call on a job we're going to drop anyway.
    emit(scope="search", index=index, key=StepKey.FILTER, status="active")
    existing = set(get_existing_job_ids())
    survivors = apply_deterministic_filters(all_jobs, existing)
    emit(scope="search", index=index, key=StepKey.FILTER, status="done",
         stat=f"{len(survivors)} of {len(all_jobs)} kept")
    emit_log(f"Filter: {len(survivors)} of {len(all_jobs)} kept",
             level="info", index=index)

    print(f"{len(all_jobs)} scraped; {len(survivors)} survive deterministic "
          f"filters (already-in-DB / applied / closed / excluded companies).")
    if not survivors:
        emit(scope="search", index=index, key=StepKey.ENRICH, status="done", stat="0 kept")
        return []

    # Description cleaning: strip EEO boilerplate / benefits tail before Sonnet.
    emit(scope="search", index=index, key=StepKey.CLEAN, status="active")
    clean_jobs(survivors, index)
    emit(scope="search", index=index, key=StepKey.CLEAN, status="done",
         stat=f"{len(survivors)} cleaned")

    # Per-job enrichment: role_type (configured roles / Other) + tags + scoring.
    emit(scope="search", index=index, key=StepKey.ENRICH, status="active")
    enrich_jobs(survivors, index)

    # Keep only the configured role types; drop Other (and any that failed to enrich).
    role_names = {role.name for role in load_roles()}
    kept = [j for j in survivors if j.get("role_type") in role_names]
    emit(scope="search", index=index, key=StepKey.ENRICH, status="done",
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
    job_id: str | None = None,
) -> bool:
    """Create a scrape run, scrape + enrich + filter, and save results.

    ``index`` positions this search within the run for UI progress.
    ``job_id`` routes the scrape to the single-job Pass 1 variant (see
    run_scrape); when set and ``search_name`` was left at its default, the
    scrape run is labeled "Single job scan" instead of "Manual run".
    Returns True if the search was fully processed (even if 0 jobs were
    saved), False if the run was aborted (e.g. the scrape timed out).
    """
    print(f"\nURL  : {url[:80]}...")
    if job_id and search_name == "Manual run":
        search_name = "Single job scan"

    run_id = create_scrape_run(search_name=search_name, linkedin_url=url)
    print(f"Run  : {run_id}")

    try:
        jobs = run_scrape(url, run_id, index, job_id=job_id)
    except TimeoutError as exc:
        emit(scope="search", index=index, key=StepKey.SCRAPE, status="error", stat="timed out")
        print(f"\nERROR: {exc}. Run aborted — nothing saved.", file=sys.stderr)
        logging.getLogger("scout").error("Scrape run %s aborted: %s", run_id, exc)
        print_token_summary()
        return False

    emit(scope="search", index=index, key=StepKey.SAVE, status="active")
    if not jobs:
        emit(scope="search", index=index, key=StepKey.SAVE, status="done", stat="0 saved")
        print("No jobs to save.")
        logging.getLogger("scout").info("Scrape run %s: no jobs to save", run_id)
        print_token_summary()
        return True

    result = save_jobs(run_id, jobs)
    emit(scope="search", index=index, key=StepKey.SAVE, status="done",
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
    parser = argparse.ArgumentParser(description="Scout job agent runner")
    parser.add_argument("--url", help="Scrape one ad-hoc LinkedIn URL, ignoring config")
    parser.add_argument("--log-model-calls", action="store_true",
                        help="Log every Claude call (model, system prompt, user "
                             "message) to model_calls.log in the configured log dir")
    args = parser.parse_args()

    validate_setup()
    log = setup_logging()
    llm_common._log_model_calls = args.log_model_calls
    init_db()
    log.info("Run started (source=%s, model call logging %s)",
             "manual URL" if args.url else "config",
             "on" if llm_common._log_model_calls else "off")

    # Tell the drawer which backend/models are driving this run. Pass 1 (the
    # browser scrape) always runs on Claude even when Pass 2/3 are on the api backend.
    config = load_config()
    is_api = config.llm_backend == "api"
    models = {
        "scrape": SCRAPER_MODEL,
        "clean": config.api_model if is_api else CLEAN_MODEL,
        "enrich": config.api_model if is_api else ENRICH_MODEL,
    }
    emit(scope="meta", backend=config.llm_backend, models=models)
    emit_log(f"Run started · backend={config.llm_backend}", level="head")

    # Load/warm the model now (before Pass 1) rather than letting the
    # first clean call eat the multi-minute cold start — see _warm_api_llm.
    # _warm_up_clean_pass follows with a real, realistically-sized clean call
    # so a server that can't handle full-size prompts fails here, not silently
    # mid-run — see its docstring.
    if is_api:
        _warm_api_llm(config)
        _warm_up_clean_pass(config)

    emit(scope="global", key=StepKey.START, status="done")

    if args.url:
        url = resolve_scan_url(args.url)
        job_id = extract_single_job_id(url)
        process_url(url=url, index=1, job_id=job_id)
        log.info("Run finished (1 URL)")
        return

    searches = config.linkedin_searches
    for i, search in enumerate(searches, 1):
        emit_log(f"Search {i}/{len(searches)}: {search.name}", level="head", index=i)
        process_url(url=search.url, search_name=search.name, index=i)

    log.info("Run finished (%d search%s)", len(searches), "es" if len(searches) != 1 else "")


if __name__ == "__main__":
    main()
