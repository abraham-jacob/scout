"""Tests for the user config: profiles/config.toml parsing and validation
(roles, gmail, filters, scoring), color assignment, prompt injection, and
role normalization."""

from unittest.mock import Mock

import httpx
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
# whole file is validated on every load. Split so [llm]-focused tests can supply
# their own [llm] block (BOILERPLATE_NO_LLM) while everything else gets a valid
# default one (BOILERPLATE_SECTIONS).
BOILERPLATE_NO_LLM = """
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

BOILERPLATE_SECTIONS = BOILERPLATE_NO_LLM + """
[llm]
backend = "claude"
max_workers = 2
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


class TestLlmSection:
    """Test the required [llm] section and optional [llm.local] subsection."""

    def _local(self, extra_llm="", local_body=None):
        """Build a config: roles + non-llm boilerplate + a local [llm] block."""
        if local_body is None:
            local_body = ('[llm.local]\nbase_url = "http://box:11434/v1"\n'
                          'model = "gpt-oss:20b"\n')
        llm = f'[llm]\nbackend = "local"\nmax_workers = 2\n{extra_llm}\n{local_body}'
        _write_config(ROLES_ONLY + BOILERPLATE_NO_LLM + "\n" + llm,
                      boilerplate=False)

    def test_missing_llm_section_raises(self):
        """No [llm] section is now an error — the backend must be explicit."""
        _write_config(ROLES_ONLY + BOILERPLATE_NO_LLM, boilerplate=False)
        with pytest.raises(ValueError, match=r"\[llm\] section is required"):
            load_config()

    def test_missing_backend_raises(self):
        """[llm] without a backend key is an error, not a silent default."""
        _write_config(ROLES_ONLY + BOILERPLATE_NO_LLM + "\n[llm]\nmax_workers = 2\n",
                      boilerplate=False)
        with pytest.raises(ValueError, match="backend is required"):
            load_config()

    def test_explicit_claude_backend(self):
        """backend = "claude" with max_workers and no [llm.local] is fine."""
        _write_config(ROLES_ONLY + BOILERPLATE_NO_LLM
                      + '\n[llm]\nbackend = "claude"\nmax_workers = 4\n',
                      boilerplate=False)
        config = load_config()
        assert config.llm_backend == "claude"
        assert config.max_workers == 4
        assert config.local_base_url is None

    def test_missing_max_workers_raises(self):
        """[llm] without max_workers is an error — the width must be explicit."""
        _write_config(ROLES_ONLY + BOILERPLATE_NO_LLM
                      + '\n[llm]\nbackend = "claude"\n', boilerplate=False)
        with pytest.raises(ValueError, match="max_workers"):
            load_config()

    def test_nonpositive_max_workers_raises(self):
        """max_workers below 1 is a config error."""
        _write_config(ROLES_ONLY + BOILERPLATE_NO_LLM
                      + '\n[llm]\nbackend = "claude"\nmax_workers = 0\n',
                      boilerplate=False)
        with pytest.raises(ValueError, match="at least 1"):
            load_config()

    def test_noninteger_max_workers_raises(self):
        """A non-integer max_workers (e.g. a float) is a config error."""
        _write_config(ROLES_ONLY + BOILERPLATE_NO_LLM
                      + '\n[llm]\nbackend = "claude"\nmax_workers = 2.5\n',
                      boilerplate=False)
        with pytest.raises(ValueError, match="integer 'max_workers'"):
            load_config()

    def test_local_backend_parses(self):
        """A full [llm.local] section populates every local field."""
        self._local(local_body=('[llm.local]\nbase_url = "http://box:11434/v1"\n'
                                 'model = "gpt-oss:20b"\napi_key = "secret"\n'
                                 'timeout = 120\n'))
        config = load_config()
        assert config.llm_backend == "local"
        assert config.max_workers == 2
        assert config.local_base_url == "http://box:11434/v1"
        assert config.local_model == "gpt-oss:20b"
        assert config.local_api_key == "secret"
        assert config.local_timeout == 120.0

    def test_local_backend_optional_fields_default(self):
        """api_key defaults to None and timeout to DEFAULT_LOCAL_TIMEOUT."""
        from app.config import DEFAULT_LOCAL_TIMEOUT
        self._local()
        config = load_config()
        assert config.local_api_key is None
        assert config.local_timeout == DEFAULT_LOCAL_TIMEOUT

    def test_invalid_backend_raises(self):
        """An unknown backend name is a config error, not a silent default."""
        _write_config(ROLES_ONLY + BOILERPLATE_NO_LLM
                      + '\n[llm]\nbackend = "gpt4all"\nmax_workers = 2\n',
                      boilerplate=False)
        with pytest.raises(ValueError, match="must be one of"):
            load_config()

    def test_local_without_local_section_raises(self):
        """backend = "local" needs a [llm.local] section."""
        _write_config(ROLES_ONLY + BOILERPLATE_NO_LLM
                      + '\n[llm]\nbackend = "local"\nmax_workers = 2\n',
                      boilerplate=False)
        with pytest.raises(ValueError, match=r"\[llm\.local\]"):
            load_config()

    def test_local_missing_base_url_raises(self):
        """[llm.local] without base_url is an error."""
        self._local(local_body='[llm.local]\nmodel = "gpt-oss:20b"\n')
        with pytest.raises(ValueError, match="base_url"):
            load_config()

    def test_local_missing_model_raises(self):
        """[llm.local] without model is an error."""
        self._local(local_body='[llm.local]\nbase_url = "http://box:11434/v1"\n')
        with pytest.raises(ValueError, match="base_url.*model|model"):
            load_config()

    def test_local_nonpositive_timeout_raises(self):
        """A zero/negative timeout is a config error."""
        self._local(local_body=('[llm.local]\nbase_url = "http://box:11434/v1"\n'
                                 'model = "gpt-oss:20b"\ntimeout = 0\n'))
        with pytest.raises(ValueError, match="timeout"):
            load_config()

    def test_local_per_pass_params_default_empty(self):
        """Without [llm.local.clean]/[llm.local.enrich], both param dicts are {}."""
        self._local()
        config = load_config()
        assert config.local_clean_params == {}
        assert config.local_enrich_params == {}

    def test_local_per_pass_params_parse(self):
        """[llm.local.clean]/[llm.local.enrich] scalars land verbatim per pass."""
        self._local(local_body=(
            '[llm.local]\nbase_url = "http://box:11434/v1"\nmodel = "m"\n'
            '[llm.local.clean]\ntemperature = 0\nreasoning_effort = "low"\n'
            '[llm.local.enrich]\ntemperature = 0.3\nreasoning_effort = "high"\n'))
        config = load_config()
        assert config.local_clean_params == {"temperature": 0,
                                             "reasoning_effort": "low"}
        assert config.local_enrich_params == {"temperature": 0.3,
                                              "reasoning_effort": "high"}

    def test_local_per_pass_reserved_key_raises(self):
        """A param table may not set a field the request builder owns."""
        self._local(local_body=(
            '[llm.local]\nbase_url = "http://box:11434/v1"\nmodel = "m"\n'
            '[llm.local.enrich]\nmodel = "sneaky"\n'))
        with pytest.raises(ValueError, match="may not set 'model'"):
            load_config()

    def test_local_per_pass_nonscalar_raises(self):
        """A nested table/array as a param value is rejected."""
        self._local(local_body=(
            '[llm.local]\nbase_url = "http://box:11434/v1"\nmodel = "m"\n'
            '[llm.local.clean]\nstop = ["a", "b"]\n'))
        with pytest.raises(ValueError, match="must be a scalar"):
            load_config()


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
        """Point runner at tmp_path profiles, optionally creating resume.md.

        Also stubs claude_executable() so these tests exercise the
        resume/profile/local-backend checks in isolation, independent of
        whether the `claude` CLI happens to be installed on the machine
        running the suite (it won't be in CI).
        """
        monkeypatch.setattr(runner, "PROFILES_DIR", tmp_path)
        resume_file = tmp_path / "resume.md"
        if resume:
            resume_file.write_text("RESUME")
        monkeypatch.setattr(runner, "RESUME_FILE", resume_file)
        monkeypatch.setattr(runner, "claude_executable", lambda: "/usr/bin/claude")

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

    _LOCAL_CONFIG = """
[llm]
backend = "local"
max_workers = 2

[llm.local]
base_url = "http://box:11434/v1"
model = "gpt-oss:20b"
"""

    def test_local_backend_probes_reachable_server(self, tmp_path, monkeypatch):
        """A reachable server serving the model passes the startup probe."""
        self._setup(tmp_path, monkeypatch)
        _write_config('[[roles]]\nname = "PM"\ndefinition = "products"\n'
                      + BOILERPLATE_NO_LLM + self._LOCAL_CONFIG, boilerplate=False)
        probed = {}

        def _fake_get(url, **kwargs):
            probed["url"] = url
            return Mock(raise_for_status=lambda: None,
                        json=lambda: {"data": [{"id": "gpt-oss:20b"}]})

        monkeypatch.setattr(runner.httpx, "get", _fake_get)
        runner.validate_setup()  # must not raise
        assert probed["url"] == "http://box:11434/v1/models"

    def test_local_backend_unreachable_exits(self, tmp_path, monkeypatch):
        """An unreachable local server aborts the run before Pass 1."""
        self._setup(tmp_path, monkeypatch)
        _write_config('[[roles]]\nname = "PM"\ndefinition = "products"\n'
                      + BOILERPLATE_NO_LLM + self._LOCAL_CONFIG, boilerplate=False)

        def _boom(url, **kwargs):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(runner.httpx, "get", _boom)
        with pytest.raises(SystemExit, match="unreachable"):
            runner.validate_setup()

    def test_local_backend_missing_model_exits(self, tmp_path, monkeypatch):
        """A reachable server that doesn't serve the model aborts before Pass 1."""
        self._setup(tmp_path, monkeypatch)
        _write_config('[[roles]]\nname = "PM"\ndefinition = "products"\n'
                      + BOILERPLATE_NO_LLM + self._LOCAL_CONFIG, boilerplate=False)

        def _fake_get(url, **kwargs):
            return Mock(raise_for_status=lambda: None,
                        json=lambda: {"data": [{"id": "llama3:8b"}]})

        monkeypatch.setattr(runner.httpx, "get", _fake_get)
        with pytest.raises(SystemExit, match="does not serve a model with the exact id"):
            runner.validate_setup()

    def test_local_backend_bad_models_response_exits(self, tmp_path, monkeypatch):
        """A non-OpenAI /models response is a setup error, not a crash."""
        self._setup(tmp_path, monkeypatch)
        _write_config('[[roles]]\nname = "PM"\ndefinition = "products"\n'
                      + BOILERPLATE_NO_LLM + self._LOCAL_CONFIG, boilerplate=False)

        def _fake_get(url, **kwargs):
            return Mock(raise_for_status=lambda: None,
                        json=lambda: "not json")

        monkeypatch.setattr(runner.httpx, "get", _fake_get)
        with pytest.raises(SystemExit, match="unexpected /models response"):
            runner.validate_setup()

    def test_check_setup_raises_setup_error(self, tmp_path, monkeypatch):
        """check_setup raises SetupError (not SystemExit) for the UI to catch."""
        self._setup(tmp_path, monkeypatch)
        _write_config('[[roles]]\nname = "PM"\ndefinition = "products"\n'
                      + BOILERPLATE_NO_LLM + self._LOCAL_CONFIG, boilerplate=False)

        def _boom(url, **kwargs):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(runner.httpx, "get", _boom)
        with pytest.raises(runner.SetupError, match="unreachable"):
            runner.check_setup()

    def test_claude_backend_skips_probe(self, tmp_path, monkeypatch):
        """The Claude backend never touches the network at startup."""
        self._setup(tmp_path, monkeypatch)
        _write_config('[[roles]]\nname = "PM"\ndefinition = "products"\n')

        def _fail(*a, **k):
            raise AssertionError("httpx.get must not be called for claude backend")

        monkeypatch.setattr(runner.httpx, "get", _fail)
        runner.validate_setup()  # must not raise


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
