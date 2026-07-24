# Architecture

Scout is **three LLM passes with cheap deterministic filtering in between**,
orchestrated by [`agent/runner.py`](https://github.com/abraham-jacob/scout/blob/main/agent/runner.py).
The module docstring at the top of that file is the canonical map of the
pipeline — this page is the reader-facing version of the same design.

<div class="arch-flow" markdown>

<div class="arch-node" markdown>
<span class="arch-node-title">Pass 1 · Browser scrape</span>
<span class="arch-node-sub">Drives Chrome, hits LinkedIn's Voyager API for every job on page 1</span>
<span class="arch-cost haiku">Haiku</span>
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

## :simple-claude:{ .claude } Pass 1 — browser scrape (Haiku)

`runner.py` spawns `claude --print --chrome` on Haiku, driven by
`agent/system_prompt.md`. This sub-agent does **no filtering** — it hits
LinkedIn's internal **Voyager job-postings API** via `javascript_tool` (not
the accessibility tree, not card-clicking) to pull every field for every job
on page 1, including virtualized cards that LinkedIn never renders: title,
company, full description, apply URL, applied status, and whether the
posting is still live.

**Getting the data out of the browser.** Each job description is 5–13 KB,
and the Claude in Chrome extension's privacy filter blocks large
`javascript_tool` return values — a full page of descriptions is far too
big to come back through the tool call. So the scrape agent instead writes
the whole batch to `window.__jobs` in the page and triggers a **blob
download** of it as `scout_<run_id>.json` to the browser's Downloads folder.
Only a one-line status comes back through the extension. `runner.py`
(`load_downloaded_jobs`) then polls the Downloads folder — config-overridable
via `[scrape] download_dir`, defaulting to `~/Downloads` — for that file,
reads it, and deletes it. There's deliberately no shell step in this
handoff, so it works identically on Windows, macOS, and Linux.

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
the Claude backend this pass runs on Haiku; on the local backend it runs on
whatever model is configured under `[llm.local]`.

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

## Progress events → the web UI

`runner.py` emits `SCOUT_PROGRESS {json}` sentinel lines on stdout as the
pipeline runs. The web UI (`app/main.py`) reads that subprocess's stdout
line by line and folds each event into an in-memory run-state dict, which
renders the live "run drawer" partial that the UI polls every second —
per-pass timers, live progress counts, which backend and model is active,
and a streaming, honest event log (a failed call logs as a failure, and its
retry logs as a retry — not as silent success).

## Data layer

Jobs land in a local DuckDB database (`data/scout.duckdb`) with two tables:
`scrape_runs` and `jobs`. `role_type` is stored per-job — derived from the
title/description at enrichment time — not per-run, since a role's
classification can change as prompts evolve. The URL that seeds Pass 1 comes
straight from your `[[linkedin_searches]]` config — no external account or
OAuth flow involved. Each `scrape_runs` row records the search's `name`
alias (shown in the run drawer and event log) alongside the URL that was
scraped.

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
Claude CLI, resume, and profile files (and local-LLM reachability, if
configured) before any browser work starts; each subprocess has a hard
wall-clock kill; local-LLM calls get tight timeouts, one retry pass, and
graceful fallbacks.
