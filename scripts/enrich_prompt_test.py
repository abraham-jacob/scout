"""Evaluate the Pass 3 enrichment prompt against the configured LLM backend.

Samples N random jobs (reproducibly, via --seed) that already have a cleaned
description in the database, runs each through the real production enrichment
path (agent.runner.enrich_one -> run_headless -> whichever [llm] backend is
configured), and writes a markdown report pairing each cleaned description with
its classification, summary, tags, and score plus timing. By default nothing is
written to the database; pass --write-db to persist each successful enrichment
into jobs' role_type/description_summary/tags/fit_score/criteria_score/
dealbreakers/match_reason/match_score (jobs where enrich_one's call failed
outright are left untouched, so a bad run can't wipe out existing enrichment).

The report is the input to a separate Sonnet judge pass, which scores each
enrichment against the rubric in agent/enrichment_prompt.md: correct role
classification, an accurate 2-4 sentence summary, sensible tags, and a score/
dealbreakers that hold up against the candidate's resume/profiles/criteria.

Usage:
    pipenv run python -m scripts.enrich_prompt_test
    pipenv run python -m scripts.enrich_prompt_test --n 12 --seed 42
    pipenv run python -m scripts.enrich_prompt_test --report /path/to/report.md
    pipenv run python -m scripts.enrich_prompt_test --write-db
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


def _job_by_id(job_id: str) -> list[dict]:
    """Return the single job matching job_id as a one-element list, or [] if missing.

    Bypasses sampling entirely — used to re-run one specific job (e.g. to check
    whether a failure/timeout reproduces) without disturbing the seeded sample.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT job_id, title, company, description_clean FROM jobs "
        "WHERE job_id = ? AND description_clean IS NOT NULL "
        "AND length(description_clean) > 0",
        [job_id],
    ).fetchone()
    conn.close()
    if row is None:
        return []
    return [{"job_id": row[0], "title": row[1], "company": row[2],
             "description_clean": row[3]}]


def _sample_jobs(n: int, seed: int) -> list[dict]:
    """Return up to n random jobs (job_id/title/company/description_clean) as dicts.

    Fetches every job with a non-empty cleaned description, then takes a
    reproducible random sample so the same seed yields the same set across runs
    (needed to compare a prompt before and after a tweak on identical inputs).
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT job_id, title, company, description_clean FROM jobs "
        "WHERE description_clean IS NOT NULL AND length(description_clean) > 0"
    ).fetchall()
    conn.close()

    jobs = [
        {"job_id": r[0], "title": r[1], "company": r[2], "description_clean": r[3]}
        for r in rows
    ]
    rng = random.Random(seed)
    rng.shuffle(jobs)
    return jobs[:n]


def _enrich_sample(jobs: list[dict]) -> list[dict]:
    """Run enrich_one on each job serially, timing it, returning result records.

    Each record carries the input, the enrichment fields (or the failure
    sentinel), and the elapsed seconds. Serial on purpose so the per-call
    latency is visible and output stays ordered.
    """
    results = []
    for i, job in enumerate(jobs, 1):
        print(f"  [{i}/{len(jobs)}] enriching {job['job_id']} "
              f"({job.get('company') or '—'})...", flush=True)
        t0 = time.monotonic()
        out = runner.enrich_one(job)
        elapsed = time.monotonic() - t0
        results.append({
            "job_id": job["job_id"],
            "title": job.get("title"),
            "company": job.get("company"),
            "description_clean": job["description_clean"],
            "role_type": out.get("role_type"),
            "description_summary": out.get("description_summary"),
            "tags": out.get("tags") or [],
            "fit_score": out.get("fit_score"),
            "criteria_score": out.get("criteria_score"),
            "dealbreakers": out.get("dealbreakers") or [],
            "match_reason": out.get("match_reason"),
            "match_score": out.get("match_score"),
            "elapsed": elapsed,
            "failed": out == runner._ENRICH_FAILURE,
        })
    return results


def _write_report(results: list[dict], path: Path, backend: str,
                  model: str | None) -> None:
    """Write the full input-vs-enrichment markdown report for the Sonnet judge pass."""
    lines = ["# Enrich-prompt evaluation report", ""]
    lines.append(f"- backend: `{backend}`" + (f" (model `{model}`)" if model else ""))
    lines.append(f"- jobs: {len(results)}")
    lines.append("")
    for i, r in enumerate(results, 1):
        lines.append(f"## {i}. {r['title'] or '(no title)'} — {r['company'] or '—'}")
        lines.append("")
        lines.append(f"`{r['job_id']}` · {r['elapsed']:.1f}s")
        lines.append("")
        if r["failed"]:
            lines.append("**ENRICH FAILED** (call failed — job would be dropped)")
            lines.append("")
        lines.append("### INPUT (description_clean)")
        lines.append("")
        lines.append("```")
        lines.append(r["description_clean"])
        lines.append("```")
        lines.append("")
        lines.append("### ENRICHMENT OUTPUT")
        lines.append("")
        lines.append(f"- role_type: `{r['role_type']}`")
        lines.append(f"- tags: {r['tags']}")
        lines.append(f"- fit_score: {r['fit_score']}")
        lines.append(f"- criteria_score: {r['criteria_score']}")
        lines.append(f"- match_score: {r['match_score']}")
        lines.append(f"- dealbreakers: {r['dealbreakers']}")
        lines.append("")
        lines.append("**description_summary**")
        lines.append("")
        lines.append(r["description_summary"] or "(none)")
        lines.append("")
        lines.append("**match_reason**")
        lines.append("")
        lines.append(r["match_reason"] or "(none)")
        lines.append("")
    path.write_text("\n".join(lines))


def _print_summary(results: list[dict]) -> None:
    """Print a compact per-job table and aggregate stats to stdout."""
    print()
    print(f"{'JOB':>12}  {'COMPANY':<20} {'ROLE':<16} {'FIT':>5} {'CRIT':>5} "
          f"{'MATCH':>6} {'SECS':>6}  STATUS")
    print("-" * 90)
    fails = 0
    for r in results:
        status = "ok" if not r["failed"] else "FAILED"
        if r["failed"]:
            fails += 1
        fit = f"{r['fit_score']:.0f}" if r["fit_score"] is not None else "-"
        crit = f"{r['criteria_score']:.0f}" if r["criteria_score"] is not None else "-"
        match = f"{r['match_score']:.1f}" if r["match_score"] is not None else "-"
        print(f"{r['job_id'][:12]:>12}  {(r['company'] or '')[:20]:<20} "
              f"{(r['role_type'] or '—')[:16]:<16} {fit:>5} {crit:>5} {match:>6} "
              f"{r['elapsed']:>6.1f}  {status}")
    print("-" * 90)
    print(f"  {len(results)} jobs   failures: {fails}")


def _write_to_db(results: list[dict]) -> int:
    """Persist each successful enrichment into the jobs table.

    Skips jobs where enrich_one's call failed outright (the _ENRICH_FAILURE
    sentinel) so a bad run can't overwrite existing enrichment with nothing.
    "Other"/unmatched classifications from a successful call ARE written, same
    as any other real enrichment result. Returns the count of rows updated.
    """
    conn = get_connection()
    updated = 0
    for r in results:
        if r["failed"]:
            continue
        conn.execute(
            "UPDATE jobs SET role_type = ?, description_summary = ?, tags = ?, "
            "fit_score = ?, criteria_score = ?, dealbreakers = ?, match_reason = ?, "
            "match_score = ? WHERE job_id = ?",
            [r["role_type"], r["description_summary"], r["tags"], r["fit_score"],
             r["criteria_score"], r["dealbreakers"], r["match_reason"],
             r["match_score"], r["job_id"]],
        )
        updated += 1
    conn.close()
    return updated


def main() -> None:
    """Sample jobs, enrich them on the configured backend, and report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--n", type=int, default=12, help="jobs to sample (default 12)")
    parser.add_argument("--seed", type=int, default=42,
                        help="RNG seed for a reproducible sample (default 42)")
    parser.add_argument("--report", type=Path,
                        default=Path("enrich_prompt_report.md"),
                        help="markdown report path (default ./enrich_prompt_report.md)")
    parser.add_argument("--write-db", action="store_true",
                        help="persist successful enrichments into the jobs table")
    parser.add_argument("--job-id", type=str, default=None,
                        help="re-run one specific job_id instead of sampling "
                             "(e.g. to check whether a failure reproduces)")
    args = parser.parse_args()

    config = load_config()
    model = config.local_model if config.llm_backend == "local" else None

    if args.job_id:
        print(f"Backend: {config.llm_backend}"
              + (f" (model {model})" if model else "")
              + f", re-running job {args.job_id}...")
        jobs = _job_by_id(args.job_id)
        if not jobs:
            print(f"No job with a non-empty description_clean found for "
                  f"job_id {args.job_id}.")
            return
    else:
        print(f"Backend: {config.llm_backend}"
              + (f" (model {model})" if model else "") + f", sampling {args.n} jobs "
              f"(seed {args.seed})...")
        jobs = _sample_jobs(args.n, args.seed)
        if not jobs:
            print("No jobs with description_clean found in the database.")
            return
    print(f"Enriching {len(jobs)} jobs on the {config.llm_backend} backend...")

    results = _enrich_sample(jobs)
    _print_summary(results)
    _write_report(results, args.report, config.llm_backend, model)
    print(f"\nFull input-vs-enrichment report written to: {args.report}")

    if args.write_db:
        updated = _write_to_db(results)
        print(f"Wrote {updated}/{len(results)} enrichments to the jobs table.")


if __name__ == "__main__":
    main()
