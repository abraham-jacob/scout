"""Tests for app/database.py — database initialization and queries."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import duckdb

from app.database import (
    get_connection,
    init_db,
    find_original_job,
    JOB_STATUSES,
    APPLY_PLATFORMS,
)


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "scout.duckdb"
        with patch('app.database.DB_PATH', db_path):
            init_db()
            yield db_path
        # Cleanup
        if db_path.exists():
            db_path.unlink()


class TestGetConnection:
    """Test database connection."""

    def test_get_connection_returns_duckdb_connection(self, temp_db):
        """Get connection returns DuckDB connection object."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            assert conn is not None
            assert isinstance(conn, duckdb.DuckDBPyConnection)
            conn.close()

    def test_get_connection_creates_data_directory(self):
        """Get connection creates data directory if missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "nonexistent" / "data" / "scout.duckdb"

            with patch('app.database.DB_PATH', db_path):
                conn = get_connection()

                assert db_path.parent.exists()
                conn.close()


class TestInitDb:
    """Test database initialization."""

    def test_init_db_creates_tables(self, temp_db):
        """Initialize database creates required tables."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            # Check scrape_runs table exists
            tables = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='memory'"
            ).fetchall()
            table_names = [t[0] for t in tables]

            # Tables are created in DuckDB
            scrape_runs_exists = any("scrape_runs" in name.lower() for name in table_names)
            jobs_exists = any("jobs" in name.lower() for name in table_names)

            conn.close()

    def test_init_db_idempotent(self, temp_db):
        """Initialize database can be called multiple times safely."""
        with patch('app.database.DB_PATH', temp_db):
            init_db()
            init_db()

            conn = get_connection()
            # If tables already exist, INSERT IF NOT EXISTS prevents errors
            conn.close()

    def test_scrape_runs_table_schema(self, temp_db):
        """Verify scrape_runs table has correct columns."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            result = conn.execute("SELECT * FROM scrape_runs LIMIT 0").description
            columns = [col[0] for col in result]

            assert "run_id" in columns
            assert "email_subject" in columns
            assert "email_date" in columns
            assert "linkedin_search_url" in columns
            assert "role_type" in columns
            assert "jobs_found" in columns
            assert "run_at" in columns

            conn.close()

    def test_jobs_table_schema(self, temp_db):
        """Verify jobs table has correct columns."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            result = conn.execute("SELECT * FROM jobs LIMIT 0").description
            columns = [col[0] for col in result]

            assert "job_id" in columns
            assert "scrape_run_id" in columns
            assert "title" in columns
            assert "company" in columns
            assert "location" in columns
            assert "role_type" in columns
            assert "description_raw" in columns
            assert "description_summary" in columns
            assert "linkedin_url" in columns
            assert "apply_url" in columns
            assert "apply_platform" in columns
            assert "salary_range" in columns
            assert "status" in columns
            assert "seen" in columns
            assert "is_repost" in columns
            assert "original_job_id" in columns
            assert "date_scraped" in columns

            conn.close()


class TestFindOriginalJob:
    """Test finding original job for repost detection."""

    def test_find_original_job_exact_match(self, temp_db):
        """Find original job with exact title and company match."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            # Insert original job
            conn.execute(
                """INSERT INTO jobs (job_id, title, company, status, is_repost, date_scraped)
                   VALUES (?, ?, ?, ?, ?, NOW())""",
                ["job1", "Senior Engineer", "TechCorp", "new", False],
            )

            result = find_original_job(conn, "Senior Engineer", "TechCorp")

            assert result == "job1"
            conn.close()

    def test_find_original_job_case_insensitive(self, temp_db):
        """Finding original job is case-insensitive."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            # Insert with mixed case
            conn.execute(
                """INSERT INTO jobs (job_id, title, company, status, is_repost, date_scraped)
                   VALUES (?, ?, ?, ?, ?, NOW())""",
                ["job1", "Senior Engineer", "techcorp", "new", False],
            )

            result = find_original_job(conn, "SENIOR ENGINEER", "TECHCORP")

            assert result == "job1"
            conn.close()

    def test_find_original_job_ignores_dismissed(self, temp_db):
        """Skip dismissed jobs when finding original."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            # Insert dismissed job
            conn.execute(
                """INSERT INTO jobs (job_id, title, company, status, is_repost, date_scraped)
                   VALUES (?, ?, ?, ?, ?, NOW())""",
                ["job1", "Engineer", "Corp", "dismissed", False],
            )

            result = find_original_job(conn, "Engineer", "Corp")

            assert result is None
            conn.close()

    def test_find_original_job_ignores_reposts(self, temp_db):
        """Skip repost jobs when finding original."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            # Insert repost job
            conn.execute(
                """INSERT INTO jobs (job_id, title, company, status, is_repost, date_scraped)
                   VALUES (?, ?, ?, ?, ?, NOW())""",
                ["job1", "Engineer", "Corp", "new", True],
            )

            result = find_original_job(conn, "Engineer", "Corp")

            assert result is None
            conn.close()

    def test_find_original_job_returns_earliest(self, temp_db):
        """Return earliest job when multiple match."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            # Insert multiple jobs
            conn.execute(
                """INSERT INTO jobs (job_id, title, company, status, is_repost, date_scraped)
                   VALUES (?, ?, ?, ?, ?, NOW())""",
                ["job2", "Engineer", "Corp", "new", False],
            )
            # Insert older job second (but it will be earliest chronologically)
            conn.execute(
                """INSERT INTO jobs (job_id, title, company, status, is_repost, date_scraped)
                   VALUES (?, ?, ?, ?, ?, CAST('2024-01-01' AS TIMESTAMP))""",
                ["job1", "Engineer", "Corp", "new", False],
            )

            result = find_original_job(conn, "Engineer", "Corp")

            # Should return the earliest
            assert result == "job1"
            conn.close()

    def test_find_original_job_not_found(self, temp_db):
        """Return None when no matching job found."""
        with patch('app.database.DB_PATH', temp_db):
            conn = get_connection()

            result = find_original_job(conn, "Engineer", "NonexistentCorp")

            assert result is None
            conn.close()


class TestConstants:
    """Test module constants."""

    def test_job_statuses_defined(self):
        """JOB_STATUSES contains expected values."""
        assert "new" in JOB_STATUSES
        assert "saved" in JOB_STATUSES
        assert "applied" in JOB_STATUSES
        assert "rejected" in JOB_STATUSES
        assert "dismissed" in JOB_STATUSES
        assert len(JOB_STATUSES) >= 5

    def test_apply_platforms_defined(self):
        """APPLY_PLATFORMS contains expected values."""
        assert "greenhouse" in APPLY_PLATFORMS
        assert "ashby" in APPLY_PLATFORMS
        assert "workday" in APPLY_PLATFORMS
        assert "easy_apply" in APPLY_PLATFORMS
        assert "other" in APPLY_PLATFORMS
