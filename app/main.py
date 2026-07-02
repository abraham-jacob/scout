"""
Scout FastAPI backend.

Routes:
  GET  /                          — main page
  GET  /jobs                      — job list partial (HTMX)
  POST /scout/run                 — trigger a scrape run
  GET  /scout/status              — run status partial (HTMX polling)
  PATCH /jobs/{job_id}/status     — update job status, returns updated card
  PATCH /jobs/{job_id}/seen       — mark job as seen
"""

import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.database import JOB_STATUSES, get_connection, init_db

BASE_DIR = Path(__file__).parent.parent

app = FastAPI(title="Scout")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# In-memory run state (single-user local app — no need for DB persistence here)
_run: dict = {
    "running": False,
    "started_at": None,
    "message": "Idle",
    "error": None,
}
_run_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup() -> None:
    """Initialise the database on first start."""
    init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_jobs(
    role_type: str = "all",
    status: str = "all",
    unseen_only: bool = False,
) -> list[dict]:
    """Query jobs from DuckDB with optional filters."""
    conn = get_connection()
    where, params = [], []

    if role_type != "all":
        where.append("j.role_type = ?")
        params.append(role_type)
    if status != "all":
        where.append("j.status = ?")
        params.append(status)
    if unseen_only:
        where.append("j.seen = false")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = conn.execute(
        f"""
        SELECT j.job_id, j.title, j.company, j.location,
               j.linkedin_url, j.apply_url, j.apply_platform,
               j.salary_range, j.status, j.seen, j.is_repost,
               j.description_raw, j.description_summary, j.date_scraped, j.role_type
        FROM jobs j
        {where_sql}
        ORDER BY j.date_scraped DESC
        """,
        params,
    ).fetchall()
    conn.close()

    cols = [
        "job_id", "title", "company", "location", "linkedin_url",
        "apply_url", "apply_platform", "salary_range", "status",
        "seen", "is_repost", "description_raw", "description_summary", "date_scraped", "role_type",
    ]
    return [dict(zip(cols, row)) for row in rows]


def _start_run_background(url: str | None, role: str | None) -> None:
    """Run the Scout agent in a background thread."""
    with _run_lock:
        _run["running"] = True
        _run["started_at"] = datetime.now(timezone.utc)
        _run["message"] = "Starting agent..."
        _run["error"] = None

    cmd = ["pipenv", "run", "python", "agent/runner.py"]
    if url:
        cmd += ["--url", url]
    if role:
        cmd += ["--role", role]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=1800,
        )
        with _run_lock:
            if result.returncode == 0:
                _run["message"] = f"Done — {datetime.now(timezone.utc).strftime('%H:%M')}"
            else:
                _run["error"] = result.stderr[-500:] if result.stderr else "Unknown error"
                _run["message"] = "Run failed"
    except subprocess.TimeoutExpired:
        with _run_lock:
            _run["error"] = "Timed out after 30 minutes"
            _run["message"] = "Run timed out"
    finally:
        with _run_lock:
            _run["running"] = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the main page."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(
    request: Request,
    role_type: str = "all",
    status: str = "all",
    unseen_only: bool = False,
) -> HTMLResponse:
    """Return the job list partial for HTMX."""
    job_list = _fetch_jobs(role_type, status, unseen_only)
    return templates.TemplateResponse(
        request,
        "partials/jobs.html",
        {
            "jobs": job_list,
            "statuses": JOB_STATUSES,
            "role_type": role_type,
            "status_filter": status,
        },
    )


@app.post("/scout/run", response_class=HTMLResponse)
async def trigger_run(
    request: Request,
    url: str = Form(default=""),
    role: str = Form(default=""),
) -> HTMLResponse:
    """Start a Scout run in the background and return the status partial."""
    with _run_lock:
        if _run["running"]:
            return templates.TemplateResponse(
                request,
                "partials/run_status.html",
                {"run": dict(_run)},
            )

    thread = threading.Thread(
        target=_start_run_background,
        args=(url or None, role or None),
        daemon=True,
    )
    thread.start()

    with _run_lock:
        run_snapshot = dict(_run)

    return templates.TemplateResponse(
        request,
        "partials/run_status.html",
        {"run": run_snapshot},
    )


@app.get("/scout/status", response_class=HTMLResponse)
async def run_status(request: Request) -> HTMLResponse:
    """Return the current run status partial (polled by HTMX)."""
    with _run_lock:
        run_snapshot = dict(_run)
    return templates.TemplateResponse(
        request,
        "partials/run_status.html",
        {"run": run_snapshot},
    )


@app.patch("/jobs/{job_id}/status", response_class=HTMLResponse)
async def update_status(
    request: Request,
    job_id: str,
    status: str = Form(...),
) -> HTMLResponse:
    """Update a job's status and return the refreshed card."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET status = ? WHERE job_id = ?",
        [status, job_id],
    )
    conn.close()

    job_list = _fetch_jobs()
    job = next((j for j in job_list if j["job_id"] == job_id), None)
    if not job:
        return HTMLResponse("", status_code=204)

    return templates.TemplateResponse(
        request,
        "partials/job_card.html",
        {"job": job, "statuses": JOB_STATUSES},
    )


@app.patch("/jobs/{job_id}/seen", response_class=HTMLResponse)
async def mark_seen(request: Request, job_id: str) -> HTMLResponse:
    """Mark a job as seen."""
    conn = get_connection()
    conn.execute("UPDATE jobs SET seen = true WHERE job_id = ?", [job_id])
    conn.close()
    return HTMLResponse("", status_code=204)
