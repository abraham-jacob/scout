"""Load extracted_jobs.json into the Scout DuckDB for UI testing."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import init_db
from agent.tools import create_scrape_run, save_jobs

DATA_FILE = Path(__file__).parent.parent / "data" / "extracted_jobs.json"


def main():
    """Import sample jobs from extracted_jobs.json into DuckDB."""
    init_db()

    data = json.loads(DATA_FILE.read_text())
    meta = data["scrape_metadata"]
    jobs = data["jobs"]

    run_id = create_scrape_run(
        search_name="Sample data import",
        linkedin_url=meta["source_url"],
        role_type=meta["role_type"],
    )
    print(f"Created scrape run: {run_id}")

    result = save_jobs(run_id, jobs)
    print(f"Saved    : {result['saved']} jobs")
    print(f"Reposts  : {result['reposts_detected']}")
    print(f"Skipped  : {result['skipped_already_exists']} already existed")
    print(f"Filtered : {result['skipped_excluded_company']} excluded companies")


if __name__ == "__main__":
    main()
