# Documentation site

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
