# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What Scout is

Scout scrapes the LinkedIn saved-search URLs configured under
`[[linkedin_searches]]`, extracts the jobs behind them, classifies and summarizes
each one with Claude, and stores the survivors in a local DuckDB database that a
small FastAPI + HTMX web UI browses. It is a single-user local app (the run state
lives in memory, not the DB) for one job seeker. Scout is open source under MIT.

Scraping (Pass 1) is triggered from the **Scout browser extension** (`extension/`),
not the web UI — the web UI is purely for browsing/filtering saved jobs now. See
"Pass 1" below.

## Commands

Dependencies are managed with **pipenv** (Python 3.12). Prefix runtime commands with
`pipenv run`.

```bash
pipenv install --dev                       # install deps

# Web UI (FastAPI). Serves the job list; scraping is triggered from the
# extension popup, not this UI (see "Pass 1" below).
pipenv run uvicorn app.main:app --reload

# Run the agent pipeline directly. --url uses the old CDP browser-scrape path
# (kept as a fallback); --ingest-file is what the extension's ingest endpoint
# spawns behind the scenes, not something you'd normally invoke by hand.
pipenv run python -m agent.runner                  # scrape every configured [[linkedin_searches]] entry (CDP path)
pipenv run python -m agent.runner --url <linkedin_url>   # scrape one ad-hoc URL, ignoring config (CDP path)

# Initialise / inspect the DuckDB schema
pipenv run python -m app.database

# Tests (pytest, config in pytest.ini — testpaths=tests, asyncio_mode=auto)
pipenv run pytest                          # all tests
pipenv run pytest tests/test_agent_runner.py           # one file
pipenv run pytest tests/test_agent_runner.py::TestName::test_case   # one test
pipenv run pytest -m unit                  # by marker (unit / integration)
pipenv run unit-tests                      # full suite with junit + HTML coverage
```

**Loading the extension:** `chrome://extensions` → enable Developer mode →
**Load unpacked** → select `extension/`. Requires the web server running
(above) — the extension talks to it over `http://127.0.0.1:8000`.

## Architecture

The system is **three passes orchestrated by `agent/runner.py`** — a browser
scrape (Pass 1) and two headless passes, description cleaning (Pass 2) and
per-job enrichment/scoring (Pass 3). Passes 2/3 always run as a
`python -m agent.runner` subprocess spawned by the web UI
(`app/main.py::_start_run_background`); Pass 1 is acquired by the **Scout
browser extension** (default) or the older CDP-driven path (fallback, kept
until the extension is proven in longer real-world use). Read
`agent/runner.py`'s module docstring first — it is the map for the whole
pipeline, and its Pass 1/2/3 numbering is authoritative.

### Pass 1 — browser scrape

**Default: the Scout browser extension (`extension/`).** Its content script
(`extension/content.js`) runs inside the user's own authenticated LinkedIn
tab — no CDP protocol traffic, no `navigator.webdriver` flag, and Voyager API
calls are same-origin with real session cookies, mechanically identical to
what LinkedIn's own SPA does when a human browses. This replaced the CDP path
below because CDP automation kept triggering LinkedIn's "verify you're
human" challenge.

- **Harvest**: `content.js` first waits for the results to render (a
  `MutationObserver` quiet-period, not a fixed sleep — LinkedIn renders
  progressively), then pulls every job id off the page via
  `harvestJobIdsUntilStable()`. Two DOM variants, handled by one combined
  selector (see "LinkedIn scraping notes" below) — but the `/jobs/search/`
  variant virtualizes the results list, mounting only ~7 cards up front and
  the rest in further bursts with gaps that can exceed the quiet-period
  window, so a single quiet-period wait alone was found to settle after just
  the first burst (a real bug: runs silently capped at ~7 jobs). The fix
  layers a poll loop on top — re-harvesting every 700ms (nudging scroll each
  round, in case mounting is scroll-triggered) until the id count holds
  steady for two consecutive checks or 20s elapses.
- **Dedupe**: ids are POSTed to `POST /api/extension/dedupe`
  (`app/main.py`), which diffs against `agent.tools.get_existing_job_ids()`
  and returns only the unseen ones — this is the biggest lever on request
  volume.
- **Fetch**: unlike the old CDP prompt's `Promise.all()` burst, the
  extension fetches new jobs **sequentially with random jitter**
  (`[extension] min_delay_ms`/`max_delay_ms` in `profiles/config.toml`,
  served by `GET /api/extension/searches`) — a burst of parallel requests
  doesn't look like a human clicking through jobs. Any `999`/`401`/`403`
  response (LinkedIn's block/auth-loss signals) **halts the whole run
  immediately**, no retry-with-backoff — retrying into a block only
  escalates throttling into flagging. A plain `404` is a normal per-job skip
  (job removed between page-load and fetch); other failures get one retry
  before being left as an error entry. On halt, whatever was already fetched
  is still ingested — partial progress is never thrown away — and the
  remaining pending ids are persisted to `chrome.storage.local` for the
  popup's Resume button.
- **Ingest**: `POST /api/extension/ingest` (`app/main.py`) writes the
  `{job_id: {...}}` batch to a temp file and spawns
  `python -m agent.runner --ingest-file <path> --run-id <uuid> --search-name ... --url ...`
  — the *same* subprocess/stdout-parsing mechanism
  `_start_run_background` already uses, so extension-triggered progress
  flows through the existing `SCOUT_PROGRESS`/`_run` state for free. In
  `runner.py`, `--ingest-file` routes to `process_ingested_jobs()`, which is
  `process_url()` minus the scrape: deterministic filters → clean → enrich →
  save, via the same `_process_scraped_jobs()`/`_save_scraped_jobs()` tail
  the CDP path uses, so extension-sourced and CDP-sourced jobs are processed
  identically.
- **Popup** (`extension/popup.html`/`.js`/`.css`) is the trigger/monitor UI:
  Saved Searches (from `GET /api/extension/searches`), a Reload button next
  to it (`POST /api/extension/reload-config`), Custom Search (a pasted URL
  or job id — classified client-side in `popup.js`, ported from
  `extract_single_job_id`/`resolve_scan_url` below, since this is
  scrape-routing logic that doesn't need a backend round-trip), Scrape
  Current Page, and an Abort button next to the Run Log. Progress during the
  harvest is live-only (`chrome.runtime.sendMessage` broadcasts from
  `content.js`, not persisted); once ingest fires, the popup switches to
  polling `GET /api/extension/status` and streams the backend's own
  `_run["log"]` lines, which *do* survive a popup close/reopen.
  `background.js` is a thin relay only (talks to `localhost` and manages tab
  creation for saved-search/resume runs) — the harvest loop itself must live
  in the content script, not the background service worker, since MV3
  workers are killed after ~30s idle and a multi-minute jittered loop needs
  a tab-lifetime context.
- **Reload**: `app/config.py::load_config()` is `@lru_cache`d for the
  process lifetime, so the popup's saved-searches list otherwise never picks
  up an edited `profiles/config.toml` without a full server restart.
  `POST /api/extension/reload-config` clears that cache and re-reads the
  file; on a parse/validation failure it returns `{"error": ...}` (400) and
  the popup deliberately keeps showing its last-known list rather than
  clearing it.
- **Abort**: stops the current run regardless of which pass it's in, via two
  independent signals fired together rather than tracking precisely which
  pass is live — a `SCOUT_ABORT` message to whichever tab is harvesting
  (checked at every natural checkpoint in `content.js`, including inside the
  Voyager fetch loop) and `POST /api/extension/kill` (mirrors the existing
  overall-timeout watchdog's `proc.kill()` in `_start_run_background`, just
  triggered by an explicit action instead of a timer). On abort mid-Pass-1,
  whatever was already fetched is **discarded, not ingested** — ingesting it
  would spawn a *new* Pass 2/3 subprocess after the kill signal already
  fired (and found nothing to kill), leaving the run looking stuck long
  after it visibly stopped. The popup re-enables every control immediately
  on click rather than waiting for the harvesting tab's own confirmation to
  round-trip back (that can lag by up to one fetch-jitter cycle).
- **Halt/resume state**: a single overwritable `chrome.storage.local` slot
  (`scout_run_state`) holds `{status: "running", tabId, ...}` (written the
  instant any harvest starts — this is what stops a reopened popup from
  looking idle and re-enabling every button mid-scrape, and `tabId` is what
  lets Abort message the right tab even after a popup close/reopen) or
  `{status: "halted", ...}`. Starting a fresh Run always overwrites it;
  Resume reads/updates it in place; any clean finish, abort, or unexpected
  error clears it.

**Fallback: CDP-driven scrape (Haiku), `agent/scrape_prompt.md` /
`agent/scrape_single_prompt.md`.** `runner.py` spawns `claude --print --chrome`
on Haiku with `scrape_prompt.md`. That sub-agent does **no filtering**: it
hits the same Voyager API via `javascript_tool` to pull every field for
every job on page 1, including virtualized cards that never render. A
single LinkedIn job URL (a `/jobs/view/<id>` link, or a search URL's
`currentJobId` toggled to "just this job") is detected by
`runner.extract_single_job_id()` and routed to `scrape_single_prompt.md`
instead. Not surfaced in the web UI anymore (no button calls
`POST /scout/run`), but left callable directly (`--url`, or no args to loop
`[[linkedin_searches]]`) — don't delete this path.

The critical constraint on this path: each job description is 5–13 KB, and the
Chrome extension's **privacy filter blocks large `javascript_tool` return
values**. So the sub-agent writes the whole batch to `window.__jobs` and
blob-**downloads** it as `scout_<run_id>.json` to the browser's Downloads
folder. Only a one-line status comes back through the extension.
`runner.py::load_downloaded_jobs` then polls the Downloads folder
(`download_dir()`, config-overridable) for that file, reads it, and deletes
it. The blob download is load-bearing; do not try to route descriptions back
through the tool return value. There is deliberately **no shell step** — the
sub-agent does not move the file — so the handoff works identically on
Windows/macOS/Linux (the poll replaces the wait-loop the agent used to run
in bash).

### Between passes — deterministic filters
`apply_deterministic_filters()` cheaply drops jobs before spending any LLM call:
scrape errors, jobs already in the DB, already-applied, closed (`jobState != LISTED`),
jobs with no company name, and companies in the config's `[filters]
exclude_companies` (also enforced again in `save_jobs`).

### Pass 2 — description cleaning (parallel), `agent/clean_prompt.md`

**Purpose:** for each survivor, strip non-role boilerplate from the raw
description — EEO/DEI statements, legal disclaimers, generic culture/benefits
marketing, "About [Company]" fluff — returning a single
`{"description_clean": "..."}` field. One call per job, `MAX_WORKERS`-wide via
a `ThreadPoolExecutor`. A failed call falls back to the raw description so
Pass 3 always has something to work with.

**Entry point:** `run_headless("clean", system_prompt, user_message)`
(`agent/llm.py`), dispatched per `[llm] backend`:
- **Claude:** Haiku (`CLEAN_MODEL` in `agent/claude.py`) via a
  `claude --print` subprocess (`_run_claude_headless`).
- **API:** the configured `[llm.api] model` against any OpenAI-compatible
  endpoint (`_run_api_llm`), with optional `[llm.api.clean]` request-parameter
  passthrough.

### Pass 3 — per-job enrichment (parallel), `agent/enrichment_prompt.md`

**Purpose:** for each survivor, all from a single prompt
(`enrichment_prompt.md`):
- classify the role into one of the **user-configured role types** (or
  `Other`)
- write a 2–4 sentence summary
- tag the job
- score it against the candidate's resume/profiles/criteria

One call per job, `MAX_WORKERS`-wide via a `ThreadPoolExecutor`. Jobs
classified `Other` (or that fail to enrich) are dropped; the rest are saved
via `agent/tools.py::save_jobs`, which also does repost detection and unwraps
LinkedIn safety-redirect apply URLs.

**Entry point:** `run_headless("enrich", system_prompt, user_message)`
(`agent/llm.py`), dispatched per `[llm] backend`:
- **Claude:** Sonnet (`ENRICH_MODEL` in `agent/claude.py`) via a
  `claude --print` subprocess (`_run_claude_headless`).
- **API:** the configured `[llm.api] model` against any OpenAI-compatible
  endpoint (`_run_api_llm`), with optional `[llm.api.enrich]` request-parameter
  passthrough.

### Configuration (`profiles/config.toml`)

All user configuration lives in `profiles/config.toml` (loaded and validated
by `app/config.py::load_config`). The file is **required**, with six required
sections and no in-code defaults, plus one optional section.

#### `[[roles]]` (required, ≥1 role type)
Each role carries the classification definition injected into the prompt's
`{{ROLE_DEFINITIONS}}`/`{{ROLE_ENUM}}` placeholders, an optional per-role
profile file for scoring, and drives the UI filter buttons and chip colors.
`jobs.role_type` stores the role's `name` verbatim. `runner.validate_setup()`
fails fast at pipeline start: the roles config must load, `profiles/resume.md`
must exist, and a role's referenced profile file must exist (roles may omit
`profile` to score on the resume alone).

#### `[[linkedin_searches]]` (required, ≥1 named saved-search URL)
Scraped every run. `name` is the alias shown in the UI/logs in place of the
raw URL.

#### `[filters]` (required)
`exclude_companies` — may be empty.

#### `[scoring]` (required)
`fit`/`criteria` weights summing to 1, plus `dealbreaker_cap` used by
`compute_match_score`.

#### `[logging]` (required)
`dir` for the daily app log and the opt-in model-call log; see
`app/logging_setup.py`.

#### `[llm]` (required)
Selects the backend for the two **headless** passes — description cleaning
and enrichment/scoring — via `agent/llm.py::run_headless()`:
- `backend` (required, `"claude"` or `"api"` — no default, so the config
  always states which one).
- `max_workers` (required, the Pass 2/3 pool width, tuned per backend).

`"api"` routes both passes (together, never split) to any OpenAI-compatible
endpoint (e.g. Ollama, local or remote) configured under `[llm.api]`
(`base_url`, `model`, optional `api_key`/`timeout`). Two optional per-pass
sub-tables, `[llm.api.clean]` and `[llm.api.enrich]`, carry request
parameters (e.g. `temperature`, `reasoning_effort`) merged verbatim into that
pass's chat-completion JSON by `agent/llm_api.py::_run_api_llm`; values must
be scalars and may not set the pipeline-owned `model`/`messages`/`stream`
keys (validated in `config._parse_api_params`). The browser scrape always
runs on Claude, regardless of `[llm] backend`.

#### `[scrape]` (optional)
- `download_dir` — where the browser saves the scrape blob; defaults to
  `~/Downloads` (correct on Windows/macOS/Linux) and `runner.download_dir()`
  expands it, so it's the only config path with a cross-platform default
  rather than failing loudly.
- `run_timeout_minutes` — the web UI's overall wall-clock guardrail (default
  30; see below).

### Progress events → web UI + extension popup
`runner.py` emits `SCOUT_PROGRESS <json>` sentinel lines on stdout. `app/main.py`
reads the subprocess stdout line by line and folds those events into the in-memory
`_run` dict (`_apply_event`), same as before. Two different things read `_run` now:
`GET /scout/status` renders `partials/run_banner.html`, a lightweight "Scout is
running…"/error strip (the web UI no longer has a full drawer — it doesn't trigger
runs anymore, so it just needs to not look misleading if a run is happening
elsewhere while it's open); `GET /api/extension/status` returns the same `_run`
snapshot as JSON, including the granular per-job `_run["log"]` lines, for the
extension popup to poll and render in detail (see "Pass 1" above). Event `key`s in
`runner.py`'s `emit()` calls must stay in sync with `GLOBAL_STEPS` / `SEARCH_STEPS`
in `app/main.py`.

### Data layer
`app/database.py` — DuckDB at `data/scout.duckdb`, two tables (`scrape_runs`, `jobs`).
`role_type` is per-job (derived from the title/description at enrichment), not per-run.
`scrape_runs.search_name` holds the configured search's `name` alias (from the
`[[linkedin_searches]]` redesign, which replaced the earlier Gmail-derived
`email_subject`/`email_date` columns — the one-time migration off that schema,
`_migrate_scrape_runs_schema()`, was retired once no pre-migration databases
remained).

### `agent/tools.py`
Plain Python DB helpers (`create_scrape_run`, `get_existing_job_ids`, `save_jobs`,
etc.) called directly by `runner.py`.

### The LLM-calling layer (`agent/llm_common.py`, `agent/claude.py`, `agent/llm_api.py`, `agent/llm.py`)
`runner.py` holds only pipeline orchestration; every function that actually talks to
a backend lives in one of four small modules, one-directional
(`runner.py` → `llm.py` → {`claude.py`, `llm_api.py`} → `llm_common.py`, never the
reverse — this is what keeps the import graph acyclic):
- `agent/llm_common.py` — cross-cutting plumbing only, no backend logic: progress
  events (`emit`/`emit_log`/`PROGRESS_SENTINEL`), `log_model_call()`, token/cost
  accounting (`_add_usage`/`print_token_summary`/`_tokens`), and `SetupError` (raised
  by both `runner.py`'s `check_setup` and `llm_api.py`'s `_verify_api_llm` — neither
  owns the other, so it lives in the shared leaf module).
- `agent/claude.py` — everything that talks to the Claude CLI: `run_claude` (Pass 1
  browser scrape), `_run_claude_headless` (Pass 2/3 claude backend), `claude_executable`,
  `_kill_process_tree`, and the `SCRAPER_MODEL`/`CLEAN_MODEL`/`ENRICH_MODEL` constants.
- `agent/llm_api.py` — everything that talks to the OpenAI-compatible endpoint:
  `_run_api_llm`, `_verify_api_llm`, `_warm_api_llm`, `_api_endpoint`.
- `agent/llm.py` — the thin router: `run_headless()` dispatches to `claude.py` or
  `llm_api.py` based on `config.llm_backend`.

`runner.py` imports directly from whichever module owns a given name (e.g.
`CLEAN_MODEL`/`ENRICH_MODEL` come straight from `agent.claude`, not funneled through
`agent.llm`) — only the actual Pass 2/3 backend *dispatch* goes through `llm.py`.
Tests mirror this: `tests/test_agent_llm_common.py`, `tests/test_agent_claude.py`,
`tests/test_agent_llm_api.py`, `tests/test_agent_llm.py`.

## Conventions

- **Never work on `main` or a `release/x.y.z` branch directly.** Scout uses a
  release-branch workflow, not plain trunk-based development:
  `release/x.y.z` (not `main`) is the **default branch** and where
  feature/bugfix work actually PRs into throughout a release cycle; `main`
  only advances once, when the finished release branch is merged in and
  tagged. Always create a feature branch off the current `release/x.y.z`
  (`git checkout -b <branch-name>`) before making changes. The one exception
  is a **hotfix**: an urgent fix to already-shipped (tagged) code, which
  branches from and PRs directly into `main`, then gets merged back into the
  active release branch too (see `CONTRIBUTING.md`'s "Hotfixes" section —
  skipping that merge-back is the easy mistake, since it silently vanishes
  once the release branch eventually merges into `main`). Both `main` and
  every `release/*`/`hotfix/*` branch are protected on GitHub (PR required,
  force-push and deletion blocked) — `release/*`/`hotfix/*` via a repo-wide
  ruleset so new branches are covered automatically, not a redone per-branch
  setting. Versioning (`x.y.z` semver) and the full release-cut checklist
  live in `CONTRIBUTING.md`'s "Releasing"/"Hotfixes" sections and
  `release_notes.md` at the repo root; `extension/manifest.json`'s
  `"version"` is the single source of truth for the project's version number.
- **Never merge a PR into `main` or a `release/x.y.z` branch.** This holds
  even mid-release-cut, and even once the human has approved the overall
  release plan — plan approval is not merge approval. Open the PR (`gh pr
  create`) and stop; the human reviews and merges it themselves. Steps that
  come after a merge (tagging, `gh release create`, deleting the spent
  branch) block on that human action.
- **CI runs on every push and PR** via [`.github/workflows/tests.yml`](.github/workflows/tests.yml)
  (`pipenv run unit-tests` — tests + branch coverage), triggered on `main`,
  `release/**`, and `hotfix/**`. Run it locally before opening a PR rather
  than relying on CI to catch failures. On push to `main` or a `release/**`
  branch it also regenerates the coverage badge onto the unprotected
  `badges` branch.
- **Every Python function must have a docstring** — this is a hard project rule; the
  codebase follows it uniformly.
- Claude model IDs are pinned as constants in `agent/claude.py` (`SCRAPER_MODEL` and
  `CLEAN_MODEL` = Haiku, `ENRICH_MODEL` = Sonnet); the api-backend model comes
  from `[llm.api] model` in the config instead. Each `claude` subprocess has a
  `SUBPROCESS_TIMEOUT_S` wall-clock kill (api calls use `[llm.api] timeout`);
  the web UI adds an overall guardrail on top (`[scrape] run_timeout_minutes`,
  default 30).
- Tests add the project root to `sys.path` via `tests/conftest.py`; import as
  `from app...` / `from agent...`.
- **Any UI change gets a real visual mockup before implementation.** Build it
  as a self-contained HTML file under `mockups/` (matching the app's real
  Tailwind classes, fonts, and dual-theme toggle — see existing files there
  for the pattern) and add an entry linking to it from `mockups/index.html`.
  Get it approved before writing the implementation.

## Documentation site

`docs/` + `mkdocs.yml` is a MkDocs Material site, deployed to GitHub Pages
(https://abraham-jacob.github.io/scout/) by `.github/workflows/docs.yml` on
every push to `main` that touches `docs/**` or `mkdocs.yml` (GitHub Pages
"Source" is set to "GitHub Actions" in repo settings — a one-time manual step,
already done). Build/preview locally with `pipenv run mkdocs build --strict`
and `pipenv run mkdocs serve`.

- **Reusable CSS components** live in `docs/stylesheets/extra.css` — reuse
  these for new pages/sections rather than inventing new patterns: `.st-step*`
  (numbered/icon-badge stepper, used by the Configuration, Web UI, and
  Architecture pages), `.st-hero`/`.st-cta`/`.st-flow` (the Home page's
  product-landing hero), `.arch-*` (the Architecture page's pipeline-flow
  diagram with cost pills), `.st-pill` (required/optional field badges in
  config-reference tables).
- **`overrides/partials/`** holds Material theme partial overrides
  (`copyright.html`, `social.html`) wired via `theme.custom_dir: overrides` in
  `mkdocs.yml` — these build the site's pinned, three-zone footer (copyright
  left, source/license center, Buy Me a Coffee/Ko-fi badges right) since
  Material's default `extra.social` config only supports icon glyphs, not
  custom badge images. `pipenv run mkdocs serve`'s live-reload watcher does
  **not** reliably pick up changes under `overrides/` or structural
  `mkdocs.yml` edits (e.g. `theme.custom_dir`, `theme.features`) — kill and
  restart the server after editing those, rather than trusting hot-reload.
- The Local LLM Backend page was renamed to `docs/openai-compatible-backend.md`
  ("OpenAI-compatible Backend") — Passes 2–3 can point at any
  OpenAI-compatible endpoint, local (e.g. Ollama) or a remote paid API, not
  just a local model; keep that framing if editing it further.
- Before trusting "the live site looks wrong" during a docs change: GitHub
  Pages serves `docs/stylesheets/extra.css` with `Cache-Control: max-age=600`,
  and browsers cache it aggressively — a stale-cache render is far more
  likely than a real regression. Check in an incognito window or with a
  cache-busting fetch before debugging further.

## LinkedIn scraping notes (in `scrape_prompt.md` and `extension/content.js`)

Two page structures exist and the harvest JS handles both — in `scrape_prompt.md`'s
Step 1 for the CDP fallback, and ported verbatim into `extension/content.js`'s
`harvestJobIds()` for the extension path: `/search-results/` uses the `componentkey`
attribute; `/search/` (and `/comm/jobs/search` redirects, which always land on
`/jobs/search/` before a content script would see them) uses
`data-occludable-job-id`. The Voyager API is the sole data source — salary is not in
the API and is regex-parsed out of the description text (`salaryFromText()` in both
places).
