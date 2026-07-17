"""Tests for app/main.py — FastAPI routes."""

import pytest
import subprocess
import sys
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch, MagicMock
import threading

from app.main import (
    app,
    _fetch_jobs,
    _start_run_background,
    _run,
    _run_lock,
)
from app.database import JOB_STATUSES


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def reset_run_state():
    """Reset run state before and after each test."""
    with _run_lock:
        original_state = dict(_run)
    yield
    with _run_lock:
        _run.clear()
        _run.update(original_state)


class TestFetchJobs:
    """Test _fetch_jobs helper function."""

    @patch('app.main.get_connection')
    def test_fetch_all_jobs(self, mock_get_conn):
        """Fetch all jobs without filters."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("job1", "Engineer", "TechCorp", "Springfield", "https://linkedin.com/jobs/view/job1",
             "https://apply.com", "easy_apply", "$150k", "new", False, False, "Raw desc",
             "Summary", "2026-01-01", "IC"),
            ("job2", "Manager", "StartupCo", "NYC", "https://linkedin.com/jobs/view/job2",
             "https://apply.com", "greenhouse", "$200k", "applied", True, False, "Raw desc",
             "Summary", "2026-01-01", "Manager"),
        ]
        mock_get_conn.return_value = mock_conn

        jobs = _fetch_jobs()

        assert len(jobs) == 2
        assert jobs[0]["job_id"] == "job1"
        assert jobs[1]["job_id"] == "job2"
        mock_conn.close.assert_called_once()

    @patch('app.main.get_connection')
    def test_fetch_jobs_by_role_type(self, mock_get_conn):
        """Fetch jobs filtered by role type."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("job1", "Manager", "Corp", "Springfield", "url", "apply", "easy_apply", None, "new",
             False, False, "desc", "summary", "2026-01-01", "Manager"),
        ]
        mock_get_conn.return_value = mock_conn

        jobs = _fetch_jobs(role_type="Manager")

        assert len(jobs) == 1
        assert jobs[0]["role_type"] == "Manager"
        # Verify role_type filter was applied
        call_sql = mock_get_conn.return_value.execute.call_args[0][0]
        assert "role_type = ?" in call_sql

    @patch('app.main.get_connection')
    def test_fetch_jobs_by_status(self, mock_get_conn):
        """Fetch jobs filtered by status."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("job1", "Engineer", "Corp", "Springfield", "url", "apply", "easy_apply", None, "applied",
             True, False, "desc", "summary", "2026-01-01", "IC"),
        ]
        mock_get_conn.return_value = mock_conn

        jobs = _fetch_jobs(status="applied")

        assert len(jobs) == 1
        assert jobs[0]["status"] == "applied"

    @patch('app.main.get_connection')
    def test_fetch_jobs_unseen_only(self, mock_get_conn):
        """Fetch only unseen jobs."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("job1", "Engineer", "Corp", "Springfield", "url", "apply", "easy_apply", None, "new",
             False, False, "desc", "summary", "2026-01-01", "IC"),
        ]
        mock_get_conn.return_value = mock_conn

        jobs = _fetch_jobs(unseen_only=True)

        assert len(jobs) == 1
        # Verify unseen filter was applied
        call_sql = mock_get_conn.return_value.execute.call_args[0][0]
        assert "seen = false" in call_sql

    @patch('app.main.get_connection')
    def test_fetch_jobs_company_search(self, mock_get_conn):
        """company adds a case-insensitive substring match on the company name."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        _fetch_jobs(company="data")

        call_sql = mock_conn.execute.call_args[0][0]
        call_params = mock_conn.execute.call_args[0][1]
        assert "j.company ILIKE ?" in call_sql
        assert call_params == ["%data%"]

    @patch('app.main.get_connection')
    def test_fetch_jobs_blank_company_ignored(self, mock_get_conn):
        """A blank/whitespace company string adds no filter."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        _fetch_jobs(company="   ")

        call_sql = mock_conn.execute.call_args[0][0]
        assert "ILIKE" not in call_sql

    @patch('app.main.get_connection')
    def test_fetch_jobs_pipeline_group(self, mock_get_conn):
        """status='pipeline' matches applied plus all post-application stages."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        _fetch_jobs(status="pipeline")

        call_sql = mock_conn.execute.call_args[0][0]
        call_params = mock_conn.execute.call_args[0][1]
        assert "j.status IN (?, ?, ?, ?, ?)" in call_sql
        assert call_params == [
            "applied", "interviewing_recruiter", "interviewing_technical",
            "offer", "rejected",
        ]

    @patch('app.main.get_connection')
    def test_fetch_jobs_hides_dismissed_by_default(self, mock_get_conn):
        """The 'all' status view excludes dismissed jobs unless opted in."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        _fetch_jobs()

        call_sql = mock_conn.execute.call_args[0][0]
        assert "j.status != 'dismissed'" in call_sql

    @patch('app.main.get_connection')
    def test_fetch_jobs_show_dismissed(self, mock_get_conn):
        """show_dismissed=True lifts the dismissed exclusion from the 'all' view."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        _fetch_jobs(show_dismissed=True)

        call_sql = mock_conn.execute.call_args[0][0]
        assert "!= 'dismissed'" not in call_sql

    @patch('app.main.get_connection')
    def test_fetch_jobs_explicit_status_ignores_dismissed_exclusion(self, mock_get_conn):
        """An explicit status filter is used verbatim, without the dismissed exclusion."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        _fetch_jobs(status="dismissed")

        call_sql = mock_conn.execute.call_args[0][0]
        assert "j.status = ?" in call_sql
        assert "!= 'dismissed'" not in call_sql

    @patch('app.main.get_connection')
    def test_fetch_jobs_empty(self, mock_get_conn):
        """Handle empty job list."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        jobs = _fetch_jobs()

        assert jobs == []


class TestIndexRoute:
    """Test GET / route."""

    def test_index_returns_html(self, client):
        """GET / returns HTML response."""
        response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers.get("content-type", "")


class TestJobsRoute:
    """Test GET /jobs route."""

    @patch('app.main._fetch_jobs')
    def test_jobs_list(self, mock_fetch, client):
        """GET /jobs returns job list partial."""
        mock_fetch.return_value = [
            {
                "job_id": "job1",
                "title": "Engineer",
                "company": "Corp",
                "location": "Springfield",
                "linkedin_url": "https://linkedin.com/jobs/view/job1",
                "apply_url": "https://apply.com",
                "apply_platform": "easy_apply",
                "salary_range": "$150k",
                "status": "new",
                "seen": False,
                "is_repost": False,
                "description_raw": "desc",
                "description_summary": "summary",
                "date_scraped": "2026-01-01",
                "role_type": "IC",
                "tags": ["Remote", "Go"],
                "match_score": None,
                "match_reason": None,
                "dealbreakers": [],
            }
        ]

        response = client.get("/jobs")

        assert response.status_code == 200
        assert "job1" in response.text or "Engineer" in response.text

    @patch('app.main._fetch_jobs')
    def test_jobs_with_filters(self, mock_fetch, client):
        """GET /jobs respects filter parameters."""
        mock_fetch.return_value = []

        response = client.get("/jobs?role_type=Manager&status=applied")

        assert response.status_code == 200
        mock_fetch.assert_called_once_with("Manager", "applied", False, "newest", False, "")

    @patch('app.main._fetch_jobs')
    def test_jobs_unseen_only_filter(self, mock_fetch, client):
        """GET /jobs unseen_only parameter."""
        mock_fetch.return_value = []

        response = client.get("/jobs?unseen_only=true")

        assert response.status_code == 200
        # Verify unseen_only was passed as True
        call_args = mock_fetch.call_args[0]
        assert call_args[2] is True


class TestCompaniesRoute:
    """Test GET /companies route."""

    @patch('app.main.get_connection')
    def test_companies_list(self, mock_get_conn, client):
        """GET /companies returns company names with job counts as JSON."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("Datadog", 2),
            ("Stripe", 1),
        ]
        mock_get_conn.return_value = mock_conn

        response = client.get("/companies")

        assert response.status_code == 200
        assert response.json() == [
            {"company": "Datadog", "count": 2},
            {"company": "Stripe", "count": 1},
        ]
        mock_conn.close.assert_called_once()

    @patch('app.main.get_connection')
    def test_companies_empty(self, mock_get_conn, client):
        """GET /companies returns an empty list when there are no jobs."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        response = client.get("/companies")

        assert response.status_code == 200
        assert response.json() == []


class TestScoutRunRoute:
    """Test POST /scout/run route."""

    @patch('app.main.check_setup')
    @patch('app.main._start_run_background')
    def test_trigger_run_with_url(self, mock_start, mock_check_setup, client, reset_run_state):
        """POST /scout/run triggers a background run with URL."""
        response = client.post("/scout/run", data={"url": "https://linkedin.com/jobs"})

        assert response.status_code == 200
        mock_start.assert_called_once()
        # Verify URL was passed
        call_args = mock_start.call_args[0]
        assert call_args[0] == "https://linkedin.com/jobs"

    @patch('app.main.check_setup')
    @patch('app.main._start_run_background')
    def test_trigger_run_log_model_calls(self, mock_start, mock_check_setup, client, reset_run_state):
        """The 'Log LLM calls' checkbox is forwarded to the background runner."""
        response = client.post("/scout/run", data={"log_model_calls": "true"})

        assert response.status_code == 200
        mock_start.assert_called_once()
        assert mock_start.call_args[0] == (None, True)

    @patch('app.main.check_setup')
    @patch('app.main._start_run_background')
    def test_trigger_run_default_no_model_logging(self, mock_start, mock_check_setup, client, reset_run_state):
        """Without the checkbox, model-call logging stays off."""
        response = client.post("/scout/run", data={})

        assert response.status_code == 200
        mock_start.assert_called_once()
        assert mock_start.call_args[0] == (None, False)

    @patch('app.main._start_run_background')
    def test_trigger_run_already_running(self, mock_start, client, reset_run_state):
        """POST /scout/run returns status if run already in progress."""
        with _run_lock:
            _run["running"] = True

        response = client.post("/scout/run", data={"url": "https://linkedin.com/jobs"})

        assert response.status_code == 200
        # Should not start another run
        mock_start.assert_not_called()

    @patch('app.main.check_setup')
    @patch('app.main._start_run_background')
    def test_trigger_run_without_url(self, mock_start, mock_check_setup, client, reset_run_state):
        """POST /scout/run without URL (reads Gmail)."""
        response = client.post("/scout/run", data={})

        assert response.status_code == 200
        mock_start.assert_called_once()
        call_args = mock_start.call_args[0]
        assert call_args[0] is None  # URL is None


class TestRunStatusRoute:
    """Test GET /scout/status route."""

    def test_run_status_idle(self, client, reset_run_state):
        """GET /scout/status returns idle status."""
        response = client.get("/scout/status")

        assert response.status_code == 200
        with _run_lock:
            assert not _run["running"]

    def test_run_status_running(self, client, reset_run_state):
        """GET /scout/status returns running status."""
        with _run_lock:
            _run["running"] = True

        response = client.get("/scout/status")

        assert response.status_code == 200
        with _run_lock:
            assert _run["running"]

    def test_run_status_error(self, client, reset_run_state):
        """GET /scout/status returns error status."""
        with _run_lock:
            _run["running"] = False
            _run["error"] = "Connection timeout"

        response = client.get("/scout/status")

        assert response.status_code == 200
        assert "Connection timeout" in response.text
        with _run_lock:
            assert _run["error"] == "Connection timeout"


class TestUpdateStatusRoute:
    """Test PATCH /jobs/{job_id}/status route."""

    @patch('app.main._fetch_jobs')
    @patch('app.main.get_connection')
    def test_update_job_status(self, mock_get_conn, mock_fetch, client):
        """PATCH /jobs/{job_id}/status updates job status."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_fetch.return_value = [
            {
                "job_id": "job1",
                "title": "Engineer",
                "company": "Corp",
                "location": "Springfield",
                "linkedin_url": "https://linkedin.com/jobs/view/job1",
                "apply_url": "https://apply.com",
                "apply_platform": "easy_apply",
                "salary_range": "$150k",
                "status": "applied",
                "seen": False,
                "is_repost": False,
                "description_raw": "desc",
                "description_summary": "summary",
                "date_scraped": "2026-01-01",
                "role_type": "IC",
                "tags": ["Remote", "Go"],
                "match_score": None,
                "match_reason": None,
                "dealbreakers": [],
            }
        ]

        response = client.patch("/jobs/job1/status", data={"status": "applied"})

        assert response.status_code == 200
        mock_conn.execute.assert_called_once()

    @patch('app.main._fetch_jobs')
    @patch('app.main.get_connection')
    def test_update_nonexistent_job_status(self, mock_get_conn, mock_fetch, client):
        """PATCH /jobs/{nonexistent}/status returns 204 if job not found."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_fetch.return_value = []

        response = client.patch("/jobs/nonexistent/status", data={"status": "applied"})

        assert response.status_code == 204

    @patch('app.main._fetch_jobs')
    @patch('app.main.get_connection')
    def test_dismiss_removes_card_when_hidden(self, mock_get_conn, mock_fetch, client):
        """Dismissing while dismissed jobs are hidden returns an empty body (card removal)."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        response = client.patch("/jobs/job1/status", data={"status": "dismissed"})

        assert response.status_code == 200
        assert response.text == ""
        mock_conn.execute.assert_called_once()
        mock_fetch.assert_not_called()

    @patch('app.main._fetch_jobs')
    @patch('app.main.get_connection')
    def test_dismiss_keeps_card_when_showing_dismissed(self, mock_get_conn, mock_fetch, client):
        """Dismissing with show_dismissed=true returns the refreshed card."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_fetch.return_value = [
            {
                "job_id": "job1",
                "title": "Engineer",
                "company": "Corp",
                "location": "Springfield",
                "linkedin_url": "https://linkedin.com/jobs/view/job1",
                "apply_url": "https://apply.com",
                "apply_platform": "easy_apply",
                "salary_range": "$150k",
                "status": "dismissed",
                "seen": False,
                "is_repost": False,
                "description_raw": "desc",
                "description_summary": "summary",
                "date_scraped": "2026-01-01",
                "role_type": "IC",
                "tags": [],
                "match_score": None,
                "match_reason": None,
                "dealbreakers": [],
            }
        ]

        response = client.patch(
            "/jobs/job1/status",
            data={"status": "dismissed", "show_dismissed": "true"},
        )

        assert response.status_code == 200
        assert "job1" in response.text
        mock_fetch.assert_called_once_with(show_dismissed=True)


class TestMarkSeenRoute:
    """Test PATCH /jobs/{job_id}/seen route."""

    @patch('app.main.get_connection')
    def test_mark_job_as_seen(self, mock_get_conn, client):
        """PATCH /jobs/{job_id}/seen marks job as seen."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        response = client.patch("/jobs/job1/seen")

        assert response.status_code == 204
        mock_conn.execute.assert_called_once()
        # Verify UPDATE seen = true was called
        call_args = mock_conn.execute.call_args[0]
        assert "seen" in call_args[0]
        assert "true" in call_args[0].lower()


def _make_proc(stdout_lines, stderr_lines=(), returncode=0):
    """Build a fake Popen whose stdout/stderr iterate the given lines."""
    proc = MagicMock()
    proc.stdout = iter(stdout_lines)
    proc.stderr = iter(stderr_lines)
    proc.returncode = returncode
    proc.wait.return_value = returncode
    return proc


class _ImmediateTimer:
    """A threading.Timer stand-in that fires its callback the moment it starts."""

    def __init__(self, interval, func):
        self._func = func

    def start(self):
        self._func()

    def cancel(self):
        pass


class TestStartRunBackground:
    """Test background run execution."""

    @patch('app.main.subprocess.Popen')
    def test_start_run_background_uses_current_interpreter(self, mock_popen, reset_run_state):
        """Runner launches with sys.executable -m agent.runner, not `pipenv`.

        Reusing the running interpreter's venv is what removes the pipenv-on-PATH
        assumption and keeps the launch identical across Windows/macOS/Linux.
        """
        mock_popen.return_value = _make_proc(stdout_lines=[], returncode=0)

        _start_run_background(None)

        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "agent.runner"]
        assert "pipenv" not in cmd

    @patch('app.main.subprocess.Popen')
    def test_start_run_background_success(self, mock_popen, reset_run_state):
        """Background run succeeds and marks the run done."""
        mock_popen.return_value = _make_proc(
            stdout_lines=[
                'SCOUT_PROGRESS {"scope": "global", "key": "start", "status": "done"}\n',
                'ordinary log line, ignored\n',
            ],
            returncode=0,
        )

        _start_run_background("https://linkedin.com")

        with _run_lock:
            assert not _run["running"]
            assert _run["done"] is True
            assert _run["error"] is None

    @patch('app.main.subprocess.Popen')
    def test_start_run_background_failure(self, mock_popen, reset_run_state):
        """Background run captures stderr on non-zero exit."""
        mock_popen.return_value = _make_proc(
            stdout_lines=[],
            stderr_lines=["Error occurred\n"],
            returncode=1,
        )

        _start_run_background("https://linkedin.com")

        with _run_lock:
            assert not _run["running"]
            assert _run["done"] is False
            assert _run["error"] is not None
            assert "Error occurred" in _run["error"]

    @patch('app.main.threading.Timer', _ImmediateTimer)
    @patch('app.main.subprocess.Popen')
    def test_start_run_background_timeout(self, mock_popen, reset_run_state):
        """Background run handles the wall-clock watchdog firing."""
        mock_popen.return_value = _make_proc(stdout_lines=[], returncode=-9)

        _start_run_background("https://linkedin.com")

        with _run_lock:
            assert not _run["running"]
            assert _run["error"] is not None
            assert "imed out" in _run["error"]


class TestRouteIntegration:
    """Integration tests for routes."""

    @patch('app.main._fetch_jobs')
    def test_index_and_jobs_flow(self, mock_fetch, client):
        """Load index then fetch jobs."""
        # Get main page
        response = client.get("/")
        assert response.status_code == 200

        # Fetch jobs
        mock_fetch.return_value = []
        response = client.get("/jobs")
        assert response.status_code == 200


class TestGmailAuthRequired:
    """Test Gmail auth-required flag and reauth endpoint."""

    def test_apply_event_sets_gmail_auth_required(self, reset_run_state):
        """An event with auth_required=True sets the flag on _run."""
        from app.main import _apply_event, _init_run_state
        with _run_lock:
            _init_run_state()
            _apply_event({
                "scope": "global",
                "key": "gmail",
                "status": "error",
                "stat": "auth expired",
                "auth_required": True,
            })
            assert _run["gmail_auth_required"] is True

    def test_apply_event_without_auth_required_leaves_flag_false(self, reset_run_state):
        """Normal events don't set gmail_auth_required."""
        from app.main import _apply_event, _init_run_state
        with _run_lock:
            _init_run_state()
            _apply_event({"scope": "global", "key": "gmail", "status": "done",
                          "stat": "1 email", "emails": ["Job alerts"]})
            assert _run["gmail_auth_required"] is False

    def test_run_status_shows_reauth_button_when_auth_required(
        self, client, reset_run_state
    ):
        """GET /scout/status renders the amber reauth box (not the red error box) on auth failure."""
        with _run_lock:
            _run["gmail_auth_required"] = True
            _run["error"] = "Gmail token expired"

        response = client.get("/scout/status")

        assert response.status_code == 200
        assert "Reauthenticate with Gmail" in response.text
        # amber box is shown; the red error pre block is NOT (auth_required takes the elif branch)
        assert "bg-rose-50" not in response.text

    def test_run_status_shows_error_box_without_auth_flag(self, client, reset_run_state):
        """Generic red error box shown when gmail_auth_required is False."""
        with _run_lock:
            _run["gmail_auth_required"] = False
            _run["error"] = "Something else went wrong"

        response = client.get("/scout/status")

        assert response.status_code == 200
        assert "bg-rose-50" in response.text
        assert "Something else went wrong" in response.text
        assert "Reauthenticate with Gmail" not in response.text

    def test_gmail_reauth_success(self, client):
        """POST /auth/gmail/reauth returns success message when auth completes."""
        with patch("app.gmail.TOKEN_FILE") as mock_token, \
             patch("app.gmail.get_gmail_service") as mock_auth:
            mock_token.unlink = MagicMock()
            mock_auth.return_value = MagicMock()

            response = client.post("/auth/gmail/reauth")

        assert response.status_code == 200
        assert "Authenticated" in response.text

    def test_gmail_reauth_failure(self, client):
        """POST /auth/gmail/reauth returns error message when auth fails."""
        with patch("app.gmail.TOKEN_FILE") as mock_token, \
             patch("app.gmail.get_gmail_service") as mock_auth:
            mock_token.unlink = MagicMock()
            mock_auth.side_effect = Exception("access_denied")

            response = client.post("/auth/gmail/reauth")

        assert response.status_code == 200
        assert "Reauth failed" in response.text
