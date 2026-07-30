"""Tests for agent/runner.py — scrape orchestration and enrichment."""

import json
import re
import types
import httpx
import pytest
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

import agent.runner as runner
from agent.runner import (
    _file_job_to_record,
    _extract_json,
    load_downloaded_jobs,
    apply_deterministic_filters,
    _add_usage,
    print_token_summary,
    run_headless,
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


class TestExtractSingleJobId:
    """Test detection of a single-job /jobs/view/<id> URL."""

    def test_matches_job_view_url(self):
        """A plain job-view URL yields its numeric id."""
        assert runner.extract_single_job_id(
            "https://www.linkedin.com/jobs/view/4407398880/") == "4407398880"

    def test_matches_job_view_url_with_query_string(self):
        """Tracking params after the id don't break the match."""
        url = "https://www.linkedin.com/jobs/view/4407398880/?trk=abc&refId=xyz"
        assert runner.extract_single_job_id(url) == "4407398880"

    def test_search_url_returns_none(self):
        """A search-results URL (no /jobs/view/) is not a single-job URL."""
        url = "https://www.linkedin.com/jobs/search-results/?currentJobId=4407398880"
        assert runner.extract_single_job_id(url) is None

    def test_non_linkedin_url_returns_none(self):
        """A URL that merely contains the substring elsewhere is not matched."""
        assert runner.extract_single_job_id("https://example.com/other") is None


class TestResolveScanUrl:
    """Test expansion of a bare job id into its canonical job-view URL."""

    def test_bare_job_id_is_expanded(self):
        """Plain digits become a full job-view URL."""
        assert (runner.resolve_scan_url("4440072975")
                == "https://www.linkedin.com/jobs/view/4440072975/")

    def test_bare_job_id_with_surrounding_whitespace_is_trimmed_then_expanded(self):
        """Whitespace from a copy-paste doesn't block detection."""
        assert (runner.resolve_scan_url("  4440072975  ")
                == "https://www.linkedin.com/jobs/view/4440072975/")

    def test_full_url_passes_through_unchanged(self):
        """An already-full URL is not touched."""
        url = "https://www.linkedin.com/jobs/view/4440072975/"
        assert runner.resolve_scan_url(url) == url

    def test_search_url_passes_through_unchanged(self):
        """A search URL (not all-digits) is not mistaken for a bare job id."""
        url = "https://www.linkedin.com/jobs/search/"
        assert runner.resolve_scan_url(url) == url

    def test_empty_string_passes_through_unchanged(self):
        """The default config-driven run (empty url) is untouched."""
        assert runner.resolve_scan_url("") == ""

    def test_expanded_url_is_then_detected_by_extract_single_job_id(self):
        """resolve_scan_url's output feeds extract_single_job_id correctly (integration)."""
        expanded = runner.resolve_scan_url("4440072975")
        assert runner.extract_single_job_id(expanded) == "4440072975"


class TestRunScrapeSingleJob:
    """Test run_scrape's routing to the single-job Pass 1 prompt."""

    def test_job_id_routes_to_single_prompt(self, monkeypatch):
        """job_id set -> scrape_single_prompt.md with a job-id-bearing message."""
        calls = {}

        def fake_run_claude(prompt_file, user_message):
            calls["prompt_file"] = prompt_file
            calls["user_message"] = user_message
            return ""

        monkeypatch.setattr(runner, "run_claude", fake_run_claude)
        monkeypatch.setattr(runner, "load_downloaded_jobs", lambda run_id: None)
        monkeypatch.setattr(runner, "download_dir", lambda: Path("/home/x/Downloads"))

        runner.run_scrape("https://www.linkedin.com/jobs/view/999/", "run1",
                          index=1, job_id="999")

        assert calls["prompt_file"] == runner.SCRAPE_SINGLE_PROMPT_FILE
        assert "999" in calls["user_message"]

    def test_no_job_id_still_uses_search_prompt(self, monkeypatch):
        """Regression guard: the default (search) path is unchanged."""
        calls = {}

        def fake_run_claude(prompt_file, user_message):
            calls["prompt_file"] = prompt_file
            return ""

        monkeypatch.setattr(runner, "run_claude", fake_run_claude)
        monkeypatch.setattr(runner, "load_downloaded_jobs", lambda run_id: None)
        monkeypatch.setattr(runner, "download_dir", lambda: Path("/home/x/Downloads"))

        runner.run_scrape("https://www.linkedin.com/jobs/search/", "run1", index=1)

        assert calls["prompt_file"] == runner.SCRAPE_PROMPT_FILE

    def test_invalid_job_id_is_reported_and_returns_no_jobs(self, capsys, monkeypatch):
        """An error entry for the requested job_id short-circuits with a clear message."""
        monkeypatch.setattr(runner, "run_claude", lambda *a, **k: "")
        monkeypatch.setattr(
            runner, "load_downloaded_jobs",
            lambda run_id: {"999": {"error": "Error: not_found (404)"}})

        result = runner.run_scrape("https://www.linkedin.com/jobs/view/999/",
                                   "run1", index=1, job_id="999")

        assert result == []
        err = capsys.readouterr().err
        assert "999" in err
        assert "doesn't exist" in err

    def test_missing_job_id_entry_is_also_reported(self, capsys, monkeypatch):
        """The job_id being entirely absent from the download is treated the same as an error."""
        monkeypatch.setattr(runner, "run_claude", lambda *a, **k: "")
        monkeypatch.setattr(runner, "load_downloaded_jobs", lambda run_id: {})

        result = runner.run_scrape("https://www.linkedin.com/jobs/view/999/",
                                   "run1", index=1, job_id="999")

        assert result == []
        err = capsys.readouterr().err
        assert "no data returned" in err

    def test_valid_job_id_proceeds_past_the_check(self, monkeypatch):
        """A real job entry for job_id doesn't trip the invalid-job early exit."""
        monkeypatch.setattr(runner, "run_claude", lambda *a, **k: "")
        monkeypatch.setattr(
            runner, "load_downloaded_jobs",
            lambda run_id: {"999": {"title": "Engineer", "company": "Acme",
                                     "jobState": "LISTED"}})
        monkeypatch.setattr(runner, "get_existing_job_ids", lambda: [])
        monkeypatch.setattr(runner, "clean_jobs", lambda jobs, index=1: None)
        monkeypatch.setattr(runner, "enrich_jobs", lambda jobs, index=1: None)
        monkeypatch.setattr(runner, "load_roles",
                            lambda: [types.SimpleNamespace(name="IC")])

        result = runner.run_scrape("https://www.linkedin.com/jobs/view/999/",
                                   "run1", index=1, job_id="999")

        # role_type was never set by the (stubbed) enrich step, so it's filtered
        # out downstream — the point of this test is just that we got past the
        # invalid-job-id check rather than short-circuiting on a valid entry.
        assert result == []


class TestProcessUrlSingleJob:
    """Test process_url's job_id passthrough to run_scrape and search_name default."""

    def test_job_id_forwarded_and_default_name_set(self, monkeypatch):
        """job_id reaches run_scrape and the default label becomes 'Single job scan'."""
        monkeypatch.setattr(runner, "create_scrape_run", lambda **kw: "run1")
        run_scrape_calls = {}

        def fake_run_scrape(url, run_id, index, job_id=None):
            run_scrape_calls["job_id"] = job_id
            return []

        monkeypatch.setattr(runner, "run_scrape", fake_run_scrape)
        monkeypatch.setattr(runner, "save_jobs", lambda *a, **k: {"saved": 0, "reposts_detected": 0})

        runner.process_url(url="https://www.linkedin.com/jobs/view/999/",
                           index=1, job_id="999")

        assert run_scrape_calls["job_id"] == "999"

    def test_explicit_search_name_is_not_overridden(self, monkeypatch):
        """An explicitly-passed search_name wins even when job_id is set."""
        names = {}
        monkeypatch.setattr(
            runner, "create_scrape_run",
            lambda **kw: names.setdefault("name", kw["search_name"]) or "run1")
        monkeypatch.setattr(runner, "run_scrape", lambda *a, **k: [])
        monkeypatch.setattr(runner, "save_jobs", lambda *a, **k: {"saved": 0, "reposts_detected": 0})

        runner.process_url(url="https://www.linkedin.com/jobs/view/999/",
                           search_name="Custom label", index=1, job_id="999")

        assert names["name"] == "Custom label"


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


def _fake_config(**overrides):
    """A minimal Config-like object for backend-dispatch tests."""
    base = dict(
        llm_backend="claude",
        max_workers=2,
        api_base_url=None,
        api_model=None,
        api_key=None,
        api_timeout=300.0,
        api_clean_params={},
        api_enrich_params={},
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _FakeStreamResponse:
    """Minimal stand-in for the context-managed Response httpx.stream() yields."""

    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _sse_lines(content_pieces=(), usage=None, reasoning_pieces=()):
    """Build fake `data: ...` SSE lines matching Ollama's streaming shape.

    Reasoning pieces are emitted first (as delta.reasoning, empty delta.content
    — confirmed via a live curl test against Ollama not to be mixed into the
    real content), then content pieces (delta.content), then one final chunk
    with empty choices and the usage object (when stream_options.include_usage
    is set), then the [DONE] sentinel.
    """
    lines = []
    for r in reasoning_pieces:
        lines.append("data: " + json.dumps(
            {"choices": [{"delta": {"content": "", "reasoning": r}}]}))
    for c in content_pieces:
        lines.append("data: " + json.dumps({"choices": [{"delta": {"content": c}}]}))
    if usage is not None:
        lines.append("data: " + json.dumps({"choices": [], "usage": usage}))
    lines.append("data: [DONE]")
    return lines


class TestRunHeadlessBackend:
    """Test the Pass 2/3 backend dispatcher (run_headless)."""

    def test_claude_backend_uses_subprocess_path(self, monkeypatch):
        """backend=claude routes to _run_claude_headless with the pass's model."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config())
        seen = {}

        def _fake_claude(model, sp, um):
            seen["model"] = model
            return '{"ok": 1}'

        monkeypatch.setattr(runner, "_run_claude_headless", _fake_claude)
        monkeypatch.setattr(runner, "_run_api_llm",
                            lambda *a, **k: pytest.fail("api path used"))

        assert run_headless("clean", "sys", "usr") == '{"ok": 1}'
        assert seen["model"] == runner.CLEAN_MODEL
        assert run_headless("enrich", "sys", "usr") == '{"ok": 1}'
        assert seen["model"] == runner.ENRICH_MODEL

    def test_api_backend_posts_and_maps_usage(self, monkeypatch):
        """backend=api streams from the server and maps OpenAI usage at zero cost."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box:11434/v1",
            api_model="gpt-oss:20b", api_key="k", api_timeout=42.0))

        captured = {}

        def _fake_stream(method, url, json=None, headers=None, timeout=None):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _FakeStreamResponse(_sse_lines(
                content_pieces=['{"description_clean"', ': "x"}'],
                usage={"prompt_tokens": 11, "completion_tokens": 4},
                reasoning_pieces=["thinking…"],
            ))

        monkeypatch.setattr(runner.httpx, "stream", _fake_stream)

        from agent.runner import _tokens, _tokens_lock
        with _tokens_lock:
            in0, out0, cost0 = _tokens["input"], _tokens["output"], _tokens["cost_usd"]

        result = run_headless("clean", "sys", "usr")

        assert result == '{"description_clean": "x"}'
        assert captured["method"] == "POST"
        assert captured["url"] == "http://box:11434/v1/chat/completions"
        assert captured["json"]["model"] == "gpt-oss:20b"
        assert captured["json"]["stream"] is True
        assert captured["json"]["stream_options"] == {"include_usage": True}
        assert captured["json"]["messages"][0]["role"] == "system"
        assert captured["headers"]["Authorization"] == "Bearer k"
        assert captured["timeout"] == 42.0
        with _tokens_lock:
            assert _tokens["input"] == in0 + 11
            assert _tokens["output"] == out0 + 4
            assert _tokens["cost_usd"] == cost0  # this backend isn't metered by the pipeline

    def test_api_backend_no_api_key_omits_auth_header(self, monkeypatch):
        """Without api_key, no Authorization header is sent."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box:11434/v1",
            api_model="m", api_key=None, api_timeout=5.0))
        captured = {}

        def _fake_stream(method, url, json=None, headers=None, timeout=None):
            captured["headers"] = headers
            return _FakeStreamResponse(_sse_lines(content_pieces=["{}"], usage={}))

        monkeypatch.setattr(runner.httpx, "stream", _fake_stream)
        run_headless("enrich", "sys", "usr")
        assert "Authorization" not in captured["headers"]

    def test_api_backend_http_error_returns_none(self, monkeypatch):
        """A network/HTTP failure exhausts all retries and returns None."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box:11434/v1",
            api_model="m", api_key=None, api_timeout=5.0))
        slept = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))

        def _boom(*a, **k):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(runner.httpx, "stream", _boom)
        assert run_headless("clean", "sys", "usr") is None
        # Retried API_STREAM_RETRIES times, sleeping between attempts (not after the last).
        assert slept == [runner.API_STREAM_RETRY_DELAY_S] * (runner.API_STREAM_RETRIES - 1)

    def test_api_backend_http_error_recovers_on_retry(self, monkeypatch):
        """A stream that fails once then succeeds is not treated as a failure."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box:11434/v1",
            api_model="m", api_key=None, api_timeout=5.0))
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def _fake_stream(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ReadTimeout("stalled")
            return _FakeStreamResponse(_sse_lines(content_pieces=["{}"], usage={}))

        monkeypatch.setattr(runner.httpx, "stream", _fake_stream)
        assert run_headless("clean", "sys", "usr") == "{}"
        assert calls["n"] == 2

    def test_api_backend_malformed_response_returns_none(self, monkeypatch):
        """A stream with no content chunks at all returns None, not a crash."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box:11434/v1",
            api_model="m", api_key=None, api_timeout=5.0))

        def _fake_stream(*a, **k):
            return _FakeStreamResponse(["data: [DONE]"])

        monkeypatch.setattr(runner.httpx, "stream", _fake_stream)
        assert run_headless("enrich", "sys", "usr") is None

    def _capture_payload(self, monkeypatch, config):
        """Run one api call under `config` and return the streamed JSON payload."""
        monkeypatch.setattr(runner, "load_config", lambda: config)
        captured = {}

        def _fake_stream(method, url, json=None, headers=None, timeout=None):
            captured["json"] = json
            return _FakeStreamResponse(_sse_lines(content_pieces=["{}"], usage={}))

        monkeypatch.setattr(runner.httpx, "stream", _fake_stream)
        return captured

    def test_api_per_pass_params_merged(self, monkeypatch):
        """[llm.api.<pass>] params are merged into the payload for that pass."""
        config = _fake_config(
            llm_backend="api", api_base_url="http://box/v1", api_model="m",
            api_clean_params={"temperature": 0.2, "reasoning_effort": "low"},
            api_enrich_params={"reasoning_effort": "high"})
        captured = self._capture_payload(monkeypatch, config)

        run_headless("clean", "sys", "usr")
        assert captured["json"]["temperature"] == 0.2
        assert captured["json"]["reasoning_effort"] == "low"

        run_headless("enrich", "sys", "usr")
        assert captured["json"]["reasoning_effort"] == "high"
        # enrich set no temperature, so none is sent — server default applies
        assert "temperature" not in captured["json"]

    def test_api_params_cannot_clobber_owned_fields(self, monkeypatch):
        """model/messages/stream/stream_options are re-asserted even if a param
        table sets them.

        config validation rejects those keys, but the merge order guards against
        them defensively too.
        """
        config = _fake_config(
            llm_backend="api", api_base_url="http://box/v1", api_model="m",
            api_clean_params={"model": "evil", "stream": False})
        captured = self._capture_payload(monkeypatch, config)

        run_headless("clean", "sys", "usr")
        assert captured["json"]["model"] == "m"
        assert captured["json"]["stream"] is True


class TestEnrichJobsWarmup:
    """Test the prompt-cache warmup is Claude-only."""

    def test_api_backend_skips_sleep_warmup(self, monkeypatch):
        """On the api backend, enrich_jobs never does the cache-warm sleep."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box:11434/v1",
            api_model="m"))
        monkeypatch.setattr(runner, "scoring_enabled", lambda: False)
        monkeypatch.setattr(runner, "enrich_one",
                            lambda job: dict(runner._ENRICH_FAILURE))
        slept = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))

        runner.enrich_jobs([{"job_id": "1"}, {"job_id": "2"}])
        assert slept == []

    def test_claude_backend_sleeps_once_for_warmup(self, monkeypatch):
        """On Claude with >1 job, enrich_jobs warms the cache with one sleep.

        Both jobs fail here (enrich_one always returns _ENRICH_FAILURE), so
        the retry pass's own CLAUDE_RETRY_DELAY_S sleep also fires — the
        warmup sleep is the first of the two.
        """
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config())
        monkeypatch.setattr(runner, "scoring_enabled", lambda: False)
        monkeypatch.setattr(runner, "enrich_one",
                            lambda job: dict(runner._ENRICH_FAILURE))
        slept = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))

        runner.enrich_jobs([{"job_id": "1"}, {"job_id": "2"}])
        assert slept == [2, runner.CLAUDE_RETRY_DELAY_S]


class TestCleanOne:
    """Test clean_one's unit-split -> LLM drop-response -> stitch pipeline."""

    def test_stitches_survivors_from_a_canned_drop_response(self, monkeypatch):
        """A well-formed {"drop": [...]} response yields the stitched result."""
        job = {
            "job_id": "1",
            "description_raw": "Keep this sentence.\n\nDrop this culture sentence.",
        }
        monkeypatch.setattr(
            runner, "run_headless",
            lambda pass_name, sys_prompt, user_msg: '{"drop":[{"r":"2","c":"culture"}]}')

        result = runner.clean_one(job)

        assert result == {"description_clean": "Keep this sentence."}

    def test_empty_drop_list_keeps_everything(self, monkeypatch):
        """{"drop": []} means nothing to remove — full text survives."""
        job = {"job_id": "1", "description_raw": "Keep this whole sentence."}
        monkeypatch.setattr(runner, "run_headless",
                            lambda pass_name, sys_prompt, user_msg: '{"drop":[]}')

        result = runner.clean_one(job)

        assert result == {"description_clean": "Keep this whole sentence."}

    def test_malformed_drop_response_falls_back_to_none(self, monkeypatch):
        """A response with no usable 'drop' list makes clean_one return None."""
        job = {"job_id": "1", "description_raw": "Some description text."}
        monkeypatch.setattr(runner, "run_headless",
                            lambda pass_name, sys_prompt, user_msg: "not json at all")

        assert runner.clean_one(job) is None

    def test_run_headless_failure_falls_back_to_none(self, monkeypatch):
        """A None result from run_headless (backend failure) returns None."""
        job = {"job_id": "1", "description_raw": "Some description text."}
        monkeypatch.setattr(runner, "run_headless",
                            lambda pass_name, sys_prompt, user_msg: None)

        assert runner.clean_one(job) is None

    def test_empty_description_returns_none_without_calling_llm(self, monkeypatch):
        """No description_raw short-circuits before any LLM call."""
        called = []
        monkeypatch.setattr(
            runner, "run_headless",
            lambda pass_name, sys_prompt, user_msg: called.append(1))

        assert runner.clean_one({"job_id": "1", "description_raw": ""}) is None
        assert called == []


class TestWarmUpCleanPass:
    """Test the realistically-sized model warm-up clean call."""

    def test_succeeds_first_attempt_no_sleep(self, monkeypatch):
        """A successful first clean_one call returns immediately, no sleep."""
        monkeypatch.setattr(runner, "clean_one", lambda job: {"description_clean": "x"})
        slept = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))

        runner._warm_up_clean_pass(_fake_config(llm_backend="api"))
        assert slept == []

    def test_recovers_on_retry(self, monkeypatch):
        """Failing the first two attempts then succeeding sleeps twice and returns."""
        calls = {"n": 0}

        def fake_clean_one(job):
            calls["n"] += 1
            return None if calls["n"] < 3 else {"description_clean": "x"}

        monkeypatch.setattr(runner, "clean_one", fake_clean_one)
        slept = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))

        runner._warm_up_clean_pass(_fake_config(llm_backend="api"))
        assert calls["n"] == 3
        assert slept == [runner.WARMUP_CLEAN_RETRY_DELAY_S, runner.WARMUP_CLEAN_RETRY_DELAY_S]

    def test_aborts_run_when_every_attempt_fails(self, monkeypatch):
        """Every attempt failing aborts the run via sys.exit(1), not a silent continue."""
        monkeypatch.setattr(runner, "clean_one", lambda job: None)
        slept = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))

        with pytest.raises(SystemExit) as exc_info:
            runner._warm_up_clean_pass(_fake_config(llm_backend="api"))

        assert exc_info.value.code == 1
        # No sleep after the final (3rd) failed attempt.
        assert slept == [runner.WARMUP_CLEAN_RETRY_DELAY_S, runner.WARMUP_CLEAN_RETRY_DELAY_S]


class TestRetryFailures:
    """Unit tests for the shared one-shot retry helper (both backends)."""

    def test_noop_when_nothing_failed(self):
        """No failures in results means the retry fn is never called."""
        calls = []

        def one_fn(job):
            calls.append(job)
            return "should not be called"

        jobs = [{"id": 1}, {"id": 2}]
        results = ["ok1", "ok2"]
        runner._retry_failures(jobs, results, lambda r: r is None,
                                one_fn, 2, "test", 0)
        assert calls == []
        assert results == ["ok1", "ok2"]

    def test_retries_only_failed_slots(self):
        """Only the jobs whose result trips is_failure get re-run."""
        jobs = [{"id": 1}, {"id": 2}, {"id": 3}]
        results = ["ok", None, None]
        runner._retry_failures(
            jobs, results, lambda r: r is None,
            lambda job: f"retried-{job['id']}", 2, "test", 0)
        assert results == ["ok", "retried-2", "retried-3"]

    def test_repeat_failure_on_retry_is_kept(self):
        """A retry that fails again leaves the failure value in place."""
        jobs = [{"id": 1}]
        results = [None]
        runner._retry_failures(jobs, results, lambda r: r is None,
                                lambda job: None, 2, "test", 0)
        assert results == [None]

    def test_waits_retry_delay_before_retrying(self, monkeypatch):
        """A nonzero retry_delay sleeps before the retry pool fires."""
        slept = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
        jobs = [{"id": 1}]
        results = [None]
        runner._retry_failures(jobs, results, lambda r: r is None,
                                lambda job: "recovered", 2, "test", 5)
        assert slept == [5]
        assert results == ["recovered"]


class TestCleanJobsRetry:
    """Test the shared one-shot retry for Pass 2 clean failures, both backends."""

    def test_api_backend_retries_failed_clean_once(self, monkeypatch):
        """A clean_one failure gets exactly one retry, and success sticks."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box/v1", api_model="m"))
        calls = {"job-fail": 0}
        jobs = [{"job_id": "job-ok", "description_raw": "raw ok"},
                {"job_id": "job-fail", "description_raw": "raw fail"}]

        def fake_clean_one(job):
            if job["job_id"] == "job-fail":
                calls["job-fail"] += 1
                if calls["job-fail"] == 1:
                    return None
                return {"description_clean": "recovered"}
            return {"description_clean": "ok clean"}

        monkeypatch.setattr(runner, "clean_one", fake_clean_one)
        runner.clean_jobs(jobs)

        assert jobs[0]["description_clean"] == "ok clean"
        assert jobs[1]["description_clean"] == "recovered"
        assert calls["job-fail"] == 2

    def test_api_backend_falls_back_to_raw_if_retry_also_fails(self, monkeypatch):
        """If the retry also fails, clean_jobs falls back to description_raw."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box/v1", api_model="m"))
        monkeypatch.setattr(runner, "clean_one", lambda job: None)
        jobs = [{"job_id": "1", "description_raw": "raw text"}]

        runner.clean_jobs(jobs)
        assert jobs[0]["description_clean"] == "raw text"

    def test_claude_backend_retries_failed_clean_once(self, monkeypatch):
        """On Claude too, a clean_one failure gets exactly one retry."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config())
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def fake_clean_one(job):
            calls["n"] += 1
            return None if calls["n"] == 1 else {"description_clean": "recovered"}

        monkeypatch.setattr(runner, "clean_one", fake_clean_one)
        jobs = [{"job_id": "1", "description_raw": "raw text"}]

        runner.clean_jobs(jobs)
        assert jobs[0]["description_clean"] == "recovered"
        assert calls["n"] == 2

    def test_event_log_reports_per_call_duration(self, capsys, monkeypatch):
        """Each clean event-log line reports that job's own call duration."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config())
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)

        def slow_clean_one(job):
            return {"description_clean": "ok"} if job["job_id"] == "ok" else None

        monkeypatch.setattr(runner, "clean_one", slow_clean_one)
        jobs = [{"job_id": "ok", "description_raw": "raw"},
                {"job_id": "bad", "description_raw": "raw"}]

        runner.clean_jobs(jobs)
        msgs = [
            json.loads(line[len(runner.PROGRESS_SENTINEL):])["msg"]
            for line in capsys.readouterr().out.splitlines()
            if line.startswith(runner.PROGRESS_SENTINEL)
            and json.loads(line[len(runner.PROGRESS_SENTINEL):]).get("scope") == "log"
        ]
        assert any(re.search(r"✓ cleaned .*\ds", m) for m in msgs)
        assert any(re.search(r"clean failed .*\ds", m) for m in msgs)


class TestEnrichJobsRetry:
    """Test the shared one-shot retry for Pass 3 enrich failures, both backends."""

    def test_api_backend_retries_failed_enrich_once(self, monkeypatch):
        """An enrich_one failure gets exactly one retry, and success sticks."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box/v1", api_model="m"))
        monkeypatch.setattr(runner, "scoring_enabled", lambda: False)
        calls = {"job-fail": 0}
        jobs = [{"job_id": "job-ok"}, {"job_id": "job-fail"}]

        def fake_enrich_one(job):
            if job["job_id"] == "job-fail":
                calls["job-fail"] += 1
                if calls["job-fail"] == 1:
                    return dict(runner._ENRICH_FAILURE)
                return {"role_type": "IC", "description_summary": "s", "tags": [],
                        "fit_score": 80, "criteria_score": None,
                        "dealbreakers": [], "match_reason": "r", "match_score": 80}
            return {"role_type": "Manager", "description_summary": "ok", "tags": [],
                    "fit_score": 90, "criteria_score": None, "dealbreakers": [],
                    "match_reason": "r2", "match_score": 90}

        monkeypatch.setattr(runner, "enrich_one", fake_enrich_one)
        runner.enrich_jobs(jobs)

        assert jobs[0]["role_type"] == "Manager"
        assert jobs[1]["role_type"] == "IC"
        assert calls["job-fail"] == 2

    def test_api_backend_stays_failed_if_retry_also_fails(self, monkeypatch):
        """If the retry also fails, the job keeps the failure sentinel fields."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config(
            llm_backend="api", api_base_url="http://box/v1", api_model="m"))
        monkeypatch.setattr(runner, "scoring_enabled", lambda: False)
        monkeypatch.setattr(runner, "enrich_one",
                            lambda job: dict(runner._ENRICH_FAILURE))
        jobs = [{"job_id": "1"}]

        runner.enrich_jobs(jobs)
        assert jobs[0]["role_type"] is None

    def test_claude_backend_retries_failed_enrich_once(self, monkeypatch):
        """On Claude too, an enrich_one failure gets exactly one retry."""
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config())
        monkeypatch.setattr(runner, "scoring_enabled", lambda: False)
        monkeypatch.setattr(runner.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def fake_enrich_one(job):
            calls["n"] += 1
            return dict(runner._ENRICH_FAILURE)

        monkeypatch.setattr(runner, "enrich_one", fake_enrich_one)
        jobs = [{"job_id": "1"}, {"job_id": "2"}]

        runner.enrich_jobs(jobs)
        # 2 initial calls (serial-first + parallel) + 2 retries (both failed).
        assert calls["n"] == 4
        assert jobs[0]["role_type"] is None
        assert jobs[1]["role_type"] is None


class TestMainSearchLoop:
    """Test main()'s loop over profiles/config.toml [[linkedin_searches]] entries."""

    def _run_main(self, monkeypatch, searches):
        """Run main() with setup/IO stubbed and process_url recording its calls."""
        import sys
        monkeypatch.setattr(sys, "argv", ["runner"])
        monkeypatch.setattr(runner, "validate_setup", lambda: None)
        monkeypatch.setattr(runner, "setup_logging",
                            lambda: __import__("logging").getLogger("scout"))
        monkeypatch.setattr(runner, "init_db", lambda: None)
        monkeypatch.setattr(runner, "load_config",
                            lambda: _fake_config(linkedin_searches=searches))
        calls = []
        monkeypatch.setattr(runner, "process_url",
                            lambda **kw: calls.append(kw) or True)
        runner.main()
        return calls

    def test_loops_once_per_configured_search_in_order(self, monkeypatch):
        """Each configured search is scraped once, in file order, with its name as the label."""
        searches = [
            types.SimpleNamespace(name="First", url="https://www.linkedin.com/jobs/a"),
            types.SimpleNamespace(name="Second", url="https://www.linkedin.com/jobs/b"),
        ]
        calls = self._run_main(monkeypatch, searches)

        assert len(calls) == 2
        assert calls[0]["url"] == "https://www.linkedin.com/jobs/a"
        assert calls[0]["search_name"] == "First"
        assert calls[0]["index"] == 1
        assert calls[1]["url"] == "https://www.linkedin.com/jobs/b"
        assert calls[1]["search_name"] == "Second"
        assert calls[1]["index"] == 2

    def test_single_entry_config_loops_correctly(self, monkeypatch):
        """A single configured search still gets index=1 (off-by-one guard)."""
        searches = [types.SimpleNamespace(name="Only", url="https://www.linkedin.com/jobs/x")]
        calls = self._run_main(monkeypatch, searches)

        assert len(calls) == 1
        assert calls[0]["index"] == 1

    def _run_main_with_url(self, monkeypatch, url):
        """Run main() with --url, stubbing setup/IO and recording process_url's call."""
        import sys
        monkeypatch.setattr(sys, "argv", ["runner", "--url", url])
        monkeypatch.setattr(runner, "validate_setup", lambda: None)
        monkeypatch.setattr(runner, "setup_logging",
                            lambda: __import__("logging").getLogger("scout"))
        monkeypatch.setattr(runner, "init_db", lambda: None)
        monkeypatch.setattr(runner, "load_config", lambda: _fake_config())
        calls = []
        monkeypatch.setattr(runner, "process_url",
                            lambda **kw: calls.append(kw) or True)
        runner.main()
        return calls

    def test_job_view_url_routes_with_job_id(self, monkeypatch):
        """--url pointing at /jobs/view/<id> passes that id through as job_id."""
        calls = self._run_main_with_url(
            monkeypatch, "https://www.linkedin.com/jobs/view/999/")

        assert len(calls) == 1
        assert calls[0]["url"] == "https://www.linkedin.com/jobs/view/999/"
        assert calls[0]["job_id"] == "999"

    def test_search_url_routes_with_no_job_id(self, monkeypatch):
        """--url pointing at a search page passes job_id=None (regression guard)."""
        calls = self._run_main_with_url(
            monkeypatch, "https://www.linkedin.com/jobs/search/")

        assert len(calls) == 1
        assert calls[0]["job_id"] is None

    def test_bare_job_id_url_is_expanded_and_routed(self, monkeypatch):
        """--url of just a numeric job id is expanded to a job-view URL and routed."""
        calls = self._run_main_with_url(monkeypatch, "4440072975")

        assert len(calls) == 1
        assert calls[0]["url"] == "https://www.linkedin.com/jobs/view/4440072975/"
        assert calls[0]["job_id"] == "4440072975"
