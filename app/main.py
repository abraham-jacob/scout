"""
Scout FastAPI backend.

Routes:
  GET  /                          — main page
  GET  /jobs                      — job list partial (HTMX)
  GET  /companies                 — company names + job counts (search autocomplete)
  POST /scout/run                 — trigger a scrape run
  GET  /scout/status              — run status partial (HTMX polling)
  PATCH /jobs/{job_id}/status     — update job status, returns updated card
  PATCH /jobs/{job_id}/seen       — mark job as seen
"""

import copy
import json
import logging
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from agent.runner import SetupError, check_setup
from app.config import load_roles, role_color_map
from app.database import JOB_STATUSES, get_connection, init_db
from app.logging_setup import setup_logging

BASE_DIR = Path(__file__).parent.parent

app = FastAPI(title="Scout")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# Step scaffolding for the run drawer. Global steps run once per run; email
# steps run once per Gmail alert email (keys must match runner.py's emit calls).
GLOBAL_STEPS = [
    ("start", "Starting agent"),
    ("gmail", "Pulling messages from Gmail"),
]
EMAIL_STEPS = [
    ("scrape", "Scraping LinkedIn (sub-agent)"),
    ("filter", "Filtering jobs"),
    ("clean", "Cleaning descriptions"),
    ("enrich", "Classifying & summarizing"),
    ("save", "Writing to storage"),
]

# In-memory run state (single-user local app — no need for DB persistence here).
# Structured so the drawer can render per-step / per-email progress live.
_run: dict = {
    "running": False,
    "error": None,
    "done": False,
    "gmail_auth_required": False,
    "started_at": None,
    "finished_at": None,
    "global_steps": [],
    "emails": [],
}
_run_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Run-state helpers (all mutate _run and must be called while holding _run_lock)
# ---------------------------------------------------------------------------

def _init_run_state() -> None:
    """Reset _run to a fresh scaffold with every step pending, first step active."""
    _run.update({
        "running": True,
        "error": None,
        "done": False,
        "gmail_auth_required": False,
        "started_at": datetime.now(timezone.utc),
        "finished_at": None,
        "global_steps": [
            {"key": k, "label": l, "status": "pending", "stat": None}
            for k, l in GLOBAL_STEPS
        ],
        "emails": [],
    })
    _run["global_steps"][0]["status"] = "active"


def _find_step(steps: list[dict], key: str | None) -> dict | None:
    """Return the step in ``steps`` with the given key, or None."""
    return next((s for s in steps if s["key"] == key), None)


def _email_group(index: int, total: int = 1, subject: str = "") -> dict:
    """Build a fresh per-email step group with all sub-steps pending."""
    return {
        "index": index,
        "total": total,
        "subject": subject,
        "steps": [
            {"key": k, "label": l, "status": "pending", "stat": None}
            for k, l in EMAIL_STEPS
        ],
    }


def _update_step(step: dict, ev: dict) -> None:
    """Apply an event's status/stat to a single step in place."""
    if ev.get("status"):
        step["status"] = ev["status"]
    if "stat" in ev:
        step["stat"] = ev["stat"]


def _apply_event(ev: dict) -> None:
    """Fold one SCOUT_PROGRESS event from the runner into _run."""
    scope = ev.get("scope")
    if scope == "global":
        step = _find_step(_run["global_steps"], ev.get("key"))
        if step:
            _update_step(step, ev)
        # The gmail event carries the email subjects — pre-create their groups
        # so the drawer shows all pending emails up front.
        if ev.get("key") == "gmail" and "emails" in ev:
            subs = ev["emails"]
            _run["emails"] = [
                _email_group(i, len(subs), s) for i, s in enumerate(subs, 1)
            ]
        if ev.get("auth_required"):
            _run["gmail_auth_required"] = True
    elif scope == "email":
        idx = ev.get("index")
        grp = next((g for g in _run["emails"] if g["index"] == idx), None)
        if grp is None:  # e.g. a manual --url run with no gmail step
            grp = _email_group(idx, ev.get("total", 1), ev.get("subject", ""))
            _run["emails"].append(grp)
        step = _find_step(grp["steps"], ev.get("key"))
        if step:
            _update_step(step, ev)


def _mark_active_as_error(msg: str) -> None:
    """Flip the currently active step (if any) to error with a short message."""
    for step in _run["global_steps"]:
        if step["status"] == "active":
            step["status"], step["stat"] = "error", msg
            return
    for grp in _run["emails"]:
        for step in grp["steps"]:
            if step["status"] == "active":
                step["status"], step["stat"] = "error", msg
                return


def _nav_state() -> dict:
    """Compute the compact nav indicator (text/colour/tooltip) from _run."""
    if _run["running"]:
        label = None
        for step in _run["global_steps"]:
            if step["status"] == "active":
                label = step["label"]
        for grp in _run["emails"]:
            for step in grp["steps"]:
                if step["status"] == "active":
                    label = step["label"]
        return {"text": label or "Running…", "cls": "running", "title": ""}
    if _run["error"]:
        return {"text": "Run failed", "cls": "error", "title": _run["error"]}
    if _run["done"]:
        t = _run["finished_at"].strftime("%H:%M") if _run["finished_at"] else ""
        return {"text": f"Done — {t}", "cls": "done", "title": ""}
    return {"text": "Idle", "cls": "idle", "title": ""}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup() -> None:
    """Initialise the database and application log on first start."""
    init_db()
    setup_logging().info("Scout web app started")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The "Applied · All" filter: applied plus every post-application stage.
PIPELINE_STATUSES = (
    "applied",
    "interviewing_recruiter",
    "interviewing_technical",
    "offer",
    "rejected",
)


def _fetch_jobs(
    role_type: str = "all",
    status: str = "all",
    unseen_only: bool = False,
    sort: str = "newest",
    show_dismissed: bool = False,
    company: str = "",
) -> list[dict]:
    """Query jobs from DuckDB with optional filters and sort order.

    status may be a single job status, "pipeline" (applied + all interview/
    offer/rejected stages), or "all". Dismissed jobs are hidden from the
    "all" view unless show_dismissed is set; other filters always win.
    company is a case-insensitive substring match; the UI only sends it for
    3+ typed characters or an autocomplete pick.
    """
    conn = get_connection()
    where, params = [], []

    if role_type != "all":
        where.append("j.role_type = ?")
        params.append(role_type)
    if company.strip():
        where.append("j.company ILIKE ?")
        params.append(f"%{company.strip()}%")
    if status == "pipeline":
        where.append(f"j.status IN ({', '.join('?' * len(PIPELINE_STATUSES))})")
        params.extend(PIPELINE_STATUSES)
    elif status != "all":
        where.append("j.status = ?")
        params.append(status)
    elif not show_dismissed:
        where.append("j.status != 'dismissed'")
    if unseen_only:
        where.append("j.seen = false")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = ("j.match_score DESC NULLS LAST, j.date_scraped DESC"
                 if sort == "match" else "j.date_scraped DESC")

    rows = conn.execute(
        f"""
        SELECT j.job_id, j.title, j.company, j.location,
               j.linkedin_url, j.apply_url, j.apply_platform,
               j.salary_range, j.status, j.seen, j.is_repost,
               j.description_raw, j.description_summary, j.date_scraped, j.role_type,
               j.tags, j.match_score, j.match_reason, j.dealbreakers
        FROM jobs j
        {where_sql}
        ORDER BY {order_sql}
        """,
        params,
    ).fetchall()
    conn.close()

    cols = [
        "job_id", "title", "company", "location", "linkedin_url",
        "apply_url", "apply_platform", "salary_range", "status",
        "seen", "is_repost", "description_raw", "description_summary", "date_scraped", "role_type",
        "tags", "match_score", "match_reason", "dealbreakers",
    ]
    return [dict(zip(cols, row)) for row in rows]


def _start_run_background(url: str | None, log_model_calls: bool = False) -> None:
    """Run the Scout agent in a subprocess, folding its progress events into _run.

    Reads the runner's stdout line by line so SCOUT_PROGRESS events update the
    drawer live; stderr is drained on a side thread to avoid a full-pipe deadlock.
    log_model_calls forwards the UI checkbox to the runner's --log-model-calls.

    The runner is launched with sys.executable (the same interpreter running
    the web app) rather than `pipenv run` so it inherits this process's virtualenv
    directly — no dependency on `pipenv` being resolvable on PATH, which is what
    makes this work identically on Windows/macOS/Linux.
    """
    cmd = [sys.executable, "-m", "agent.runner"]
    if url:
        cmd += ["--url", url]
    if log_model_calls:
        cmd.append("--log-model-calls")

    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    err_lines: list[str] = []
    err_thread = threading.Thread(
        target=lambda: err_lines.extend(proc.stderr), daemon=True
    )
    err_thread.start()

    # Overall wall-clock guardrail (the runner has its own per-subprocess caps).
    timed_out = {"v": False}

    def _kill() -> None:
        timed_out["v"] = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    watchdog = threading.Timer(1800, _kill)
    watchdog.start()

    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("SCOUT_PROGRESS "):
                try:
                    ev = json.loads(line[len("SCOUT_PROGRESS "):])
                except json.JSONDecodeError:
                    continue
                with _run_lock:
                    _apply_event(ev)
        proc.wait()
    finally:
        watchdog.cancel()
    err_thread.join(timeout=2)

    with _run_lock:
        _run["running"] = False
        _run["finished_at"] = datetime.now(timezone.utc)
        if timed_out["v"]:
            _run["error"] = "Timed out after 30 minutes"
            _mark_active_as_error("timed out")
            logging.getLogger("scout").error("Run timed out after 30 minutes")
        elif proc.returncode != 0:
            _run["error"] = ("".join(err_lines)[-500:]).strip() or "Unknown error"
            _mark_active_as_error("run failed")
            logging.getLogger("scout").error(
                "Run failed (exit %s): %s", proc.returncode, _run["error"])
        else:
            _run["done"] = True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the main page."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"role_names": [r.name for r in load_roles()]},
    )


@app.get("/jobs", response_class=HTMLResponse)
async def jobs(
    request: Request,
    role_type: str = "all",
    status: str = "all",
    unseen_only: bool = False,
    sort: str = "newest",
    show_dismissed: bool = False,
    company: str = "",
) -> HTMLResponse:
    """Return the job list partial for HTMX."""
    job_list = _fetch_jobs(role_type, status, unseen_only, sort, show_dismissed, company)
    return templates.TemplateResponse(
        request,
        "partials/jobs.html",
        {
            "jobs": job_list,
            "statuses": JOB_STATUSES,
            "role_type": role_type,
            "status_filter": status,
            "role_colors": role_color_map(load_roles()),
        },
    )


@app.get("/companies")
async def companies() -> list[dict]:
    """Return distinct company names with job counts for the search autocomplete."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT company, COUNT(*) FROM jobs GROUP BY company ORDER BY company"
    ).fetchall()
    conn.close()
    return [{"company": name, "count": count} for name, count in rows]


def _render_drawer(request: Request) -> HTMLResponse:
    """Render the run drawer partial from a snapshot of the current run state."""
    with _run_lock:
        snapshot = copy.deepcopy(_run)
        nav = _nav_state()
    return templates.TemplateResponse(
        request,
        "partials/run_drawer.html",
        {"run": snapshot, "nav": nav},
    )


@app.post("/scout/run", response_class=HTMLResponse)
async def trigger_run(
    request: Request,
    url: str = Form(default=""),
    log_model_calls: bool = Form(default=False),
) -> HTMLResponse:
    """Start a Scout run in the background and return the run drawer partial.

    Runs the same setup checks the CLI runs (check_setup) synchronously first,
    so a broken config or an unreachable / wrong-model local-LLM backend
    surfaces in the drawer immediately — before any subprocess, browser, or
    scrape work is started and wasted.
    """
    with _run_lock:
        already_running = _run["running"]
    if already_running:
        return _render_drawer(request)

    try:
        check_setup()
    except SetupError as exc:
        with _run_lock:
            _init_run_state()
            _run["running"] = False
            _run["error"] = str(exc)
            _run["finished_at"] = datetime.now(timezone.utc)
        logging.getLogger("scout").error("Run blocked by setup check: %s", exc)
        return _render_drawer(request)

    with _run_lock:
        already_running = _run["running"]
        if not already_running:
            _init_run_state()
    if already_running:
        return _render_drawer(request)

    logging.getLogger("scout").info(
        "Run triggered from UI (url=%s, model call logging %s)",
        url or "gmail", "on" if log_model_calls else "off")
    threading.Thread(
        target=_start_run_background,
        args=(url or None, log_model_calls),
        daemon=True,
    ).start()

    return _render_drawer(request)


@app.get("/scout/status", response_class=HTMLResponse)
async def run_status(request: Request) -> HTMLResponse:
    """Return the current run drawer partial (polled by HTMX while running)."""
    return _render_drawer(request)


@app.post("/auth/gmail/reauth", response_class=HTMLResponse)
def gmail_reauth() -> HTMLResponse:
    """Delete the stale OAuth token and run a fresh browser-based auth flow.

    Blocks until the user completes the Google consent screen (run_local_server
    opens a browser tab on a random port), then returns a status fragment that
    HTMX swaps in place of the Reauthenticate button.
    """
    from app.gmail import TOKEN_FILE, get_gmail_service
    TOKEN_FILE.unlink(missing_ok=True)
    try:
        get_gmail_service()
        return HTMLResponse(
            '<p class="text-sm text-emerald-600 dark:text-emerald-400 font-medium">'
            "Authenticated. Try running Scout again.</p>"
        )
    except Exception as exc:
        logging.getLogger("scout").error("Gmail reauth failed: %s", exc)
        return HTMLResponse(
            f'<p class="text-sm text-rose-600 dark:text-rose-400">'
            f"Reauth failed: {exc}</p>"
        )


@app.patch("/jobs/{job_id}/status", response_class=HTMLResponse)
async def update_status(
    request: Request,
    job_id: str,
    status: str = Form(...),
    show_dismissed: bool = Form(default=False),
) -> HTMLResponse:
    """Update a job's status and return the refreshed card.

    When the job is dismissed while the list is hiding dismissed jobs,
    return an empty body so the outerHTML swap removes the card.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET status = ? WHERE job_id = ?",
        [status, job_id],
    )
    conn.close()

    if status == "dismissed" and not show_dismissed:
        return HTMLResponse("")

    job_list = _fetch_jobs(show_dismissed=True)
    job = next((j for j in job_list if j["job_id"] == job_id), None)
    if not job:
        return HTMLResponse("", status_code=204)

    return templates.TemplateResponse(
        request,
        "partials/job_card.html",
        {"job": job, "statuses": JOB_STATUSES,
         "role_colors": role_color_map(load_roles())},
    )


@app.patch("/jobs/{job_id}/seen", response_class=HTMLResponse)
async def mark_seen(request: Request, job_id: str) -> HTMLResponse:
    """Mark a job as seen."""
    conn = get_connection()
    conn.execute("UPDATE jobs SET seen = true WHERE job_id = ?", [job_id])
    conn.close()
    return HTMLResponse("", status_code=204)
