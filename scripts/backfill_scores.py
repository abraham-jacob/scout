"""Backfill match scores for existing jobs, or recompute them for free.

Two modes:

**Default (LLM backfill)** — re-runs the enrichment call over the stored
description_raw of every job with no fit_score yet, and updates ONLY the
scoring columns (fit_score, criteria_score, dealbreakers, match_reason,
match_score). role_type, description_summary, and tags are left untouched.
Requires the profile files (see profiles/README.md).

**--recompute** — no LLM calls at all: rebuilds match_score for every job
that has a stored fit_score, using the current [scoring] weights and cap in
profiles/config.toml. Use after changing weights or cap policy.

Usage:
    pipenv run python -m scripts.backfill_scores              # score all unscored jobs
    pipenv run python -m scripts.backfill_scores --limit 5    # trial run on 5 jobs
    pipenv run python -m scripts.backfill_scores --dry-run    # show what would be scored
    pipenv run python -m scripts.backfill_scores --recompute  # re-derive final scores only
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.runner import (  # noqa: E402
    compute_match_score,
    enrich_one,
    print_token_summary,
    scoring_enabled,
)
from app.database import get_connection, init_db  # noqa: E402

MAX_WORKERS = 8


def fetch_unscored(limit: int | None = None) -> list[dict]:
    """Return jobs with no fit_score yet (fit_score IS NULL), oldest first."""
    conn = get_connection()
    sql = """
        SELECT job_id, title, description_raw, description_clean
        FROM jobs
        WHERE fit_score IS NULL
        ORDER BY date_scraped ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [
        {"job_id": r[0], "title": r[1], "description_raw": r[2], "description_clean": r[3]}
        for r in rows
    ]


def recompute() -> None:
    """Re-derive match_score from the stored subscores — zero LLM calls."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT job_id, fit_score, criteria_score, dealbreakers
        FROM jobs WHERE fit_score IS NOT NULL
    """).fetchall()
    for job_id, fit, criteria, dealbreakers in rows:
        score = compute_match_score(fit, criteria, dealbreakers or [])
        conn.execute(
            "UPDATE jobs SET match_score = ? WHERE job_id = ?",
            [score, job_id],
        )
    conn.close()
    print(f"Recomputed match_score for {len(rows)} job(s).")


def backfill(limit: int | None = None, dry_run: bool = False) -> None:
    """Score every unscored job via parallel enrichment calls.

    Jobs whose enrichment call fails keep fit_score = NULL, so re-running the
    script retries exactly the jobs that are still missing scores. Jobs the
    model classifies as Other also stay NULL (they have no profile to score
    against).
    """
    init_db()  # ensures the scoring columns exist on older databases
    if not scoring_enabled():
        print("Scoring is not enabled: profiles/resume.md and every profile file "
              "referenced in profiles/config.toml must exist. "
              "See profiles/README.md.")
        sys.exit(1)

    jobs = fetch_unscored(limit)
    if not jobs:
        print("Nothing to do — every job already has a fit score.")
        return

    if dry_run:
        print(f"Would score {len(jobs)} job(s):")
        for job in jobs:
            print(f"  {job['job_id']}  {job['title']}")
        return

    print(f"Scoring {len(jobs)} job(s) ({MAX_WORKERS} parallel Sonnet calls)...")
    t0 = time.monotonic()
    # One serial call first so the parallel batch reads the cached system
    # prompt instead of racing MAX_WORKERS cache writes (see enrich_jobs).
    results = [enrich_one(jobs[0])]
    if len(jobs) > 1:
        time.sleep(2)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            results += list(pool.map(enrich_one, jobs[1:]))

    conn = get_connection()
    scored, skipped_other, failed = 0, 0, 0
    for job, res in zip(jobs, results):
        # role_type=None signals the enrichment call itself failed — leave the
        # row NULL so a re-run picks it up again.
        if res.get("role_type") is None:
            failed += 1
            print(f"  FAILED (left unscored): {job['job_id']}  {job['title']}")
            continue
        if res.get("fit_score") is None:
            # Classified Other (or model declined to score) — nothing to store.
            skipped_other += 1
            print(f"  no score (role_type={res.get('role_type')}): "
                  f"{job['job_id']}  {job['title']}")
            continue
        conn.execute(
            """
            UPDATE jobs
            SET fit_score = ?, criteria_score = ?, dealbreakers = ?,
                match_reason = ?, match_score = ?
            WHERE job_id = ?
            """,
            [
                res["fit_score"],
                res.get("criteria_score"),
                res.get("dealbreakers") or [],
                res.get("match_reason"),
                res.get("match_score"),
                job["job_id"],
            ],
        )
        scored += 1
        print(f"  {job['job_id']}  {job['title']}\n"
              f"    -> match={res.get('match_score')} (fit={res['fit_score']}, "
              f"criteria={res.get('criteria_score')}, "
              f"dealbreakers={res.get('dealbreakers') or []})")
    conn.close()

    print(f"\nDone in {time.monotonic() - t0:.0f}s — "
          f"{scored} scored, {skipped_other} unscorable, {failed} failed")
    print_token_summary()


def main() -> None:
    """Parse CLI arguments and run the backfill or recompute."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None,
                        help="only process the first N unscored jobs")
    parser.add_argument("--dry-run", action="store_true",
                        help="list unscored jobs without calling the model")
    parser.add_argument("--recompute", action="store_true",
                        help="re-derive match_score from stored subscores (no LLM calls)")
    args = parser.parse_args()
    if args.recompute:
        recompute()
    else:
        backfill(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
