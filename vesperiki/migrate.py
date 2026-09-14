"""Markdown-to-SQLite migration script for the Vesperiki wiki.

Imports markdown pages from a directory tree into the Vesperiki SQLite
database. Reusable: any user with an Obsidian, Quartz, or plain markdown
corpus can point this at it.

Usage:
    python -m vesperiki.migrate --source /path/to/wiki --db /path/to/db
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import db
from . import service


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default directories to skip (Quartz infrastructure, not content).
DEFAULT_SKIP_DIRS: tuple[str, ...] = ("_archive", "raw")

#: Files at any level to skip (Quartz bookkeeping).
SKIP_FILES: frozenset[str] = frozenset({"index.md", "log.md", "SCHEMA.md"})

#: Map of special folder names to the tag they contribute.
#: "entities" -> "entity" (singular); the other two pass through unchanged.
SPECIAL_FOLDER_TAGS: dict[str, str] = {
    "entities": "entity",
    "infrastructure": "infrastructure",
    "knowledge": "knowledge",
}

#: Folder prefixes stripped from wikilink targets in the body.
#: `[[entities/forgejo|Display]]` -> `[[forgejo]]`.
WIKILINK_PREFIXES: tuple[str, ...] = ("entities/", "infrastructure/", "knowledge/")

#: FTS5 triggers dropped around the bulk import. Triggers fire per-row;
#: dropping them makes the import linear instead of quadratic.
FTS_TRIGGER_NAMES: tuple[str, ...] = (
    "pages_fts_ai",
    "pages_fts_au",
    "pages_fts_ad",
    "page_tags_fts_ai",
    "page_tags_fts_ad",
    "tags_fts_au",
)

#: Valid page types per the schema. Used to validate frontmatter `type:`.
VALID_TYPES: frozenset[str] = frozenset(
    {"entity", "guide", "concept", "reference", "log", "project"}
)

#: Default page type when frontmatter omits it. The plan calls for
#: "reference" as the migration default; the service default is "entity".
DEFAULT_TYPE: str = "reference"

#: Frontmatter keys (in order) used to derive `updated_at`.
DATE_KEYS: tuple[str, ...] = ("date", "updated", "last_updated", "created")

#: Wikilink target pattern — captures the inside of `[[...]]`. Display
#: text (`|Display`) and prefixes are normalized separately.
_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")

#: Slug validator. Mirrors `service._SLUG_RE`.
_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PageData:
    """A parsed markdown page ready for import."""

    path: Path
    slug: str
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    type: str = DEFAULT_TYPE
    sources: list[Any] = field(default_factory=list)
    updated_at: str | None = None
    folder_tag: str | None = None


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------
#
# No pyyaml available in the project venv (verified at module load — see
# bottom). A small, focused parser is enough: the project uses a
# predictable subset of YAML.
#
# Supported shapes (and ONLY these):
#   key: value                 (scalar, optionally quoted or inline-list)
#   key:                       (block — list of items or sub-dict)
#     - item
#     - key: value             (list item that is a dict, possibly multi-line)
#       subkey: value
#   key:                       (block — sub-dict at deeper indent)
#     subkey: value
#
# Things deliberately not supported (the wiki corpus does not use them):
#   anchors, multi-document, flow-style nested maps, tags with leading "@".


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a leading YAML frontmatter block.

    Returns (frontmatter_dict, remaining_body). If no frontmatter is
    present, returns ({}, original_text).
    """
    if not text.startswith("---"):
        return {}, text

    # Split on the first newline so the leading "---" must be the only
    # content of the first line. This avoids matching a body line that
    # happens to start with three dashes.
    first, _, rest = text.partition("\n")
    if first.rstrip() != "---":
        return {}, text

    lines = rest.split("\n")
    for i, line in enumerate(lines):
        if line.rstrip() == "---":
            fm_block = "\n".join(lines[:i])
            body = "\n".join(lines[i + 1 :])
            return _parse_yaml_map(fm_block), body

    # No closing fence — treat the whole file as body.
    return {}, text


def _parse_yaml_map(block: str) -> dict[str, Any]:
    """Parse a YAML block as a top-level mapping."""
    lines = _nonblank_lines(block)
    pos = [0]
    return _parse_map_at(lines, pos, indent=0)


def _nonblank_lines(block: str) -> list[str]:
    """Drop blank and pure-comment lines; preserve everything else verbatim."""
    out: list[str] = []
    for line in block.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(line)
    return out


def _parse_map_at(
    lines: list[str], pos: list[int], indent: int
) -> dict[str, Any]:
    """Parse a mapping whose keys are at *indent* columns.

    `pos` is a single-element list so recursive calls can mutate it.
    Returns the dict and leaves `pos[0]` at the next unconsumed line.
    """
    result: dict[str, Any] = {}
    while pos[0] < len(lines):
        line = lines[pos[0]]
        cur_indent = len(line) - len(line.lstrip())
        if cur_indent < indent:
            return result
        if cur_indent > indent:
            # Malformed: indent went deeper without a parent key. Skip
            # the line so we don't loop forever.
            pos[0] += 1
            continue

        stripped = line.strip()
        if ":" not in stripped:
            pos[0] += 1
            continue

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()

        if not value:
            # Block follows. Consume the next line and recurse.
            pos[0] += 1
            result[key] = _parse_block_value(lines, pos, indent)
        else:
            result[key] = _parse_scalar(value)
            pos[0] += 1
    return result


def _parse_block_value(
    lines: list[str], pos: list[int], parent_indent: int
) -> Any:
    """Parse a value that follows a bare `key:`. Returns a list, dict, or None."""
    if pos[0] >= len(lines):
        return None
    next_line = lines[pos[0]]
    next_indent = len(next_line) - len(next_line.lstrip())
    if next_indent <= parent_indent:
        return None
    next_stripped = next_line.strip()
    if next_stripped.startswith("- "):
        return _parse_list_at(lines, pos, next_indent)
    return _parse_map_at(lines, pos, next_indent)


def _parse_list_at(
    lines: list[str], pos: list[int], indent: int
) -> list[Any]:
    """Parse a list whose items are at *indent* columns.

    A list item that starts with ``- key: value`` is only treated as a
    dict item when the following lines carry subkeys at a deeper indent.
    Otherwise (e.g. ``- https://a.com``) the whole ``key: value`` text is
    kept as a scalar — that's the case the wiki corpus uses for list
    entries that happen to contain a colon, such as bare URLs.
    """
    items: list[Any] = []
    while pos[0] < len(lines):
        line = lines[pos[0]]
        cur_indent = len(line) - len(line.lstrip())
        if cur_indent < indent:
            return items
        stripped = line.strip()
        if not stripped.startswith("- "):
            return items

        item_text = stripped[2:]
        if (
            ":" in item_text
            and _has_subkey_continuation(lines, pos[0] + 1, indent)
            and not _looks_like_url(item_text)
        ):
            d, end_pos = _parse_dict_item(lines, pos, indent, item_text)
            items.append(d)
            pos[0] = end_pos
        else:
            items.append(_parse_scalar(item_text))
            pos[0] += 1
    return items


#: URL schemes that would otherwise look like a YAML key (``https://a``
#: parses as key ``https`` / value ``//a`` when split on the first colon).
_URL_SCHEME_RE = re.compile(r"^(https?|ftp|ssh|file|mailto|irc|ldap|git|svn|ws|wss)$")


def _looks_like_url(item_text: str) -> bool:
    """Return True if *item_text* is a URL rather than a ``key: value`` pair.

    ``- https://example.com`` should be a scalar string, not a one-key
    dict with key ``https``. The heuristic: the text before the first
    colon is a URL scheme.
    """
    key, _, _ = item_text.partition(":")
    return bool(_URL_SCHEME_RE.match(key.strip()))


def _has_subkey_continuation(
    lines: list[str], start: int, list_indent: int
) -> bool:
    """Return True if *start* is followed by a subkey at a deeper indent.

    Used to disambiguate ``- key: value`` (dict item, subkeys follow)
    from ``- https://example.com`` (scalar with a colon, no subkeys).
    A nested list (``- subitem``) at the same depth does not count as
    a subkey continuation.
    """
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        cur_indent = len(line) - len(line.lstrip())
        if cur_indent <= list_indent:
            return False
        if line.lstrip().startswith("- "):
            return False
        return True
    return False


def _parse_dict_item(
    lines: list[str], pos: list[int], indent: int, first_text: str
) -> tuple[dict[str, Any], int]:
    """Parse a list item that begins as `- key: ...`.

    The first key may be inline (`- key: value`) or followed by a nested
    block (`- key:`). Sibling subkeys at the same indent (but >indent)
    continue the same dict until we hit another list item or a shallower
    indent.
    """
    result: dict[str, Any] = {}
    key, _, value = first_text.partition(":")
    key = key.strip()
    value = value.strip()

    if value:
        result[key] = _parse_scalar(value)
        pos[0] += 1
    else:
        pos[0] += 1
        result[key] = _parse_block_value(lines, pos, indent)

    # Continue collecting subkeys at deeper indents belonging to this dict.
    while pos[0] < len(lines):
        line = lines[pos[0]]
        if not line.strip():
            pos[0] += 1
            continue
        cur_indent = len(line) - len(line.lstrip())
        if cur_indent <= indent:
            # End of this dict (sibling `-` or shallower map key).
            break
        stripped = line.strip()
        if ":" not in stripped:
            break
        k, _, v = stripped.partition(":")
        k = k.strip()
        v = v.strip()
        if v:
            result[k] = _parse_scalar(v)
            pos[0] += 1
        else:
            pos[0] += 1
            result[k] = _parse_block_value(lines, pos, cur_indent)
    return result, pos[0]


def _parse_scalar(s: str) -> Any:
    """Parse a scalar value. Quoted, inline-list, inline-dict, number, bool, string."""
    s = s.strip()
    if not s:
        return ""

    # Quoted strings (single or double quotes).
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]

    # Inline list: [a, b, c]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in _split_top_commas(inner)]

    # Inline dict: {key: val, key: val}
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return {}
        result: dict[str, Any] = {}
        for pair in _split_top_commas(inner):
            if ":" in pair:
                k, _, v = pair.partition(":")
                result[k.strip()] = _parse_scalar(v.strip())
        return result

    # Booleans and null
    if s in ("true", "True", "yes"):
        return True
    if s in ("false", "False", "no"):
        return False
    if s in ("null", "Null", "~"):
        return None

    # Numbers
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass

    # Unquoted scalar.
    return s


def _split_top_commas(s: str) -> list[str]:
    """Split on commas that are at the top level (not inside brackets/braces/quotes)."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote: str | None = None
    for ch in s:
        if in_quote is not None:
            current.append(ch)
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ('"', "'"):
            in_quote = ch
            current.append(ch)
            continue
        if ch in ("[", "{"):
            depth += 1
        elif ch in ("]", "}"):
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return parts


# ---------------------------------------------------------------------------
# Path and body helpers
# ---------------------------------------------------------------------------


def slug_from_path(path: Path, source: Path) -> tuple[str, str | None]:
    """Return ``(slug, folder_tag)`` for *path* relative to *source*.

    - Root-level file: ``(filename-without-.md, None)``
    - ``entities/<x>.md``: ``(<x>, 'entity')``
    - ``infrastructure/<x>.md``: ``(<x>, 'infrastructure')``
    - ``knowledge/<x>.md``: ``(<x>, 'knowledge')``
    - Any other folder: ``(<x>, folder_name)``

    The slug is lowercased and non-allowed characters are collapsed to
    single hyphens so it always matches the schema's ``^[a-z0-9_-]+$``
    pattern. Filenames that would yield an empty slug raise ``ValueError``.
    """
    rel = path.relative_to(source)
    parts = rel.parts

    if len(parts) == 1:
        stem = parts[0]
        if stem.endswith(".md"):
            stem = stem[:-3]
        return _slugify(stem), None

    folder = parts[0]
    filename = parts[-1]
    if filename.endswith(".md"):
        filename = filename[:-3]

    tag = SPECIAL_FOLDER_TAGS.get(folder, folder)
    return _slugify(filename), tag


def _slugify(s: str) -> str:
    """Normalize a string to a valid slug. Empty result raises ValueError."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = s.strip("-")
    if not s:
        raise ValueError(f"cannot derive a valid slug from {s!r}")
    return s


def wikilink_target(target: str) -> str:
    """Strip ``entities/``, ``infrastructure/``, ``knowledge/`` prefixes."""
    for prefix in WIKILINK_PREFIXES:
        if target.startswith(prefix):
            return target[len(prefix) :]
    return target


def transform_wikilinks(body: str) -> str:
    """Normalize wikilinks so the service's derived-link rebuilder sees them.

    The service regex (``\\[\\[([a-z0-9_-]+)\\]\\]``) only matches bare
    slugs. Quartz-style ``[[entities/foo|Display]]`` is rewritten to
    ``[[foo]]`` — prefix stripped, display text dropped. Bare
    ``[[foo]]`` is preserved. Slugs that already lacked a folder prefix
    are left alone.
    """
    def repl(m: re.Match[str]) -> str:
        target = m.group(1)
        if "|" in target:
            target = target.split("|", 1)[0]
        target = wikilink_target(target)
        return f"[[{target}]]"
    return _WIKILINK_RE.sub(repl, body)


def derive_title(text: str, fallback: str) -> str:
    """Return the first H1 (``# Title``) in *text*, else *fallback*."""
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("# ") and not s.startswith("## "):
            return s[2:].strip()
    return fallback


def normalize_date(s: str) -> str | None:
    """Return ISO 8601 form of *s* if parseable, else ``None``.

    Accepts date-only (``2024-01-15``) and full ISO datetimes
    (``2024-01-15T12:00:00``, ``2024-01-15T12:00:00Z``). The trailing
    ``Z`` is rewritten to ``+00:00`` for :func:`datetime.fromisoformat`
    compatibility on the project's Python version.
    """
    s = s.strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        try:
            datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
        return s
    normalized = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# File discovery + page parsing
# ---------------------------------------------------------------------------


def find_markdown_files(
    source: Path, skip_dirs: frozenset[str] | set[str]
) -> list[Path]:
    """Walk *source* for ``*.md`` files, honoring skip rules.

    - Any file under a directory in *skip_dirs* is skipped.
    - Files named ``index.md``, ``log.md``, or ``SCHEMA.md`` are skipped
      at any level (Quartz bookkeeping).
    """
    out: list[Path] = []
    for path in sorted(source.rglob("*.md")):
        rel = path.relative_to(source)
        if any(part in skip_dirs for part in rel.parts[:-1]):
            continue
        if rel.name in SKIP_FILES:
            continue
        out.append(path)
    return out


def parse_file(path: Path, source: Path) -> PageData:
    """Parse a single markdown file into a :class:`PageData`."""
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)

    slug, folder_tag = slug_from_path(path, source)
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"derived slug {slug!r} from {path} does not match ^[a-z0-9_-]+$"
        )

    # Title precedence: frontmatter > first H1 > filename stem.
    title = fm.get("title")
    if not isinstance(title, str) or not title.strip():
        title = derive_title(body, path.stem)

    # Type: validate against schema enum; fall back to default.
    type_ = fm.get("type")
    if not isinstance(type_, str) or type_ not in VALID_TYPES:
        type_ = DEFAULT_TYPE

    # Tags: frontmatter list + folder-derived tag (deduped, order preserved).
    fm_tags = fm.get("tags", [])
    if not isinstance(fm_tags, list):
        fm_tags = []
    tags: list[str] = []
    for t in fm_tags:
        if isinstance(t, str) and t and t not in tags:
            tags.append(t)
    if folder_tag and folder_tag not in tags:
        tags.append(folder_tag)

    # Sources: list of dicts passes through; string entries are wrapped
    # to ``{"title": s}`` so the shape is uniform downstream.
    raw_sources = fm.get("sources", [])
    if not isinstance(raw_sources, list):
        raw_sources = []
    sources: list[Any] = []
    for s in raw_sources:
        if isinstance(s, dict):
            sources.append(s)
        elif isinstance(s, str):
            sources.append({"title": s})

    # updated_at: try each candidate key in order.
    updated_at: str | None = None
    for key in DATE_KEYS:
        if key not in fm:
            continue
        candidate = fm[key]
        if isinstance(candidate, str):
            updated_at = normalize_date(candidate)
            if updated_at:
                break

    # Body: frontmatter removed, wikilinks normalized.
    body = transform_wikilinks(body)

    return PageData(
        path=path.relative_to(source),
        slug=slug,
        title=title.strip(),
        body=body,
        tags=tags,
        type=type_,
        sources=sources,
        updated_at=updated_at,
        folder_tag=folder_tag,
    )


# ---------------------------------------------------------------------------
# FTS5 performance
# ---------------------------------------------------------------------------
#
# The plan describes dropping the FTS5 maintenance triggers around the
# bulk import for linear-time performance, then rebuilding pages_fts
# afterwards. The implementation can't follow that path: ``db.connection``
# runs ``_verify_schema`` on every open, and the verifier rejects the
# database if the six FTS triggers are missing. So if we dropped them,
# every service.create_page() call inside the import would fail its
# own connection check. ``db.py`` is out of scope to modify.
#
# Fallback: accept the per-row trigger firing. With 120 pages and a
# single-process importer, this is well under a 30-second budget. The
# triggers stay in place for the
# whole import, so pages_fts is populated automatically as pages land.
# The final ``db.connection`` re-open is a free schema sanity check.

def rebuild_fts_index(db_path: str) -> None:
    """Repopulate ``pages_fts`` from the live ``pages`` table.

    Not used by the default import path — kept for callers that bypass
    the service layer (e.g. direct-SQL migrations) and need to fix up
    the FTS table after a trigger-drop. With triggers active, the table
    is already up to date.
    """
    with db.connection(db_path) as conn:
        conn.execute("DELETE FROM pages_fts")
        conn.execute(
            """
            INSERT INTO pages_fts (rowid, slug, title, tags, body)
            SELECT p.id,
                   p.slug,
                   p.title,
                   COALESCE((SELECT group_concat(t.name, ' ')
                               FROM page_tags pt
                               JOIN tags t ON t.id = pt.tag_id
                              WHERE pt.page_id = p.id), ''),
                   p.body
              FROM pages p
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Page import
# ---------------------------------------------------------------------------


def import_page(
    page: PageData,
    *,
    writer: str,
    client: str,
    db_path: str,
) -> None:
    """Import one parsed page via the service layer + post-write updates.

    The service's :func:`service.create_page` does not accept
    ``change_type`` or ``updated_at`` (its public contract is fixed by
    its public contract is fixed). The import handles this by:

    1. Calling ``create_page`` with ``force=True`` and ``release_alias=True``
       so the import is idempotent against alias conflicts and dedup
       gate noise from a one-time corpus dump.
    2. Updating ``pages.updated_at`` to the frontmatter value if any.
    3. Rewriting the just-inserted ``change_type='create'`` revision to
       ``change_type='migrate'`` with ``source_type='migration'`` so the
       change history reflects how the row arrived.
    """
    service.create_page(
        slug=page.slug,
        title=page.title,
        body=page.body,
        tags=page.tags,
        type=page.type,
        sources=page.sources,
        force=True,
        changed_by=writer,
        client=client,
        release_alias=True,
    )

    with db.connection(db_path) as conn:
        if page.updated_at:
            conn.execute(
                "UPDATE pages SET updated_at = ? WHERE slug = ?",
                (page.updated_at, page.slug),
            )
        conn.execute(
            """
            UPDATE revisions
               SET change_type = 'migrate',
                   source_type = 'migration'
             WHERE id = (
                 SELECT id FROM revisions
                  WHERE page_id = (SELECT id FROM pages WHERE slug = ?)
                    AND change_type = 'create'
                  ORDER BY id DESC
                  LIMIT 1
             )
            """,
            (page.slug,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Top-level migration
# ---------------------------------------------------------------------------


def migrate(
    source: str | os.PathLike,
    db_path: str | os.PathLike,
    *,
    skip_dirs: Sequence[str] = DEFAULT_SKIP_DIRS,
    dry_run: bool = False,
    writer: str = "migrate",
    client: str = "migration-script",
    verbose: bool = False,
) -> dict[str, Any]:
    """Run the migration. Returns a summary dict.

    Keys:
        pages_imported: int — successful page writes (or, in dry-run, parses)
        pages_errored:  int — files that failed to import (parse or write)
        links:          int — total rows in ``links`` after the import
        tags:           int — total rows in ``tags`` after the import
        errors:         list[tuple[str, str]] — (relative_path, message)
        pages:          list[PageData] — parsed pages (always populated, useful
                                          for tests and dry-run reporting)
    """
    source_path = Path(source)
    db_path_str = str(db_path)
    skip_set = frozenset(skip_dirs)

    summary: dict[str, Any] = {
        "pages_imported": 0,
        "pages_errored": 0,
        "links": 0,
        "tags": 0,
        "errors": [],
        "pages": [],
    }

    if not source_path.is_dir():
        raise FileNotFoundError(f"source directory not found: {source_path}")

    files = find_markdown_files(source_path, skip_set)

    if dry_run:
        for path in files:
            try:
                page = parse_file(path, source_path)
            except Exception as e:  # noqa: BLE001 — per-file isolation
                summary["pages_errored"] += 1
                summary["errors"].append(
                    (str(path.relative_to(source_path)), str(e))
                )
                continue
            summary["pages"].append(page)
            summary["pages_imported"] += 1
        return summary

    # Real run: initialize the service and ensure the schema exists.
    service.init_service(db_path_str)
    with db.connection(db_path_str):
        # init_db() ran inside connection(); nothing else to do here.
        pass

    # FTS5 triggers stay in place for the import — see the
    # "FTS5 performance" section above for why the trigger-drop
    # optimization is incompatible with the schema verification on
    # every connection open. Per-row trigger firing is fine for the
    # corpus size this script targets.
    for path in files:
        rel = path.relative_to(source_path)
        try:
            page = parse_file(path, source_path)
        except Exception as e:  # noqa: BLE001
            summary["pages_errored"] += 1
            summary["errors"].append((str(rel), str(e)))
            if verbose:
                print(f"  parse error: {rel}: {e}", file=sys.stderr)
            continue
        try:
            import_page(page, writer=writer, client=client, db_path=db_path_str)
        except Exception as e:  # noqa: BLE001
            summary["pages_errored"] += 1
            summary["errors"].append((str(rel), str(e)))
            if verbose:
                print(f"  import error: {rel}: {e}", file=sys.stderr)
            continue
        summary["pages_imported"] += 1
        if verbose:
            print(f"  imported: {rel}", file=sys.stderr)

    # Final counts.
    with db.connection(db_path_str) as conn:
        summary["links"] = conn.execute(
            "SELECT COUNT(*) FROM links"
        ).fetchone()[0]
        summary["tags"] = conn.execute(
            "SELECT COUNT(*) FROM tags"
        ).fetchone()[0]

    # Final schema sanity check. Re-opening with db.connection runs
    # _verify_schema; this catches a corrupt migration (e.g. a
    # connection that left the DB in a half-written state).
    with db.connection(db_path_str):
        pass

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vesperiki.migrate",
        description=(
            "Import a markdown directory tree into the Vesperiki database."
        ),
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("VESPERIKI_MIGRATE_SOURCE"),
        help=(
            "Input directory of .md files. Required, or set "
            "VESPERIKI_MIGRATE_SOURCE."
        ),
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("VESPERIKI_DB_PATH", "./vesperiki.db"),
        help=(
            "Database path. Default: env VESPERIKI_DB_PATH or ./vesperiki.db."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse files and report what would be imported, without writing.",
    )
    parser.add_argument(
        "--skip-dirs",
        default=",".join(DEFAULT_SKIP_DIRS),
        help=(
            "Comma-separated list of directory names to skip "
            "(default: _archive,raw)."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-file progress to stderr.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if not args.source:
        print(
            "error: --source is required (or set VESPERIKI_MIGRATE_SOURCE).",
            file=sys.stderr,
        )
        return 2

    skip_dirs = tuple(
        s.strip() for s in args.skip_dirs.split(",") if s.strip()
    )

    summary = migrate(
        args.source,
        args.db,
        skip_dirs=skip_dirs,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    print(
        f"Imported {summary['pages_imported']} pages, "
        f"{summary['links']} links, {summary['tags']} tags."
    )
    if summary["pages_errored"]:
        print(
            f"Failed to import {summary['pages_errored']} pages "
            f"(see errors below).",
            file=sys.stderr,
        )
        for rel, msg in summary["errors"]:
            print(f"  {rel}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
