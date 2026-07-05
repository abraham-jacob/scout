"""Tests for the user config: profiles/config.toml parsing and validation
(roles, gmail, filters, scoring), color assignment, prompt injection, and
role normalization."""

import pytest

import agent.runner as runner
import app.config as app_config
from agent.runner import _normalize_role, build_enrich_system_prompt
from app.config import (
    ROLE_COLOR_PALETTE,
    Role,
    load_config,
    load_roles,
    role_color_map,
)

# Valid non-roles sections appended to roles-focused test configs, since the
# whole file is validated on every load.
BOILERPLATE_SECTIONS = """
[gmail]
label = "Test Label"

[filters]
exclude_companies = []

[scoring]
fit_weight = 0.85
criteria_weight = 0.15
dealbreaker_cap = 30.0

[logging]
dir = "logs"
"""


def _write_config(text: str, boilerplate: bool = True) -> None:
    """Write TOML to the per-test config path set up by isolated_roles_config.

    Appends valid [gmail]/[filters]/[scoring] sections unless boilerplate is
    False (for tests exercising those sections' own validation).
    """
    if boilerplate:
        text += BOILERPLATE_SECTIONS
    app_config.CONFIG_FILE.write_text(text)


class TestLoadRoles:
    """Test load_roles parsing and validation."""

    def test_standard_test_config_parses(self):
        """The fixture-provided config yields the Manager/IC roles."""
        assert [r.name for r in load_roles()] == ["Manager", "IC"]

    def test_absent_file_raises(self):
        """config.toml is required — no file, no roles, no silent default."""
        app_config.CONFIG_FILE.unlink()
        with pytest.raises(ValueError, match="not found"):
            load_roles()

    def test_empty_config_raises(self):
        """A config.toml that exists but defines no roles is an error, not a
        silent fallback — a job search needs at least one target role."""
        _write_config("# nothing configured yet\n")
        with pytest.raises(ValueError, match="at least one role"):
            load_roles()

    def test_custom_roles_parsed(self):
        """Custom roles come back in order, with optional profile."""
        _write_config("""
[[roles]]
name = "Product Manager"
definition = "owns product strategy and execution"
profile = "profile_pm.md"

[[roles]]
name = "Designer"
definition = "owns UX and visual design"
""")
        roles = load_roles()
        assert roles == [
            Role("Product Manager", "owns product strategy and execution", "profile_pm.md"),
            Role("Designer", "owns UX and visual design", None),
        ]

    def test_missing_definition_raises(self):
        """A role without a definition is a config error, not a silent default."""
        _write_config('[[roles]]\nname = "PM"\n')
        with pytest.raises(ValueError, match="name.*definition|definition"):
            load_roles()

    def test_duplicate_name_raises(self):
        """Duplicate role names (case-insensitive) are rejected."""
        _write_config("""
[[roles]]
name = "PM"
definition = "a"

[[roles]]
name = "pm"
definition = "b"
""")
        with pytest.raises(ValueError, match="duplicate"):
            load_roles()

    def test_other_is_reserved(self):
        """'Other' can't be a configured role — it's the drop bucket."""
        _write_config('[[roles]]\nname = "Other"\ndefinition = "x"\n')
        with pytest.raises(ValueError, match="reserved"):
            load_roles()


ROLES_ONLY = '[[roles]]\nname = "PM"\ndefinition = "products"\n'


class TestConfigSections:
    """Test validation of the [gmail]/[filters]/[scoring] sections."""

    def test_full_config_parses(self):
        """The standard test config yields every section's values."""
        config = load_config()
        assert config.gmail_label == "Job Alerts"
        assert config.exclude_companies == ["ExcludedCorp"]
        assert (config.fit_weight, config.criteria_weight,
                config.dealbreaker_cap) == (0.85, 0.15, 30.0)

    def test_empty_exclude_list_is_fine(self):
        """[filters] exclude_companies = [] is explicitly allowed."""
        _write_config(ROLES_ONLY)  # boilerplate has an empty exclude list
        assert load_config().exclude_companies == []

    def test_missing_gmail_section_raises(self):
        """No [gmail] -> error naming the section."""
        _write_config(ROLES_ONLY + BOILERPLATE_SECTIONS.replace(
            '[gmail]\nlabel = "Test Label"\n', ""), boilerplate=False)
        with pytest.raises(ValueError, match=r"\[gmail\]"):
            load_config()

    def test_empty_gmail_label_raises(self):
        """A blank label is as bad as a missing one."""
        _write_config(ROLES_ONLY + BOILERPLATE_SECTIONS.replace(
            'label = "Test Label"', 'label = "  "'), boilerplate=False)
        with pytest.raises(ValueError, match=r"\[gmail\]"):
            load_config()

    def test_missing_exclude_companies_raises(self):
        """[filters] without the exclude_companies key is an error."""
        _write_config(ROLES_ONLY + BOILERPLATE_SECTIONS.replace(
            "exclude_companies = []", ""), boilerplate=False)
        with pytest.raises(ValueError, match="exclude_companies"):
            load_config()

    def test_missing_scoring_key_raises(self):
        """Each [scoring] key is required."""
        _write_config(ROLES_ONLY + BOILERPLATE_SECTIONS.replace(
            "dealbreaker_cap = 30.0", ""), boilerplate=False)
        with pytest.raises(ValueError, match="dealbreaker_cap"):
            load_config()

    def test_weights_must_sum_to_one(self):
        """fit_weight + criteria_weight != 1 is a config error."""
        _write_config(ROLES_ONLY + BOILERPLATE_SECTIONS.replace(
            "fit_weight = 0.85", "fit_weight = 0.5"), boilerplate=False)
        with pytest.raises(ValueError, match="sum to 1"):
            load_config()

    def test_cap_out_of_range_raises(self):
        """dealbreaker_cap outside 0-100 is a config error."""
        _write_config(ROLES_ONLY + BOILERPLATE_SECTIONS.replace(
            "dealbreaker_cap = 30.0", "dealbreaker_cap = 130"), boilerplate=False)
        with pytest.raises(ValueError, match="dealbreaker_cap"):
            load_config()


class TestScrapeSection:
    """Test the optional [scrape] download_dir config key."""

    def test_defaults_to_downloads_when_absent(self):
        """No [scrape] section -> download_dir defaults to ~/Downloads."""
        from app.config import DEFAULT_DOWNLOAD_DIR
        _write_config(ROLES_ONLY)  # boilerplate has no [scrape] section
        assert load_config().download_dir == DEFAULT_DOWNLOAD_DIR

    def test_override_is_used(self):
        """A [scrape] download_dir value overrides the default."""
        _write_config(ROLES_ONLY + '\n[scrape]\ndownload_dir = "/data/dl"\n')
        assert load_config().download_dir == "/data/dl"

    def test_blank_download_dir_raises(self):
        """A present-but-blank download_dir is a typo, not a fallback."""
        _write_config(ROLES_ONLY + '\n[scrape]\ndownload_dir = "  "\n')
        with pytest.raises(ValueError, match="download_dir"):
            load_config()


class TestDownloadDirResolution:
    """Test agent.runner.download_dir path resolution."""

    def test_expands_tilde(self, monkeypatch):
        """~ in the configured download_dir resolves under the home directory."""
        from pathlib import Path
        monkeypatch.setenv("HOME", "/home/tester")
        _write_config(ROLES_ONLY + '\n[scrape]\ndownload_dir = "~/dl"\n')
        assert runner.download_dir() == Path("/home/tester/dl")

    def test_absolute_path_passes_through(self):
        """An absolute configured path is used as-is."""
        from pathlib import Path
        _write_config(ROLES_ONLY + '\n[scrape]\ndownload_dir = "/srv/downloads"\n')
        assert runner.download_dir() == Path("/srv/downloads")


class TestRoleColorMap:
    """Test palette assignment by config order."""

    def test_first_two_slots_keep_original_colors(self):
        """Manager-first/IC-second configs keep violet and teal (slots 0 and 1)."""
        colors = role_color_map(load_roles())
        assert "violet" in colors["Manager"]
        assert "teal" in colors["IC"]

    def test_palette_wraps(self):
        """More roles than palette entries wrap back to the start."""
        roles = [Role(f"R{i}", "d") for i in range(len(ROLE_COLOR_PALETTE) + 2)]
        colors = role_color_map(roles)
        assert colors["R0"] == colors[f"R{len(ROLE_COLOR_PALETTE)}"]


class TestPromptInjection:
    """Test that configured roles land in the enrichment system prompt."""

    def test_custom_roles_in_prompt(self, tmp_path, monkeypatch):
        """Role names and definitions replace the prompt placeholders."""
        monkeypatch.setattr(runner, "_enrich_system_prompt_cache", None)
        monkeypatch.setattr(runner, "RESUME_FILE", tmp_path / "missing.md")
        _write_config("""
[[roles]]
name = "Product Manager"
definition = "owns product strategy and execution"
""")
        prompt = build_enrich_system_prompt()
        assert "**`Product Manager`** — owns product strategy and execution" in prompt
        assert '"Product Manager" | "Other"' in prompt
        assert "{{ROLE_DEFINITIONS}}" not in prompt
        assert "{{ROLE_ENUM}}" not in prompt

    def test_roleless_profile_scoring_on_resume_alone(self, tmp_path, monkeypatch):
        """A role with no profile file still allows scoring with just resume.md."""
        monkeypatch.setattr(runner, "_enrich_system_prompt_cache", None)
        monkeypatch.setattr(runner, "PROFILES_DIR", tmp_path)
        resume = tmp_path / "resume.md"
        resume.write_text("RESUME CONTENT")
        monkeypatch.setattr(runner, "RESUME_FILE", resume)
        monkeypatch.setattr(runner, "CRITERIA_FILE", tmp_path / "criteria.md")
        _write_config('[[roles]]\nname = "PM"\ndefinition = "products"\n')

        assert runner.scoring_enabled()
        prompt = build_enrich_system_prompt()
        assert "RESUME CONTENT" in prompt
        assert "fit_score" in prompt

    def test_missing_referenced_profile_disables_scoring(self, tmp_path, monkeypatch):
        """A profile referenced in config but absent on disk turns scoring off."""
        monkeypatch.setattr(runner, "PROFILES_DIR", tmp_path)
        resume = tmp_path / "resume.md"
        resume.write_text("RESUME CONTENT")
        monkeypatch.setattr(runner, "RESUME_FILE", resume)
        _write_config(
            '[[roles]]\nname = "PM"\ndefinition = "products"\nprofile = "nope.md"\n'
        )
        assert not runner.scoring_enabled()


class TestValidateSetup:
    """Test the fail-fast setup validation at pipeline start."""

    def _setup(self, tmp_path, monkeypatch, resume=True):
        """Point runner at tmp_path profiles, optionally creating resume.md."""
        monkeypatch.setattr(runner, "PROFILES_DIR", tmp_path)
        resume_file = tmp_path / "resume.md"
        if resume:
            resume_file.write_text("RESUME")
        monkeypatch.setattr(runner, "RESUME_FILE", resume_file)

    def test_ok_with_resume_and_roleless_profiles(self, tmp_path, monkeypatch):
        """Resume present + roles without profile files -> no exit."""
        self._setup(tmp_path, monkeypatch)
        _write_config('[[roles]]\nname = "PM"\ndefinition = "products"\n')
        runner.validate_setup()  # must not raise

    def test_missing_resume_exits(self, tmp_path, monkeypatch):
        """No resume.md -> the pipeline refuses to start."""
        self._setup(tmp_path, monkeypatch, resume=False)
        with pytest.raises(SystemExit, match="resume.md is required"):
            runner.validate_setup()

    def test_empty_roles_config_exits(self, tmp_path, monkeypatch):
        """A present-but-empty roles config aborts with the config error."""
        self._setup(tmp_path, monkeypatch)
        _write_config("# no roles\n")
        with pytest.raises(SystemExit, match="at least one role"):
            runner.validate_setup()

    def test_dangling_profile_reference_exits(self, tmp_path, monkeypatch):
        """A role pointing at a nonexistent profile file aborts the run."""
        self._setup(tmp_path, monkeypatch)
        _write_config(
            '[[roles]]\nname = "PM"\ndefinition = "products"\nprofile = "nope.md"\n'
        )
        with pytest.raises(SystemExit, match="nope.md"):
            runner.validate_setup()


class TestNormalizeRole:
    """Test _normalize_role mapping of model output onto configured names."""

    def test_exact_and_case_insensitive_match(self):
        """Configured names match canonically regardless of case."""
        assert _normalize_role("Manager") == "Manager"
        assert _normalize_role("ic") == "IC"

    def test_other_passes_through(self):
        """'Other' is always the canonical drop bucket."""
        assert _normalize_role("other") == "Other"

    def test_unknown_returns_none(self):
        """A hallucinated role name is dropped, not saved."""
        assert _normalize_role("Wizard") is None
        assert _normalize_role(None) is None
