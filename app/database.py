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

APPLY_PLATFORMS = ["greenhouse", "ashby", "workday", "easy_apply", "other"]


def get_connection() -> duckdb.DuckDBPyConnection:
    """Return a connection to the Scout DuckDB database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))


def init_db():
    """Create all tables if they do not already exist."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scrape_runs (
            run_id          VARCHAR PRIMARY KEY,
            email_subject   VARCHAR,
            email_date      TIMESTAMP,
            linkedin_search_url VARCHAR,
            role_type       VARCHAR,
            jobs_found      INTEGER DEFAULT 0,
            run_at          TIMESTAMP DEFAULT current_timestamp
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id              VARCHAR PRIMARY KEY,
            scrape_run_id       VARCHAR REFERENCES scrape_runs(run_id),
            title               VARCHAR,
            company             VARCHAR,
            location            VARCHAR,
            job_type            VARCHAR,
            role_type           VARCHAR,
            description_raw     VARCHAR,
            description_summary VARCHAR,
            match_score         FLOAT,
            linkedin_url        VARCHAR,
            apply_url           VARCHAR,
            apply_platform      VARCHAR,
            salary_range        VARCHAR,
            status              VARCHAR DEFAULT 'new',
            seen                BOOLEAN DEFAULT false,
            is_repost           BOOLEAN DEFAULT false,
            original_job_id     VARCHAR,
            date_scraped        TIMESTAMP DEFAULT current_timestamp,
            applied_at          TIMESTAMP,
            rejected_at         TIMESTAMP
        )
    """)
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
