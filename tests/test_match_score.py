"""Tests for the match-score feature: validation, derivation, persistence,
system-prompt assembly, sorting, and the scores backfill."""

import pytest
from unittest.mock import patch

import agent.runner as runner
import app.database as app_database
from agent.runner import (
    _clean_score,
    build_enrich_system_prompt,
    compute_match_score,
    scoring_enabled,
)

# The [scoring] values in conftest's STANDARD_TEST_CONFIG.
DEALBREAKER_CAP = 30.0
from agent.tools import save_jobs
from app.database import init_db
from app.main import _fetch_jobs
from scripts.backfill_scores import backfill, fetch_unscored, recompute


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the app at a fresh DuckDB file in tmp_path and initialise it."""
    db_path = tmp_path / "test.duckdb"
    monkeypatch.setattr(app_database, "DB_PATH", db_path)
    init_db()
    return db_path


@pytest.fixture
def profile_files(tmp_path, monkeypatch):
    """Create resume + the standard test roles' profile files (and reset the
    prompt cache).

    The isolated_roles_config autouse fixture provides the Manager/IC test
    config, so the referenced profiles are profile_manager.md/profile_ic.md.
    """
    monkeypatch.setattr(runner, "_enrich_system_prompt_cache", None)
    resume = tmp_path / "resume.md"
    ic = tmp_path / "profile_ic.md"
    mgr = tmp_path / "profile_manager.md"
    criteria = tmp_path / "criteria.md"
    resume.write_text("RESUME CONTENT")
    ic.write_text("IC PROFILE CONTENT")
    mgr.write_text("MANAGER PROFILE CONTENT")
    monkeypatch.setattr(runner, "PROFILES_DIR", tmp_path)
    monkeypatch.setattr(runner, "RESUME_FILE", resume)
    monkeypatch.setattr(runner, "CRITERIA_FILE", criteria)
    return {"resume": resume, "ic": ic, "mgr": mgr, "criteria": criteria}


def _insert_job(job_id: str, **cols):
    """Insert a minimal job row directly, with optional extra columns."""
    base = {"title": f"Title {job_id}", "company": "TechCorp",
            "description_raw": "A long job description."}
    base.update(cols)
    names = ", ".join(["job_id"] + list(base))
    placeholders = ", ".join("?" * (len(base) + 1))
    conn = app_database.get_connection()
    conn.execute(
        f"INSERT INTO jobs ({names}) VALUES ({placeholders})",
        [job_id] + list(base.values()),
    )
    conn.close()


class TestCleanScore:
    """Test _clean_score validation of model-produced scores."""

    def test_valid_scores_pass(self):
        """Ints and floats in range come back as floats."""
        assert _clean_score(87) == 87.0
        assert _clean_score(72.5) == 72.5
        assert _clean_score(0) == 0.0

    def test_out_of_range_clamped(self):
        """Scores outside 0-100 are clamped, not rejected."""
        assert _clean_score(150) == 100.0
        assert _clean_score(-10) == 0.0

    def test_non_numeric_returns_none(self):
        """Strings, None, bools, and containers yield None."""
        assert _clean_score("87") is None
        assert _clean_score(None) is None
        assert _clean_score(True) is None
        assert _clean_score([87]) is None


class TestComputeMatchScore:
    """Test the FIT/CRITERIA weighting and dealbreaker cap."""

    def test_weighted_sum(self):
        """85/15 weighting of fit and criteria."""
        assert compute_match_score(80.0, 60.0, []) == pytest.approx(77.0)

    def test_no_criteria_falls_back_to_fit(self):
        """Without criteria.md the score is pure fit."""
        assert compute_match_score(80.0, None, []) == 80.0

    def test_dealbreaker_caps_score(self):
        """A hit dealbreaker caps even a perfect fit."""
        assert compute_match_score(100.0, 100.0, ["On-site 4+ days"]) == DEALBREAKER_CAP

    def test_dealbreaker_does_not_raise_low_score(self):
        """The cap is a ceiling, not a floor."""
        assert compute_match_score(10.0, 10.0, ["Crypto"]) == 10.0

    def test_no_fit_score_returns_none(self):
        """Without a fit score there is no match score."""
        assert compute_match_score(None, 90.0, []) is None


class TestSystemPromptAssembly:
    """Test build_enrich_system_prompt with and without profile files."""

    def test_without_profiles_base_prompt_only(self, tmp_path, monkeypatch):
        """No profile files -> scoring disabled, base prompt only."""
        monkeypatch.setattr(runner, "_enrich_system_prompt_cache", None)
        monkeypatch.setattr(runner, "RESUME_FILE", tmp_path / "missing.md")
        assert not scoring_enabled()
        prompt = build_enrich_system_prompt()
        assert "role_type" in prompt
        assert "fit_score" not in prompt

    def test_with_profiles_includes_scoring_and_artifacts(self, profile_files):
        """All three files -> scoring prompt + resume + both profiles included."""
        assert scoring_enabled()
        prompt = build_enrich_system_prompt()
        assert "fit_score" in prompt
        assert "RESUME CONTENT" in prompt
        assert "IC PROFILE CONTENT" in prompt
        assert "MANAGER PROFILE CONTENT" in prompt
        assert "CRITERIA CONTENT" not in prompt  # criteria.md not created

    def test_criteria_appended_when_present(self, profile_files):
        """criteria.md is optional and appended when it exists."""
        profile_files["criteria"].write_text("CRITERIA CONTENT")
        prompt = build_enrich_system_prompt()
        assert "CRITERIA CONTENT" in prompt

    def test_prompt_is_cached(self, profile_files):
        """The assembled prompt is built once per process."""
        first = build_enrich_system_prompt()
        profile_files["resume"].write_text("CHANGED")
        assert build_enrich_system_prompt() is first


class TestScorePersistence:
    """Round-trip scoring fields through save_jobs."""

    def test_save_jobs_persists_scores(self, tmp_db):
        """save_jobs writes all five scoring columns."""
        job = {
            "job_id": "j1",
            "title": "Staff Engineer",
            "company": "TechCorp",
            "linkedin_url": "https://linkedin.com/jobs/view/j1",
            "apply_platform": "other",
            "description_raw": "desc",
            "role_type": "IC",
            "fit_score": 88.0,
            "criteria_score": 60.0,
            "dealbreakers": ["On-site 4+ days"],
            "match_reason": "Strong fit but on-site.",
            "match_score": 30.0,
        }
        conn = app_database.get_connection()
        conn.execute("INSERT INTO scrape_runs (run_id) VALUES ('run1')")
        conn.close()

        save_jobs("run1", [job])

        conn = app_database.get_connection()
        row = conn.execute(
            "SELECT fit_score, criteria_score, dealbreakers, match_reason, "
            "match_score FROM jobs WHERE job_id = 'j1'").fetchone()
        conn.close()
        assert row == (88.0, 60.0, ["On-site 4+ days"], "Strong fit but on-site.", 30.0)

    def test_unscored_job_saves_nulls(self, tmp_db):
        """A job with no scoring fields saves NULL scores and empty dealbreakers."""
        job = {
            "job_id": "j2",
            "title": "Engineer",
            "company": "TechCorp",
            "linkedin_url": "https://linkedin.com/jobs/view/j2",
            "apply_platform": "other",
            "description_raw": "desc",
            "role_type": "IC",
        }
        conn = app_database.get_connection()
        conn.execute("INSERT INTO scrape_runs (run_id) VALUES ('run1')")
        conn.close()

        save_jobs("run1", [job])

        conn = app_database.get_connection()
        row = conn.execute(
            "SELECT fit_score, match_score, dealbreakers FROM jobs "
            "WHERE job_id = 'j2'").fetchone()
        conn.close()
        assert row == (None, None, [])


class TestSortByMatch:
    """Test the sort parameter on _fetch_jobs."""

    def test_sort_by_match_nulls_last(self, tmp_db):
        """sort='match' orders by score desc with unscored jobs last."""
        _insert_job("low", match_score=40.0)
        _insert_job("unscored")
        _insert_job("high", match_score=90.0)

        ids = [j["job_id"] for j in _fetch_jobs(sort="match")]
        assert ids == ["high", "low", "unscored"]

    def test_default_sort_is_newest(self, tmp_db):
        """Default sort ignores match_score entirely."""
        conn = app_database.get_connection()
        conn.execute("""
            INSERT INTO jobs (job_id, title, company, match_score, date_scraped)
            VALUES ('old', 't', 'c', 99.0, '2026-01-01'),
                   ('new', 't', 'c', 10.0, '2026-06-01')
        """)
        conn.close()
        ids = [j["job_id"] for j in _fetch_jobs()]
        assert ids == ["new", "old"]


class TestBackfillScores:
    """Test the scores backfill script."""

    def test_requires_profiles(self, tmp_db, monkeypatch, capsys):
        """Backfill exits with guidance when profiles are missing."""
        monkeypatch.setattr("scripts.backfill_scores.scoring_enabled", lambda: False)
        with pytest.raises(SystemExit):
            backfill()

    def test_updates_only_scoring_columns(self, tmp_db, monkeypatch):
        """Backfill writes scores but never touches role_type/summary/tags."""
        _insert_job("a", role_type="IC", description_summary="orig", tags=["Go"])

        enriched = {"role_type": "Manager", "description_summary": "rewritten",
                    "tags": ["Changed"], "fit_score": 90.0, "criteria_score": 80.0,
                    "dealbreakers": [], "match_reason": "why", "match_score": 88.5}
        monkeypatch.setattr("scripts.backfill_scores.scoring_enabled", lambda: True)
        with patch("scripts.backfill_scores.enrich_one", return_value=enriched):
            backfill()

        conn = app_database.get_connection()
        row = conn.execute(
            "SELECT match_score, match_reason, role_type, description_summary, tags "
            "FROM jobs WHERE job_id = 'a'").fetchone()
        conn.close()
        assert row[0] == 88.5
        assert row[1] == "why"
        assert row[2] == "IC"          # unchanged
        assert row[3] == "orig"        # unchanged
        assert row[4] == ["Go"]        # unchanged

    def test_failures_and_other_roles_stay_null(self, tmp_db, monkeypatch):
        """Failed calls and Other-classified jobs keep fit_score NULL."""
        _insert_job("failed_job")
        _insert_job("other_job")

        def fake_enrich(job):
            """Return a failure for one job and an unscorable Other for the other."""
            if job["job_id"] == "failed_job":
                return {"role_type": None, "fit_score": None}
            return {"role_type": "Other", "fit_score": None}

        monkeypatch.setattr("scripts.backfill_scores.scoring_enabled", lambda: True)
        with patch("scripts.backfill_scores.enrich_one", side_effect=fake_enrich):
            backfill()

        assert {j["job_id"] for j in fetch_unscored()} == {"failed_job", "other_job"}

    def test_recompute_rederives_final_scores(self, tmp_db):
        """--recompute rebuilds match_score from stored subscores without LLM calls."""
        _insert_job("a", fit_score=80.0, criteria_score=60.0,
                    dealbreakers=[], match_score=1.0)
        _insert_job("b", fit_score=100.0, criteria_score=100.0,
                    dealbreakers=["Crypto"], match_score=100.0)
        _insert_job("c")  # no subscores — untouched

        recompute()

        conn = app_database.get_connection()
        rows = dict(conn.execute(
            "SELECT job_id, match_score FROM jobs").fetchall())
        conn.close()
        assert rows["a"] == pytest.approx(77.0)
        assert rows["b"] == DEALBREAKER_CAP
        assert rows["c"] is None
