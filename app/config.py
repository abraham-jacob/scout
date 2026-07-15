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
- ``[llm]`` (required) — ``backend``: which model backend runs the two headless
  passes, description cleaning (Pass 2) and enrichment/scoring (Pass 3). Either
  ``"claude"`` (the pipeline's original all-Claude behavior) or ``"local"`` to
  route both passes to a local OpenAI-compatible server (e.g. Ollama); there is
  no default, so the config always states which one is in use. ``max_workers``
  (also required): the width of the Pass 2/3 worker pool, a positive integer —
  tune it to the active backend (a Claude run can go wide; a memory-constrained
  local box may need 1). Pass 1 (the browser scrape) always runs on Claude. When
  ``backend`` is ``"local"``, a ``[llm.local]`` subsection must supply
  ``base_url`` (the server's OpenAI-compatible endpoint) and ``model``, and may
  supply an optional ``api_key`` and ``timeout`` (seconds). Two further optional
  sub-tables, ``[llm.local.clean]`` and ``[llm.local.enrich]``, carry per-pass
  request parameters (e.g. ``temperature``, ``reasoning_effort``) merged verbatim
  into the chat-completion JSON for that pass — see ``_parse_local_params``.

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

# LLM backend for the two headless passes (clean + enrich). "claude" runs them
# on the Claude CLI; "local" routes them to an OpenAI-compatible server. The
# [llm] section and its `backend` are required — there is no default backend, so
# the config always states which one is in use rather than leaving it implicit.
VALID_LLM_BACKENDS = ("claude", "local")
# Keys the local-backend request builder owns; a user's per-pass param table
# (see _parse_local_params) may not set these, so a config typo can't clobber
# the messages/model/stream the pipeline controls. Everything else is fair game:
# the code no longer forces a temperature (the server default applies unless the
# table sets one), and only response_format defaults to JSON mode, which a table
# entry may still override.
RESERVED_LOCAL_PARAM_KEYS = ("model", "messages", "stream")
# Read timeout (seconds) for a local-LLM clean/enrich call when [llm.local]
# omits `timeout`. Deliberately tight: a warm local model answers a clean/enrich
# call in well under a minute, so a call still running past this is a stall, not
# slow progress — better to time out and let the one-shot retry try a fresh call
# than to block the whole (sequential, at max_workers=1) pass on a wedged one.
# The one call that legitimately needs to wait minutes — the cold model load —
# is the run-start warm-up, which uses its own generous timeout (runner.py), not
# this one.
DEFAULT_LOCAL_TIMEOUT = 60.0


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
    llm_backend: str
    max_workers: int
    local_base_url: str | None
    local_model: str | None
    local_api_key: str | None
    local_timeout: float
    local_clean_params: dict
    local_enrich_params: dict


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


def _require_positive_int(section: dict, section_name: str, key: str) -> int:
    """Return section[key] as an int ≥ 1, or raise a ValueError naming it."""
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"profiles/config.toml: [{section_name}] needs an integer '{key}'"
        )
    if value < 1:
        raise ValueError(
            f"profiles/config.toml: [{section_name}] '{key}' must be at least 1"
        )
    return value


def _parse_local_params(local: dict, pass_name: str) -> dict:
    """Return the [llm.local.<pass_name>] per-pass request params, or {} if unset.

    These optional sub-tables (pass_name is "clean" or "enrich") carry parameters
    merged verbatim into the OpenAI-compatible chat-completion payload for that
    pass — e.g. temperature or GPT-OSS's reasoning_effort. Values must be scalars
    (str/int/float/bool); nested tables/arrays are rejected as they don't belong
    in the request body and are almost always a mistake. Keys the request builder
    owns (RESERVED_LOCAL_PARAM_KEYS) are rejected so a typo can't clobber them.
    Param values themselves are NOT range-checked — they're model-specific, so an
    invalid one is left for the server to reject rather than hardcoded here.
    """
    section = local.get(pass_name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError(
            f"profiles/config.toml: [llm.local.{pass_name}] must be a table of "
            "request parameters (e.g. temperature, reasoning_effort)"
        )
    for key, value in section.items():
        if key in RESERVED_LOCAL_PARAM_KEYS:
            raise ValueError(
                f"profiles/config.toml: [llm.local.{pass_name}] may not set "
                f"'{key}' — that field is controlled by the pipeline"
            )
        if isinstance(value, bool):
            continue
        if not isinstance(value, (str, int, float)):
            raise ValueError(
                f"profiles/config.toml: [llm.local.{pass_name}] '{key}' must be a "
                "scalar (string, number, or boolean), not a table or array"
            )
    return dict(section)


def _parse_llm(
    data: dict,
) -> tuple[str, int, str | None, str | None, str | None, float, dict, dict]:
    """Parse and validate the required [llm] / optional [llm.local] sections.

    Returns (backend, max_workers, base_url, model, api_key, timeout,
    clean_params, enrich_params). The [llm] section is required and must state
    `backend` ("claude" or "local") and `max_workers` (the Pass 2/3 pool width, a
    positive int) explicitly — there are no hidden defaults, so the config always
    says which backend runs and how wide. When backend is "local", [llm.local]
    must supply non-empty base_url and model; api_key and timeout are optional, as
    are the [llm.local.clean] / [llm.local.enrich] per-pass param tables.
    Malformed values raise ValueError, matching the rest of the loader.
    """
    llm = data.get("llm")
    if not isinstance(llm, dict):
        raise ValueError(
            'profiles/config.toml: [llm] section is required — it must set '
            '`backend` ("claude" or "local") and `max_workers`. See '
            "profiles/README.md."
        )

    backend = str(llm.get("backend") or "").strip()
    if backend not in VALID_LLM_BACKENDS:
        raise ValueError(
            "profiles/config.toml: [llm] backend is required and must be one of "
            f"{', '.join(VALID_LLM_BACKENDS)} (got {backend!r})"
        )

    max_workers = _require_positive_int(llm, "llm", "max_workers")

    if backend != "local":
        return (backend, max_workers, None, None, None,
                DEFAULT_LOCAL_TIMEOUT, {}, {})

    local = llm.get("local")
    if not isinstance(local, dict):
        raise ValueError(
            "profiles/config.toml: [llm] backend = \"local\" requires a "
            "[llm.local] section with base_url and model"
        )
    base_url = str(local.get("base_url") or "").strip()
    model = str(local.get("model") or "").strip()
    if not base_url or not model:
        raise ValueError(
            "profiles/config.toml: [llm.local] needs a non-empty 'base_url' "
            "(the server's OpenAI-compatible endpoint) and 'model'"
        )
    api_key_raw = local.get("api_key")
    api_key = str(api_key_raw).strip() if api_key_raw else None
    timeout = DEFAULT_LOCAL_TIMEOUT
    if "timeout" in local:
        timeout = _require_number(local, "llm.local", "timeout")
        if timeout <= 0:
            raise ValueError(
                "profiles/config.toml: [llm.local] timeout must be a positive "
                "number of seconds"
            )
    clean_params = _parse_local_params(local, "clean")
    enrich_params = _parse_local_params(local, "enrich")
    return (backend, max_workers, base_url, model, api_key, timeout,
            clean_params, enrich_params)


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

    (llm_backend, max_workers, local_base_url, local_model, local_api_key,
     local_timeout, local_clean_params, local_enrich_params) = _parse_llm(data)

    return Config(
        roles=roles,
        gmail_label=gmail_label,
        exclude_companies=exclude_companies,
        fit_weight=fit_weight,
        criteria_weight=criteria_weight,
        dealbreaker_cap=dealbreaker_cap,
        log_dir=log_dir,
        download_dir=download_dir,
        llm_backend=llm_backend,
        max_workers=max_workers,
        local_base_url=local_base_url,
        local_model=local_model,
        local_api_key=local_api_key,
        local_timeout=local_timeout,
        local_clean_params=local_clean_params,
        local_enrich_params=local_enrich_params,
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
