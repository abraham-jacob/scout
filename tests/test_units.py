"""Tests for agent/units.py — sentence/bullet unit splitting and stitching."""

from agent.units import (
    Unit,
    parse_drop_response,
    render_units,
    split_into_units,
    stitch_units,
)

# The real concatenated-sentence pattern found in 74% of stored
# description_raw values — no space, no bullet char, no newline between
# LinkedIn's collapsed list items.
CONCATENATED_EXAMPLE = (
    "Architect distributed systems on GCP for high-volume, high-sensitivity "
    "data.Build well-structured APIs and services in Python to power AI "
    "applications and frontends.Own schema and data modeling for long-term "
    "growth and flexibility."
)


class TestSplitIntoUnits:
    """Test split_into_units against real-world description shapes."""

    def test_empty_input_returns_no_units(self):
        """Empty or whitespace-only text produces an empty unit list."""
        assert split_into_units("") == []
        assert split_into_units("   \n  ") == []

    def test_concatenated_sentences_are_split_apart(self):
        """LinkedIn's run-on concatenated list items split into 3 sentences."""
        units = split_into_units(CONCATENATED_EXAMPLE)
        assert len(units) == 3
        assert units[0].text.startswith("Architect distributed systems")
        assert units[1].text.startswith("Build well-structured APIs")
        assert units[2].text.startswith("Own schema and data modeling")
        assert all(not u.is_bullet for u in units)

    def test_indices_are_sequential_starting_at_one(self):
        """Unit indices are 1-based and sequential across the whole document."""
        units = split_into_units(CONCATENATED_EXAMPLE)
        assert [u.index for u in units] == [1, 2, 3]

    def test_blank_line_marks_a_new_paragraph(self):
        """A blank-line break starts a new paragraph on the first unit after it."""
        text = "First paragraph sentence.\n\nSecond paragraph sentence."
        units = split_into_units(text)
        assert len(units) == 2
        assert units[0].is_para_start is True
        assert units[1].is_para_start is True

    def test_single_newline_only_is_one_paragraph(self):
        """No blank line anywhere means everything is one paragraph."""
        text = "Heading line\nBody sentence one. Body sentence two."
        units = split_into_units(text)
        assert units[0].is_para_start is True
        assert all(not u.is_para_start for u in units[1:])

    def test_bullet_markers_become_their_own_verbatim_unit(self):
        """Each common bullet-marker variant is captured as one bullet unit."""
        text = (
            "Responsibilities:\n"
            "• Design backend services\n"
            "- Collaborate with data science\n"
            "* Ship weekly\n"
            "1. Own the roadmap\n"
            "a) Mentor engineers"
        )
        units = split_into_units(text)
        bullets = [u for u in units if u.is_bullet]
        assert len(bullets) == 5
        assert bullets[0].text == "• Design backend services"
        assert bullets[1].text == "- Collaborate with data science"
        assert bullets[4].text == "a) Mentor engineers"

    def test_inline_bullet_char_is_not_treated_as_a_list_marker(self):
        """A '•' used mid-line as a separator stays one prose unit."""
        text = "$150K - $310K • Offers Equity • Offers Bonus"
        units = split_into_units(text)
        assert len(units) == 1
        assert not units[0].is_bullet

    def test_heading_with_no_terminal_punctuation_is_one_unit(self):
        """A bare heading with no sentence-ending punctuation is one unit."""
        units = split_into_units("Benefits")
        assert len(units) == 1
        assert units[0].text == "Benefits"

    def test_common_abbreviations_do_not_create_false_sentence_boundaries(self):
        """"U.S." and friends don't get mistaken for a sentence end.

        Confirmed directly against production data: yasbd otherwise splits
        "outside the U.S. receive" into two sentences at the abbreviation,
        occasionally leaving a stray one-word "U.S." unit behind once its
        neighbors are dropped.
        """
        text = "Full-time employees outside the U.S. receive full benefits."
        units = split_into_units(text)
        assert len(units) == 1
        assert units[0].text == text

    def test_abbreviation_heading_stays_intact(self):
        """"U.S. Benefits." (a real section label) is not split at "U.S."."""
        units = split_into_units("U.S. Benefits. Full-time employees receive medical coverage.")
        assert units[0].text == "U.S. Benefits."
        assert units[1].text == "Full-time employees receive medical coverage."


class TestRenderUnits:
    """Test the [[uN]] rendering format sent to the LLM."""

    def test_blank_line_precedes_later_paragraph_starts_only(self):
        """A blank line appears before a later paragraph's first unit, not unit 1."""
        units = [
            Unit(index=1, text="First.", is_para_start=True, is_bullet=False),
            Unit(index=2, text="Second.", is_para_start=False, is_bullet=False),
            Unit(index=3, text="Third.", is_para_start=True, is_bullet=False),
        ]
        rendered = render_units(units)
        lines = rendered.split("\n")
        assert lines == [
            "[[u1]] First.",
            "[[u2]] Second.",
            "",
            "[[u3]] Third.",
        ]

    def test_single_unit_has_no_leading_or_trailing_blank_line(self):
        """A single-unit document renders with no stray blank lines."""
        units = [Unit(index=1, text="Only.", is_para_start=True, is_bullet=False)]
        assert render_units(units) == "[[u1]] Only."


class TestParseDropResponse:
    """Test parsing the model's {"drop": [...]} response shape."""

    def test_well_formed_multi_range_response(self):
        """Multiple valid ranges expand into the correct index set."""
        parsed = {"drop": [{"r": "3-5", "c": "culture"}, {"r": "9", "c": "benefits"}]}
        assert parse_drop_response(parsed, unit_count=10) == {3, 4, 5, 9}

    def test_empty_drop_list_is_a_success_not_a_failure(self):
        """{"drop": []} means nothing to remove — a valid empty set, not None."""
        assert parse_drop_response({"drop": []}, unit_count=10) == set()

    def test_overlapping_range_is_skipped_first_seen_wins(self):
        """A later range overlapping an already-accepted one is dropped."""
        parsed = {"drop": [{"r": "1-5", "c": "culture"}, {"r": "4-6", "c": "benefits"}]}
        assert parse_drop_response(parsed, unit_count=10) == {1, 2, 3, 4, 5}

    def test_out_of_range_entry_is_skipped(self):
        """A range exceeding unit_count or below 1 is skipped, not fatal."""
        parsed = {"drop": [{"r": "8-20", "c": "eeo"}, {"r": "2", "c": "benefits"}]}
        assert parse_drop_response(parsed, unit_count=10) == {2}

    def test_inverted_range_is_skipped(self):
        """A range where lo > hi is skipped."""
        parsed = {"drop": [{"r": "9-3", "c": "eeo"}]}
        assert parse_drop_response(parsed, unit_count=10) == set()

    def test_malformed_range_string_is_skipped(self):
        """A non-numeric or missing 'r' value is skipped, not fatal."""
        parsed = {"drop": [{"r": "abc"}, {"c": "eeo"}, {"r": "2"}]}
        assert parse_drop_response(parsed, unit_count=10) == {2}

    def test_wrong_top_level_shape_returns_none(self):
        """A response with no usable 'drop' list signals total fallback."""
        assert parse_drop_response({}, unit_count=10) is None
        assert parse_drop_response({"drop": "oops"}, unit_count=10) is None
        assert parse_drop_response({"other": []}, unit_count=10) is None
        assert parse_drop_response("not a dict", unit_count=10) is None


class TestStitchUnits:
    """Test rebuilding cleaned text from surviving units."""

    def test_no_drops_reconstructs_all_content(self):
        """With nothing dropped, all unit text survives."""
        units = split_into_units(CONCATENATED_EXAMPLE)
        result = stitch_units(units, drop=set())
        assert "Architect distributed systems" in result
        assert "Build well-structured APIs" in result
        assert "Own schema and data modeling" in result

    def test_fully_dropped_middle_paragraph_leaves_one_blank_line(self):
        """Dropping an entire middle paragraph leaves exactly one blank line."""
        text = "Para one sentence.\n\nPara two sentence.\n\nPara three sentence."
        units = split_into_units(text)
        # unit index 2 is the sole unit of the middle paragraph
        middle_index = units[1].index
        result = stitch_units(units, drop={middle_index})
        assert result == "Para one sentence.\n\nPara three sentence."

    def test_dropping_leading_paragraph_leaves_no_stray_blank_line(self):
        """Dropping the first paragraph doesn't leave a leading blank line."""
        text = "Drop me.\n\nKeep me."
        units = split_into_units(text)
        result = stitch_units(units, drop={units[0].index})
        assert result == "Keep me."

    def test_dropped_bullet_leaves_no_stray_whitespace(self):
        """Dropping a bullet next to surviving sentences leaves clean output."""
        text = "Responsibilities:\n• Keep this bullet\n• Drop this bullet"
        units = split_into_units(text)
        drop_index = next(u.index for u in units if u.text == "• Drop this bullet")
        result = stitch_units(units, drop={drop_index})
        assert result == "Responsibilities:\n• Keep this bullet"

    def test_round_trip_with_no_drops_matches_deconcatenated_content(self):
        """Stitching everything back together preserves all sentence content."""
        units = split_into_units(CONCATENATED_EXAMPLE)
        result = stitch_units(units, drop=set())
        assert result == (
            "Architect distributed systems on GCP for high-volume, "
            "high-sensitivity data. Build well-structured APIs and services "
            "in Python to power AI applications and frontends. Own schema "
            "and data modeling for long-term growth and flexibility."
        )
