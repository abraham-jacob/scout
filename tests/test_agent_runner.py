"""Tests for agent/runner.py — scrape orchestration and enrichment."""

import json
import pytest
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

from agent.runner import (
    _file_job_to_record,
    _extract_json,
    load_downloaded_jobs,
    apply_deterministic_filters,
    _add_usage,
    print_token_summary,
)


class TestFileJobToRecord:
    """Test conversion of downloaded job to save schema."""

    def test_convert_job_with_all_fields(self):
        """Convert job with all fields populated."""
        obj = {
            "title": "Senior Engineer",
            "company": "TechCorp",
            "location": "Springfield, USA",
            "apply_url": "https://apply.example.com",
            "apply_platform": "greenhouse",
            "salary_range": "$150k-$200k",
            "description_raw": "Job description text",
        }

        record = _file_job_to_record("job123", obj)

        assert record["job_id"] == "job123"
        assert record["title"] == "Senior Engineer"
        assert record["company"] == "TechCorp"
        assert record["location"] == "Springfield, USA"
        assert record["linkedin_url"] == "https://www.linkedin.com/jobs/view/job123"
        assert record["apply_url"] == "https://apply.example.com"
        assert record["apply_platform"] == "greenhouse"
        assert record["salary_range"] == "$150k-$200k"
        assert record["description_raw"] == "Job description text"

    def test_convert_job_with_minimal_fields(self):
        """Convert job with minimal fields."""
        obj = {"title": "Engineer", "company": "Corp"}

        record = _file_job_to_record("job456", obj)

        assert record["job_id"] == "job456"
        assert record["title"] == "Engineer"
        assert record["company"] == "Corp"
        assert record["location"] is None
        assert record["apply_platform"] == "other"  # default
        assert record["linkedin_url"] == "https://www.linkedin.com/jobs/view/job456"

    def test_job_constructs_correct_linkedin_url(self):
        """Verify LinkedIn URL is constructed correctly."""
        record = _file_job_to_record("987654321", {})

        assert record["linkedin_url"] == "https://www.linkedin.com/jobs/view/987654321"


class TestExtractJson:
    """Test JSON extraction from model output."""

    def test_extract_pure_json(self):
        """Extract JSON from clean JSON output."""
        json_str = '{"role_type": "IC", "tags": ["Python", "AWS"]}'

        result = _extract_json(json_str)

        assert result["role_type"] == "IC"
        assert result["tags"] == ["Python", "AWS"]

    def test_extract_json_with_prose(self):
        """Extract JSON from output with surrounding prose."""
        output = '''Here's the analysis:

{"role_type": "Manager", "tags": ["Leadership"]}

That's it!'''

        result = _extract_json(output)

        assert result["role_type"] == "Manager"
        assert result["tags"] == ["Leadership"]

    def test_extract_json_multiline(self):
        """Extract multiline JSON from output."""
        output = '''Some text before

{
  "role_type": "IC",
  "tags": ["Go", "Kubernetes"]
}

Some text after'''

        result = _extract_json(output)

        assert result["role_type"] == "IC"
        assert result["tags"] == ["Go", "Kubernetes"]

    def test_extract_json_malformed(self):
        """Return empty dict for malformed JSON."""
        output = "No JSON here { broken json }"

        result = _extract_json(output)

        assert result == {}

    def test_extract_json_empty_string(self):
        """Handle empty string input."""
        result = _extract_json("")

        assert result == {}

    def test_extract_json_none(self):
        """Handle None input."""
        result = _extract_json(None)

        assert result == {}


class TestLoadDownloadedJobs:
    """Test reading the browser's blob download from the Downloads folder."""

    def test_load_from_download_dir(self):
        """Read jobs straight from the configured Downloads folder."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dl = Path(tmpdir)
            job_file = dl / "scout_run123.json"
            jobs_data = {"job1": {"title": "Engineer"}, "job2": {"title": "Manager"}}
            job_file.write_text(json.dumps(jobs_data))

            with patch('agent.runner.download_dir', return_value=dl):
                result = load_downloaded_jobs("run123")

            assert result == jobs_data

    def test_deletes_file_after_read(self):
        """The run file is cleaned up so the folder doesn't accumulate blobs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dl = Path(tmpdir)
            job_file = dl / "scout_run123.json"
            job_file.write_text(json.dumps({"job1": {"title": "Engineer"}}))

            with patch('agent.runner.download_dir', return_value=dl):
                load_downloaded_jobs("run123")

            assert not job_file.exists()

    def test_waits_for_delayed_download(self):
        """A file that lands mid-poll is still picked up (replaces bash wait)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dl = Path(tmpdir)
            job_file = dl / "scout_late.json"

            def _write_later():
                time.sleep(0.3)
                job_file.write_text(json.dumps({"j": {"title": "T"}}))

            with patch('agent.runner.download_dir', return_value=dl), \
                 patch('agent.runner.DOWNLOAD_WAIT_S', 5):
                writer = threading.Thread(target=_write_later)
                writer.start()
                result = load_downloaded_jobs("late")
                writer.join()

            assert result == {"j": {"title": "T"}}

    def test_load_nonexistent_file(self):
        """Return None if the file never appears within the wait window."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('agent.runner.download_dir', return_value=Path(tmpdir)), \
                 patch('agent.runner.DOWNLOAD_WAIT_S', 0):
                result = load_downloaded_jobs("nonexistent")

            assert result is None

    def test_load_invalid_json(self):
        """Return None if the file can't be parsed before the deadline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dl = Path(tmpdir)
            job_file = dl / "scout_run789.json"
            job_file.write_text("{ invalid json }")

            with patch('agent.runner.download_dir', return_value=dl), \
                 patch('agent.runner.DOWNLOAD_WAIT_S', 0):
                result = load_downloaded_jobs("run789")

            assert result is None


class TestApplyDeterministicFilters:
    """Test job filtering before enrichment."""

    def test_filter_removes_errors(self):
        """Remove jobs with errors."""
        all_jobs = {
            "job1": {"title": "Engineer", "company": "Corp"},
            "job2": {"error": "failed to scrape"},
        }
        existing_ids = set()

        result = apply_deterministic_filters(all_jobs, existing_ids)

        assert len(result) == 1
        assert result[0]["job_id"] == "job1"

    def test_filter_removes_existing(self):
        """Remove jobs already in database."""
        all_jobs = {
            "job1": {"title": "Engineer", "company": "Corp"},
            "job2": {"title": "Manager", "company": "Corp"},
        }
        existing_ids = {"job2"}

        result = apply_deterministic_filters(all_jobs, existing_ids)

        assert len(result) == 1
        assert result[0]["job_id"] == "job1"

    def test_filter_removes_applied(self):
        """Remove jobs already applied to."""
        all_jobs = {
            "job1": {"title": "Engineer", "company": "Corp", "applied": False},
            "job2": {"title": "Manager", "company": "Corp", "applied": True},
        }
        existing_ids = set()

        result = apply_deterministic_filters(all_jobs, existing_ids)

        assert len(result) == 1
        assert result[0]["job_id"] == "job1"

    def test_filter_removes_closed_jobs(self):
        """Remove closed jobs (jobState != LISTED)."""
        all_jobs = {
            "job1": {"title": "Engineer", "company": "Corp", "jobState": "LISTED"},
            "job2": {"title": "Manager", "company": "Corp", "jobState": "CLOSED"},
        }
        existing_ids = set()

        result = apply_deterministic_filters(all_jobs, existing_ids)

        assert len(result) == 1
        assert result[0]["job_id"] == "job1"

    def test_filter_removes_missing_company(self):
        """Remove jobs whose scrape yielded no company name (None or blank)."""
        all_jobs = {
            "job1": {"title": "Engineer", "company": None},
            "job2": {"title": "Engineer", "company": "  "},
            "job3": {"title": "Engineer"},
            "job4": {"title": "Engineer", "company": "TechCorp"},
        }
        existing_ids = set()

        result = apply_deterministic_filters(all_jobs, existing_ids)

        assert len(result) == 1
        assert result[0]["job_id"] == "job4"

    def test_filter_removes_excluded_company(self):
        """Remove excluded-company jobs."""
        all_jobs = {
            "job1": {"title": "Engineer", "company": "ExcludedCorp"},
            "job2": {"title": "Engineer", "company": "TechCorp"},
        }
        existing_ids = set()

        result = apply_deterministic_filters(all_jobs, existing_ids)

        assert len(result) == 1
        assert result[0]["job_id"] == "job2"

    def test_filter_excluded_company_case_insensitive(self):
        """Excluded-company filtering is case-insensitive."""
        all_jobs = {
            "job1": {"title": "Engineer", "company": "EXCLUDEDCORP"},
            "job2": {"title": "Engineer", "company": "excludedcorp"},
        }
        existing_ids = set()

        result = apply_deterministic_filters(all_jobs, existing_ids)

        assert len(result) == 0

    def test_filter_preserves_valid_jobs(self):
        """Keep valid jobs that pass all filters."""
        all_jobs = {
            "job1": {
                "title": "Engineer",
                "company": "TechCorp",
                "location": "Springfield",
                "jobState": "LISTED",
                "applied": False,
            },
            "job2": {
                "title": "Manager",
                "company": "StartupInc",
                "location": "NYC",
            },
        }
        existing_ids = set()

        result = apply_deterministic_filters(all_jobs, existing_ids)

        assert len(result) == 2
        assert all(isinstance(job, dict) for job in result)
        assert all("job_id" in job for job in result)


class TestGmailAuthError:
    """Test runner behaviour when Gmail token is expired."""

    def test_refresh_error_emits_auth_required(self, capsys, monkeypatch):
        """RefreshError from get_job_alert_emails emits auth_required and exits 1."""
        import sys
        from google.auth.exceptions import RefreshError
        import agent.runner as runner

        def _raise(**_kw):
            raise RefreshError("invalid_grant: Token has been expired or revoked.")

        monkeypatch.setattr(sys, "argv", ["runner"])
        monkeypatch.setattr(runner, "validate_setup", lambda: None)
        monkeypatch.setattr(runner, "setup_logging",
                            lambda: __import__("logging").getLogger("scout"))
        monkeypatch.setattr(runner, "init_db", lambda: None)
        monkeypatch.setattr(runner, "get_job_alert_emails", _raise)

        with pytest.raises(SystemExit) as exc_info:
            runner.main()

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert '"auth_required": true' in out
        assert '"status": "error"' in out


class TestClaudeExecutable:
    """Test cross-platform resolution of the claude CLI (C)."""

    def test_resolves_via_which(self, monkeypatch):
        """Returns the absolute path shutil.which finds."""
        import agent.runner as runner
        runner.claude_executable.cache_clear()
        monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/local/bin/claude")
        try:
            assert runner.claude_executable() == "/usr/local/bin/claude"
        finally:
            runner.claude_executable.cache_clear()

    def test_raises_when_missing(self, monkeypatch):
        """Raises FileNotFoundError when claude isn't on PATH."""
        import agent.runner as runner
        runner.claude_executable.cache_clear()
        monkeypatch.setattr(runner.shutil, "which", lambda name: None)
        try:
            with pytest.raises(FileNotFoundError, match="claude"):
                runner.claude_executable()
        finally:
            runner.claude_executable.cache_clear()

    def test_validate_setup_exits_when_claude_missing(self, monkeypatch):
        """validate_setup turns a missing claude CLI into a clean startup exit."""
        import agent.runner as runner
        runner.claude_executable.cache_clear()
        monkeypatch.setattr(runner, "load_roles", lambda: [Mock(profile=None)])
        monkeypatch.setattr(runner.shutil, "which", lambda name: None)
        try:
            with pytest.raises(SystemExit):
                runner.validate_setup()
        finally:
            runner.claude_executable.cache_clear()


class TestKillProcessTree:
    """Test cross-platform subprocess-tree kill (B)."""

    def test_posix_kills_process_group(self, monkeypatch):
        """On POSIX, SIGKILL is sent to the child's process group."""
        import agent.runner as runner
        killed = {}
        monkeypatch.setattr(runner.os, "name", "posix")
        monkeypatch.setattr(runner.os, "getpgid", lambda pid: 4242)
        monkeypatch.setattr(runner.os, "killpg",
                            lambda pgid, sig: killed.update(pgid=pgid, sig=sig))
        proc = MagicMock()
        proc.pid = 999

        runner._kill_process_tree(proc)

        assert killed == {"pgid": 4242, "sig": runner.signal.SIGKILL}

    def test_posix_swallows_already_dead(self, monkeypatch):
        """A process that already exited doesn't raise out of the kill."""
        import agent.runner as runner
        monkeypatch.setattr(runner.os, "name", "posix")

        def _gone(pid):
            raise ProcessLookupError()

        monkeypatch.setattr(runner.os, "getpgid", _gone)
        proc = MagicMock()
        proc.pid = 1

        runner._kill_process_tree(proc)  # must not raise

    def test_windows_uses_taskkill(self, monkeypatch):
        """On Windows, taskkill /T is invoked to walk the tree."""
        import agent.runner as runner
        calls = {}
        monkeypatch.setattr(runner.os, "name", "nt")
        monkeypatch.setattr(runner.subprocess, "run",
                            lambda cmd, **kw: calls.update(cmd=cmd))
        proc = MagicMock()
        proc.pid = 4321

        runner._kill_process_tree(proc)

        assert calls["cmd"] == ["taskkill", "/F", "/T", "/PID", "4321"]


class TestRunScrapeNoFile:
    """Test run_scrape's guidance when no download file appears."""

    def test_warns_about_save_as_dialog(self, capsys, monkeypatch):
        """A missing download file yields Save-As + download_dir guidance."""
        import agent.runner as runner

        monkeypatch.setattr(runner, "run_claude", lambda *a, **k: "")
        monkeypatch.setattr(runner, "load_downloaded_jobs", lambda run_id: None)
        monkeypatch.setattr(runner, "download_dir", lambda: Path("/home/x/Downloads"))

        result = runner.run_scrape("http://linkedin.test", "run1", index=1)

        assert result == []
        err = capsys.readouterr().err
        assert "ask where to save each file" in err.lower()
        assert "download_dir" in err  # points at the config override too


class TestTokenTracking:
    """Test token and cost tracking."""

    def test_add_usage_increments_tokens(self):
        """Add usage increments token counters."""
        from agent.runner import _tokens, _tokens_lock

        with _tokens_lock:
            initial_input = _tokens["input"]
            initial_output = _tokens["output"]

        usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 150,
        }

        _add_usage(usage, 0.05)

        with _tokens_lock:
            assert _tokens["input"] == initial_input + 100
            assert _tokens["output"] == initial_output + 50
            assert _tokens["cache_read"] >= 200

    def test_add_usage_zero_values(self):
        """Handle empty usage dict."""
        from agent.runner import _tokens, _tokens_lock

        with _tokens_lock:
            initial_calls = _tokens["calls"]

        _add_usage({}, 0.0)

        with _tokens_lock:
            assert _tokens["calls"] == initial_calls + 1

    def test_print_token_summary(self, capsys):
        """Print token summary."""
        print_token_summary()

        captured = capsys.readouterr()
        assert "TOKEN USAGE SUMMARY" in captured.out
        assert "API calls" in captured.out
        assert "Estimated cost" in captured.out
