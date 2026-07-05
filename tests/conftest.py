"""Pytest configuration and fixtures."""

import sys
from pathlib import Path

import pytest

# Add project root to Python path so imports work
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


STANDARD_TEST_CONFIG = """\
[gmail]
label = "Job Alerts"

[filters]
exclude_companies = ["ExcludedCorp"]

[scoring]
fit_weight = 0.85
criteria_weight = 0.15
dealbreaker_cap = 30.0

[logging]
dir = "logs"

[[roles]]
name = "Manager"
definition = "the core of the job is leading or managing an engineering team"
profile = "profile_manager.md"

[[roles]]
name = "IC"
definition = "a senior hands-on individual-contributor engineering role"
profile = "profile_ic.md"
"""


@pytest.fixture(autouse=True)
def isolated_roles_config(tmp_path, monkeypatch):
    """Point app.config at a per-test config file so tests never read the
    user's real profiles/config.toml.

    Pre-populated with a full valid config (config.toml is required — there
    are no in-code defaults) matching the historical hardcoded behavior:
    Manager/IC roles, ExcludedCorp excluded, 0.85/0.15 weights, cap 30. Tests
    that need a different config overwrite or unlink app.config.CONFIG_FILE.
    """
    import app.config as app_config
    config = tmp_path / "config.toml"
    config.write_text(STANDARD_TEST_CONFIG)
    monkeypatch.setattr(app_config, "CONFIG_FILE", config)
