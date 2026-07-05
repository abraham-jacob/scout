"""User configuration loaded from profiles/config.toml.

Five required sections plus one optional section (profiles/README.md has a
copyable example):

- ``[[roles]]`` — the role types the enrichment pass classifies each job
  into. At least one is required: a job search needs at least one target
  role — with zero roles the keep/drop decision is undefined. Drives the
  prompt's role definitions, per-role scoring profiles, and the UI filter
  buttons and chip colors.
- ``[gmail]`` — ``label``: the Gmail label the job-alert emails live under.
- ``[filters]`` — ``exclude_companies``: companies dropped before any LLM
  call (and again at save time). May be empty.
- ``[scoring]`` — ``fit_weight``/``criteria_weight`` (must sum to 1) and
  ``dealbreaker_cap`` (0–100) for the final match-score derivation.
- ``[logging]`` — ``dir``: the directory the daily application log (and the
  opt-in model-interaction log) are written to; relative paths are resolved
  against the project root.
- ``[scrape]`` (optional) — ``download_dir``: where the browser saves the
  scrape blob. Defaults to the OS Downloads folder (``~/Downloads``, correct
  on Windows/macOS/Linux); override it only if you've changed Chrome's
  download location. Unlike the sections above it has a sensible cross-platform
  default because it's an environment path, not a behavior knob.

Missing required sections or malformed values raise ValueError rather than
falling back to hidden defaults, so a typo can't silently change behavior.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
PROFILES_DIR = BASE_DIR / "profiles"
CONFIG_FILE = PROFILES_DIR / "config.toml"

# Chrome's default download folder on every supported OS. Used when the
# optional [scrape] download_dir isn't set (see the module docstring).
DEFAULT_DOWNLOAD_DIR = "~/Downloads"


@dataclass(frozen=True)
class Role:
    """One role type the user is hunting for.

    ``name`` is the canonical label stored in jobs.role_type and shown in the
    UI. ``definition`` is the classification guidance injected into the
    enrichment prompt. ``profile`` is an optional markdown filename inside
    profiles/ that jobs of this role are scored against; without one, scoring
    falls back to the resume alone.
    """

    name: str
    definition: str
    profile: str | None = None


# Chip color classes assigned to roles by config order (wraps past the end).
# The first two entries keep the colors Manager and IC have always had.
ROLE_COLOR_PALETTE = [
    "bg-violet-100 text-violet-700 dark:bg-violet-500/15 dark:text-violet-300 dark:ring-1 dark:ring-inset dark:ring-violet-400/30",
    "bg-teal-100 text-teal-700 dark:bg-teal-500/15 dark:text-teal-300 dark:ring-1 dark:ring-inset dark:ring-teal-400/30",
    "bg-sky-100 text-sky-700 dark:bg-sky-500/15 dark:text-sky-300 dark:ring-1 dark:ring-inset dark:ring-sky-400/30",
    "bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-300 dark:ring-1 dark:ring-inset dark:ring-amber-400/30",
    "bg-rose-100 text-rose-700 dark:bg-rose-500/15 dark:text-rose-300 dark:ring-1 dark:ring-inset dark:ring-rose-400/30",
    "bg-lime-100 text-lime-700 dark:bg-lime-500/15 dark:text-lime-300 dark:ring-1 dark:ring-inset dark:ring-lime-400/30",
]


@dataclass(frozen=True)
class Config:
    """The full validated user config (see the module docstring)."""

    roles: list[Role]
    gmail_label: str
    exclude_companies: list[str]
    fit_weight: float
    criteria_weight: float
    dealbreaker_cap: float
    log_dir: str
    download_dir: str


def _parse_roles(data: dict) -> list[Role]:
    """Parse and validate the [[roles]] entries out of the raw TOML data."""
    entries = data.get("roles", [])
    if not entries:
        raise ValueError(
            "profiles/config.toml defines no [[roles]] — at least one role "
            "type is required. See profiles/README.md for an example."
        )

    roles: list[Role] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries, 1):
        name = str(entry.get("name") or "").strip()
        definition = str(entry.get("definition") or "").strip()
        if not name or not definition:
            raise ValueError(
                f"profiles/config.toml: [[roles]] entry {i} needs a non-empty "
                f"'name' and 'definition'"
            )
        if name.lower() == "other":
            raise ValueError(
                "profiles/config.toml: the role name 'Other' is reserved for "
                "jobs that match no configured role"
            )
        if name.lower() in seen:
            raise ValueError(f"profiles/config.toml: duplicate role name '{name}'")
        seen.add(name.lower())
        profile = entry.get("profile")
        roles.append(Role(name, definition, str(profile) if profile else None))
    return roles


def _require_number(section: dict, section_name: str, key: str) -> float:
    """Return section[key] as a float, or raise a ValueError naming it."""
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"profiles/config.toml: [{section_name}] needs a numeric '{key}'"
        )
    return float(value)


def load_config() -> Config:
    """Load and validate the full profiles/config.toml.

    Raises ValueError when the file is missing, a section/key is missing, or
    a value is malformed. Failing loudly is deliberate: there are no hidden
    defaults to fall back to.
    """
    if not CONFIG_FILE.exists():
        raise ValueError(
            "profiles/config.toml not found — it is required. "
            "See profiles/README.md for an example."
        )
    data = tomllib.loads(CONFIG_FILE.read_text())

    roles = _parse_roles(data)

    gmail = data.get("gmail")
    if not isinstance(gmail, dict) or not str(gmail.get("label") or "").strip():
        raise ValueError(
            "profiles/config.toml: a [gmail] section with a non-empty 'label' "
            "is required"
        )
    gmail_label = str(gmail["label"]).strip()

    filters = data.get("filters")
    if not isinstance(filters, dict) or "exclude_companies" not in filters:
        raise ValueError(
            "profiles/config.toml: a [filters] section with 'exclude_companies' "
            "is required (an empty list is fine)"
        )
    raw_excludes = filters["exclude_companies"]
    if not isinstance(raw_excludes, list) or any(
        not isinstance(c, str) or not c.strip() for c in raw_excludes
    ):
        raise ValueError(
            "profiles/config.toml: [filters] exclude_companies must be a list "
            "of non-empty company names"
        )
    exclude_companies = [c.strip() for c in raw_excludes]

    scoring = data.get("scoring")
    if not isinstance(scoring, dict):
        raise ValueError(
            "profiles/config.toml: a [scoring] section with fit_weight, "
            "criteria_weight, and dealbreaker_cap is required"
        )
    fit_weight = _require_number(scoring, "scoring", "fit_weight")
    criteria_weight = _require_number(scoring, "scoring", "criteria_weight")
    dealbreaker_cap = _require_number(scoring, "scoring", "dealbreaker_cap")
    if abs(fit_weight + criteria_weight - 1.0) > 1e-6:
        raise ValueError(
            "profiles/config.toml: [scoring] fit_weight + criteria_weight "
            f"must sum to 1 (got {fit_weight} + {criteria_weight})"
        )
    if not 0 <= dealbreaker_cap <= 100:
        raise ValueError(
            "profiles/config.toml: [scoring] dealbreaker_cap must be between "
            "0 and 100"
        )

    logging_section = data.get("logging")
    if not isinstance(logging_section, dict) or not str(
        logging_section.get("dir") or ""
    ).strip():
        raise ValueError(
            "profiles/config.toml: a [logging] section with a non-empty 'dir' "
            "is required"
        )
    log_dir = str(logging_section["dir"]).strip()

    # [scrape] is optional; download_dir defaults to the OS Downloads folder.
    # If the section/key is present it must be a non-empty path (a blank value
    # is a typo, not an intent to fall back).
    download_dir = DEFAULT_DOWNLOAD_DIR
    scrape = data.get("scrape")
    if isinstance(scrape, dict) and "download_dir" in scrape:
        raw_download = str(scrape.get("download_dir") or "").strip()
        if not raw_download:
            raise ValueError(
                "profiles/config.toml: [scrape] download_dir must be a "
                "non-empty path when set (omit it to use ~/Downloads)"
            )
        download_dir = raw_download

    return Config(
        roles=roles,
        gmail_label=gmail_label,
        exclude_companies=exclude_companies,
        fit_weight=fit_weight,
        criteria_weight=criteria_weight,
        dealbreaker_cap=dealbreaker_cap,
        log_dir=log_dir,
        download_dir=download_dir,
    )


def load_roles() -> list[Role]:
    """Return the configured role types (the [[roles]] part of load_config)."""
    return load_config().roles


def role_color_map(roles: list[Role]) -> dict[str, str]:
    """Map each role name to its chip color classes, by config order."""
    return {
        role.name: ROLE_COLOR_PALETTE[i % len(ROLE_COLOR_PALETTE)]
        for i, role in enumerate(roles)
    }
