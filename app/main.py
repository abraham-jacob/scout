"""
Scout FastAPI backend.

Routes:
  GET  /                          — main page
  GET  /jobs                      — job list partial (HTMX)
  GET  /companies                 — company names + job counts (search autocomplete)
  POST /scout/run                 — trigger a scrape run (CDP-fallback path; not surfaced in the UI, see below)
  GET  /scout/status              — lightweight run banner partial (HTMX polling)
  PATCH /jobs/{job_id}/status     — update job status, returns updated card
  PATCH /jobs/{job_id}/seen       — mark job as seen
  GET  /api/extension/searches    — saved searches + pacing config (browser extension)
  POST /api/extension/reload-config — re-read profiles/config.toml, bypassing the cache
  POST /api/extension/dedupe      — job_ids not already in the DB (browser extension)
  POST /api/extension/ingest      — accept extension-scraped jobs, run Pass 2/3
  POST /api/extension/kill        — abort the current run's Pass 2/3 subprocess
  GET  /api/extension/status      — run state as JSON (browser extension popup polling)
"""

import copy
import json
import logging
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from agent.runner import SetupError, check_setup, extract_single_job_id, resolve_scan_url
from agent.step_keys import StepKey
from agent.tools import get_existing_job_ids
from app.config import load_config, load_roles, role_color_map
from app.database import JOB_STATUSES, get_connection, init_db
from app.logging_setup import setup_logging

BASE_DIR = Path(__file__).parent.parent

app = FastAPI(title="Scout")
# The browser extension posts from a chrome-extension://<id> origin (the id
# isn't stable until packed, hence the regex rather than a hardcoded value).
# The server stays bound to localhost regardless (see _start_run_background's
# docstring) — this widens who the browser lets *read* the response, not who
# can reach the port.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"chrome-extension://.*",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

# Step scaffolding for the run state. Global steps run once per run; search
# steps run once per configured LinkedIn search (keys must match runner.py's
# emit calls). Consumed by the extension popup via GET /api/extension/status
# for detailed live progress; the web UI's own banner only reads run.running/
# run.error/nav.text, not these steps directly.
GLOBAL_STEPS = [
    (StepKey.START, "Starting agent"),
]
SEARCH_STEPS = [
    (StepKey.SCRAPE, "Scraping LinkedIn (sub-agent)"),
    (StepKey.FILTER, "Filtering jobs"),
    (StepKey.CLEAN, "Cleaning descriptions"),
    (StepKey.ENRICH, "Classifying & summarizing"),
    (StepKey.SAVE, "Writing to storage"),
]

# Max lines kept in the run's event-log ( _run["log"] ), oldest lines drop off.
# Rendered by the extension popup (GET /api/extension/status).
RUN_LOG_MAXLEN = 200

# In-memory run state (single-user local app — no need for DB persistence here).
# Structured so per-step / per-search progress can be rendered live —
# primarily by the extension popup now (see GET /api/extension/status).
_run: dict = {
    "running": False,
    "error": None,
    "done": False,
    "killed": False,
    "started_at": None,
    "finished_at": None,
    "backend": None,
    "models": {},
    "global_steps": [],
    "searches": [],
    "log": [],
}
_run_lock = threading.Lock()

# Handle to the currently running Pass 2/3 subprocess and whether the user
# explicitly stopped it — both live outside _run itself (not as fields on
# it) because _run gets wholesale copy.deepcopy'd by GET endpoints
# (extension_status, etc.) and a live subprocess.Popen isn't safely
# deep-copyable. Guarded by the same _run_lock as _run. See
# POST /api/extension/kill and _start_run_background's use of both.
_current_proc: subprocess.Popen | None = None
_kill_requested = False


# ---------------------------------------------------------------------------
# Run-state helpers (all mutate _run and must be called while holding _run_lock)
# ---------------------------------------------------------------------------

def _init_run_state(url: str | None = None, search_name: str | None = None) -> None:
    """Reset _run to a fresh scaffold with every step pending, first step active.

    When neither ``url`` nor ``search_name`` is set (the default, config-driven
    run), pre-populates the search groups synchronously from
    ``load_config().linkedin_searches`` so the drawer shows every configured
    search immediately on click, rather than waiting for the subprocess's
    first stdout line. Otherwise a single group is pre-created, labeled
    ``search_name`` if given (extension-ingest runs already know their real
    search name), else "Single job" for a ``/jobs/view/<id>`` URL, else
    "Ad-hoc URL" — since the runner's ``--url`` path never emits a name for
    its one search.

    Also resets _current_proc/_kill_requested (module globals, not _run
    fields — see their definitions) so a previous run's kill request can't
    leak into this one.
    """
    global _current_proc, _kill_requested
    _current_proc = None
    _kill_requested = False
    single = bool(url) or bool(search_name)
    searches = [] if single else load_config().linkedin_searches
    if search_name:
        ad_hoc_label = search_name
    elif url and extract_single_job_id(url):
        ad_hoc_label = "Single job"
    else:
        ad_hoc_label = "Ad-hoc URL"
    _run.update({
        "running": True,
        "error": None,
        "done": False,
        "killed": False,
        "started_at": datetime.now(timezone.utc),
        "finished_at": None,
        "backend": None,
        "models": {},
        "global_steps": [
            {"key": k, "label": l, "status": "pending", "stat": None,
             "started_at": None, "elapsed": None}
            for k, l in GLOBAL_STEPS
        ],
        "searches": (
            [_search_group(1, 1, ad_hoc_label)] if single
            else [_search_group(i, len(searches), s.name)
                  for i, s in enumerate(searches, 1)]
        ),
        "log": [],
    })
    _run["global_steps"][0]["status"] = "active"


def _find_step(steps: list[dict], key: str | None) -> dict | None:
    """Return the step in ``steps`` with the given key, or None."""
    return next((s for s in steps if s["key"] == key), None)


def _search_group(index: int, total: int = 1, name: str = "") -> dict:
    """Build a fresh per-search step group with all sub-steps pending."""
    return {
        "index": index,
        "total": total,
        "name": name,
        "steps": [
            {"key": k, "label": l, "status": "pending", "stat": None,
             "started_at": None, "elapsed": None}
            for k, l in SEARCH_STEPS
        ],
    }


def _elapsed_seconds(start: datetime, end: datetime) -> int:
    """Whole seconds between two aware datetimes, floored."""
    return int((end - start).total_seconds())


def _freeze_step_elapsed(step: dict, now: datetime) -> None:
    """Permanently store a step's elapsed time the first time it stops being active.

    No-ops if the step never started or already has a frozen ``elapsed`` —
    safe to call from every status-transition path (_update_step,
    _mark_active_as_error) without double-computing.
    """
    if step.get("started_at") and step.get("elapsed") is None:
        step["elapsed"] = _elapsed_seconds(step["started_at"], now)


def _update_step(step: dict, ev: dict) -> None:
    """Apply an event's status/stat to a single step in place.

    Tracks ``started_at`` the first time a step goes active (repeat "active"
    events — e.g. live N-of-M progress during clean/enrich — don't reset it)
    and freezes ``elapsed`` once the step leaves the active state, so the
    drawer can show a live timer while running and a fixed duration after.
    """
    status = ev.get("status")
    if status:
        if status == "active" and step["status"] != "active":
            step["started_at"] = datetime.now(timezone.utc)
        elif status != "active":
            _freeze_step_elapsed(step, datetime.now(timezone.utc))
        step["status"] = status
    if "stat" in ev:
        step["stat"] = ev["stat"]


def _apply_event(ev: dict) -> None:
    """Fold one SCOUT_PROGRESS event from the runner into _run."""
    scope = ev.get("scope")
    if scope == "meta":
        _run["backend"] = ev.get("backend")
        _run["models"] = ev.get("models") or {}
    elif scope == "log":
        elapsed = 0
        if _run["started_at"]:
            elapsed = _elapsed_seconds(_run["started_at"], datetime.now(timezone.utc))
        _run["log"].append({
            "ts": elapsed,
            "level": ev.get("level", "info"),
            "msg": ev.get("msg", ""),
        })
        if len(_run["log"]) > RUN_LOG_MAXLEN:
            _run["log"] = _run["log"][-RUN_LOG_MAXLEN:]
    elif scope == "global":
        step = _find_step(_run["global_steps"], ev.get("key"))
        if step:
            _update_step(step, ev)
    elif scope == "search":
        idx = ev.get("index")
        grp = next((g for g in _run["searches"] if g["index"] == idx), None)
        if grp is None:  # e.g. a manual --url run with no pre-created group
            grp = _search_group(idx, ev.get("total", 1), ev.get("name", ""))
            _run["searches"].append(grp)
        step = _find_step(grp["steps"], ev.get("key"))
        if step:
            _update_step(step, ev)


def _mark_active_as_error(msg: str) -> None:
    """Flip the currently active step (if any) to error with a short message.

    Freezes ``elapsed`` too, same as _update_step, so a timed-out/crashed
    run's per-step timer stops instead of ticking forever in a closed drawer.
    """
    def _fail(step: dict) -> None:
        step["status"], step["stat"] = "error", msg
        _freeze_step_elapsed(step, datetime.now(timezone.utc))

    for step in _run["global_steps"]:
        if step["status"] == "active":
            _fail(step)
            return
    for grp in _run["searches"]:
        for step in grp["steps"]:
            if step["status"] == "active":
                _fail(step)
                return


def _nav_state() -> dict:
    """Compute the compact nav indicator (text/colour/tooltip) from _run."""
    if _run["running"]:
        label = None
        for step in _run["global_steps"]:
            if step["status"] == "active":
                label = step["label"]
        for grp in _run["searches"]:
            for step in grp["steps"]:
                if step["status"] == "active":
                    label = step["label"]
        return {"text": label or "Running…", "cls": "running", "title": ""}
    if _run["killed"]:
        return {"text": "Stopped by user", "cls": "idle", "title": _run["error"]}
    if _run["error"]:
        return {"text": "Run failed", "cls": "error", "title": _run["error"]}
    if _run["done"]:
        # finished_at is stored in UTC (datetime.now(timezone.utc)); astimezone()
        # with no args converts an aware datetime to the system's local zone, so
        # the drawer shows the wall-clock time the user actually finished at.
        t = (_run["finished_at"].astimezone().strftime("%H:%M")
             if _run["finished_at"] else "")
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
    min_score: int = 0,
    invert: bool = False,
) -> list[dict]:
    """Query jobs from DuckDB with optional filters and sort order.

    status may be a single job status, "pipeline" (applied + all interview/
    offer/rejected stages), or "all". Dismissed jobs are hidden from the
    "all" view unless show_dismissed is set; other filters always win.
    company is a case-insensitive substring match; the UI only sends it for
    3+ typed characters or an autocomplete pick. min_score filters to
    match_score >= min_score when > 0 (or < min_score when invert is set),
    which also excludes unscored jobs (NULL match_score) either way since a
    NULL comparison is never true in SQL.
    """
    conn = get_connection()
    where, params = [], []

    if role_type != "all":
        where.append("j.role_type = ?")
        params.append(role_type)
    if company.strip():
        where.append("j.company ILIKE ?")
        params.append(f"%{company.strip()}%")
    if min_score > 0:
        where.append(f"j.match_score {'<' if invert else '>='} ?")
        params.append(min_score)
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

    result = conn.execute(
        f"""
        SELECT j.job_id, j.title, j.company, j.location,
               j.linkedin_url, j.apply_url, j.apply_platform,
               j.salary_range, j.status, j.seen, j.is_repost, j.original_job_id,
               j.description_raw, j.description_summary, j.date_scraped, j.role_type,
               j.tags, j.match_score, j.match_reason, j.dealbreakers
        FROM jobs j
        {where_sql}
        ORDER BY {order_sql}
        """,
        params,
    )
    cols = [d[0] for d in result.description]
    rows = result.fetchall()
    conn.close()

    return [dict(zip(cols, row)) for row in rows]


def _start_run_background(
    url: str | None,
    log_model_calls: bool = False,
    ingest_file: str | None = None,
    run_id: str | None = None,
    search_name: str | None = None,
) -> None:
    """Run the Scout agent in a subprocess, folding its progress events into _run.

    Reads the runner's stdout line by line so SCOUT_PROGRESS events update the
    drawer live; stderr is drained on a side thread to avoid a full-pipe deadlock.
    log_model_calls forwards the UI checkbox to the runner's --log-model-calls.

    When ``ingest_file`` is set (the browser-extension path — see
    ``/api/extension/ingest``), the runner skips its own Pass 1 scrape and
    runs Pass 2/3 + save on jobs already scraped by the extension, via
    ``--ingest-file``/``--run-id``/``--search-name`` instead of ``--url``
    alone. This is the same subprocess/stdout-parsing mechanism as a normal
    run — extension-triggered progress flows into the same drawer/_run state
    for free, rather than needing a second progress-reporting path.

    The runner is launched with sys.executable (the same interpreter running
    the web app) rather than `pipenv run` so it inherits this process's virtualenv
    directly — no dependency on `pipenv` being resolvable on PATH, which is what
    makes this work identically on Windows/macOS/Linux.
    """
    cmd = [sys.executable, "-m", "agent.runner"]
    if ingest_file:
        cmd += ["--ingest-file", ingest_file, "--run-id", run_id or ""]
        if search_name:
            cmd += ["--search-name", search_name]
        if url:
            cmd += ["--url", url]
    elif url:
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
    global _current_proc
    with _run_lock:
        _current_proc = proc

    err_lines: list[str] = []
    err_thread = threading.Thread(
        target=lambda: err_lines.extend(proc.stderr), daemon=True
    )
    err_thread.start()

    # Overall wall-clock guardrail (the runner has its own per-subprocess caps).
    timed_out = {"v": False}
    timeout_minutes = load_config().run_timeout_minutes

    def _kill() -> None:
        timed_out["v"] = True
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    watchdog = threading.Timer(timeout_minutes * 60, _kill)
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
        # Safety net: agent.runner deletes ingest_file itself once it reads it
        # (mirrors load_downloaded_jobs's cleanup), but that line is never
        # reached if the subprocess fails before getting there — e.g.
        # validate_setup() rejecting a broken/unreachable [llm] backend. Without
        # this, a setup failure leaks the temp file on every ingest attempt.
        if ingest_file:
            Path(ingest_file).unlink(missing_ok=True)
    err_thread.join(timeout=2)

    with _run_lock:
        _current_proc = None
        _run["running"] = False
        _run["finished_at"] = datetime.now(timezone.utc)
        if _kill_requested:
            _run["error"] = "Stopped by user"
            _run["killed"] = True
            _mark_active_as_error("stopped by user")
            logging.getLogger("scout").info("Run stopped by user (Abort)")
        elif timed_out["v"]:
            _run["error"] = f"Timed out after {timeout_minutes} minutes"
            _mark_active_as_error("timed out")
            logging.getLogger("scout").error(
                "Run timed out after %d minutes", timeout_minutes
            )
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
    min_score: int = 0,
    invert: bool = False,
) -> HTMLResponse:
    """Return the job list partial for HTMX."""
    job_list = _fetch_jobs(role_type, status, unseen_only, sort, show_dismissed, company, min_score, invert)
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


def _finalize_snapshot(snapshot: dict, now: datetime) -> None:
    """Compute this render's live elapsed seconds for the header and any active step.

    Called on a deep-copied snapshot (never the live _run) so per-render timer
    math never races the background run thread. Steps already marked done/error
    keep their frozen ``elapsed`` from _update_step.
    """
    if snapshot["started_at"]:
        end = snapshot["finished_at"] or now
        snapshot["run_elapsed"] = _elapsed_seconds(snapshot["started_at"], end)
    else:
        snapshot["run_elapsed"] = 0

    def _finalize_step(step: dict) -> None:
        if step["status"] == "active" and step.get("started_at"):
            step["elapsed"] = _elapsed_seconds(step["started_at"], now)

    for step in snapshot["global_steps"]:
        _finalize_step(step)
    for group in snapshot["searches"]:
        for step in group["steps"]:
            _finalize_step(step)


def _render_run_banner(request: Request) -> HTMLResponse:
    """Render the lightweight run banner from a snapshot of the current run state.

    Detailed step-by-step/event-log progress used to live here too (the old
    run drawer) — that moved to the browser extension's popup (see
    GET /api/extension/status), since the extension is what triggers runs
    now. This stays just enough to not be misleading if the web UI is left
    open in a tab during a run started elsewhere.
    """
    with _run_lock:
        snapshot = copy.deepcopy(_run)
        nav = _nav_state()
    _finalize_snapshot(snapshot, datetime.now(timezone.utc))
    return templates.TemplateResponse(
        request,
        "partials/run_banner.html",
        {"run": snapshot, "nav": nav},
    )


@app.post("/scout/run", response_class=HTMLResponse)
async def trigger_run(
    request: Request,
    url: str = Form(default=""),
    log_model_calls: bool = Form(default=False),
) -> HTMLResponse:
    """Start a Scout run in the background and return the run banner partial.

    Kept as the CDP-based Pass 1 fallback's trigger — not surfaced in the web
    UI anymore (the browser extension owns triggering runs, see
    POST /api/extension/ingest), but left callable directly until the
    extension is proven in real use. Runs the same setup checks the CLI runs
    (check_setup) synchronously first, so a broken config or an unreachable /
    wrong-model api backend surfaces immediately — before any subprocess,
    browser, or scrape work is started and wasted. ``url`` is expanded via
    resolve_scan_url first so a bare job id is already a real URL by the time
    it's used for labeling or handed to the runner subprocess.
    """
    url = resolve_scan_url(url)
    with _run_lock:
        already_running = _run["running"]
    if already_running:
        return _render_run_banner(request)

    try:
        check_setup()
    except SetupError as exc:
        with _run_lock:
            _init_run_state()
            _run["running"] = False
            _run["error"] = str(exc)
            _run["finished_at"] = datetime.now(timezone.utc)
        logging.getLogger("scout").error("Run blocked by setup check: %s", exc)
        return _render_run_banner(request)

    with _run_lock:
        already_running = _run["running"]
        if not already_running:
            _init_run_state(url or None)
    if already_running:
        return _render_run_banner(request)

    logging.getLogger("scout").info(
        "Run triggered from UI (url=%s, model call logging %s)",
        url or "config searches", "on" if log_model_calls else "off")
    threading.Thread(
        target=_start_run_background,
        args=(url or None, log_model_calls),
        daemon=True,
    ).start()

    return _render_run_banner(request)


@app.get("/scout/status", response_class=HTMLResponse)
async def run_status(request: Request) -> HTMLResponse:
    """Return the current run banner partial (polled continuously by the web UI)."""
    return _render_run_banner(request)


# ---------------------------------------------------------------------------
# Browser extension API (Pass 1 acquisition — see mockups/extension_popup.html)
# ---------------------------------------------------------------------------

@app.get("/api/extension/searches")
async def extension_searches() -> dict:
    """Return configured saved searches and Voyager-loop pacing for the popup.

    Single source of truth stays profiles/config.toml — the extension holds
    no config of its own beyond what it fetches here.
    """
    config = load_config()
    return {
        "searches": [{"name": s.name, "url": s.url} for s in config.linkedin_searches],
        "min_delay_ms": config.extension_min_delay_ms,
        "max_delay_ms": config.extension_max_delay_ms,
    }


@app.post("/api/extension/reload-config")
async def extension_reload_config() -> JSONResponse:
    """Clear the cached Config and re-read profiles/config.toml, for the popup's Reload button.

    load_config() is process-lifetime cached (see its docstring), so the
    popup's saved-searches list otherwise never picks up an edited
    config.toml without a full server restart. On a parse/validation
    failure (e.g. the file was mid-edit), the cache is left cleared but
    nothing here is mutated beyond that — the popup keeps showing its
    last-known list and surfaces the error instead, rather than wiping a
    working list over a broken edit.
    """
    load_config.cache_clear()
    try:
        config = load_config()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({
        "searches": [{"name": s.name, "url": s.url} for s in config.linkedin_searches],
        "min_delay_ms": config.extension_min_delay_ms,
        "max_delay_ms": config.extension_max_delay_ms,
    })


@app.post("/api/extension/dedupe")
async def extension_dedupe(payload: dict = Body(...)) -> dict:
    """Return the job_ids from the request not already in the DB, in order.

    get_existing_job_ids() excludes only dismissed jobs, so a straight diff
    is correct here — applied/closed jobs are still "known" for dedup.
    """
    job_ids = payload.get("job_ids")
    if not isinstance(job_ids, list):
        return {"new_ids": []}
    existing = set(get_existing_job_ids())
    return {"new_ids": [j for j in job_ids if j not in existing]}


@app.post("/api/extension/ingest")
async def extension_ingest(payload: dict = Body(...)) -> JSONResponse:
    """Accept jobs harvested by the extension and run Pass 2/3 in the background.

    Writes ``jobs`` to a temp file and spawns the runner the same way a
    normal run does (_start_run_background), just with --ingest-file instead
    of a fresh scrape — so this returns immediately without blocking on
    Pass 2/3, and progress flows through the existing SCOUT_PROGRESS/_run
    pipeline the web UI and this endpoint's JSON sibling (/api/extension/status)
    both read from. Runs serialize (mirrors the extension's own one-Run-
    button-at-a-time UI): a request while another run is in flight is rejected
    rather than queued.
    """
    search_name = str(payload.get("search_name") or "Extension run").strip() or "Extension run"
    url = str(payload.get("url") or "").strip()
    jobs = payload.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return JSONResponse({"error": "jobs must be a non-empty object"}, status_code=400)

    with _run_lock:
        if _run["running"]:
            return JSONResponse({"error": "a run is already in progress"}, status_code=409)
        _init_run_state(url=url or None, search_name=search_name)

    run_id = str(uuid.uuid4())
    fd, path = tempfile.mkstemp(prefix=f"scout_ingest_{run_id}_", suffix=".json")
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(jobs, f)

    logging.getLogger("scout").info(
        "Ingest triggered from extension (search=%s, %d jobs)", search_name, len(jobs))
    threading.Thread(
        target=_start_run_background,
        kwargs={
            "url": url or None,
            "ingest_file": path,
            "run_id": run_id,
            "search_name": search_name,
        },
        daemon=True,
    ).start()

    return JSONResponse({"accepted": len(jobs), "run_id": run_id})


@app.post("/api/extension/kill")
async def extension_kill() -> JSONResponse:
    """Abort the current run's Pass 2/3 subprocess, for the popup's Abort button.

    Mirrors _start_run_background's existing overall-timeout watchdog —
    same proc.kill() mechanism, already proven safe there — just triggered
    by an explicit user action instead of a timer. A no-op (200,
    {"killed": false}) when no subprocess is currently tracked: the run may
    still be in Pass 1 (browser harvest, nothing server-side yet) or already
    finished. The popup's Abort button always fires this *and* a browser-side
    SCOUT_ABORT message together, so exactly one of them actually does
    something in the common case; see extension/popup.js's abortRun().
    """
    global _kill_requested
    with _run_lock:
        proc = _current_proc
        if proc is not None:
            _kill_requested = True
    if proc is None:
        return JSONResponse({"killed": False})
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return JSONResponse({"killed": True})


@app.get("/api/extension/status")
async def extension_status() -> dict:
    """Return the current run state as JSON (the popup's polling source).

    JSON sibling of GET /scout/status (which renders the lightweight HTML
    banner) — same _run snapshot, including the granular per-job "log"
    lines Pass 2/3 already emits, so the popup recovers full detail on
    reopen without persisting anything itself.
    """
    with _run_lock:
        snapshot = copy.deepcopy(_run)
        nav = _nav_state()
    _finalize_snapshot(snapshot, datetime.now(timezone.utc))
    snapshot["nav"] = nav
    return snapshot


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


@app.patch("/jobs/bulk_dismiss")
async def bulk_dismiss(job_ids: list[str] = Body(embed=True)) -> JSONResponse:
    """Dismiss every job in job_ids and return how many rows were updated.

    The caller (the filter bar's bulk-dismiss action) always passes the
    job_ids of whatever is currently rendered, since the server has already
    applied every active filter — there is no separate filter re-evaluation
    here.
    """
    if not job_ids:
        return JSONResponse({"dismissed": 0})
    conn = get_connection()
    placeholders = ", ".join("?" * len(job_ids))
    conn.execute(
        f"UPDATE jobs SET status = 'dismissed' WHERE job_id IN ({placeholders})",
        job_ids,
    )
    conn.close()
    return JSONResponse({"dismissed": len(job_ids)})


@app.patch("/jobs/{job_id}/seen", response_class=HTMLResponse)
async def mark_seen(request: Request, job_id: str) -> HTMLResponse:
    """Mark a job as seen."""
    conn = get_connection()
    conn.execute("UPDATE jobs SET seen = true WHERE job_id = ?", [job_id])
    conn.close()
    return HTMLResponse("", status_code=204)
