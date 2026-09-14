"""Tests for vesperiki.sections: parse_sections, slugify, replace_section.

Pure unit tests — no DB. Validates the markdown split logic, id
slugification, and the line-splice that preserves heading lines and
body trailing newlines.
"""

from __future__ import annotations

import pytest

from vesperiki import sections


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

def test_slugify_basic() -> None:
    """The canonical example from the spec."""
    assert sections.slugify("Installation Steps") == "installation-steps"


def test_slugify_lowercase() -> None:
    assert sections.slugify("UPPERCASE") == "uppercase"


def test_slugify_strips_specials() -> None:
    """Punctuation becomes nothing; the surrounding spaces collapse."""
    assert sections.slugify("Foo: Bar!") == "foo-bar"


def test_slugify_collapses_runs_of_whitespace() -> None:
    assert sections.slugify("Multiple   Spaces") == "multiple-spaces"


def test_slugify_strips_surrounding_hyphens() -> None:
    assert sections.slugify("---foo---") == "foo"


def test_slugify_keeps_underscore_and_hyphen_inside() -> None:
    assert sections.slugify("foo_bar-baz") == "foo_bar-baz"


# ---------------------------------------------------------------------------
# parse_sections
# ---------------------------------------------------------------------------

def test_parse_sections_intro_plus_three() -> None:
    body = (
        "intro line one\n"
        "intro line two\n"
        "## First\n"
        "content of first\n"
        "## Second\n"
        "content of second\n"
        "## Third\n"
        "content of third\n"
    )
    parsed = sections.parse_sections(body)
    assert len(parsed) == 4
    assert parsed[0] == {
        "id": "intro",
        "heading": None,
        "content": "intro line one\nintro line two",
    }
    assert parsed[1]["id"] == "first"
    assert parsed[1]["heading"] == "First"
    assert parsed[1]["content"] == "content of first"
    assert parsed[2]["id"] == "second"
    assert parsed[2]["heading"] == "Second"
    assert parsed[2]["content"] == "content of second"
    assert parsed[3]["id"] == "third"
    assert parsed[3]["heading"] == "Third"
    # Trailing newline of the body lands in the last section's content.
    assert parsed[3]["content"] == "content of third\n"


def test_parse_sections_no_headings_yields_only_intro() -> None:
    body = "just text\nno sections here\n"
    parsed = sections.parse_sections(body)
    assert parsed == [
        {
            "id": "intro",
            "heading": None,
            "content": "just text\nno sections here\n",
        }
    ]


def test_parse_sections_empty_intro_when_body_starts_with_heading() -> None:
    body = "## Heading\ncontent\n"
    parsed = sections.parse_sections(body)
    assert len(parsed) == 2
    assert parsed[0]["id"] == "intro"
    assert parsed[0]["heading"] is None
    assert parsed[0]["content"] == ""
    assert parsed[1]["id"] == "heading"
    assert parsed[1]["heading"] == "Heading"


def test_parse_sections_keeps_subsections_as_content() -> None:
    """### is not a section delimiter — it stays inside the section body."""
    body = "## Top\n### Subsection\nmore content\n"
    parsed = sections.parse_sections(body)
    assert len(parsed) == 2
    assert parsed[1]["id"] == "top"
    assert parsed[1]["content"] == "### Subsection\nmore content\n"


def test_parse_sections_keeps_h1_as_body() -> None:
    """'# Page Title' is a body line, not a section delimiter."""
    body = "# Page Title\nintro line\n## Heading\nbody\n"
    parsed = sections.parse_sections(body)
    assert len(parsed) == 2
    assert parsed[0]["content"] == "# Page Title\nintro line"
    assert parsed[1]["id"] == "heading"


def test_parse_sections_blank_line_in_section() -> None:
    """A blank line inside a section stays as part of the content."""
    body = "## H\n\nblank line above\n"
    parsed = sections.parse_sections(body)
    assert parsed[1]["content"] == "\nblank line above\n"


# ---------------------------------------------------------------------------
# replace_section
# ---------------------------------------------------------------------------

def test_replace_section_middle_preserves_a_and_c() -> None:
    body = (
        "intro\n"
        "## A\n"
        "content of A\n"
        "## B\n"
        "content of B\n"
        "## C\n"
        "content of C\n"
    )
    new = sections.replace_section(body, "b", "new B")
    assert new == (
        "intro\n"
        "## A\n"
        "content of A\n"
        "## B\n"
        "new B\n"
        "## C\n"
        "content of C\n"
    )


def test_replace_section_missing_raises_keyerror() -> None:
    body = "## A\ncontent\n"
    with pytest.raises(KeyError):
        sections.replace_section(body, "nonexistent", "x")


def test_replace_section_first_match_wins_for_duplicate_headings() -> None:
    body = "## Dup\nfirst\n## Dup\nsecond\n"
    new = sections.replace_section(body, "dup", "replacement")
    assert new == "## Dup\nreplacement\n## Dup\nsecond\n"
    # The second Dup's content remains untouched.
    assert "second" in new
    assert "first" not in new


def test_replace_section_intro() -> None:
    body = "old intro\n## H\ncontent\n"
    new = sections.replace_section(body, "intro", "new intro")
    assert new == "new intro\n## H\ncontent\n"


def test_replace_section_adds_trailing_newline_for_clean_boundary() -> None:
    """Without a trailing newline on new_content, one is appended."""
    body = "## H\nold\n"
    new = sections.replace_section(body, "h", "new")
    assert new == "## H\nnew\n"


def test_replace_section_preserves_existing_trailing_newline() -> None:
    """A body without a trailing newline stays without one."""
    body = "## H\nold"
    new = sections.replace_section(body, "h", "new")
    assert new == "## H\nnew"


def test_replace_section_preserves_other_sections_blank_lines() -> None:
    """Blank lines in untouched sections remain intact."""
    body = "## A\n\nblank in A\n## B\ncontent B\n"
    new = sections.replace_section(body, "b", "fresh B")
    assert "## A\n\nblank in A" in new
    assert new.endswith("## B\nfresh B\n")


def test_replace_section_round_trip() -> None:
    """Parsing a body, then patching each section with its original content,
    returns the original body (heading lines preserved verbatim)."""
    body = (
        "intro line\n"
        "## One\n"
        "content one\n"
        "## Two\n"
        "content two\n"
    )
    parsed = sections.parse_sections(body)
    out = body
    for section in parsed:
        out = sections.replace_section(out, section["id"], section["content"])
    assert out == body


def test_replace_section_no_intro() -> None:
    """A body that begins directly with a heading has no intro to preserve."""
    body = "## H\ncontent\n"
    new = sections.replace_section(body, "h", "fresh")
    assert new == "## H\nfresh\n"


def test_replace_section_strips_leading_heading_line() -> None:
    """new_content starting with the heading line must not duplicate it.

    Without the strip, splicing "## Overview\nnew content" after the
    preserved "## Overview" heading line would emit two consecutive
    "## Overview" lines. The intro section (heading=None) must keep
    verbatim content even when it starts with "## ".
    """
    body = (
        "## Overview\n"
        "old content\n"
        "## Details\n"
        "stuff\n"
    )
    new = sections.replace_section(body, "overview", "## Overview\nnew content")
    # Exactly one "## Overview" line, the new body text present, the
    # following section intact.
    assert new.count("## Overview") == 1
    assert "new content" in new
    assert "## Details\nstuff" in new
    assert "old content" not in new


def test_replace_section_strips_bare_heading_only() -> None:
    """new_content that is exactly the heading line is treated as empty."""
    body = "## Overview\nold\n## Details\nstuff\n"
    new = sections.replace_section(body, "overview", "## Overview")
    assert new.count("## Overview") == 1
    assert "## Overview\n## Details" in new
    assert "old" not in new


def test_replace_section_without_heading_unchanged() -> None:
    """new_content without a leading heading line behaves exactly as before."""
    body = "## Overview\nold\n## Details\nstuff\n"
    new = sections.replace_section(body, "overview", "new content")
    assert new == "## Overview\nnew content\n## Details\nstuff\n"
