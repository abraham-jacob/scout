"""
DB-layer tools for the Scout agent: Python helpers called directly by
runner.py (create_scrape_run, save_jobs, get_existing_job_ids, etc.).
"""

import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote

from app.config import load_config
from app.database import get_connection, find_original_job


def _unwrap_linkedin_redirect(url: str | None) -> str | None:
    """Extract the real destination from a linkedin.com/safety/go redirect URL."""
    if not url:
        return None
    if "linkedin.com/safety/go" in url:
        params = parse_qs(urlparse(url).query)
        if "url" in params:
            return unquote(params["url"][0])
    return url


def create_scrape_run(
    search_name: str,
    linkedin_url: str,
) -> str:
    """Insert a new scrape_run row and return its run_id."""
    run_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO scrape_runs (run_id, search_name, linkedin_search_url)
        VALUES (?, ?, ?)
        """,
        [run_id, search_name, linkedin_url],
    )
    conn.close()
    return run_id


def get_existing_job_ids() -> list[str]:
    """Return every active (non-dismissed) job_id already in the DB.

    A global, role-agnostic dedup skip-list: a job_id is globally unique and
    its role is derived from its title at save time, so dedup doesn't need a
    run-level role.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT job_id FROM jobs WHERE status != 'dismissed'"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def save_jobs(scrape_run_id: str, jobs: list[dict]) -> dict:
    """
    Persist a list of enriched jobs. Assumes the caller (apply_deterministic_filters,
    via one earlier get_existing_job_ids() fetch) already dropped jobs already in
    the DB. Skips excluded companies (config [filters] exclude_companies),
    detects reposts, and persists each job's role_type, description_summary,
    and tags (produced by the per-job enrichment step in runner.py). Returns
    a summary of what was saved vs skipped.
    """
    excluded = {c.lower() for c in load_config().exclude_companies}
    saved, skipped_excluded, reposts = 0, 0, 0
    conn = get_connection()

    for job in jobs:
        # `or ""` (not a .get default): the scrape can yield company = None.
        company = job.get("company") or ""

        if company.lower().strip() in excluded:
            skipped_excluded += 1
            continue

        job_id = job["job_id"]

        # Unwrap LinkedIn safety redirects on the apply URL
        job["apply_url"] = _unwrap_linkedin_redirect(job.get("apply_url"))

        # Repost detection
        original_id = find_original_job(conn, job["title"], company)
        is_repost = original_id is not None

        conn.execute(
            """
            INSERT INTO jobs (
                job_id, scrape_run_id, title, company, location, role_type,
                linkedin_url, apply_url, apply_platform, salary_range,
                description_raw, description_clean, description_summary, tags,
                fit_score, criteria_score, dealbreakers, match_reason,
                match_score, status, is_repost, original_job_id, date_scraped
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)
            """,
            [
                job_id,
                scrape_run_id,
                job["title"],
                company,
                job.get("location"),
                job.get("role_type"),
                job.get("linkedin_url"),
                job.get("apply_url"),
                job.get("apply_platform", "other"),
                job.get("salary_range"),
                job.get("description_raw"),
                job.get("description_clean"),
                job.get("description_summary"),
                job.get("tags") or [],
                job.get("fit_score"),
                job.get("criteria_score"),
                job.get("dealbreakers") or [],
                job.get("match_reason"),
                job.get("match_score"),
                is_repost,
                original_id,
                datetime.now(timezone.utc).isoformat(),
            ],
        )

        if is_repost:
            reposts += 1
        saved += 1

    # Update jobs_found count on the run
    conn.execute(
        "UPDATE scrape_runs SET jobs_found = jobs_found + ? WHERE run_id = ?",
        [saved, scrape_run_id],
    )
    conn.close()

    return {
        "saved": saved,
        "reposts_detected": reposts,
        "skipped_excluded_company": skipped_excluded,
    }
