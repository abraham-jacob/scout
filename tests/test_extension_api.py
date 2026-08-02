"""Tests for app/main.py's /api/extension/* routes — the browser extension's
backend surface (searches, dedupe, ingest, status)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app, _run, _run_lock, _start_run_background


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


def _make_proc(stdout_lines, stderr_lines=(), returncode=0):
    """Build a fake Popen whose stdout/stderr iterate the given lines."""
    proc = MagicMock()
    proc.stdout = iter(stdout_lines)
    proc.stderr = iter(stderr_lines)
    proc.returncode = returncode
    proc.wait.return_value = returncode
    return proc


@pytest.fixture
def reset_run_state():
    """Reset run state before and after each test."""
    with _run_lock:
        original_state = dict(_run)
    yield
    with _run_lock:
        _run.clear()
        _run.update(original_state)


class TestExtensionSearchesRoute:
    """Test GET /api/extension/searches."""

    def test_returns_configured_searches(self, client):
        """Searches come straight from profiles/config.toml (STANDARD_TEST_CONFIG)."""
        response = client.get("/api/extension/searches")

        assert response.status_code == 200
        body = response.json()
        assert body["searches"] == [
            {"name": "Test Search",
             "url": "https://www.linkedin.com/jobs/search-results/?keywords=engineer"}
        ]

    def test_includes_default_pacing_when_extension_section_absent(self, client):
        """[extension] is optional — defaults are served when the config omits it."""
        response = client.get("/api/extension/searches")

        body = response.json()
        assert body["min_delay_ms"] == 3000
        assert body["max_delay_ms"] == 8000

    def test_honors_configured_pacing(self, client, monkeypatch):
        """A configured [extension] section overrides the defaults."""
        import app.config as app_config
        config_path = app_config.CONFIG_FILE
        config_path.write_text(
            config_path.read_text() + "\n[extension]\nmin_delay_ms = 1000\nmax_delay_ms = 2000\n"
        )
        app_config.load_config.cache_clear()

        response = client.get("/api/extension/searches")

        body = response.json()
        assert body["min_delay_ms"] == 1000
        assert body["max_delay_ms"] == 2000


class TestExtensionDedupeRoute:
    """Test POST /api/extension/dedupe."""

    @patch("app.main.get_existing_job_ids")
    def test_filters_known_ids_and_preserves_order(self, mock_get_ids, client):
        """Known ids are dropped; the remaining order matches the request."""
        mock_get_ids.return_value = ["job2", "job4"]

        response = client.post(
            "/api/extension/dedupe",
            json={"job_ids": ["job1", "job2", "job3", "job4", "job5"]},
        )

        assert response.status_code == 200
        assert response.json() == {"new_ids": ["job1", "job3", "job5"]}

    @patch("app.main.get_existing_job_ids")
    def test_empty_input_returns_empty(self, mock_get_ids, client):
        """An empty job_ids list is a no-op, not an error."""
        mock_get_ids.return_value = []

        response = client.post("/api/extension/dedupe", json={"job_ids": []})

        assert response.status_code == 200
        assert response.json() == {"new_ids": []}

    @patch("app.main.get_existing_job_ids")
    def test_missing_job_ids_key_returns_empty(self, mock_get_ids, client):
        """A malformed payload (no job_ids key) fails gracefully, not with a 500."""
        mock_get_ids.return_value = []

        response = client.post("/api/extension/dedupe", json={})

        assert response.status_code == 200
        assert response.json() == {"new_ids": []}

    @patch("app.main.get_existing_job_ids")
    def test_no_known_ids_returns_everything(self, mock_get_ids, client):
        """When nothing in the batch is already known, the full list passes through."""
        mock_get_ids.return_value = []

        response = client.post("/api/extension/dedupe", json={"job_ids": ["a", "b"]})

        assert response.json() == {"new_ids": ["a", "b"]}


class TestExtensionIngestRoute:
    """Test POST /api/extension/ingest."""

    @patch("app.main._start_run_background")
    def test_ingest_returns_immediately_with_a_run_id(self, mock_start, client, reset_run_state):
        """The endpoint returns as soon as the subprocess is queued, not after
        Pass 2/3 finishes — _start_run_background is patched to a no-op Mock,
        so a real response here proves the route doesn't block on it."""
        response = client.post(
            "/api/extension/ingest",
            json={
                "search_name": "Platform Eng",
                "url": "https://www.linkedin.com/jobs/search/",
                "jobs": {"job1": {"title": "Engineer", "company": "TechCorp"}},
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 1
        assert "run_id" in body and len(body["run_id"]) == 36  # UUID
        mock_start.assert_called_once()

    @patch("app.main._start_run_background")
    def test_ingest_writes_jobs_to_a_temp_file_and_passes_it_through(
        self, mock_start, client, reset_run_state
    ):
        """The jobs payload lands on disk (same {job_id: {...}} shape runner.py
        expects) and the path is forwarded to _start_run_background."""
        jobs = {"job1": {"title": "Engineer", "company": "TechCorp"}}

        response = client.post(
            "/api/extension/ingest",
            json={"search_name": "Platform Eng", "url": "", "jobs": jobs},
        )

        assert response.status_code == 200
        kwargs = mock_start.call_args.kwargs
        ingest_path = Path(kwargs["ingest_file"])
        assert ingest_path.exists()
        assert json.loads(ingest_path.read_text()) == jobs
        assert kwargs["run_id"] == response.json()["run_id"]
        assert kwargs["search_name"] == "Platform Eng"
        ingest_path.unlink()

    @patch("app.main._start_run_background")
    def test_empty_jobs_payload_fails_gracefully(self, mock_start, client, reset_run_state):
        """An empty jobs object is rejected with 400, not a 500 or a silent no-op run."""
        response = client.post(
            "/api/extension/ingest",
            json={"search_name": "Platform Eng", "url": "", "jobs": {}},
        )

        assert response.status_code == 400
        mock_start.assert_not_called()

    @patch("app.main._start_run_background")
    def test_malformed_jobs_payload_fails_gracefully(self, mock_start, client, reset_run_state):
        """A jobs value that isn't an object (e.g. a list) is rejected, not a 500."""
        response = client.post(
            "/api/extension/ingest",
            json={"search_name": "Platform Eng", "url": "", "jobs": ["not", "a", "dict"]},
        )

        assert response.status_code == 400
        mock_start.assert_not_called()

    @patch("app.main._start_run_background")
    def test_missing_jobs_key_fails_gracefully(self, mock_start, client, reset_run_state):
        """A payload with no jobs key at all is rejected, not a 500."""
        response = client.post(
            "/api/extension/ingest", json={"search_name": "Platform Eng", "url": ""}
        )

        assert response.status_code == 400
        mock_start.assert_not_called()

    @patch("app.main._start_run_background")
    def test_rejects_when_a_run_is_already_in_progress(self, mock_start, client, reset_run_state):
        """Runs serialize — a concurrent ingest while one is running is rejected,
        not queued, matching the popup's own one-Run-button-at-a-time UI."""
        with _run_lock:
            _run["running"] = True

        response = client.post(
            "/api/extension/ingest",
            json={
                "search_name": "Platform Eng",
                "url": "",
                "jobs": {"job1": {"title": "Engineer", "company": "TechCorp"}},
            },
        )

        assert response.status_code == 409
        mock_start.assert_not_called()

    @patch("app.main._start_run_background")
    def test_defaults_search_name_when_omitted(self, mock_start, client, reset_run_state):
        """A missing/blank search_name falls back to 'Extension run'."""
        response = client.post(
            "/api/extension/ingest",
            json={"url": "", "jobs": {"job1": {"title": "Engineer", "company": "TechCorp"}}},
        )

        assert response.status_code == 200
        assert mock_start.call_args.kwargs["search_name"] == "Extension run"


class TestStartRunBackgroundIngestCleanup:
    """_start_run_background must always clean up the ingest temp file, even
    when the subprocess fails before reaching agent.runner's own unlink() —
    e.g. validate_setup() rejecting a broken/unreachable [llm] backend before
    the --ingest-file branch is ever read. Found via manual verification:
    without this, every failed ingest attempt leaked a temp file."""

    @patch("app.main.subprocess.Popen")
    def test_temp_file_deleted_on_subprocess_failure(self, mock_popen, tmp_path, reset_run_state):
        """A non-zero exit (e.g. setup validation failing) still deletes the file."""
        ingest_file = tmp_path / "scout_ingest_test.json"
        ingest_file.write_text("{}")
        mock_popen.return_value = _make_proc(
            stdout_lines=[], stderr_lines=["Setup error: unreachable\n"], returncode=1)

        _start_run_background(None, ingest_file=str(ingest_file), run_id="r1",
                              search_name="Extension run")

        assert not ingest_file.exists()

    @patch("app.main.subprocess.Popen")
    def test_temp_file_deleted_on_subprocess_success(self, mock_popen, tmp_path, reset_run_state):
        """The happy path also leaves no file behind (belt-and-suspenders with
        agent.runner's own unlink — missing_ok makes the second delete a no-op)."""
        ingest_file = tmp_path / "scout_ingest_test.json"
        ingest_file.write_text("{}")
        mock_popen.return_value = _make_proc(stdout_lines=[], returncode=0)

        _start_run_background(None, ingest_file=str(ingest_file), run_id="r1",
                              search_name="Extension run")

        assert not ingest_file.exists()

    @patch("app.main.subprocess.Popen")
    def test_normal_run_without_ingest_file_is_unaffected(self, mock_popen, reset_run_state):
        """A regular (non-extension) run passes ingest_file=None and doesn't
        touch the filesystem for cleanup."""
        mock_popen.return_value = _make_proc(stdout_lines=[], returncode=0)

        _start_run_background("https://linkedin.com")  # should not raise

        with _run_lock:
            assert _run["done"] is True


class TestExtensionStatusRoute:
    """Test GET /api/extension/status — the JSON sibling of GET /scout/status."""

    def test_idle_shape(self, client, reset_run_state):
        """An idle run reports running=False and an empty log."""
        response = client.get("/api/extension/status")

        assert response.status_code == 200
        body = response.json()
        assert body["running"] is False
        assert body["log"] == []
        assert body["nav"]["text"] == "Idle"

    def test_running_state_and_log_are_exposed(self, client, reset_run_state):
        """Granular per-job log lines (Pass 2/3) round-trip through the JSON
        endpoint — this is the extension popup's sole source for them now
        that the web UI's run drawer has been replaced by a lightweight
        banner with no step/log detail (see app/templates/partials/run_banner.html)."""
        with _run_lock:
            _run["running"] = True
            _run["log"] = [
                {"ts": 3, "level": "good",
                 "msg": "✓ cleaned Senior Platform Engineer @ Stripe (1/9) · 2s"}
            ]

        response = client.get("/api/extension/status")

        body = response.json()
        assert body["running"] is True
        assert body["log"][0]["msg"] == "✓ cleaned Senior Platform Engineer @ Stripe (1/9) · 2s"

    def test_search_name_is_exposed_for_popup_display(self, client, reset_run_state):
        """The configured search's friendly alias (not its raw URL) is what
        the popup would show — the web UI's banner deliberately doesn't
        render this anymore, so this endpoint is the only surface left that
        does."""
        from app.main import _search_group
        with _run_lock:
            _run["running"] = True
            _run["searches"] = [_search_group(1, 1, "Senior IC Bay Area")]

        response = client.get("/api/extension/status")

        body = response.json()
        assert body["searches"][0]["name"] == "Senior IC Bay Area"

    def test_matches_html_banner_state(self, client, reset_run_state):
        """The JSON and HTML status endpoints read the same underlying _run state."""
        with _run_lock:
            _run["error"] = "boom"

        json_body = client.get("/api/extension/status").json()
        html_body = client.get("/scout/status").text

        assert json_body["error"] == "boom"
        assert "boom" in html_body
