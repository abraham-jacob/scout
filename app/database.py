import duckdb
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "scout.duckdb"

JOB_STATUSES = [
    "new",
    "saved",
    "applied",
    "interviewing_recruiter",
    "interviewing_technical",
    "offer",
    "rejected",
    "dismissed",
]


def get_connection() -> duckdb.DuckDBPyConnection:
    """Return a connection to the Scout DuckDB database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))


# Write-only audit log by design: agent/tools.py's create_scrape_run/save_jobs
# populate it every run (one row per configured search, jobs_found updated as
# jobs land), but nothing in the app currently reads it back — no route, no
# template. jobs.scrape_run_id's FK against it is real provenance (every job
# traces to the run that scraped it), so keep the table; a "run history" UI
# surfacing it is a possible future addition, not a gap to fix now.
_SCRAPE_RUNS_COLUMNS = """(
    run_id              VARCHAR PRIMARY KEY,
    search_name         VARCHAR,
    linkedin_search_url VARCHAR,
    jobs_found          INTEGER DEFAULT 0,
    run_at              TIMESTAMP DEFAULT current_timestamp
)"""

_JOBS_COLUMNS = """(
    job_id              VARCHAR PRIMARY KEY,
    scrape_run_id       VARCHAR REFERENCES scrape_runs(run_id),
    title               VARCHAR,
    company             VARCHAR,
    location            VARCHAR,
    role_type           VARCHAR,
    description_raw     VARCHAR,
    description_clean   VARCHAR,
    description_summary VARCHAR,
    match_score         FLOAT,
    fit_score           FLOAT,
    criteria_score      FLOAT,
    dealbreakers        VARCHAR[],
    match_reason        VARCHAR,
    linkedin_url        VARCHAR,
    apply_url           VARCHAR,
    apply_platform      VARCHAR,
    salary_range        VARCHAR,
    tags                VARCHAR[],
    status              VARCHAR DEFAULT 'new',
    seen                BOOLEAN DEFAULT false,
    is_repost           BOOLEAN DEFAULT false,
    original_job_id     VARCHAR,
    date_scraped        TIMESTAMP DEFAULT current_timestamp
)"""


def init_db():
    """Create all tables if they do not already exist."""
    conn = get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS scrape_runs " + _SCRAPE_RUNS_COLUMNS)
    conn.execute("CREATE TABLE IF NOT EXISTS jobs " + _JOBS_COLUMNS)
    conn.close()


def find_original_job(conn: duckdb.DuckDBPyConnection, title: str, company: str) -> str | None:
    """Return the job_id of an existing active job with the same title and company, or None."""
    row = conn.execute("""
        SELECT job_id FROM jobs
        WHERE lower(title) = lower(?)
          AND lower(company) = lower(?)
          AND status != 'dismissed'
          AND is_repost = false
        ORDER BY date_scraped ASC
        LIMIT 1
    """, [title, company]).fetchone()
    return row[0] if row else None


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DB_PATH}")
