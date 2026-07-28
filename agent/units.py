"""Splits a raw job description into numbered units for Pass 2 cleaning.

A "unit" is one sentence of prose or one bullet-list line — the smallest
chunk the clean-pass LLM can name for removal without touching anything else.
Splitting happens here in Python (not the LLM) so the model only ever marks
ranges for deletion; it can never rewrite, paraphrase, or drop content we
didn't explicitly tell it to.

The three-step contract used by agent/runner.py::clean_one is:
    units    = split_into_units(description_raw)
    rendered = render_units(units)              # -> sent to the LLM
    drop     = parse_drop_response(parsed_json, len(units))  # <- LLM response
    cleaned  = stitch_units(units, drop)
"""

import re
from dataclasses import dataclass

from yasbd import BoundaryDetector

# LinkedIn's scrape collapses list items directly against each other with no
# separator at all — measured against the production DB, 74% of stored
# description_raw values have a sentence-ending period immediately followed
# by the next sentence's capital letter, e.g. "...data.Build well-structured
# APIs...". yasbd's BoundaryDetector requires whitespace after terminal
# punctuation to find a boundary — without this fix it returns zero internal
# boundaries for that text and the whole splitting scheme silently does
# nothing. This only inserts a space where a lowercase letter or digit is
# immediately followed by ".Capital" — it deliberately leaves rarer collapses
# (e.g. "U.S.Government", "GCP.High-volume") merged into one coarser unit
# rather than risk misfiring elsewhere; that's safe (no data loss, just a
# less granular drop boundary), not fixed further here.
_DECONCAT_RE = re.compile(r"(?<=[a-z0-9])\.(?=[A-Z])")

# A line is treated as a literal bullet (kept verbatim, never sentence-split)
# only when a marker sits at the very start of the line. This deliberately
# does NOT match "•" used mid-line as an inline separator (observed in real
# data: "$150K - $310K • Offers Equity • Offers Bonus" is one compensation
# sentence, not three bullets) — only a marker followed by whitespace at
# line-start counts.
_BULLET_RE = re.compile(r"^\s*(?:[•●◦‣▪\-\*]|\d+[.)]|[a-zA-Z][.)])\s+\S")

_BLANK_LINE_RE = re.compile(r"\n\s*\n")

_LANG = "en"

# yasbd's rule set treats a period followed by whitespace and a capital
# letter as a sentence boundary even inside a common abbreviation — confirmed
# directly against real production data: "Full-time employees outside the
# U.S. receive..." (correctly spaced, no concatenation involved) still gets
# split into "...outside the U.S." / "receive..." with "U.S." occasionally
# left standing alone as its own unit once neighbors are dropped. These are
# the abbreviations actually observed (or reasonably expected) in scraped
# LinkedIn postings' compensation/location boilerplate; their internal
# periods are temporarily replaced with a sentinel before boundary detection
# and restored afterward so they can never be mistaken for a sentence end.
_ABBREVIATIONS = (
    "U.S.", "U.K.", "e.g.", "i.e.", "etc.", "vs.", "Inc.", "Corp.", "Ltd.",
    "Co.", "Mr.", "Mrs.", "Ms.", "Dr.", "Jr.", "Sr.", "Ph.D.", "M.S.", "B.S.",
)
_ABBREV_SENTINEL = "\x00"


def _protect_abbreviations(text: str) -> str:
    """Replace the periods in known abbreviations with a sentinel character.

    Must run before _deconcatenate and BoundaryDetector.detect() so neither
    ever sees a "real" period inside an abbreviation like "U.S." — the
    sentinel is restored by _restore_abbreviations once a sentence has been
    sliced out.
    """
    for abbr in _ABBREVIATIONS:
        text = text.replace(abbr, abbr.replace(".", _ABBREV_SENTINEL))
    return text


def _restore_abbreviations(text: str) -> str:
    """Undo _protect_abbreviations on one already-sliced-out sentence."""
    return text.replace(_ABBREV_SENTINEL, ".")


@dataclass(frozen=True)
class Unit:
    """One numbered chunk of a job description sent to the clean-pass LLM.

    index is the 1-based number the [[uN]] marker and the model's drop
    ranges both key off of. is_para_start marks the first unit of a new
    paragraph (a blank-line-delimited block in the original text) so
    render_units can restore the blank-line break the LLM's paragraph-scoped
    Team-context test relies on, and stitch_units can restore it too when the
    paragraph survives. is_bullet marks a literal bullet/numbered-list line
    (kept on its own line when stitched) as opposed to a sentence carved out
    of a prose run (space-joined with its neighbors when stitched, since the
    de-concatenation fix is what supplied the only whitespace between them).
    """

    index: int
    text: str
    is_para_start: bool
    is_bullet: bool


def _deconcatenate(text: str) -> str:
    """Insert a space where LinkedIn's scrape glued two sentences together.

    Must run before BoundaryDetector.detect() — it changes character offsets,
    so detection has to see the fixed-up text, not the original.
    """
    return _DECONCAT_RE.sub(". ", text)


def _split_prose(text: str, detector: BoundaryDetector) -> list[str]:
    """Split one de-concatenated prose run into its component sentences.

    A run with no detected boundary at all (e.g. a bare heading with no
    terminal punctuation) comes back as a single-element list — that's a
    legitimate one-sentence unit, not an error.
    """
    fixed = _deconcatenate(_protect_abbreviations(text))
    bounds = list(detector.detect(fixed))
    sentences = []
    start = 0
    for end in bounds:
        sentence = _restore_abbreviations(fixed[start:end].strip())
        if sentence:
            sentences.append(sentence)
        start = end
    tail = _restore_abbreviations(fixed[start:].strip())
    if tail:
        sentences.append(tail)
    return sentences


def split_into_units(description_raw: str) -> list[Unit]:
    """Split a raw job description into numbered sentence/bullet units.

    Paragraphs are blank-line-delimited blocks (a description with no blank
    line anywhere — 10/127 in the sampled DB — is simply one paragraph, which
    the LLM's paragraph-scoped Team-context test still applies to correctly).
    Within a paragraph, a line matching the bullet marker regex becomes its
    own unit verbatim; runs of non-bullet lines are joined and fed through
    yasbd (after de-concatenation) to produce sentence units. Returns [] for
    empty/whitespace-only input.
    """
    text = (description_raw or "").strip()
    if not text:
        return []

    detector = BoundaryDetector(lang=_LANG)
    units: list[Unit] = []
    index = 1

    for paragraph in _BLANK_LINE_RE.split(text):
        paragraph = paragraph.strip("\n")
        if not paragraph.strip():
            continue

        para_units: list[tuple[str, bool]] = []  # (text, is_bullet)
        prose_buffer: list[str] = []

        def _flush_prose() -> None:
            if not prose_buffer:
                return
            joined = " ".join(prose_buffer)
            prose_buffer.clear()
            for sentence in _split_prose(joined, detector):
                para_units.append((sentence, False))

        for line in paragraph.split("\n"):
            line = line.strip()
            if not line:
                continue
            if _BULLET_RE.match(line):
                _flush_prose()
                para_units.append((line, True))
            else:
                prose_buffer.append(line)
        _flush_prose()

        for i, (unit_text, is_bullet) in enumerate(para_units):
            units.append(Unit(
                index=index,
                text=unit_text,
                is_para_start=(i == 0),
                is_bullet=is_bullet,
            ))
            index += 1

    return units


def render_units(units: list[Unit]) -> str:
    """Render numbered units into the [[uN]] text the clean prompt expects.

    A blank line precedes the first unit of every paragraph after the first
    (never before unit 1) — paragraph structure is otherwise invisible once
    units are flattened into a single numbered list, and the prompt's
    Team-context test is explicitly paragraph-scoped.
    """
    lines: list[str] = []
    for unit in units:
        if unit.is_para_start and unit.index != 1:
            lines.append("")
        lines.append(f"[[u{unit.index}]] {unit.text}")
    return "\n".join(lines)


_RANGE_RE = re.compile(r"^(\d+)(?:-(\d+))?$")


def parse_drop_response(parsed: dict, unit_count: int) -> set[int] | None:
    """Turn the model's {"drop": [...]} payload into unit indices to remove.

    Returns None only when the top-level shape is unusable ("drop" missing
    or not a list — this also naturally covers _extract_json returning {}
    for a totally unparseable response), signaling the caller should fall
    back to the raw description exactly as on any other clean-call failure.
    Otherwise, returns a (possibly empty) set: each range entry is validated
    independently and a bad one is skipped rather than aborting the whole
    response, matching this pipeline's fall-back-gracefully house style — an
    LLM that got the overall shape right but fumbled one range still cleaned
    everything else correctly. A range overlapping one already accepted is
    skipped (first-seen wins) rather than erroring, since the cost of that
    defensive choice is "kept slightly more text," never a crash.
    """
    if not isinstance(parsed, dict):
        return None
    drop_field = parsed.get("drop")
    if not isinstance(drop_field, list):
        return None

    accepted: set[int] = set()
    for entry in drop_field:
        if not isinstance(entry, dict):
            continue
        raw_range = entry.get("r")
        if not isinstance(raw_range, str):
            continue
        match = _RANGE_RE.match(raw_range.strip())
        if not match:
            continue
        lo = int(match.group(1))
        hi = int(match.group(2)) if match.group(2) else lo
        if lo > hi or lo < 1 or hi > unit_count:
            continue
        candidate = set(range(lo, hi + 1))
        if candidate & accepted:
            continue
        accepted |= candidate

    return accepted


def stitch_units(units: list[Unit], drop: set[int]) -> str:
    """Rebuild cleaned description text from the units the model kept.

    Units are regrouped into paragraphs via is_para_start. A paragraph that
    loses every unit to the drop set contributes nothing — no stray blank
    line is left behind — so two kept paragraphs sandwiching a fully-dropped
    one end up separated by exactly one blank line, not two. Within a
    surviving paragraph, consecutive sentence units are space-joined (they
    had no original whitespace between them; that's exactly what
    _deconcatenate compensated for), while each surviving bullet unit gets
    its own line. Paragraphs are joined with one blank line.
    """
    paragraphs: list[list[Unit]] = []
    for unit in units:
        if unit.is_para_start or not paragraphs:
            paragraphs.append([])
        paragraphs[-1].append(unit)

    out_paragraphs: list[str] = []
    for paragraph in paragraphs:
        survivors = [u for u in paragraph if u.index not in drop]
        if not survivors:
            continue

        rendered_lines: list[str] = []
        prose_buf: list[str] = []
        for unit in survivors:
            if unit.is_bullet:
                if prose_buf:
                    rendered_lines.append(" ".join(prose_buf))
                    prose_buf = []
                rendered_lines.append(unit.text)
            else:
                prose_buf.append(unit.text)
        if prose_buf:
            rendered_lines.append(" ".join(prose_buf))

        out_paragraphs.append("\n".join(rendered_lines))

    return "\n\n".join(out_paragraphs)
