"""Tests for the tags feature: validation, persistence, and backfill."""

import duckdb
import pytest
from unittest.mock import patch

import app.database as app_database
from agent.runner import MAX_TAGS, _clean_tags
from agent.tools import save_jobs
from app.database import init_db
from scripts.backfill_tags import backfill, fetch_untagged


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the app at a fresh DuckDB file in tmp_path and initialise it."""
    db_path = tmp_path / "test.duckdb"
    monkeypatch.setattr(app_database, "DB_PATH", db_path)
    init_db()
    return db_path


def _insert_job(job_id: str, tags=None, description="A long job description."):
    """Insert a minimal job row directly, with optional tags."""
    conn = app_database.get_connection()
    conn.execute(
        """
        INSERT INTO jobs (job_id, title, company, description_raw, tags)
        VALUES (?, ?, ?, ?, ?)
        """,
        [job_id, f"Title {job_id}", "TechCorp", description, tags],
    )
    conn.close()


class TestCleanTags:
    """Test _clean_tags validation of model-produced tag lists."""

    def test_valid_list_passes_through(self):
        """A clean list of tags is returned unchanged."""
        assert _clean_tags(["Remote", "8+ yrs", "Kubernetes"]) == [
            "Remote", "8+ yrs", "Kubernetes"]

    def test_non_list_returns_empty(self):
        """Anything that is not a list yields []."""
        assert _clean_tags(None) == []
        assert _clean_tags("Remote, Hybrid") == []
        assert _clean_tags({"tag": "Remote"}) == []
        assert _clean_tags(42) == []

    def test_caps_at_max_tags(self):
        """More than MAX_TAGS tags are truncated, keeping the first ones."""
        raw = [f"Tag {i}" for i in range(15)]
        cleaned = _clean_tags(raw)
        assert len(cleaned) == MAX_TAGS
        assert cleaned == raw[:MAX_TAGS]

    def test_dedupes_case_insensitively(self):
        """Duplicate tags are dropped; first occurrence wins."""
        assert _clean_tags(["Remote", "remote", "REMOTE", "AWS"]) == ["Remote", "AWS"]

    def test_strips_whitespace_and_drops_empties(self):
        """Tags are stripped; empty or whitespace-only tags are dropped."""
        assert _clean_tags(["  Remote  ", "", "   ", "Go"]) == ["Remote", "Go"]

    def test_drops_non_string_items(self):
        """Non-string items inside the list are skipped."""
        assert _clean_tags(["Remote", 5, None, ["nested"], "Go"]) == ["Remote", "Go"]


class TestTagsPersistence:
    """Round-trip tags through save_jobs and the DuckDB VARCHAR[] column."""

    def test_save_jobs_persists_tags(self, tmp_db):
        """save_jobs writes the tags list; it comes back as a Python list."""
        job = {
            "job_id": "j1",
            "title": "Staff Engineer",
            "company": "TechCorp",
            "location": "Springfield",
            "linkedin_url": "https://linkedin.com/jobs/view/j1",
            "apply_platform": "other",
            "description_raw": "desc",
            "description_summary": "summary",
            "role_type": "IC",
            "tags": ["Remote", "8+ yrs", "Kubernetes"],
        }
        conn = app_database.get_connection()
        conn.execute(
            "INSERT INTO scrape_runs (run_id) VALUES ('run1')")
        conn.close()

        result = save_jobs("run1", [job])
        assert result["saved"] == 1

        conn = app_database.get_connection()
        row = conn.execute(
            "SELECT tags FROM jobs WHERE job_id = 'j1'").fetchone()
        conn.close()
        assert row[0] == ["Remote", "8+ yrs", "Kubernetes"]

    def test_save_jobs_defaults_missing_tags_to_empty_list(self, tmp_db):
        """A job with no tags key is saved with an empty (non-NULL) list."""
        job = {
            "job_id": "j2",
            "title": "Engineering Manager",
            "company": "TechCorp",
            "linkedin_url": "https://linkedin.com/jobs/view/j2",
            "apply_platform": "other",
            "description_raw": "desc",
            "role_type": "Manager",
        }
        conn = app_database.get_connection()
        conn.execute("INSERT INTO scrape_runs (run_id) VALUES ('run1')")
        conn.close()

        save_jobs("run1", [job])

        conn = app_database.get_connection()
        row = conn.execute(
            "SELECT tags FROM jobs WHERE job_id = 'j2'").fetchone()
        conn.close()
        assert row[0] == []

    def test_init_db_creates_full_schema(self, tmp_path, monkeypatch):
        """init_db creates the jobs table with all current columns."""
        db_path = tmp_path / "new.duckdb"
        monkeypatch.setattr(app_database, "DB_PATH", db_path)
        init_db()

        conn = duckdb.connect(str(db_path))
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'jobs'").fetchall()}
        conn.close()
        for expected in ("tags", "fit_score", "criteria_score",
                         "dealbreakers", "match_reason", "description_clean"):
            assert expected in cols


class TestBackfill:
    """Test the one-time tags backfill script."""

    def test_fetch_untagged_only_returns_null_tags(self, tmp_db):
        """Jobs that already have tags (even []) are not selected."""
        _insert_job("a", tags=None)
        _insert_job("b", tags=["Remote"])
        _insert_job("c", tags=[])

        untagged = fetch_untagged()
        assert [j["job_id"] for j in untagged] == ["a"]

    def test_backfill_updates_only_tags(self, tmp_db):
        """Backfill sets tags but never touches role_type or the summary."""
        _insert_job("a", tags=None)
        conn = app_database.get_connection()
        conn.execute(
            "UPDATE jobs SET role_type = 'IC', description_summary = 'orig' "
            "WHERE job_id = 'a'")
        conn.close()

        enriched = {"role_type": "Manager",
                    "description_summary": "rewritten",
                    "tags": ["Remote", "Go"]}
        with patch("scripts.backfill_tags.enrich_one", return_value=enriched):
            backfill()

        conn = app_database.get_connection()
        row = conn.execute(
            "SELECT tags, role_type, description_summary FROM jobs "
            "WHERE job_id = 'a'").fetchone()
        conn.close()
        assert row[0] == ["Remote", "Go"]
        assert row[1] == "IC"            # unchanged
        assert row[2] == "orig"          # unchanged

    def test_backfill_leaves_failures_null_for_retry(self, tmp_db):
        """A failed enrichment (role_type=None) keeps tags NULL so a re-run retries it."""
        _insert_job("a", tags=None)

        failed = {"role_type": None, "description_summary": None, "tags": []}
        with patch("scripts.backfill_tags.enrich_one", return_value=failed):
            backfill()

        conn = app_database.get_connection()
        row = conn.execute(
            "SELECT tags FROM jobs WHERE job_id = 'a'").fetchone()
        conn.close()
        assert row[0] is None
        assert len(fetch_untagged()) == 1

    def test_backfill_dry_run_writes_nothing(self, tmp_db):
        """--dry-run lists jobs without calling the model or writing tags."""
        _insert_job("a", tags=None)

        with patch("scripts.backfill_tags.enrich_one") as mock_enrich:
            backfill(dry_run=True)
        mock_enrich.assert_not_called()
        assert len(fetch_untagged()) == 1

    def test_backfill_respects_limit(self, tmp_db):
        """--limit N only processes the N oldest untagged jobs."""
        _insert_job("a", tags=None)
        _insert_job("b", tags=None)

        enriched = {"role_type": "IC", "description_summary": "s", "tags": ["Go"]}
        with patch("scripts.backfill_tags.enrich_one", return_value=enriched):
            backfill(limit=1)

        assert len(fetch_untagged()) == 1
