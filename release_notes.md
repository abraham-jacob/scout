# Release Notes

All notable changes to Scout are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/) as best applies to a personal,
single-user tool — Scout has no published package or API for anything else
to depend on, so a version bump communicates *how much changed*, not a
compatibility contract. `extension/manifest.json`'s `version` field is the
single source of truth for the project's version number.

## [0.1.0] - 2026-08-01

### Added

- **Chrome extension (`extension/`) for Pass 1 scraping.** Its content
  script scrapes LinkedIn's Voyager API from inside the user's own
  authenticated tab — same-origin requests, no CDP fingerprint — replacing
  the CDP-driven automation that kept triggering LinkedIn's bot-detection
  challenge. Requests go out sequentially with random jitter instead of in
  a burst, and any block/auth-loss signal (HTTP 999/401/403) halts the run
  immediately rather than retrying into it.
- **Extension popup** as the new trigger/monitor UI: saved searches, a
  Custom Search box (any LinkedIn search URL, job URL, or bare job ID), and
  a Scrape Current Page button, each with live progress during the harvest
  and polled Pass 2/3 progress after ingest.
- **Halt/resume.** A halted run persists its pending job ids to
  `chrome.storage.local` and offers a Resume button — including after the
  popup itself has been closed and reopened.
- New backend routes: `GET /api/extension/searches`,
  `POST /api/extension/dedupe`, `POST /api/extension/ingest`,
  `GET /api/extension/status`.
- New optional `[extension]` config section (`min_delay_ms`/`max_delay_ms`)
  for tuning the Voyager request pacing.
- **Reload Saved Searches** button next to the popup's search list, to pick
  up an edited `config.toml` without restarting the server.
- **Abort** button next to the Run Log, to stop a run in progress regardless
  of which pass it's in.

### Changed

- The web UI no longer triggers scrapes — the "Run Scout" button, the
  URL/job-ID paste field, and the full run drawer are gone, replaced by a
  lightweight "Scout is running…" status banner. Triggering and monitoring
  runs now lives entirely in the extension popup.
- `agent/runner.py`'s scrape-independent pipeline tail (deterministic
  filters → clean → enrich → save) is now shared, via
  `_process_scraped_jobs()`/`process_ingested_jobs()`, between the CDP path
  and extension-sourced jobs — both are processed identically.

### Fixed

- A run in progress could look idle (all buttons enabled) if the extension
  popup was closed and reopened mid-scrape, risking a duplicate run being
  started against the same search.
- Pass 1's harvest could silently cap at ~7 jobs on the virtualized
  `/jobs/search/` DOM variant, which mounts only the first burst of cards up
  front; a poll loop now re-harvests every 700ms until the job-id count
  holds steady instead of relying on a single quiet-period wait.

Docs (`README.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `profiles/README.md`, the
MkDocs site) updated to describe the extension as the default Pass 1 path,
including a walkthrough of Saved Searches/Custom Search/Scrape Current Page
on the "Using Scout" page and new FAQ entries for common extension errors;
the CDP-driven scrape (`agent/scrape_prompt.md`) stays in place as a
fallback until the extension is proven in longer real-world use.
