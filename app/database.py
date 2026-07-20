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


_SCRAPE_RUNS_COLUMNS = """(
    run_id              VARCHAR PRIMARY KEY,
    search_name         VARCHAR,
    linkedin_search_url VARCHAR,
    role_type           VARCHAR,
    jobs_found          INTEGER DEFAULT 0,
    run_at              TIMESTAMP DEFAULT current_timestamp
)"""

_JOBS_COLUMNS = """(
    job_id              VARCHAR PRIMARY KEY,
    scrape_run_id       VARCHAR REFERENCES scrape_runs(run_id),
    title               VARCHAR,
    company             VARCHAR,
    location            VARCHAR,
    job_type            VARCHAR,
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
    date_scraped        TIMESTAMP DEFAULT current_timestamp,
    applied_at          TIMESTAMP,
    rejected_at         TIMESTAMP
)"""


def _migrate_scrape_runs_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """One-time migration from the Gmail-era scrape_runs schema to search_name.

    The [[linkedin_searches]] redesign replaced the Gmail-derived
    email_subject/email_date columns with a single search_name column (the
    configured search's alias); email_date has no replacement, since a
    config-driven search has no natural date source.

    DuckDB refuses any ALTER/DROP on a table that another table has a
    foreign key on (jobs.scrape_run_id references scrape_runs.run_id), even
    for unrelated columns — so an in-place ALTER TABLE isn't possible while
    jobs exists. Instead this backs up scrape_runs verbatim (preserving the
    original email_subject/email_date values as a historical record),
    rebuilds both tables from scratch inside one transaction, and copies the
    data back across — jobs is rebuilt with an unchanged schema purely to
    drop and re-add the FK, and every run_id/job_id is preserved so existing
    jobs.scrape_run_id references keep resolving correctly.

    Runs once per database: skipped if scrape_runs doesn't exist yet (fresh
    install) or already has search_name instead of email_subject (already
    migrated or already backed up).
    """
    tables = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall()}
    if "scrape_runs" not in tables or "scrape_runs_backup" in tables:
        return
    columns = {r[0] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'scrape_runs'"
    ).fetchall()}
    if "email_subject" not in columns:
        return

    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("CREATE TABLE scrape_runs_backup AS SELECT * FROM scrape_runs")
        has_jobs = "jobs" in tables
        if has_jobs:
            conn.execute("CREATE TABLE jobs_migration_tmp AS SELECT * FROM jobs")
            conn.execute("DROP TABLE jobs")
        conn.execute("DROP TABLE scrape_runs")
        conn.execute("CREATE TABLE scrape_runs " + _SCRAPE_RUNS_COLUMNS)
        conn.execute("""
            INSERT INTO scrape_runs (run_id, search_name, linkedin_search_url,
                                      role_type, jobs_found, run_at)
            SELECT run_id, email_subject, linkedin_search_url, role_type,
                   jobs_found, run_at
            FROM scrape_runs_backup
        """)
        if has_jobs:
            conn.execute("CREATE TABLE jobs " + _JOBS_COLUMNS)
            conn.execute("INSERT INTO jobs SELECT * FROM jobs_migration_tmp")
            conn.execute("DROP TABLE jobs_migration_tmp")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db():
    """Create all tables if they do not already exist."""
    conn = get_connection()
    _migrate_scrape_runs_schema(conn)
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
