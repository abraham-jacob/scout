"""Tests for agent/tools.py — database operations."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from agent.tools import (
    _unwrap_linkedin_redirect,
    create_scrape_run,
    get_existing_job_ids,
    save_jobs,
)


class TestUnwrapLinkedinRedirect:
    """Test LinkedIn safety/go redirect URL unwrapping."""

    def test_unwrap_redirect_url(self):
        """Extract real URL from LinkedIn safety redirect."""
        redirect = "https://www.linkedin.com/safety/go?url=https%3A%2F%2Fexample.com%2Fjob"
        assert _unwrap_linkedin_redirect(redirect) == "https://example.com/job"

    def test_pass_through_regular_url(self):
        """Return non-redirect URLs unchanged."""
        url = "https://example.com/job"
        assert _unwrap_linkedin_redirect(url) == url

    def test_none_input(self):
        """Return None for None input."""
        assert _unwrap_linkedin_redirect(None) is None

    def test_empty_string(self):
        """Return None for empty input."""
        assert _unwrap_linkedin_redirect("") is None

    def test_redirect_without_url_param(self):
        """Return redirect URL unchanged if no url param."""
        redirect = "https://www.linkedin.com/safety/go?other=param"
        assert _unwrap_linkedin_redirect(redirect) == redirect


class TestCreateScrapeRun:
    """Test scrape run creation in database."""

    @patch('agent.tools.get_connection')
    def test_create_scrape_run(self, mock_get_conn):
        """Create a new scrape run and return its ID."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn

        run_id = create_scrape_run(
            search_name="Senior IC Bay Area",
            linkedin_url="https://linkedin.com/jobs",
        )

        assert run_id is not None
        assert len(run_id) == 36  # UUID length
        mock_conn.execute.assert_called_once()
        mock_conn.close.assert_called_once()


class TestGetExistingJobIds:
    """Test retrieving existing job IDs from database."""

    @patch('agent.tools.get_connection')
    def test_get_all_job_ids(self, mock_get_conn):
        """Get all non-dismissed job IDs."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("job1",),
            ("job2",),
            ("job3",),
        ]
        mock_get_conn.return_value = mock_conn

        job_ids = get_existing_job_ids()

        assert job_ids == ["job1", "job2", "job3"]
        mock_conn.execute.assert_called_once()
        assert "dismissed" in mock_conn.execute.call_args[0][0]

    @patch('agent.tools.get_connection')
    def test_get_empty_job_ids(self, mock_get_conn):
        """Return empty list when no jobs found."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_conn.return_value = mock_conn

        job_ids = get_existing_job_ids()

        assert job_ids == []


class TestSaveJobs:
    """Test saving enriched jobs to database."""

    @patch('agent.tools.find_original_job')
    @patch('agent.tools.get_connection')
    def test_save_single_job(self, mock_get_conn, mock_find_orig):
        """Save a single job to database."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_find_orig.return_value = None

        job = {
            "job_id": "12345",
            "title": "Senior Engineer",
            "company": "TechCorp",
            "location": "Springfield, USA",
            "linkedin_url": "https://linkedin.com/jobs/view/12345",
            "apply_url": "https://example.com/apply",
            "apply_platform": "easy_apply",
            "salary_range": "$150k-$200k",
            "description_raw": "Job description here",
            "description_summary": "Summary",
            "role_type": "IC",
        }

        result = save_jobs("run_id", [job])

        assert result["saved"] == 1
        assert result["reposts_detected"] == 0

    @patch('agent.tools.find_original_job')
    @patch('agent.tools.get_connection')
    def test_save_job_with_null_company(self, mock_get_conn, mock_find_orig):
        """A job whose scrape yielded company = None saves instead of crashing.

        Regression: job.get("company", "") returns None when the key is
        present with a null value, and None.lower() blew up the whole run.
        """
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_find_orig.return_value = None

        job = {
            "job_id": "12345",
            "title": "Engineer",
            "company": None,
            "location": "NYC",
            "linkedin_url": "https://linkedin.com/jobs/view/12345",
            "apply_platform": "easy_apply",
            "description_raw": "Job",
            "role_type": "IC",
        }

        result = save_jobs("run_id", [job])

        assert result["saved"] == 1

    @patch('agent.tools.find_original_job')
    @patch('agent.tools.get_connection')
    def test_detect_repost(self, mock_get_conn, mock_find_orig):
        """Detect and mark reposts."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_find_orig.return_value = "original_job_id"

        job = {
            "job_id": "12345",
            "title": "Engineer",
            "company": "TechCorp",
            "location": "NYC",
            "linkedin_url": "https://linkedin.com/jobs/view/12345",
            "apply_platform": "easy_apply",
            "description_raw": "Job",
            "role_type": "IC",
        }

        result = save_jobs("run_id", [job])

        assert result["saved"] == 1
        assert result["reposts_detected"] == 1

    @patch('agent.tools.find_original_job')
    @patch('agent.tools.get_connection')
    def test_unwrap_apply_url(self, mock_get_conn, mock_find_orig):
        """Unwrap LinkedIn safety redirect in apply URLs."""
        mock_conn = MagicMock()
        mock_get_conn.return_value = mock_conn
        mock_find_orig.return_value = None

        job = {
            "job_id": "12345",
            "title": "Engineer",
            "company": "TechCorp",
            "location": "NYC",
            "linkedin_url": "https://linkedin.com/jobs/view/12345",
            "apply_url": "https://www.linkedin.com/safety/go?url=https%3A%2F%2Fexample.com",
            "apply_platform": "easy_apply",
            "description_raw": "Job",
            "role_type": "IC",
        }

        result = save_jobs("run_id", [job])

        assert result["saved"] == 1
        # Verify the execute was called and the URL was unwrapped
        call_args = mock_conn.execute.call_args_list
        assert any("example.com" in str(call) for call in call_args)
