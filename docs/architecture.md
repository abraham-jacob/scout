# Architecture

Scout is **three LLM passes with cheap deterministic filtering in between**,
orchestrated by [`agent/runner.py`](https://github.com/abraham-jacob/scout/blob/main/agent/runner.py).
The module docstring at the top of that file is the canonical map of the
pipeline — this page is the reader-facing version of the same design.

<div class="arch-flow" markdown>

<div class="arch-node" markdown>
<span class="arch-node-title">Pass 1 · Browser scrape</span>
<span class="arch-node-sub">Scout browser extension hits LinkedIn's Voyager API from inside your own session</span>
<span class="arch-cost free">Free</span>
</div>

<div class="arch-node" markdown>
<span class="arch-node-title">Deterministic filters</span>
<span class="arch-node-sub">Drops dupes, applied, closed, excluded — before spending a token</span>
<span class="arch-cost free">Free</span>
</div>

<div class="arch-node" markdown>
<span class="arch-node-title">Pass 2 · Clean</span>
<span class="arch-node-sub">Strips boilerplate from each description, in parallel</span>
<span class="arch-cost haiku">Haiku</span>
<span class="arch-or">or</span>
<span class="arch-cost gateway">OpenAI-compatible inference gateway</span>
</div>

<div class="arch-node" markdown>
<span class="arch-node-title">Pass 3 · Enrich & score</span>
<span class="arch-node-sub">Classifies, summarizes, tags, and scores 0–100, in parallel</span>
<span class="arch-cost sonnet">Sonnet</span>
<span class="arch-or">or</span>
<span class="arch-cost gateway">OpenAI-compatible inference gateway</span>
</div>

<div class="arch-node" markdown>
<span class="arch-node-title">DuckDB → Web UI</span>
<span class="arch-node-sub">Survivors saved locally; the FastAPI + HTMX UI browses them</span>
<span class="arch-cost free">Free</span>
</div>

</div>

## :material-puzzle:{ .chrome } Pass 1 — browser scrape (Scout extension)

**Default: the Scout browser extension** (`extension/`). Its content script
(`extension/content.js`) runs inside your own authenticated LinkedIn tab —
no automated-browser fingerprint. It hits LinkedIn's
internal **Voyager job-postings API** the same way LinkedIn's own SPA does,
pulling every field for every job behind a saved search, including
virtualized cards the page itself never renders: title, company, full
description, apply URL, applied status, and whether the posting is still
live.

**Harvest → dedupe → fetch.** `content.js` waits for the results to settle,
then collects every job id on the page. Those ids are posted to
`POST /api/extension/dedupe`, which drops anything already in the database —
the biggest lever on request volume. Remaining jobs are fetched
**sequentially with random jitter** (`[extension] min_delay_ms`/`max_delay_ms`,
default 3000/8000) rather than in a parallel burst, so the traffic pattern
looks like a human clicking through jobs, not a scraper. A block/auth-loss
signal from LinkedIn halts the run immediately rather than retrying into it;
whatever was already fetched is still ingested, and the popup can Resume
the rest later.

**Ingest.** The batch is posted to `POST /api/extension/ingest`, which spawns
`python -m agent.runner --ingest-file <path>` — the same subprocess Pass 2/3
always run as, so extension-triggered runs report progress through the exact
mechanism described below.

**Popup.** The extension's popup (`extension/popup.html`) is the
trigger/monitor UI: your saved searches (each with a Run button), a Custom
Search box for a pasted URL or job id, Scrape Current Page, and an Abort
button. It streams Pass 1 live as it happens, then keeps streaming once
Pass 2/3 pick up server-side, through to a saved-jobs summary.

## Between passes — deterministic filters

Before spending another token, `apply_deterministic_filters()` cheaply drops:

- jobs that errored during scraping
- jobs already in the database
- jobs already applied to
- closed postings (`jobState != LISTED`)
- jobs with no company name
- jobs from companies in `[filters] exclude_companies` (enforced again in
  `save_jobs` as a second line of defense)

Filtering is free; LLM calls aren't.

## Pass 2 — description cleaning (Haiku or OpenAI API, parallel)

For each survivor, one headless call strips non-role boilerplate out of the
raw description — EEO/DEI statements, legal disclaimers, generic
culture/benefits marketing, "About [Company]" fluff — driven by
`agent/clean_prompt.md` and returning a single `{"description_clean": "..."}`
field. This runs `max_workers`-wide via a thread pool. A failed call falls
back to the raw description, so Pass 3 always has something to work with. On
the Claude backend this pass runs on Haiku; on the api backend it runs on
whatever model is configured under `[llm.api]`.

## Pass 3 — per-job enrichment and scoring (Sonnet or OpenAI API, parallel)

For each survivor, one headless call — driven by
`agent/enrichment_prompt.md`, which covers classification, summary, tags, and
scoring in a single prompt — classifies the role into one of the
**user-configured role types** (or `Other`), writes a 2–4 sentence summary,
tags the job, and scores it against the candidate's resume, per-role
profile, and hard criteria. This also runs `max_workers`-wide. Jobs
classified `Other`, or that fail to enrich, are dropped; the rest are saved
via `agent/tools.py::save_jobs`, which also does repost detection and
unwraps LinkedIn's safety-redirect apply URLs.

## Progress events → extension popup + web UI

`runner.py` emits `SCOUT_PROGRESS {json}` sentinel lines on stdout as the
pipeline runs. `app/main.py` reads that subprocess's stdout line by line and
folds each event into an in-memory run-state dict — same mechanism
regardless of whether the run was triggered by the extension or
`--ingest-file` directly. Two different things read that state now: the
**extension popup** polls `GET /api/extension/status` and renders the full
detail — per-pass timers, live progress counts, which backend and model is
active, and a streaming, honest event log (a failed call logs as a failure,
and its retry logs as a retry — not as silent success). The **web UI**
doesn't trigger runs anymore, so it only needs a lightweight
"Scout is running…"/error strip (`GET /scout/status`, `partials/run_banner.html`)
that stays honest if a run is happening elsewhere while the page is open —
not a full drawer.

## Data layer

Jobs land in a local DuckDB database (`data/scout.duckdb`) with two tables:
`scrape_runs` and `jobs`. `role_type` is stored per-job — derived from the
title/description at enrichment time — not per-run, since a role's
classification can change as prompts evolve. The URL that seeds Pass 1 comes
straight from your `[[linkedin_searches]]` config — no external account or
OAuth flow involved. Each `scrape_runs` row records the search's `name`
alias (shown in the extension popup's event log) alongside the URL that was
scraped.

### scrape_runs

| Column | Type | Notes |
|---|---|---|
| `run_id` | `VARCHAR` | Primary key |
| `search_name` | `VARCHAR` | The search's `name` alias from `[[linkedin_searches]]` |
| `linkedin_search_url` | `VARCHAR` | The URL that was scraped |
| `jobs_found` | `INTEGER` | Default `0`; updated as jobs land |
| `run_at` | `TIMESTAMP` | Default `current_timestamp` |

A write-only audit log, one row per configured search per run. Nothing in
the app reads it back today; `jobs.scrape_run_id` gives every job real
provenance back to the run that scraped it.
{: .st-table-caption }

### jobs

| Column | Type | Notes |
|---|---|---|
| `job_id` | `VARCHAR` | Primary key |
| `scrape_run_id` | `VARCHAR` | References `scrape_runs(run_id)` |
| `title` | `VARCHAR` | |
| `company` | `VARCHAR` | |
| `location` | `VARCHAR` | |
| `role_type` | `VARCHAR` | Classified per-job at enrichment time, not per-run |
| `description_raw` | `VARCHAR` | As scraped |
| `description_clean` | `VARCHAR` | Boilerplate-stripped, from Pass 2 |
| `description_summary` | `VARCHAR` | 2–4 sentence summary, from Pass 3 |
| `match_score` | `FLOAT` | Weighted combination of fit and criteria |
| `fit_score` | `FLOAT` | Resume/profile fit |
| `criteria_score` | `FLOAT` | Fit against `criteria.md` |
| `dealbreakers` | `VARCHAR[]` | |
| `match_reason` | `VARCHAR` | |
| `linkedin_url` | `VARCHAR` | |
| `apply_url` | `VARCHAR` | Unwrapped from LinkedIn's safety-redirect URL |
| `apply_platform` | `VARCHAR` | |
| `salary_range` | `VARCHAR` | Regex-parsed from the description text |
| `tags` | `VARCHAR[]` | |
| `status` | `VARCHAR` | Default `'new'`; see the pipeline statuses in `app/database.py::JOB_STATUSES` |
| `seen` | `BOOLEAN` | Default `false` |
| `is_repost` | `BOOLEAN` | Default `false`, from repost detection in `save_jobs` |
| `original_job_id` | `VARCHAR` | Set when `is_repost` is true |
| `date_scraped` | `TIMESTAMP` | Default `current_timestamp` |

One row per surviving job, written by `agent/tools.py::save_jobs` after
Pass 3.
{: .st-table-caption }

## Design notes

A few decisions here weren't obvious going in, and are worth calling out:

**Scraping the API, not the DOM.** LinkedIn virtualizes its job list — most
cards on a 25-job page never render, and DOM scraping misses them. Scout
hits the Voyager API from inside your logged-in session instead, getting
every field for every job in one batch. Salary isn't in the API, so it's
regex-parsed out of the description text.

**Spending tokens where judgment lives.** Every architectural seam exists to
avoid paying Sonnet prices for mechanical work: deterministic filters run
before any LLM call; the scrape and clean passes run on Haiku; cleaning
strips boilerplate *specifically so the Sonnet enrichment pass reads fewer
input tokens*; and the parallel enrichment wave is preceded by one serial
call plus a short pause — warming the Anthropic prompt cache so the parallel
calls read the large shared system prompt from cache instead of each paying
to write it. Every run prints its exact token usage and cost when it
finishes.

**Failing loudly, recovering quietly.** Config validation raises on the
first problem instead of silently defaulting; setup checks verify the
Claude CLI, resume, and profile files (and API-backend reachability, if
configured) before any browser work starts; each subprocess has a hard
wall-clock kill; API-backend calls get tight timeouts, one retry pass, and
graceful fallbacks.
