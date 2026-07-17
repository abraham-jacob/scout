# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Scout is

Scout scrapes LinkedIn job-alert emails, extracts the jobs behind them, classifies
and summarizes each one with Claude, and stores the survivors in a local DuckDB
database that a small FastAPI + HTMX web UI browses. It is a single-user local app
(the run state lives in memory, not the DB) for one job seeker.

## Commands

Dependencies are managed with **pipenv** (Python 3.12). Prefix runtime commands with
`pipenv run`.

```bash
pipenv install --dev                       # install deps

# Web UI (FastAPI). Serves the job list and the "Run Scout" button.
pipenv run uvicorn app.main:app --reload

# Run the agent pipeline directly (the web UI shells out to this same command):
pipenv run python -m agent.runner                  # pull URLs from Gmail
pipenv run python -m agent.runner --url <linkedin_url>   # scrape one URL, skip Gmail
pipenv run python -m agent.runner --max-emails 5

# Initialise / inspect the DuckDB schema
pipenv run python -m app.database

# Tests (pytest, config in pytest.ini — testpaths=tests, asyncio_mode=auto)
pipenv run pytest                          # all tests
pipenv run pytest tests/test_agent_runner.py           # one file
pipenv run pytest tests/test_agent_runner.py::TestName::test_case   # one test
pipenv run pytest -m unit                  # by marker (unit / integration)
pipenv run unit-tests                      # full suite with junit + HTML coverage
```

## Architecture

The system is **three passes orchestrated by `agent/runner.py`** — a browser
scrape (Pass 1) and two headless passes, description cleaning (Pass 2) and
per-job enrichment/scoring (Pass 3) — launched as a subprocess by the web UI.
Read `agent/runner.py`'s module docstring first — it is the map for the whole
pipeline, and its Pass 1/2/3 numbering is authoritative.

### Pass 1 — browser scrape (Haiku), `agent/system_prompt.md`
`runner.py` spawns `claude --print --chrome` on Haiku with `system_prompt.md`. That
sub-agent does **no filtering**: it hits LinkedIn's internal **Voyager job-postings
API** via `javascript_tool` (not the accessibility tree, not card-clicking) to pull
every field for every job on page 1, including virtualized cards that never render.

The critical constraint: each job description is 5–13 KB, and the Chrome extension's
**privacy filter blocks large `javascript_tool` return values**. So the sub-agent
writes the whole batch to `window.__jobs` and blob-**downloads** it as
`scout_<run_id>.json` to the browser's Downloads folder. Only a one-line status
comes back through the extension. `runner.py::load_downloaded_jobs` then polls the
Downloads folder (`download_dir()`, config-overridable) for that file, reads it, and
deletes it. The blob download is load-bearing; do not try to route descriptions back
through the tool return value. There is deliberately **no shell step** — the sub-agent
does not move the file — so the handoff works identically on Windows/macOS/Linux
(the poll replaces the wait-loop the agent used to run in bash).

### Between passes — deterministic filters
`apply_deterministic_filters()` cheaply drops jobs before spending any LLM call:
scrape errors, jobs already in the DB, already-applied, closed (`jobState != LISTED`),
jobs with no company name, and companies in the config's `[filters]
exclude_companies` (also enforced again in `save_jobs`).

### Pass 2 — description cleaning (Haiku, parallel), `agent/clean_prompt.md`
For each survivor, one headless call (`run_headless("clean", …)`) strips non-role
boilerplate from the raw description — EEO/DEI statements, legal disclaimers,
generic culture/benefits marketing, "About [Company]" fluff — and returns a single
`{"description_clean": "..."}` JSON field. Runs `MAX_WORKERS`-wide via a
`ThreadPoolExecutor`. A failed call falls back to the raw description so Pass 3
always has something to work with. On the Claude backend this is Haiku
(`CLEAN_MODEL`); on the local backend it's the configured `[llm.local] model`.

### Pass 3 — per-job enrichment (Sonnet, parallel), `agent/enrichment_prompt.md`
For each survivor, one headless `claude --print` Sonnet call classifies the role into
one of the **user-configured role types** (or `Other`), writes a 2–4 sentence
summary, tags the job, and scores it against the candidate's resume/profiles/criteria.
`enrichment_prompt.md` is a single file covering classification, summary, tags, and
scoring instructions. Runs `MAX_WORKERS`-wide via a `ThreadPoolExecutor`. Jobs classified `Other`
(or that fail to enrich) are dropped; the rest are saved via `agent/tools.py::save_jobs`,
which also does repost detection and unwraps LinkedIn safety-redirect apply URLs.

All user configuration lives in `profiles/config.toml` (loaded and validated by
`app/config.py::load_config`). The file is **required**, with six required
sections and no in-code defaults: `[[roles]]` (≥1 role type), `[gmail]` (label),
`[filters]` (exclude_companies, may be empty), `[scoring]` (fit/criteria
weights summing to 1, plus dealbreaker_cap used by `compute_match_score`),
`[logging]` (dir for the daily app log and the opt-in model-call log; see
`app/logging_setup.py`), and `[llm]` (`backend` + `max_workers`, below). One
optional section, `[scrape]`, carries `download_dir` — where the browser saves
the scrape blob; it defaults to `~/Downloads` (correct on Windows/macOS/Linux)
and `runner.download_dir()` expands it, so it's the only config path with a
cross-platform default rather than failing loudly. `[llm]` carries `backend`
(required, `"claude"` or `"local"` — no default, so the config always states
which one) which selects the backend for the two **headless** passes —
description cleaning and enrichment/scoring — via `runner.run_headless()`, and
`max_workers` (required, the Pass 2/3 pool width, tuned per backend). `"local"`
routes both passes (together, never split) to a local OpenAI-compatible server
(e.g. Ollama) configured under `[llm.local]` (`base_url`, `model`, optional
`api_key`/`timeout`). Two optional per-pass sub-tables, `[llm.local.clean]` and
`[llm.local.enrich]`, carry request parameters (e.g. `temperature`,
`reasoning_effort`) merged verbatim into that pass's chat-completion JSON by
`runner._run_local_llm`; values must be scalars and may not set the
pipeline-owned `model`/`messages`/`stream` keys (validated in
`config._parse_local_params`). The browser scrape always runs on Claude.
Each role carries the classification definition injected into the prompt's
`{{ROLE_DEFINITIONS}}`/`{{ROLE_ENUM}}` placeholders, an optional per-role profile
file for scoring, and drives the UI filter buttons and chip colors. `jobs.role_type`
stores the role's `name` verbatim. `runner.validate_setup()` fails fast at pipeline
start: the roles config must load, `profiles/resume.md` must exist, and a role's
referenced profile file must exist (roles may omit `profile` to score on the resume
alone).

### Progress events → web UI
`runner.py` emits `SCOUT_PROGRESS <json>` sentinel lines on stdout. `app/main.py`
reads the subprocess stdout line by line and folds those events into the in-memory
`_run` dict (`_apply_event`), which renders the live "run drawer" partial that HTMX
polls at `GET /scout/status`. Event `key`s in `runner.py`'s `emit()` calls must stay
in sync with `GLOBAL_STEPS` / `EMAIL_STEPS` in `app/main.py`.

### Data layer
`app/database.py` — DuckDB at `data/scout.duckdb`, two tables (`scrape_runs`, `jobs`).
`role_type` is per-job (derived from the title/description at enrichment), not per-run.
`app/gmail.py` — OAuth via `credentials.json` → `token.json`; pulls unread emails
under the configured Gmail label (`[gmail] label` in `profiles/config.toml`) and
extracts the "See all jobs" URL.

### `agent/tools.py` dual role
It holds both the plain Python DB helpers `runner.py` calls directly AND
`TOOL_DEFINITIONS` / `dispatch_tool` (Anthropic tool schemas for an agent to call
`save_jobs` mid-run). The current pipeline calls the Python helpers directly; the
tool-definition path is available but not on the main flow.

## Conventions

- **Never work on the `main` branch directly.** Always create a feature branch
  (`git checkout -b <branch-name>`) before making changes. PRs merge into `main`.
  `main` is branch-protected on GitHub (PR required, force-push and deletion
  blocked), so a direct push would be rejected anyway.
- **CI runs on every push and PR** via [`.github/workflows/tests.yml`](.github/workflows/tests.yml)
  (`pipenv run unit-tests` — tests + branch coverage). Run it locally before
  opening a PR rather than relying on CI to catch failures. On push to `main`
  it also regenerates the coverage badge onto the unprotected `badges` branch.
- **Every Python function must have a docstring** — this is a hard project rule; the
  codebase follows it uniformly.
- Claude model IDs are pinned as constants in `runner.py` (`SCRAPER_MODEL` and
  `CLEAN_MODEL` = Haiku, `ENRICH_MODEL` = Sonnet); the local-backend model comes
  from `[llm.local] model` in the config instead. Each `claude` subprocess has a
  `SUBPROCESS_TIMEOUT_S` wall-clock kill (local calls use `[llm.local] timeout`);
  the web UI adds a 30-minute overall guardrail.
- Tests add the project root to `sys.path` via `tests/conftest.py`; import as
  `from app...` / `from agent...`.

## LinkedIn scraping notes (in `system_prompt.md`)

Two page structures exist and the Step-1 JS handles both: `/search-results/` uses the
`componentkey` attribute; `/search/` (and `/comm/jobs/search` redirects) uses
`data-occludable-job-id`. The Voyager API is the sole data source — salary is not in
the API and is regex-parsed out of the description text.
