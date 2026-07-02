"""
Scout agent runner.

Two Claude passes, orchestrated here:

1. A browser subprocess (Haiku, system_prompt.md) navigates LinkedIn and scrapes
   EVERY job on page 1 into /tmp/scout_<run_id>.json. It does no filtering — the
   descriptions are multi-KB, so to dodge the Chrome extension's privacy filter
   the data rides the download file rather than the tool return value.
2. This runner reads that file, applies cheap deterministic filters (already in
   DB / applied / closed / Capital One), then fires one headless Sonnet call per
   surviving job (enrichment_prompt.md, in parallel) to classify it as
   IC / Manager / Other and summarize the description. Jobs classified Other (or
   that fail to enrich) are dropped; IC/Manager jobs are saved.

Usage:
    python -m agent.runner                 # reads Gmail for URLs
    python -m agent.runner --url <url>     # specific URL
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.database import init_db
from app.gmail import get_job_alert_emails
from agent.tools import create_scrape_run, save_jobs, get_existing_job_ids

BASE_DIR = Path(__file__).parent.parent
PROMPT_DIR = BASE_DIR / "agent"
SYSTEM_PROMPT_FILE = PROMPT_DIR / "system_prompt.md"
ENRICH_PROMPT_FILE = PROMPT_DIR / "enrichment_prompt.md"

# Browser scrape runs on Haiku (cheap, mechanical); per-job enrichment runs on
# Sonnet (the classification + summary is the judgment step).
SCRAPER_MODEL = "claude-haiku-4-5-20251001"
ENRICH_MODEL = "claude-sonnet-4-6"

# Hard wall-clock cap on each claude subprocess (the browser scrape and each
# enrichment call). Past this we kill the subprocess so a runaway or stuck agent
# can't hang the run indefinitely.
SUBPROCESS_TIMEOUT_S = 240  # 4 minutes

# Where the agent hands off the downloaded job batch. The browser can only save
# to the Downloads folder, so the agent moves it to /tmp; we read /tmp first and
# fall back to Downloads in case the move did not happen.
TMP_DIR = Path("/tmp")
DOWNLOADS_DIR = Path.home() / "Downloads"

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
    """Read the window.__jobs blob the browser agent downloaded for this run.

    The browser download writes scout_<run_id>.json to Downloads; the agent
    moves it into /tmp. Fall back to Downloads if the move didn't happen.
    """
    for path in (TMP_DIR / f"scout_{run_id}.json", DOWNLOADS_DIR / f"scout_{run_id}.json"):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return None


def _file_job_to_record(job_id: str, obj: dict) -> dict:
    """Map a downloaded window.__jobs entry to the save_jobs schema.

    role_type and description_summary are added later by the enrichment step.
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

    cmd = [
        "claude",
        "--print",
        "--model", SCRAPER_MODEL,
        "--verbose",
        "--chrome",
        "--dangerously-skip-permissions",
        "--exclude-dynamic-system-prompt-sections",
        "--system-prompt", system_prompt_file.read_text(),
        "--output-format", "stream-json",
        user_message,
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(BASE_DIR),
        start_new_session=True,  # own process group so we can kill the whole tree
    )

    # Guardrail: hard-kill the whole subprocess group if it runs past the cap so
    # a stuck or runaway browser agent can't hang the run indefinitely.
    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

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
# Pass 2 — per-job enrichment (Sonnet, parallel)
# ---------------------------------------------------------------------------

def enrich_one(job: dict) -> dict:
    """Classify one job (IC / Manager / Other) and summarize it.

    Fires a single headless Sonnet call (enrichment_prompt.md) with the job's
    title + description. Returns {"role_type": ..., "description_summary": ...};
    on any failure returns role_type=None so the job is dropped downstream.
    """
    title = job.get("title") or ""
    desc = job.get("description_raw") or ""
    user_message = f"Job title: {title}\n\nJob description:\n{desc}"

    cmd = [
        "claude",
        "--print",
        "--model", ENRICH_MODEL,
        "--exclude-dynamic-system-prompt-sections",
        "--system-prompt", ENRICH_PROMPT_FILE.read_text(),
        "--output-format", "json",
        user_message,
    ]

    try:
        proc = subprocess.run(
            cmd, cwd=str(BASE_DIR), capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
        )
        envelope = json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        print(f"  enrich TIMEOUT (> {SUBPROCESS_TIMEOUT_S}s) for "
              f"{job.get('job_id')} — killed, dropping job", file=sys.stderr)
        return {"role_type": None, "description_summary": None}
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
        print(f"  enrich failed for {job.get('job_id')}: {exc}", file=sys.stderr)
        return {"role_type": None, "description_summary": None}

    _add_usage(
        envelope.get("usage", {}),
        envelope.get("total_cost_usd", envelope.get("cost_usd", 0.0)),
    )
    parsed = _extract_json(envelope.get("result", ""))
    return {
        "role_type": parsed.get("role_type"),
        "description_summary": parsed.get("description_summary"),
    }


def enrich_jobs(jobs: list[dict]) -> None:
    """Enrich each job in-place with role_type + description_summary.

    One headless Sonnet call per job, run in parallel.
    """
    print(f"Enriching {len(jobs)} jobs (parallel Sonnet calls)...", flush=True)
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(enrich_one, jobs))
    for job, res in zip(jobs, results):
        job["role_type"] = res.get("role_type")
        job["description_summary"] = res.get("description_summary")
    print(f"Enrichment done in {time.monotonic() - t0:.0f}s", flush=True)


# ---------------------------------------------------------------------------
# Scrape orchestration
# ---------------------------------------------------------------------------

def apply_deterministic_filters(all_jobs: dict, existing_ids: set) -> list[dict]:
    """Drop jobs we already know to exclude, cheaply, before the LLM step.

    Excludes: scrape-error entries, jobs already in the DB, jobs already applied
    to, closed listings (jobState != "LISTED"), and Capital One. Returns the
    surviving job records (save_jobs schema) to be enriched.
    """
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
        if (obj.get("company") or "").lower().strip() == "capital one":
            continue
        survivors.append(_file_job_to_record(job_id, obj))
    return survivors


def run_scrape(url: str, scrape_run_id: str) -> list[dict]:
    """Scrape → deterministic filter → enrich → keep only IC/Manager jobs."""
    user_message = f"""Run Scout for this LinkedIn job alert.

LinkedIn URL: {url}
Scrape run ID: {scrape_run_id}

Follow the system prompt exactly. Scrape every job on page 1 into the download file.
"""

    run_claude(SYSTEM_PROMPT_FILE, user_message)

    all_jobs = load_downloaded_jobs(scrape_run_id)
    if all_jobs is None:
        print(f"WARNING: no downloaded job file for run {scrape_run_id} "
              f"(checked /tmp and Downloads). Nothing to save.", file=sys.stderr)
        return []

    # Deterministic pre-filters — cheap, and done BEFORE enrichment so we never
    # spend a Sonnet call on a job we're going to drop anyway.
    existing = set(get_existing_job_ids())
    survivors = apply_deterministic_filters(all_jobs, existing)

    print(f"{len(all_jobs)} scraped; {len(survivors)} survive deterministic "
          f"filters (already-in-DB / applied / closed / Capital One).")
    if not survivors:
        return []

    # Per-job enrichment: role_type (IC/Manager/Other) + description_summary.
    enrich_jobs(survivors)

    # Keep only IC / Manager; drop Other (and any that failed to enrich).
    kept = [j for j in survivors if j.get("role_type") in ("IC", "Manager")]
    print(f"Enriched {len(survivors)}; kept {len(kept)} IC/Manager, "
          f"dropped {len(survivors) - len(kept)} Other/failed.")
    return kept


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_url(url: str, email_subject: str = "Manual run", email_date: str = "") -> None:
    """Create a scrape run, scrape + enrich + filter, and save results."""
    print(f"\nURL  : {url[:80]}...")

    run_id = create_scrape_run(
        email_subject=email_subject,
        email_date=email_date,
        linkedin_url=url,
        role_type=None,  # a run no longer has a single role; role is per-job
    )
    print(f"Run  : {run_id}")

    try:
        jobs = run_scrape(url, run_id)
    except TimeoutError as exc:
        print(f"\nERROR: {exc}. Run aborted — nothing saved.", file=sys.stderr)
        print_token_summary()
        return

    if not jobs:
        print("No jobs to save.")
        print_token_summary()
        return

    result = save_jobs(run_id, jobs)
    print(f"\nSave result: {result}")
    print_token_summary()


def process_email(email: dict) -> None:
    """Run a full scrape for a single Gmail alert email."""
    url = email.get("see_all_jobs_url")
    if not url:
        print(f"No URL found in email: {email.get('subject')}")
        return

    print(f"\nSubject : {email.get('subject')}")
    process_url(
        url=url,
        email_subject=email.get("subject", ""),
        email_date=email.get("date", ""),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Scout job agent runner")
    parser.add_argument("--url", help="Scrape a specific LinkedIn URL (skips Gmail)")
    parser.add_argument("--max-emails", type=int, default=5, help="Max Gmail emails to process")
    args = parser.parse_args()

    init_db()

    if args.url:
        process_url(url=args.url)
        return

    emails = get_job_alert_emails(max_results=args.max_emails)
    if not emails:
        print("No unread job alert emails found.")
        return

    for email in emails:
        process_email(email)


if __name__ == "__main__":
    main()
