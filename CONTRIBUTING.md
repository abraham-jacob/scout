# Contributing to Scout

Thanks for your interest in Scout. This project is young and mostly built by one
person so far, but it's meant to be shared — the notes below are the same
conventions the codebase already follows, written down so a new contributor
doesn't have to reverse-engineer them from the diff history.

## Before you start

- Read the [README](README.md) first — architecture, setup, and the three-pass
  pipeline design are covered there.
- For anything non-trivial (a new feature, a behavior change, a new
  dependency), open an issue or start a discussion before writing code. It's a
  much cheaper conversation before the PR than after.
- Scout automates browsing *your own* LinkedIn account via *your own* logged-in
  Chrome session. Keep that principle in any change you propose — nothing that
  turns this into a scraping/crawling tool at scale.

## Workflow

`main` is protected: it requires a pull request to merge, force-pushes are
blocked, and the branch can't be deleted. There is no situation where you
should push directly to `main`.

1. **Branch from `main`.** Never commit on `main` directly.
   ```bash
   git checkout main && git pull
   git checkout -b your-feature-name
   ```
2. **Make focused commits.** Prefer several small, well-scoped commits over one
   giant one — it makes review (and future `git blame`) much easier.
3. **Run the test suite before opening a PR** (see below).
4. **Open a PR against `main`.** Describe *why* the change is needed, not just
   what changed — the diff already shows the what.
5. **Keep the PR focused.** One logical change per PR. If you notice something
   unrelated that needs fixing, file it separately.

## Environment setup

```bash
git clone https://github.com/abraham-jacob/scout.git && cd scout
pipenv install --dev
```

You'll need your own `profiles/config.toml`, `profiles/resume.md`, Gmail OAuth
credentials, and a logged-in Chrome/LinkedIn session to run the app
end-to-end — see the README's [Quick Start](README.md#-quick-start). None of
that is required just to read the code, run the test suite, or work on a
non-pipeline change (e.g. the web UI, database layer, or config parsing).

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
- Read the module docstring at the top of [`agent/runner.py`](agent/runner.py)
  before touching the pipeline — it's the map for the whole three-pass
  architecture (Pass 1 browser scrape, Pass 2 clean, Pass 3 enrich) and the
  reasoning behind several non-obvious design choices (the blob-download
  handoff, Voyager-API-not-DOM scraping, prompt-cache warming).

## Testing

```bash
pipenv run pytest                                    # full suite
pipenv run pytest tests/test_agent_runner.py          # one file
pipenv run pytest tests/test_agent_runner.py::TestName::test_case
pipenv run pytest -m unit                              # unit tests only
pipenv run pytest -m integration                       # integration tests only
```

- New behavior needs a test. Bug fixes should include a regression test that
  fails before the fix and passes after.
- Tests live under `tests/`, named `test_*.py`; `tests/conftest.py` adds the
  project root to `sys.path`, so import as `from app...` / `from agent...`.
- If you're changing a prompt (`agent/clean_prompt.md` or
  `agent/enrichment_prompt.md`), also see the eval harnesses in
  [`scripts/`](scripts/) — `clean_prompt_test.py` and `enrich_prompt_test.py`
  run the real prompt against captured job descriptions and use an
  LLM-as-judge to score quality. A prompt change with no eval delta is not
  well-tested.

## Commit messages

Explain the *why* — the motivation or the bug being fixed — not just a
restatement of the diff. Keep the subject line short; use the body for
context if it's needed. Match the tone/format of existing history
(`git log --oneline`) rather than inventing a new convention.

## Reporting bugs / requesting features

Open a [GitHub issue](https://github.com/abraham-jacob/scout/issues). For
bugs, include: what you expected, what happened instead, and enough
reproduction context to act on (config shape, which backend — Claude or
local LLM — you're running, relevant log lines). Personal data (resume
content, actual job listings, API keys) never belongs in an issue — redact
before pasting.

## License

By contributing, you agree that your contributions will be licensed under the
project's [MIT License](LICENSE).
