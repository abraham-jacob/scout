# Profiles & user config

This directory holds your personal configuration and the artifacts the
enrichment pass scores each job against. Everything here except this README
is **git-ignored** — these files contain personal data and never leave your
machine.

## `config.toml` (**required**)

The user config: five required sections, plus one optional `[scrape]`:

```toml
[gmail]
label = "Daily LinkedIn Search"   # the label your job-alert emails live under

[filters]
exclude_companies = ["Capital One"]   # dropped before any LLM call; [] is fine

[scoring]
fit_weight = 0.85        # weights must sum to 1
criteria_weight = 0.15
dealbreaker_cap = 30.0   # score ceiling (0-100) when a dealbreaker is hit

[logging]
dir = "~/.local/state/scout/logs"   # daily app log + opt-in model-call log;
                                    # ~ expands, relative paths = project root

[scrape]                            # OPTIONAL — omit unless you've changed
download_dir = "~/Downloads"        # Chrome's download folder. Defaults to
                                    # ~/Downloads (works on Win/Mac/Linux).
```

### `[[roles]]`

Defines the role types Scout keeps. Each `[[roles]]` entry has a `name` (the
label stored in the DB and shown in the UI), a `definition` (classification
guidance for the enrichment model — what counts, example titles, explicit
exclusions), and an optional `profile` (a markdown file in this directory the
role is scored against). Jobs matching no configured role are classified
`Other` and dropped. Chip and filter colors are assigned automatically in the
order roles are listed.

The file must exist and define **at least one** role — with zero roles
there is nothing for Scout to keep, so the pipeline (and the web UI) refuse
to run. There are no built-in default roles.

```toml
[[roles]]
name = "Product Manager"
definition = """the core of the job is owning product strategy and \
execution... Examples: Product Manager, Senior/Group PM, Director of \
Product. Project/program management does not count."""
profile = "profile_pm.md"   # optional — omit to score on the resume alone
```

## Scoring files

`resume.md` is **required** — the pipeline refuses to start without it, and
every kept job is scored against it. Per-role profiles are optional refinements
on top: a role with one is scored against resume + profile, a role without one
against the resume alone. A profile that is referenced in `config.toml` but
missing on disk is a setup error and also stops the run.

| File | Contents |
|---|---|
| `resume.md` | **Required.** Your latest resume, converted to markdown / plain text. |
| `profile_<role>.md` | Optional, one per role (referenced from `config.toml`): what you are looking for in that kind of role — level, kind of work, technologies, scope. Jobs of a role with no profile are scored against the resume alone. |
| `criteria.md` | Optional. Preferences outside the resume: workplace, compensation, domains to seek/avoid, company stage. Drives the `criteria_weight` share of the final score (the rest is resume+profile fit). Without this file the score is 100% fit. |

Mark any criteria line as a hard veto by prefixing it with `**DEALBREAKER**:` —
jobs violating one are capped at a low score no matter how well they fit, and
the violated item is shown on the job card. Example:

```markdown
## Workplace
- Remote strongly preferred; Hybrid up to 2 days is fine
- **DEALBREAKER**: On-site 4+ days a week

## Domains to avoid
- Ad Tech (soft penalty)
- **DEALBREAKER**: Crypto / Web3
```

## After adding or editing files

- **New scrape runs** score automatically.
- **Existing jobs**: run `pipenv run python -m scripts.backfill_scores`
  (one Sonnet call per unscored job; updates only the scoring columns).
- **Changed your mind about weights or the cap?**
  `pipenv run python -m scripts.backfill_scores --recompute` rebuilds every
  final score from the stored subscores with zero LLM calls.
