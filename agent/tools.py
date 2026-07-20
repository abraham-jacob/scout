"""
DB-layer tools for the Scout agent.

Two roles:
  1. Python helpers used by runner.py directly (create_scrape_run, save_jobs, etc.)
  2. Anthropic tool definitions (TOOL_DEFINITIONS) passed to the claude CLI so the
     agent can call save_jobs and get_existing_job_ids mid-run.
"""

import json
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, unquote

import duckdb

from app.config import load_config
from app.database import get_connection, find_original_job

# ---------------------------------------------------------------------------
# Anthropic tool schemas — passed to the agent so it can call back into the DB
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "save_jobs",
        "description": (
            "Save a batch of extracted job listings to the Scout database. "
            "Call this once per page (or at the end of the run) with all jobs found. "
            "The tool handles repost detection and excluded-company filtering automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scrape_run_id": {
                    "type": "string",
                    "description": "The run ID provided at the start of this scrape session.",
                },
                "jobs": {
                    "type": "array",
                    "description": "List of job objects extracted from LinkedIn.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "job_id":        {"type": "string"},
                            "title":         {"type": "string"},
                            "company":       {"type": "string"},
                            "location":      {"type": "string"},
                            "linkedin_url":  {"type": "string"},
                            "apply_platform":{"type": "string", "enum": ["easy_apply", "greenhouse", "ashby", "workday", "other"]},
                            "apply_url":     {"type": ["string", "null"]},
                            "salary_range":  {"type": ["string", "null"]},
                            "description_raw": {"type": "string"},
                        },
                        "required": ["job_id", "title", "company", "location", "linkedin_url", "apply_platform", "description_raw"],
                    },
                },
            },
            "required": ["scrape_run_id", "jobs"],
        },
    },
    {
        "name": "get_existing_job_ids",
        "description": (
            "Return the set of job_ids already in the database for this search alert. "
            "Use this at the start of a run to skip jobs already scraped."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "role_type": {
                    "type": "string",
                    "description": "One of the role-type names configured in "
                                   "profiles/config.toml (e.g. 'Manager', 'IC').",
                },
            },
            "required": ["role_type"],
        },
    },
]

# ---------------------------------------------------------------------------
# Python implementations (called by runner.py and by the tool dispatcher)
# ---------------------------------------------------------------------------

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
    role_type: str,
) -> str:
    """Insert a new scrape_run row and return its run_id."""
    run_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO scrape_runs (run_id, search_name, linkedin_search_url, role_type)
        VALUES (?, ?, ?, ?)
        """,
        [run_id, search_name, linkedin_url, role_type],
    )
    conn.close()
    return run_id


def get_existing_job_ids(role_type: str | None = None) -> list[str]:
    """Return active job_ids already in the DB, optionally filtered by role_type.

    Pass role_type=None (the default) for every active job_id — a global,
    role-agnostic dedup skip-list. Since a job_id is globally unique and its
    role is derived from its title at save time, dedup no longer needs the
    run's role guessed from the URL beforehand.
    """
    conn = get_connection()
    if role_type is None:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE status != 'dismissed'"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE role_type = ? AND status != 'dismissed'",
            [role_type],
        ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def save_jobs(scrape_run_id: str, jobs: list[dict]) -> dict:
    """
    Persist a list of enriched jobs. Skips excluded companies (config
    [filters] exclude_companies), detects reposts, and persists each job's
    role_type, description_summary, and tags (produced by the per-job
    enrichment step in runner.py). Returns a summary of what was saved vs
    skipped.
    """
    excluded = {c.lower() for c in load_config().exclude_companies}
    saved, skipped_existing, skipped_excluded, reposts = 0, 0, 0, 0
    conn = get_connection()

    for job in jobs:
        # `or ""` (not a .get default): the scrape can yield company = None.
        company = job.get("company") or ""

        if company.lower().strip() in excluded:
            skipped_excluded += 1
            continue

        job_id = job["job_id"]

        # Check if already in DB
        exists = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ?", [job_id]
        ).fetchone()
        if exists:
            skipped_existing += 1
            continue

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
        "skipped_already_exists": skipped_existing,
        "skipped_excluded_company": skipped_excluded,
    }


def dispatch_tool(tool_name: str, tool_input: dict) -> str:
    """
    Called by runner.py when the agent requests a tool call.
    Returns the result as a JSON string.
    """
    if tool_name == "save_jobs":
        result = save_jobs(tool_input["scrape_run_id"], tool_input["jobs"])
    elif tool_name == "get_existing_job_ids":
        result = get_existing_job_ids(tool_input["role_type"])
    else:
        result = {"error": f"Unknown tool: {tool_name}"}
    return json.dumps(result)
