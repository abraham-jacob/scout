# Match scoring

Below this section you will find the candidate's **resume**, a **profile**
section for some or all of the role types, and possibly their **criteria**.
Score the job against them and add four more fields to your JSON output:
`fit_score`, `criteria_score`, `dealbreakers`, and `match_reason`.

If you classified the job's `role_type` as `Other`, set `fit_score`,
`criteria_score`, and `match_reason` to `null` and `dealbreakers` to `[]` —
do not score off-target roles.

## `fit_score` — 0 to 100

How well the candidate's background matches what this job is asking for.
Compare the job description against the **resume** plus the profile section
matching your `role_type` classification (a job classified `X` is scored
against the "X Profile" section). If no profile is provided for that role
type, judge against the resume alone.

Calibration:

- **90–100** — the job could have been written for the candidate: level, domain, and
  core skills all line up, with no significant gaps.
- **70–89** — strong match on level and most core skills; one real gap or a
  domain stretch.
- **50–69** — plausible but a reach: right general shape, wrong level, or
  several missing core requirements.
- **25–49** — significant mismatch in level, domain, or required skills.
- **0–24** — wrong role for the candidate in kind, not degree.

Judge only from what the description actually requires — do not reward
keyword overlap on incidental technologies.

## `criteria_score` — 0 to 100, or null

How well the job satisfies the **criteria** section (workplace, compensation,
domains, company preferences). Ignore resume fit here — this is purely about
preferences. If no criteria section is provided, set `null`.

## `dealbreakers` — array of strings

List each criteria item explicitly marked `**DEALBREAKER**` that this job
violates, as a short phrase quoting the violated item (e.g.
`"On-site 4+ days"`). Only items carrying the `DEALBREAKER` mark belong here —
never promote a soft preference. Empty array when nothing is violated, when
the description doesn't say, or when no criteria are provided.

## `match_reason` — one sentence

A single plain-text sentence explaining the score to the candidate: the main reasons
it fits or doesn't, mentioning any dealbreaker hit. No markdown.

## Output format (replaces the earlier one)

Return **only** the JSON object — no preamble, no explanation, no markdown
code fences:

```json
{"role_type": {{ROLE_ENUM}}, "description_summary": "...", "tags": ["..."], "fit_score": 87, "criteria_score": 70, "dealbreakers": [], "match_reason": "..."}
```
