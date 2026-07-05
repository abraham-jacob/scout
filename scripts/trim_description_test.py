"""Dry-run test for description trimming heuristics.

Reads description_raw for every job in the database, applies the boilerplate
trimming logic, and prints a per-job comparison plus aggregate stats. Nothing
is written to the database.

Usage:
    pipenv run python -m scripts.trim_description_test
    pipenv run python -m scripts.trim_description_test --show-cut   # print what was trimmed
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import get_connection  # noqa: E402

_BOILERPLATE_RE = re.compile(
    r"equal opportunity employer"
    r"|we do not discriminate"
    r"|affirmative action"
    r"|\breasonable accommodation\b"
    r"|pursuant to .{0,40} law"
    r"|we are committed to (?:building )?a diverse"
    r"|\beeo\b",
    re.IGNORECASE,
)

_MAX_DESCRIPTION_CHARS = 5000


def _trim_description(text: str) -> tuple[str, str | None]:
    """Return (trimmed_text, matched_pattern_or_None).

    Searches only the second half of the text to avoid false-positives in
    job requirement copy.
    """
    if not text:
        return text, None
    search_start = len(text) // 2
    m = _BOILERPLATE_RE.search(text, search_start)
    matched = m.group(0) if m else None
    if m:
        text = text[: m.start()].rstrip()
    capped = len(text) > _MAX_DESCRIPTION_CHARS
    text = text[:_MAX_DESCRIPTION_CHARS]
    trigger = matched or ("cap" if capped else None)
    return text, trigger


def main() -> None:
    """Fetch all jobs, apply trimming, and print a comparison report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--show-cut",
        action="store_true",
        help="print the first 200 chars that were cut from each trimmed job",
    )
    args = parser.parse_args()

    conn = get_connection()
    rows = conn.execute(
        "SELECT job_id, title, company, description_raw FROM jobs "
        "WHERE description_raw IS NOT NULL ORDER BY date_scraped DESC"
    ).fetchall()
    conn.close()

    if not rows:
        print("No jobs with description_raw found in the database.")
        return

    total_before = total_after = trimmed_count = capped_count = 0
    print(f"{'JOB':>12}  {'COMPANY':<22} {'BEFORE':>7} {'AFTER':>7} {'SAVED':>6}  TRIGGER")
    print("-" * 80)

    for job_id, title, company, raw in rows:
        before = len(raw)
        trimmed, trigger = _trim_description(raw)
        after = len(trimmed)
        saved = before - after
        pct = saved / before * 100 if before else 0

        total_before += before
        total_after += after
        if trigger:
            trimmed_count += 1
        if trigger == "cap":
            capped_count += 1

        trigger_label = f'"{trigger[:30]}"' if trigger and trigger != "cap" else (trigger or "—")
        short_company = (company or "")[:22]
        print(f"{job_id[:12]:>12}  {short_company:<22} {before:>7,} {after:>7,} "
              f"{pct:>5.0f}%  {trigger_label}")

        if args.show_cut and saved > 0:
            cut_text = raw[after: after + 200].replace("\n", " ")
            print(f"             cut: «{cut_text}»")

    total_saved = total_before - total_after
    pct_total = total_saved / total_before * 100 if total_before else 0
    print("-" * 80)
    print(f"  {len(rows)} jobs   before: {total_before:,} chars   after: {total_after:,} chars   "
          f"saved: {total_saved:,} ({pct_total:.0f}%)")
    print(f"  regex match: {trimmed_count - capped_count}   "
          f"cap only: {capped_count}   "
          f"unchanged: {len(rows) - trimmed_count}")


if __name__ == "__main__":
    main()
