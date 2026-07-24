"""One-off Pass 2 (clean_one) test against real stored job descriptions.

Pulls N jobs' raw descriptions straight out of the local DuckDB and runs
clean_one() against each, timing every call — no browser scrape, no Pass 1/3,
no writes back to the DB. Handy for sanity-checking the local LLM backend
(e.g. after a server-side tuning change) against realistically-sized prompts
without waiting on a full run.

Usage:
    pipenv run python -m scripts.test_clean_pass                # 10 random jobs
    pipenv run python -m scripts.test_clean_pass --count 5       # 5 random jobs
    pipenv run python -m scripts.test_clean_pass --recent        # N most recent instead of random
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent.runner as runner  # noqa: E402
from app.database import get_connection  # noqa: E402


def fetch_jobs(count: int, recent: bool) -> list[tuple]:
    """Return up to `count` (job_id, title, company, description_raw) rows.

    Ordered by date_scraped DESC when `recent` is set, otherwise random.
    """
    order_sql = "date_scraped DESC" if recent else "random()"
    conn = get_connection()
    rows = conn.execute(f"""
        SELECT job_id, title, company, description_raw
        FROM jobs
        WHERE description_raw IS NOT NULL AND description_raw != ''
        ORDER BY {order_sql}
        LIMIT ?
    """, [count]).fetchall()
    conn.close()
    return rows


def run_test(count: int, recent: bool) -> None:
    """Run clean_one() against `count` stored jobs and print a timed report."""
    rows = fetch_jobs(count, recent)
    if not rows:
        print("No jobs with a stored description_raw found — run a scrape first.")
        return

    label = "most recent" if recent else "random"
    print(f"Testing clean_one() against {len(rows)} {label} real jobs "
          f"(Pass 2 only, no DB writes)\n")

    elapsed_all = []
    ok_count = 0
    for i, (job_id, title, company, desc) in enumerate(rows, 1):
        job = {"job_id": job_id, "description_raw": desc}
        t0 = time.monotonic()
        result = runner.clean_one(job)
        elapsed = time.monotonic() - t0
        elapsed_all.append(elapsed)
        if result is not None:
            ok_count += 1

        status = "OK" if result is not None else "FAILED"
        preview = ""
        if result and result.get("description_clean"):
            preview = result["description_clean"][:80].replace("\n", " ")

        print(f"{i:2}/{len(rows)} [{status:6}] {elapsed:6.1f}s  "
              f"{title[:40]:40} @ {company[:25]:25}")
        if preview:
            print(f"        -> {preview}...")

    avg = sum(elapsed_all) / len(elapsed_all)
    print(f"\n{ok_count}/{len(rows)} OK — "
          f"avg {avg:.1f}s, min {min(elapsed_all):.1f}s, max {max(elapsed_all):.1f}s")


def main() -> None:
    """Parse CLI arguments and run the Pass 2 timing test."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=10,
                        help="number of jobs to test (default 10)")
    parser.add_argument("--recent", action="store_true",
                        help="use the N most recently scraped jobs instead of random")
    args = parser.parse_args()
    run_test(count=args.count, recent=args.recent)


if __name__ == "__main__":
    main()
