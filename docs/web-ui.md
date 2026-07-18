# Using the Web UI

Start the UI with:

```bash
pipenv run uvicorn app.main:app        # http://127.0.0.1:8000
```

It's a single-user, local-only FastAPI + HTMX app — no login, no network
exposure, just a job list and a **▶ Run Scout** button. Clicking it launches
the pipeline as a subprocess and streams live progress into a "run drawer" —
per-pass timers, live *N of M* counts, which model is doing what, and a
scrolling event log of every job's outcome.

![Live run drawer: three passes with timers, progress counts, and a streaming event log](images/run_drawer.gif)

## Sort and filter

Filter by role type, application status (including the full interview
pipeline — Recruiter → Technical → Offer/Rejected), unseen-only, or company
name with autocomplete search. Sort by newest or best match.

![Filter bar: role, status, sort, unseen-only, and company search](images/feature_sort_filter.png)

## Job cards

Every card surfaces what matters at a glance — title, company, location,
salary range, and how it was posted (new vs. repost) — with the full
original description one click away.

![A full job card with title, match score, tags, summary, and apply links](images/feature_job_card.png)

## Description summaries

No more scrolling past boilerplate. Every job gets a clean 2–4 sentence
summary of the actual role, generated after the noise (EEO statements,
benefits marketing, "About the Company" filler) is stripped out.

![A boilerplate-free, 2-4 sentence job summary](images/feature_description_summary.png)

## Tags

Each job is tagged with the details you'd otherwise dig for — workplace
type, salary band, tech stack, team size, seniority — so you can scan a card
instead of reading it.

![Tag chips for role type, salary, workplace, seniority, and tech stack](images/feature_tagging.png)

## Match score

Every job is scored 0–100 against your resume, an optional per-role
profile, and your hard criteria — with dealbreakers (like an unacceptable
commute or on-site requirement) capping the score regardless of how good the
rest of the fit is. See [Configuration](getting-started.md) for how to define
dealbreakers.

![A job title with its computed match-score badge](images/feature_job_match_score.png)

## Application pipeline

Move a job through New → Saved → Applied → Interviewing
(Recruiter/Technical) → Offer/Rejected right from its card. The status
filter understands the whole pipeline, not just exact matches.

![The status dropdown showing every pipeline stage from New to Dismissed](images/feature_track_jobs.png)

## Apply links

Every card links straight to the fastest path to apply — the company's own
site or Easy Apply — plus the original LinkedIn listing, with LinkedIn's
safety-redirect wrapper unwrapped so the link goes where it says it does.

![Apply on company site and LinkedIn links on a job card](images/feature_links_to_apply.png)

## Switching LLM backends

Run the description-cleaning and enrichment passes on the Claude API for
best-in-class quality, or point them at any OpenAI-compatible local server
(Ollama, etc.) for a fully free, fully private run — no job description ever
leaves your machine. Switch backends with one line in `profiles/config.toml`
(see [Local LLM Backend](local-llm.md)); the run drawer always shows exactly
which backend and model did the work.

![The run drawer's backend badge switching between Claude and a local model](images/feature_backend_toggle.gif)
