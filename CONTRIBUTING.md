# Contributing to Scout

Thanks for your interest in Scout. This project is young and mostly built by one
person so far, but it's meant to be shared — the notes below are the same
conventions the codebase already follows, written down so a new contributor
doesn't have to reverse-engineer them from the diff history.

## Before you start

- Read the [README](https://github.com/abraham-jacob/scout/blob/main/README.md)
  first — architecture, setup, and the three-pass pipeline design are covered
  there.
- For anything non-trivial (a new feature, a behavior change, a new
  dependency), open an issue or start a discussion before writing code. It's a
  much cheaper conversation before the PR than after.
- Scout automates browsing *your own* LinkedIn account via *your own* logged-in
  Chrome session. Keep that principle in any change you propose — nothing that
  turns this into a scraping/crawling tool at scale.

## Workflow

Scout uses a release-branch model, not plain trunk-based development.
`release/x.y.z` — not `main` — is the **default branch** and the active
integration branch for the in-progress release: all feature and bugfix work
PRs into it throughout the cycle. `main` only moves once, at the very end of
a cycle, when the finished `release/x.y.z` branch is merged in and tagged
(see "Releasing" below). Both `main` and every `release/*` branch are
protected: a pull request is required to merge, force-pushes are blocked,
and neither can be deleted directly (a spent release branch is deleted
explicitly as the last step of the Releasing checklist, not by a stray
force-push or admin override). The one exception to "work lands on
`release/x.y.z`, not `main`" is a **hotfix** — an urgent fix to
already-shipped code that can't wait for the current release cycle to
finish, which branches from and merges directly to `main`; see "Hotfixes"
below.

Every push and every PR against `main` or a `release/*` branch automatically
runs the test suite via
[GitHub Actions](https://github.com/abraham-jacob/scout/blob/main/.github/workflows/tests.yml)
— that's the "Tests" and "Coverage" badges at the top of the README. A PR
with a failing run is visible immediately in the PR's checks tab.

1. **Branch from the current release branch**, not `main` — check
   `git branch --show-current` on a fresh clone/pull to confirm which
   `release/x.y.z` is currently checked out as default.
   ```bash
   git checkout release/x.y.z && git pull
   git checkout -b your-feature-name
   ```
2. **Make focused commits.** Prefer several small, well-scoped commits over one
   giant one — it makes review (and future `git blame`) much easier.
3. **Run the test suite locally before opening a PR** (see below) — don't
   rely on CI to catch something you could've caught in ten seconds.
4. **Open a PR against the current `release/x.y.z` branch**, not `main`.
   Describe *why* the change is needed, not just what changed — the diff
   already shows the what.
5. **Keep the PR focused.** One logical change per PR. If you notice something
   unrelated that needs fixing, file it separately.

## Environment setup

```bash
git clone https://github.com/abraham-jacob/scout.git && cd scout
pipenv install --dev
```

You'll need your own `profiles/config.toml`, `profiles/resume.md`, a
logged-in Chrome/LinkedIn session, and the Scout extension loaded unpacked
(`chrome://extensions` → Developer mode → Load unpacked → `extension/`) to
run the app end-to-end — see the [Configuration guide](https://abraham-jacob.github.io/scout/getting-started/).
None of that is required just to read the code, run the test suite, or work
on a non-pipeline change (e.g. the web UI, database layer, or config
parsing).

## Code conventions

- **Every Python function has a docstring.** This is a hard rule, applied
  uniformly across the codebase — no exceptions for "obvious" helpers or
  one-liners.
- **Fail loudly, not silently.** Config validation and setup checks raise
  clear `ValueError`/`SetupError`s on the first problem rather than falling
  back to a hidden default. If you're adding a new config option, follow that
  pattern — see `app/config.py`.
- **Comments explain *why*, not *what*.** Code should be readable enough that
  a comment restating it is unnecessary; reserve comments for non-obvious
  constraints, workarounds, or invariants.
- **Don't add abstractions ahead of need.** A bug fix doesn't need a
  refactor bundled in; a one-off script doesn't need a generic framework.
  Three similar lines beat a premature abstraction.
- Read the module docstring at the top of
  [`agent/runner.py`](https://github.com/abraham-jacob/scout/blob/main/agent/runner.py)
  before touching the pipeline — it's the map for the whole three-pass
  architecture (Pass 1 browser scrape — the `extension/` Chrome extension by
  default, with an older CDP-driven path kept as a fallback — Pass 2 clean,
  Pass 3 enrich) and the reasoning behind several non-obvious design choices
  (Voyager-API-not-DOM scraping, the CDP path's blob-download handoff,
  prompt-cache warming).

## Testing

```bash
pipenv run unit-tests                                # full suite, JUnit XML + branch coverage — same as CI
pipenv run pytest                                    # full suite, no coverage — faster for iteration
pipenv run pytest tests/test_agent_runner.py          # one file
pipenv run pytest tests/test_agent_runner.py::TestName::test_case
pipenv run pytest -m unit                              # unit tests only
pipenv run pytest -m integration                       # integration tests only
```

Use `pipenv run unit-tests` before opening a PR — it's exactly what CI runs,
so a clean local run means a clean CI run. Coverage output lands in
`htmlcov/` (open `htmlcov/index.html` in a browser) and
`junit_xml_test_report.xml`; both are git-ignored, generated fresh each run.

- New behavior needs a test. Bug fixes should include a regression test that
  fails before the fix and passes after.
- Tests live under `tests/`, named `test_*.py`; `tests/conftest.py` adds the
  project root to `sys.path`, so import as `from app...` / `from agent...`.
- If you're changing a prompt (`agent/clean_prompt.md` or
  `agent/enrichment_prompt.md`), also see the eval harnesses in
  [`scripts/`](https://github.com/abraham-jacob/scout/tree/main/scripts) —
  `clean_prompt_test.py` and `enrich_prompt_test.py`
  run the real prompt against captured job descriptions and use an
  LLM-as-judge to score quality. A prompt change with no eval delta is not
  well-tested.

## Docs

The documentation site (built with MkDocs Material, deployed to GitHub
Pages) lives under `docs/`, configured by `mkdocs.yml` at the repo root.

```bash
pipenv install --dev              # picks up mkdocs / mkdocs-material
pipenv run mkdocs serve           # http://127.0.0.1:8000, live reload
pipenv run mkdocs build --strict  # same check CI runs — do this before pushing docs changes
```

`mkdocs serve` defaults to port 8000, the same as `uvicorn app.main:app` —
if you need both running at once, use `pipenv run mkdocs serve -a 127.0.0.1:8001`.

## Commit messages

Explain the *why* — the motivation or the bug being fixed — not just a
restatement of the diff. Keep the subject line short; use the body for
context if it's needed. Match the tone/format of existing history
(`git log --oneline`) rather than inventing a new convention.

## Releasing

Scout uses `x.y.z` [semantic versioning](https://semver.org/). There's no
publish step (no package registry, no Chrome Web Store) — a release is just
a git tag plus a GitHub Release, and it's a manual, deliberate step, not
automated on every merge. `extension/manifest.json`'s `version` field is the
single source of truth for the project's version number. Because feature
work accumulates on `release/x.y.z` throughout a cycle (see "Workflow"
above), cutting a release means merging that whole branch into `main`, not
just bumping a number:

1. On the `release/x.y.z` branch: finalize
   [`release_notes.md`](https://github.com/abraham-jacob/scout/blob/main/release_notes.md) — add/complete the `## [x.y.z] -
   YYYY-MM-DD` section (reverse-chronological, [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
   style: `Added`/`Changed`/`Fixed`/`Removed` subheadings) — and bump
   `extension/manifest.json`'s `"version"` to match. Commit.
2. Open a PR from `release/x.y.z` into `main` (title: `Release vx.y.z`) and
   merge it once CI is green.
3. Tag the resulting commit on `main`:
   ```bash
   git checkout main && git pull
   git tag -a vx.y.z -m "vx.y.z"
   git push origin main --tags
   ```
4. Create the GitHub Release from the tag:
   ```bash
   gh release create vx.y.z --title vx.y.z --notes-file -
   ```
   then paste the new `release_notes.md` section's body on stdin (Ctrl-D to
   finish) — or open `gh release create vx.y.z` without `--notes-file` for
   an interactive editor instead.
5. Delete the spent `release/x.y.z` branch — it's fully merged into `main`
   at this point, so nothing is lost:
   ```bash
   git push origin --delete release/x.y.z
   ```
6. Cut the next cycle's release branch from `main` and make it the new
   default branch:
   ```bash
   git checkout main && git checkout -b release/x.y+1.0
   git push origin release/x.y+1.0
   gh repo edit --default-branch release/x.y+1.0
   ```
   `release/*` branches are covered by a repo-wide ruleset, so the new
   branch is automatically protected (PR required, `test` status check
   required, no force-push/deletion) — no per-branch protection setup
   needed.

Steps 2 onward always happen after the release branch's work is done and
its PR has merged — never tag a branch mid-review.

## Hotfixes

A hotfix is for an urgent fix to code that's **already shipped** (tagged on
`main`) and can't wait for the current `release/x.y.z` cycle to finish.
Unlike everything else in "Workflow" above, it branches from and merges
directly to `main`, skipping the release branch entirely — and then, unlike
a normal release, it also has to be merged into the *current* release
branch so that branch doesn't silently regress the fix once it eventually
merges into `main` itself.

`hotfix/*` branches are covered by their own repo-wide ruleset (PR required,
`test` status check required, no force-push/deletion), the same protection
`release/*` branches get.

1. Branch from `main`, not the current release branch:
   ```bash
   git checkout main && git pull
   git checkout -b hotfix/x.y.z
   ```
   `x.y.z` is the next **patch** version above whatever's currently tagged
   on `main` (e.g. `main` at `v0.1.0` → `hotfix/0.1.1`).
2. Fix the bug and add a regression test. Commit.
3. On the hotfix branch, add a `## [x.y.z] - YYYY-MM-DD` entry to
   `release_notes.md` (typically just a `Fixed` subsection) and bump
   `extension/manifest.json`'s `"version"` to match.
4. Open a PR from `hotfix/x.y.z` into `main` (title: `Hotfix vx.y.z`), merge
   once CI is green.
5. Tag and release from `main`, exactly like a normal release:
   ```bash
   git checkout main && git pull
   git tag -a vx.y.z -m "vx.y.z"
   git push origin main --tags
   gh release create vx.y.z --title vx.y.z --notes-file -
   ```
6. **Merge the fix into the active `release/x.y.z` branch too** — open a
   second PR, `hotfix/x.y.z` into the current release branch (or
   cherry-pick the fix commit if the branches have diverged too much for a
   clean merge). **Don't skip this step** — it's the one that's easy to
   forget, and forgetting it means the fix silently disappears the moment
   the release branch eventually merges into `main` and overwrites it.
7. Delete `hotfix/x.y.z` once both merges (step 4 and step 6) are done.

## Reporting bugs / requesting features

Open a [GitHub issue](https://github.com/abraham-jacob/scout/issues). For
bugs, include: what you expected, what happened instead, and enough
reproduction context to act on (config shape, which backend — Claude or
API — you're running, relevant log lines). Personal data (resume
content, actual job listings, API keys) never belongs in an issue — redact
before pasting.

## License

By contributing, you agree that your contributions will be licensed under the
project's [MIT License](https://github.com/abraham-jacob/scout/blob/main/LICENSE).
