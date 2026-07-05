"""One-time backfill: populate the jobs.tags column for existing rows.

Re-runs the enrichment call (agent/enrichment_prompt.md, Sonnet) over the
stored description_raw of every job whose tags column is still NULL, and
updates ONLY the tags column — role_type and description_summary are left
untouched so already-triaged jobs cannot be reclassified.

Usage:
    pipenv run python -m scripts.backfill_tags            # backfill all untagged jobs
    pipenv run python -m scripts.backfill_tags --limit 5  # trial run on 5 jobs
    pipenv run python -m scripts.backfill_tags --dry-run  # show what would be tagged
"""

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.runner import enrich_one, print_token_summary  # noqa: E402
from app.database import get_connection, init_db  # noqa: E402

MAX_WORKERS = 8


def fetch_untagged(limit: int | None = None) -> list[dict]:
    """Return jobs with no tags yet (tags IS NULL), oldest first."""
    conn = get_connection()
    sql = """
        SELECT job_id, title, description_raw
        FROM jobs
        WHERE tags IS NULL
        ORDER BY date_scraped ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [
        {"job_id": r[0], "title": r[1], "description_raw": r[2]}
        for r in rows
    ]


def backfill(limit: int | None = None, dry_run: bool = False) -> None:
    """Tag every untagged job via parallel enrichment calls and save the tags.

    Jobs whose enrichment call fails keep tags = NULL, so re-running the
    script retries exactly the jobs that are still missing tags.
    """
    init_db()  # ensures the tags column exists on older databases
    jobs = fetch_untagged(limit)
    if not jobs:
        print("Nothing to do — every job already has tags.")
        return

    if dry_run:
        print(f"Would tag {len(jobs)} job(s):")
        for job in jobs:
            print(f"  {job['job_id']}  {job['title']}")
        return

    print(f"Tagging {len(jobs)} job(s) ({MAX_WORKERS} parallel Sonnet calls)...")
    t0 = time.monotonic()
    # One serial call first so the parallel batch reads the cached system
    # prompt instead of racing MAX_WORKERS cache writes (see enrich_jobs).
    results = [enrich_one(jobs[0])]
    if len(jobs) > 1:
        time.sleep(2)
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            results += list(pool.map(enrich_one, jobs[1:]))

    conn = get_connection()
    tagged, failed = 0, 0
    for job, res in zip(jobs, results):
        # role_type=None signals the enrichment call itself failed — leave the
        # row NULL so a re-run picks it up again.
        if res.get("role_type") is None:
            failed += 1
            print(f"  FAILED (left untagged): {job['job_id']}  {job['title']}")
            continue
        tags = res.get("tags") or []
        conn.execute(
            "UPDATE jobs SET tags = ? WHERE job_id = ?",
            [tags, job["job_id"]],
        )
        tagged += 1
        print(f"  {job['job_id']}  {job['title']}\n    -> {tags}")
    conn.close()

    print(f"\nDone in {time.monotonic() - t0:.0f}s — {tagged} tagged, {failed} failed")
    print_token_summary()


def main() -> None:
    """Parse CLI arguments and run the backfill."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None,
                        help="only process the first N untagged jobs")
    parser.add_argument("--dry-run", action="store_true",
                        help="list untagged jobs without calling the model")
    args = parser.parse_args()
    backfill(limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
