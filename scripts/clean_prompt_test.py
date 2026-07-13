"""Evaluate the Pass 2 clean prompt against the configured LLM backend.

Samples N random jobs (reproducibly, via --seed) that have a raw description in
the database, runs each through the real production cleaning path
(agent.runner.clean_one -> run_headless -> whichever [llm] backend is configured),
and writes a markdown report pairing each raw description with its cleaned output
plus char-reduction stats. By default nothing is written to the database; pass
--write-db to persist each successful cleaning into jobs.description_clean (jobs
where clean_one failed / returned None are left untouched, so a bad run can't
wipe out an existing cleaned description).

The report is the input to a separate Sonnet judge pass, which scores each
cleaning against the rubric in agent/clean_prompt.md: boilerplate (EEO/DEI,
benefits, culture, generic "About" copy) should be gone while responsibilities,
qualifications, tech, comp, and location survive.

Usage:
    pipenv run python -m scripts.clean_prompt_test
    pipenv run python -m scripts.clean_prompt_test --n 12 --seed 42
    pipenv run python -m scripts.clean_prompt_test --report /path/to/report.md
    pipenv run python -m scripts.clean_prompt_test --write-db
"""

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import runner  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import get_connection  # noqa: E402


def _sample_jobs(n: int, seed: int) -> list[dict]:
    """Return up to n random jobs (job_id/title/company/description_raw) as dicts.

    Fetches every job with a non-empty raw description, then takes a reproducible
    random sample so the same seed yields the same set across runs (needed to
    compare a prompt before and after a tweak on identical inputs).
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT job_id, title, company, description_raw FROM jobs "
        "WHERE description_raw IS NOT NULL AND length(description_raw) > 0"
    ).fetchall()
    conn.close()

    jobs = [
        {"job_id": r[0], "title": r[1], "company": r[2], "description_raw": r[3]}
        for r in rows
    ]
    rng = random.Random(seed)
    rng.shuffle(jobs)
    return jobs[:n]


def _clean_sample(jobs: list[dict]) -> list[dict]:
    """Run clean_one on each job serially, timing it, returning result records.

    Each record carries the raw text, the cleaned text (or None on failure), the
    before/after char counts, and the elapsed seconds. Serial on purpose so the
    per-call latency is visible and output stays ordered.
    """
    results = []
    for i, job in enumerate(jobs, 1):
        raw = job["description_raw"]
        print(f"  [{i}/{len(jobs)}] cleaning {job['job_id']} "
              f"({job.get('company') or '—'})...", flush=True)
        t0 = time.monotonic()
        out = runner.clean_one(job)
        elapsed = time.monotonic() - t0
        cleaned = out.get("description_clean") if out else None
        results.append({
            "job_id": job["job_id"],
            "title": job.get("title"),
            "company": job.get("company"),
            "raw": raw,
            "cleaned": cleaned,
            "before": len(raw),
            "after": len(cleaned) if cleaned else 0,
            "elapsed": elapsed,
        })
    return results


def _write_report(results: list[dict], path: Path, backend: str,
                  model: str | None) -> None:
    """Write the full raw-vs-cleaned markdown report for the Sonnet judge pass."""
    lines = ["# Clean-prompt evaluation report", ""]
    lines.append(f"- backend: `{backend}`" + (f" (model `{model}`)" if model else ""))
    lines.append(f"- jobs: {len(results)}")
    lines.append("")
    for i, r in enumerate(results, 1):
        pct = (r["before"] - r["after"]) / r["before"] * 100 if r["before"] else 0
        lines.append(f"## {i}. {r['title'] or '(no title)'} — {r['company'] or '—'}")
        lines.append("")
        lines.append(f"`{r['job_id']}` · {r['before']:,} → {r['after']:,} chars "
                     f"({pct:.0f}% removed) · {r['elapsed']:.1f}s")
        lines.append("")
        if r["cleaned"] is None:
            lines.append("**CLEAN FAILED** (returned None — would fall back to raw)")
            lines.append("")
        lines.append("### RAW")
        lines.append("")
        lines.append("```")
        lines.append(r["raw"])
        lines.append("```")
        lines.append("")
        lines.append("### CLEANED")
        lines.append("")
        lines.append("```")
        lines.append(r["cleaned"] if r["cleaned"] is not None else "(none)")
        lines.append("```")
        lines.append("")
    path.write_text("\n".join(lines))


def _write_to_db(results: list[dict]) -> int:
    """Persist each successful cleaning into jobs.description_clean.

    Skips jobs where clean_one returned None (a failed call) so a bad run can't
    overwrite an existing cleaned description with nothing. Returns the count of
    rows actually updated.
    """
    conn = get_connection()
    updated = 0
    for r in results:
        if r["cleaned"] is None:
            continue
        conn.execute(
            "UPDATE jobs SET description_clean = ? WHERE job_id = ?",
            [r["cleaned"], r["job_id"]],
        )
        updated += 1
    conn.close()
    return updated


def _print_summary(results: list[dict]) -> None:
    """Print a compact per-job table and aggregate stats to stdout."""
    print()
    print(f"{'JOB':>12}  {'COMPANY':<20} {'BEFORE':>7} {'AFTER':>7} {'SAVED':>6} "
          f"{'SECS':>6}  STATUS")
    print("-" * 78)
    fails = 0
    for r in results:
        pct = (r["before"] - r["after"]) / r["before"] * 100 if r["before"] else 0
        status = "ok" if r["cleaned"] is not None else "FAILED"
        if r["cleaned"] is None:
            fails += 1
        print(f"{r['job_id'][:12]:>12}  {(r['company'] or '')[:20]:<20} "
              f"{r['before']:>7,} {r['after']:>7,} {pct:>5.0f}% {r['elapsed']:>6.1f}  "
              f"{status}")
    print("-" * 78)
    total_before = sum(r["before"] for r in results)
    total_after = sum(r["after"] for r in results)
    pct = (total_before - total_after) / total_before * 100 if total_before else 0
    print(f"  {len(results)} jobs   {total_before:,} → {total_after:,} chars "
          f"({pct:.0f}% removed)   failures: {fails}")


def main() -> None:
    """Sample jobs, clean them on the configured backend, and report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=12, help="jobs to sample (default 12)")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for a reproducible sample (default 42)")
    parser.add_argument("--report", type=Path,
                        default=Path("clean_prompt_report.md"),
                        help="markdown report path (default ./clean_prompt_report.md)")
    parser.add_argument("--write-db", action="store_true",
                        help="persist successful cleanings into jobs.description_clean")
    args = parser.parse_args()

    config = load_config()
    model = config.local_model if config.llm_backend == "local" else None
    print(f"Backend: {config.llm_backend}"
          + (f" (model {model})" if model else "") + f", sampling {args.n} jobs "
          f"(seed {args.seed})...")

    jobs = _sample_jobs(args.n, args.seed)
    if not jobs:
        print("No jobs with description_raw found in the database.")
        return
    print(f"Cleaning {len(jobs)} jobs on the {config.llm_backend} backend...")

    results = _clean_sample(jobs)
    _print_summary(results)
    _write_report(results, args.report, config.llm_backend, model)
    print(f"\nFull raw-vs-cleaned report written to: {args.report}")

    if args.write_db:
        updated = _write_to_db(results)
        print(f"Wrote {updated}/{len(results)} cleaned descriptions to "
              f"jobs.description_clean.")


if __name__ == "__main__":
    main()
