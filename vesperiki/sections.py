"""Section-level patching for Vesperiki page bodies.

Lets a caller replace the content of a single ``## Heading`` section of a
page without rewriting the entire body. Section ids are the
``slugify``-normalised heading text; the leading intro (everything before
the first ``## `` heading) is section ``"intro"`` with ``heading=None``.

Heading discrimination rules — ``## `` only:
  - ``# Title``         — page title, body line, not a section
  - ``## Section``      — a section
  - ``### Subsection``  — body line belonging to the enclosing section
"""

from __future__ import annotations

import re

#: Pattern that marks a line as a section heading.
_HEADING_PREFIX = "## "

#: Slugification: collapse anything that isn't a-z, 0-9, space, hyphen,
#: or underscore into nothing; then turn runs of whitespace into a single
#: hyphen and strip surrounding hyphens/underscores.
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9\s_-]+")
_SLUG_SPACE_RE = re.compile(r"\s+")


def slugify(heading: str) -> str:
    """Normalize a heading text into a section id.

    ``"Installation Steps"`` -> ``"installation-steps"``. ``"Foo: Bar!"``
    -> ``"foo-bar"``. Surrounding hyphens and underscores are stripped;
    runs of whitespace collapse to a single hyphen.
    """
    s = heading.lower()
    s = _SLUG_STRIP_RE.sub("", s)
    s = _SLUG_SPACE_RE.sub("-", s)
    s = s.strip("-_")
    return s


def _section_line_ranges(lines: list[str]) -> list[tuple[int, int, str | None]]:
    """Return ``[(start_line_idx, end_line_idx, heading_or_None), ...]``.

    The first entry is always the intro range with ``heading=None``;
    end is the line index of the first ``## `` heading (exclusive),
    or ``len(lines)`` if no headings exist. Each subsequent entry holds
    the lines that belong to one section: end is exclusive, pointing at
    the next heading line (or end of body).
    """
    n = len(lines)
    first_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.startswith(_HEADING_PREFIX):
            first_idx = idx
            break

    if first_idx is None:
        return [(0, n, None)]

    ranges: list[tuple[int, int, str | None]] = [(0, first_idx, None)]
    i = first_idx
    while i < n:
        heading = lines[i][len(_HEADING_PREFIX):].strip()
        j = i + 1
        while j < n and not lines[j].startswith(_HEADING_PREFIX):
            j += 1
        ranges.append((i + 1, j, heading))
        i = j
    return ranges


def parse_sections(body: str) -> list[dict]:
    """Split a markdown body by ``## `` headings.

    Returns a list of ``{"id": str, "heading": str | None, "content": str}``.
    The intro (text before the first ``## ``) is the first entry with
    ``heading=None`` and ``id="intro"``. Only ``## `` delimits sections —
    ``# `` page titles and ``### `` subsections are kept verbatim inside
    the surrounding section's content.
    """
    lines = body.split("\n")
    ranges = _section_line_ranges(lines)
    sections: list[dict] = []
    for start, end, heading in ranges:
        sections.append(
            {
                "id": "intro" if heading is None else slugify(heading),
                "heading": heading,
                "content": "\n".join(lines[start:end]),
            }
        )
    return sections


def replace_section(body: str, section_id: str, new_content: str) -> str:
    """Return a new body with the matching section's content replaced.

    The section's original heading line is preserved verbatim; only the
    content between the heading and the next heading (or end of body) is
    swapped. If two sections share the same id, the first match is
    patched. Raises ``KeyError`` if no section has ``section_id``.

    If ``new_content`` itself begins with the matching ``## Heading``
    line (with or without a trailing newline), that line is stripped
    before splicing. This lets callers paste a full section — heading
    included — without producing a duplicate heading in the output. The
    intro section (``heading=None``) never matches a heading line.

    ``new_content`` is written verbatim otherwise. A trailing newline is
    appended if missing so the boundary with the next section (or EOF)
    stays clean. Trailing newlines on the body itself are preserved.
    """
    lines = body.split("\n")
    ranges = _section_line_ranges(lines)

    target: tuple[int, int, str | None] | None = None
    for start, end, heading in ranges:
        sid = "intro" if heading is None else slugify(heading)
        if sid == section_id:
            target = (start, end, heading)
            break
    if target is None:
        raise KeyError(f"section {section_id!r} not found")

    start, end, heading = target

    # Strip a leading "## Heading" line from new_content when it matches
    # the target section's heading. The heading is preserved by the splice
    # below, so a caller-supplied duplicate would render as
    # "## Heading\n## Heading\n...".
    if heading is not None and new_content:
        prefix = "## " + heading
        if new_content.startswith(prefix):
            rest = new_content[len(prefix):]
            if rest.startswith("\n"):
                new_content = rest[1:]
            elif rest == "":
                new_content = ""
            # Otherwise the match is mid-string; leave new_content alone.

    if new_content and not new_content.endswith("\n"):
        new_content = new_content + "\n"
    # Strip the trailing "" produced by splitting content that ends in \n
    # so we don't add an empty line at the splice site.
    new_lines = new_content.split("\n")
    if new_lines and new_lines[-1] == "":
        new_lines = new_lines[:-1]

    rebuilt = lines[:start] + new_lines + lines[end:]
    # Preserve a trailing newline that existed in the original body.
    if body.endswith("\n") and (not rebuilt or rebuilt[-1] != ""):
        rebuilt.append("")
    return "\n".join(rebuilt)
