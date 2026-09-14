"""Markdown export script for the Vesperiki wiki.

Dumps the SQLite database to a directory of Obsidian-compatible markdown files.
This is the "portability hatch": DB-canonical is the project's
biggest one-way architectural commitment, and export proves it's reversible.
The acceptance test is literal: export, open in Obsidian, confirm the graph
and tags work.

Usage:
    python -m vesperiki.export --db PATH --output DIR
    python -m vesperiki.export --db PATH --output DIR --with-history
    python -m vesperiki.export --db PATH --output DIR --status all
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from . import db


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default status filter: only export active pages.
DEFAULT_STATUS: str = "active"

#: Special --status value that means "export every page regardless of status".
SPECIAL_STATUS_ALL: str = "all"

#: Media MIME-type → file-extension mapping. SVG and WebP have unusual types.
MEDIA_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}
#: Extension used when the mime_type isn't in the map.
MEDIA_EXT_FALLBACK: str = "bin"

#: Slug pattern. Matches the schema's `^[a-z0-9_-]+$` constraint. Restricting
#: the regex to this set prevents accidentally rewriting URLs that happen to
#: contain `/p/` in some other context.
_SLUG_RE = r"[a-z0-9_-]+"

#: Markdown link `[text](/p/slug)`. Captures (text, slug).
_LINK_MD_RE = re.compile(rf"\[([^\]\n]*)\]\(/p/({_SLUG_RE})\)")
#: HTML link `<a href="/p/slug">text</a>`. Captures (slug, text).
_LINK_HTML_RE = re.compile(
    rf"""<a\b[^>]*\bhref="/p/({_SLUG_RE})"[^>]*>([^<]*)</a>""",
    re.IGNORECASE,
)

#: Markdown image `![alt](/api/media/{id})` (or `/media/{id}`). Captures
#: (alt, media_id).
_IMAGE_MD_RE = re.compile(
    rf"!\[([^\]\n]*)\]\(/(?:api/)?media/(\d+)\)"
)
#: HTML image `<img src="/api/media/{id}">` (or `/media/{id}`). Captures
#: the media_id; the full original tag is also captured so we can extract alt
#: text for the markdown rewrite.
_IMAGE_HTML_RE = re.compile(
    rf"""<img\b([^>]*)\b(?:src|data-src)="/(?:api/)?media/(\d+)"([^>]*)>""",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# YAML emitter
# ---------------------------------------------------------------------------
#
# No pyyaml in the venv (verified at module load, see migrate.py). The
# shapes we need are limited, so a focused emitter is simpler than pulling
# in a dependency. Supported shapes:
#   - strings (auto-quote when needed)
#   - numbers (int, float)
#   - bool, None (lowercase literals)
#   - lists of scalars (flow: [a, b, c])
#   - lists of dicts / nested lists (block)
#   - dicts (block)
#
# Not supported (the wiki corpus doesn't use them): anchors, multi-doc,
# flow-style nested maps, tags with leading "@".

#: Characters / patterns that always require quoting. Conservative: when in
#: doubt, quote. (Forgiving parsers accept bare strings with these chars,
#: but Obsidian's parser is strict.)
_YAML_NEEDS_QUOTING = re.compile(
    r"""[:#'"\[\]{}|>%&*`,@?\n\r\t]|^[-+?!]|\s$|^\s"""
)

#: Bare tokens that the parser would treat as bool / null. Kept lowercase
#: and titlecase because YAML 1.1 accepts both.
_YAML_BARE_KEYWORDS = frozenset({
    "true", "false", "null", "yes", "no", "on", "off", "~",
    "True", "False", "Null", "Yes", "No", "On", "Off",
})


def yaml_quote(s: str) -> str:
    """Return a YAML double-quoted form of *s* with required escapes.

    Double-quoted YAML scalars use the same backslash escapes as JSON
    (plus a few extras). A literal newline / tab / carriage return inside
    the quotes would be invalid YAML — they must be encoded as the
    two-character sequences ``\\n``, ``\\t``, ``\\r``. Backslashes and
    double quotes are also escaped.
    """
    escaped = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def yaml_scalar(v: Any) -> str:
    """Format a scalar value (str, int, float, bool, None) as YAML."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        if v == "":
            return '""'
        # Quote if the string has YAML-meaningful chars, is a reserved keyword,
        # or has leading/trailing whitespace.
        if (
            _YAML_NEEDS_QUOTING.search(v)
            or v.strip() != v
            or v in _YAML_BARE_KEYWORDS
        ):
            return yaml_quote(v)
        return v
    raise TypeError(f"unsupported scalar type: {type(v).__name__}")


def _render_kv(key: str, value: Any, indent: int) -> list[str]:
    """Render one ``key: value`` pair (possibly multi-line) at *indent* depth.

    ``indent`` is in two-space units. Dict values become block maps; list
    values use flow form when all items are simple scalars and block form
    otherwise.
    """
    prefix = "  " * indent
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{key}: {{}}"]
        lines: list[str] = [f"{prefix}{key}:"]
        for k, v in value.items():
            lines.extend(_render_kv(k, v, indent + 1))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}{key}: []"]
        if all(isinstance(x, (str, int, float, bool)) or x is None for x in value):
            body = ", ".join(yaml_scalar(x) for x in value)
            return [f"{prefix}{key}: [{body}]"]
        return [f"{prefix}{key}:"] + _render_block_list(value, indent + 1)
    return [f"{prefix}{key}: {yaml_scalar(value)}"]


def _render_block_list(items: list, indent: int) -> list[str]:
    """Block-style YAML list. Items at *indent*; dict subkeys at *indent + 1*.

    Returns a list of lines with no leading or trailing newlines; the caller
    joins them with ``"\\n"``.
    """
    item_indent = "  " * indent
    sub_indent = "  " * (indent + 1)
    lines: list[str] = []
    for item in items:
        if isinstance(item, dict):
            kv = list(item.items())
            if not kv:
                lines.append(f"{item_indent}- {{}}")
                continue
            k, v = kv[0]
            lines.extend(_render_list_dict_kv(k, v, indent, on_dash=True))
            for k, v in kv[1:]:
                lines.extend(_render_list_dict_kv(k, v, indent + 1, on_dash=False))
        elif isinstance(item, list):
            if not item:
                lines.append(f"{item_indent}- []")
            else:
                lines.append(f"{item_indent}-")
                lines.extend(_render_block_list(item, indent + 1))
        else:
            lines.append(f"{item_indent}- {yaml_scalar(item)}")
    return lines


def _render_list_dict_kv(
    key: str, value: Any, indent: int, *, on_dash: bool
) -> list[str]:
    """Render one (key, value) inside a list-of-dicts item.

    ``on_dash=True`` means the first key of the dict (printed on the
    ``- key: ...`` line). ``on_dash=False`` means a continuation key
    (printed on a ``  key: ...`` line). Nested values cascade the indent
    one level deeper.

    The continuation prefix is one indent unit deeper than the dash line,
    so a continuation key sits at the same column as the first key (the
    spec's ``- type: ...`` / ``  ref: ...`` style). To keep subkeys
    aligned with that column we cascade one level deeper than the
    continuation prefix, not from the dash indent.
    """
    prefix = "  " * indent
    lead = "- " if on_dash else ""
    nested_indent = indent + (2 if on_dash else 1)
    if isinstance(value, dict):
        if not value:
            return [f"{prefix}{lead}{key}: {{}}"]
        lines = [f"{prefix}{lead}{key}:"]
        for k, v in value.items():
            lines.extend(_render_kv(k, v, nested_indent))
        return lines
    if isinstance(value, list):
        if not value:
            return [f"{prefix}{lead}{key}: []"]
        if all(isinstance(x, (str, int, float, bool)) or x is None for x in value):
            body = ", ".join(yaml_scalar(x) for x in value)
            return [f"{prefix}{lead}{key}: [{body}]"]
        return [f"{prefix}{lead}{key}:"] + _render_block_list(value, nested_indent)
    return [f"{prefix}{lead}{key}: {yaml_scalar(value)}"]


def render_frontmatter(meta: dict[str, Any]) -> str:
    """Render a frontmatter dict as a YAML block, wrapped in ``---`` fences.

    The returned string ends with a blank line so the body can begin on its
    own line after the closing fence.
    """
    lines: list[str] = ["---"]
    for key, value in meta.items():
        lines.extend(_render_kv(key, value, indent=0))
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Link rewriting
# ---------------------------------------------------------------------------


def rewrite_markdown_links(body: str) -> str:
    """Rewrite ``[text](/p/slug)`` → ``[[slug|text]]`` (or ``[[slug]]`` if
    ``text`` equals the slug). HTML anchors are handled separately by
    :func:`rewrite_html_links`.
    """
    def repl(m: re.Match[str]) -> str:
        text, slug = m.group(1), m.group(2)
        text = text.strip()
        if not text or text == slug:
            return f"[[{slug}]]"
        return f"[[{slug}|{text}]]"
    return _LINK_MD_RE.sub(repl, body)


def rewrite_html_links(body: str) -> str:
    """Rewrite ``<a href="/p/slug">text</a>`` → ``[[slug|text]]``.

    Inner text is taken verbatim; surrounding whitespace is stripped. The
    function deliberately does NOT recurse into nested ``<a>`` tags — the
    pattern uses a non-``<`` character class, and Obsidian-imported wikis
    don't nest anchors in practice.
    """
    def repl(m: re.Match[str]) -> str:
        slug, text = m.group(1), m.group(2)
        text = text.strip()
        if not text or text == slug:
            return f"[[{slug}]]"
        return f"[[{slug}|{text}]]"
    return _LINK_HTML_RE.sub(repl, body)


def rewrite_links(body: str) -> str:
    """Rewrite both markdown and HTML ``/p/slug`` links to wikilinks."""
    body = rewrite_markdown_links(body)
    body = rewrite_html_links(body)
    return body


# ---------------------------------------------------------------------------
# Media rewriting
# ---------------------------------------------------------------------------


def rewrite_media_refs(
    body: str, media_id_to_ext: dict[int, str], *, prefix: str = "media/"
) -> str:
    """Rewrite media references in *body* to relative paths.

    Markdown images ``![alt](/api/media/{id})`` (or ``/media/{id}``) become
    ``![alt]({prefix}{id}.{ext})``. HTML ``<img>`` tags with a matching
    ``src`` / ``data-src`` are converted to markdown images, preserving the
    ``alt`` attribute when present. Media ids absent from
    ``media_id_to_ext`` are left untouched (the BLOB wasn't exported, so
    rewriting would create a broken link).
    """
    body = _rewrite_md_images(body, media_id_to_ext, prefix=prefix)
    body = _rewrite_html_images(body, media_id_to_ext, prefix=prefix)
    return body


def _rewrite_md_images(
    body: str, media_id_to_ext: dict[int, str], *, prefix: str
) -> str:
    def repl(m: re.Match[str]) -> str:
        alt, raw_id = m.group(1), m.group(2)
        ext = media_id_to_ext.get(int(raw_id))
        if ext is None:
            return m.group(0)
        return f"![{alt}]({prefix}{raw_id}.{ext})"
    return _IMAGE_MD_RE.sub(repl, body)


def _rewrite_html_images(
    body: str, media_id_to_ext: dict[int, str], *, prefix: str
) -> str:
    def repl(m: re.Match[str]) -> str:
        pre, raw_id, post = m.group(1), m.group(2), m.group(3)
        ext = media_id_to_ext.get(int(raw_id))
        if ext is None:
            return m.group(0)
        # Pull alt="..." out of either half of the original tag.
        attrs = pre + " " + post
        alt_match = re.search(r'\balt\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
        alt = alt_match.group(1) if alt_match else ""
        return f"![{alt}]({prefix}{raw_id}.{ext})"
    return _IMAGE_HTML_RE.sub(repl, body)


# ---------------------------------------------------------------------------
# Frontmatter assembly
# ---------------------------------------------------------------------------


#: Order of the frontmatter keys. Optional keys (verified_at, origin, rel
#: buckets) are appended below if they have values; the rest are always
#: present.
_FRONT_KEYS_ORDER: tuple[str, ...] = (
    "title",
    "type",
    "tags",
    "status",
    "sources",
    "updated_at",
    "verified_at",
    "confidence",
    "origin",
)


def build_frontmatter(
    *,
    page: sqlite3.Row,
    tags: list[str],
    origin: dict[str, str] | None,
    explicit_links_by_rel: dict[str, list[str]],
) -> dict[str, Any]:
    """Build the frontmatter dict for one page.

    Optional fields are included only when they have values (e.g.
    ``verified_at`` is omitted when NULL). Typed relations become
    top-level keys, one per ``link_rels`` value, in the order the
    service emits them.
    """
    sources: list[Any] = []
    if page["sources"]:
        try:
            parsed = json.loads(page["sources"])
            if isinstance(parsed, list):
                sources = parsed
        except (json.JSONDecodeError, TypeError):
            sources = []

    meta: dict[str, Any] = {
        "title": page["title"],
        "type": page["type"],
        "tags": tags,
        "status": page["status"],
        "sources": sources,
        "updated_at": page["updated_at"],
    }
    if page["verified_at"] is not None:
        meta["verified_at"] = page["verified_at"]
    meta["confidence"] = page["confidence"]

    if origin:
        meta["origin"] = origin

    # Typed explicit relations. The plan calls for one frontmatter key per
    # `link_rels.name`; pages without explicit links for a given rel simply
    # omit the key entirely.
    for rel, slugs in explicit_links_by_rel.items():
        if slugs:
            meta[rel] = slugs

    return meta


# ---------------------------------------------------------------------------
# Database accessors
# ---------------------------------------------------------------------------


def _fetch_tags(conn: sqlite3.Connection, page_ids: list[int]) -> dict[int, list[str]]:
    """Return ``{page_id: [tag_name, ...]}`` for *page_ids*, sorted by name."""
    if not page_ids:
        return {}
    placeholders = ",".join("?" * len(page_ids))
    rows = conn.execute(
        f"SELECT pt.page_id, t.name FROM page_tags pt "
        f"JOIN tags t ON t.id = pt.tag_id "
        f"WHERE pt.page_id IN ({placeholders}) "
        f"ORDER BY pt.page_id, t.name",
        page_ids,
    ).fetchall()
    out: dict[int, list[str]] = {pid: [] for pid in page_ids}
    for row in rows:
        out[row["page_id"]].append(row["name"])
    return out


def _fetch_origin(conn: sqlite3.Connection, page_id: int) -> dict[str, str] | None:
    """Return the origin dict from the first create revision, or None.

    The export keeps ``changed_by`` and ``session_id``, plus ``client``
    when present (always, in practice — ``client`` is
    NOT NULL on the revisions table).
    """
    row = conn.execute(
        "SELECT changed_by, client, session_id FROM revisions "
        "WHERE page_id = ? AND change_type = 'create' "
        "ORDER BY id ASC LIMIT 1",
        (page_id,),
    ).fetchone()
    if row is None:
        return None
    origin: dict[str, str] = {}
    if row["changed_by"]:
        origin["changed_by"] = row["changed_by"]
    if row["client"]:
        origin["client"] = row["client"]
    if row["session_id"]:
        origin["session_id"] = row["session_id"]
    return origin or None


def _fetch_explicit_links(
    conn: sqlite3.Connection, page_id: int
) -> dict[str, list[str]]:
    """Return ``{rel: [slug, ...]}`` for explicit outgoing links from *page_id*."""
    rows = conn.execute(
        "SELECT l.rel, p.slug FROM links l "
        "JOIN pages p ON p.id = l.target_id "
        "WHERE l.source_id = ? AND l.origin = 'explicit' "
        "ORDER BY l.rel, p.slug",
        (page_id,),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row["rel"], []).append(row["slug"])
    return out


def _fetch_all_media(conn: sqlite3.Connection) -> dict[int, str]:
    """Return ``{media_id: file_extension}`` for every media row."""
    rows = conn.execute(
        "SELECT id, mime_type FROM media"
    ).fetchall()
    return {
        row["id"]: MEDIA_EXTENSIONS.get(row["mime_type"] or "", MEDIA_EXT_FALLBACK)
        for row in rows
    }


# ---------------------------------------------------------------------------
# Page export
# ---------------------------------------------------------------------------


def _export_page(
    conn: sqlite3.Connection,
    page: sqlite3.Row,
    *,
    tags: list[str],
    origin: dict[str, str] | None,
    explicit_links_by_rel: dict[str, list[str]],
    media_id_to_ext: dict[int, str],
    output_dir: Path,
) -> Path:
    """Write one page's markdown file. Returns the file path."""
    explicit_links_by_rel = dict(explicit_links_by_rel)
    meta = build_frontmatter(
        page=page,
        tags=tags,
        origin=origin,
        explicit_links_by_rel=explicit_links_by_rel,
    )
    frontmatter = render_frontmatter(meta)

    body = page["body"]
    body = rewrite_media_refs(body, media_id_to_ext)
    body = rewrite_links(body)

    path = output_dir / f"{page['slug']}.md"
    path.write_text(frontmatter + body + "\n", encoding="utf-8")
    return path


def _export_revision(
    conn: sqlite3.Connection,
    page: sqlite3.Row,
    revision: sqlite3.Row,
    *,
    media_id_to_ext: dict[int, str],
    history_dir: Path,
) -> Path:
    """Write one revision's markdown file under ``.history/{slug}/``.

    The frontmatter is intentionally minimal — the spec calls this archival
    output, not for Obsidian consumption — but carries the structured
    provenance columns so a reader can identify who changed what and when.
    """
    rev_meta: dict[str, Any] = {
        "slug": page["slug"],
        "page_id": page["id"],
        "revision_id": revision["id"],
        "changed_by": revision["changed_by"],
        "client": revision["client"],
        "changed_at": revision["changed_at"],
        "change_type": revision["change_type"],
    }
    if revision["session_id"]:
        rev_meta["session_id"] = revision["session_id"]
    if revision["source_type"]:
        rev_meta["source_type"] = revision["source_type"]
    if revision["change_summary"]:
        rev_meta["change_summary"] = revision["change_summary"]
    frontmatter = render_frontmatter(rev_meta)

    body = revision["body"]
    # Apply the same rewrites as the main export so the file is consistent
    # with the rest of the output directory. The path from .history/{slug}/
    # to media/{id}.{ext} needs an extra ../ to reach the media dir.
    body = rewrite_media_refs(body, media_id_to_ext, prefix="../../media/")
    body = rewrite_links(body)

    rev_dir = history_dir / page["slug"]
    rev_dir.mkdir(parents=True, exist_ok=True)
    path = rev_dir / f"r{revision['id']}.md"
    path.write_text(frontmatter + body + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Media export
# ---------------------------------------------------------------------------


def _export_media(
    conn: sqlite3.Connection,
    media_id_to_ext: dict[int, str],
    output_dir: Path,
) -> int:
    """Write BLOBs to ``output_dir/media/{id}.{ext}``. Returns file count."""
    if not media_id_to_ext:
        return 0
    media_dir = output_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for media_id, ext in media_id_to_ext.items():
        row = conn.execute(
            "SELECT data FROM media WHERE id = ?", (media_id,)
        ).fetchone()
        if row is None:
            continue
        data = row["data"]
        if isinstance(data, (bytes, bytearray, memoryview)):
            payload = bytes(data)
        elif isinstance(data, str):
            payload = data.encode("utf-8")
        else:
            continue
        (media_dir / f"{media_id}.{ext}").write_bytes(payload)
        written += 1
    return written


# ---------------------------------------------------------------------------
# Top-level export
# ---------------------------------------------------------------------------


def export(
    db_path: str | os.PathLike,
    output_dir: str | os.PathLike,
    *,
    with_history: bool = False,
    status: str = DEFAULT_STATUS,
) -> dict[str, Any]:
    """Run the export. Returns a summary dict.

    Keys:
        pages:        int — number of .md files written
        media:        int — number of media files written
        revisions:    int — number of revision files written (only with
                            ``with_history=True``)
        output_dir:   str — absolute output directory path
    """
    db_path_str = str(db_path)
    out_path = Path(output_dir).expanduser().resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    if status != SPECIAL_STATUS_ALL:
        # Validate the status filter against the schema's allowed values so
        # a typo'd --status surfaces a clear error rather than exporting
        # zero pages silently.
        _validate_status(status)

    summary: dict[str, Any] = {
        "pages": 0,
        "media": 0,
        "revisions": 0,
        "output_dir": str(out_path),
    }

    with db.connection(db_path_str) as conn:
        media_id_to_ext = _fetch_all_media(conn)
        summary["media"] = _export_media(conn, media_id_to_ext, out_path)

        if status == SPECIAL_STATUS_ALL:
            page_rows = conn.execute(
                "SELECT * FROM pages ORDER BY id ASC"
            ).fetchall()
        else:
            page_rows = conn.execute(
                "SELECT * FROM pages WHERE status = ? ORDER BY id ASC",
                (status,),
            ).fetchall()

        if not page_rows:
            return summary

        page_ids = [r["id"] for r in page_rows]
        tags_by_page = _fetch_tags(conn, page_ids)

        for page in page_rows:
            tags = tags_by_page.get(page["id"], [])
            origin = _fetch_origin(conn, page["id"])
            explicit_links = _fetch_explicit_links(conn, page["id"])
            _export_page(
                conn,
                page,
                tags=tags,
                origin=origin,
                explicit_links_by_rel=explicit_links,
                media_id_to_ext=media_id_to_ext,
                output_dir=out_path,
            )
            summary["pages"] += 1

        if with_history:
            history_dir = out_path / ".history"
            for page in page_rows:
                revisions = conn.execute(
                    "SELECT id, page_id, body, changed_by, client, changed_at, "
                    "change_type, change_summary, session_id, source_type, "
                    "source_refs, trigger "
                    "FROM revisions WHERE page_id = ? ORDER BY id ASC",
                    (page["id"],),
                ).fetchall()
                for rev in revisions:
                    _export_revision(
                        conn, page, rev,
                        media_id_to_ext=media_id_to_ext,
                        history_dir=history_dir,
                    )
                    summary["revisions"] += 1

    return summary


_VALID_STATUSES = ("active", "stale", "deprecated")


def _validate_status(status: str) -> None:
    """Raise ValueError if *status* is not a recognized filter."""
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"--status must be one of {', '.join(_VALID_STATUSES)} "
            f"or '{SPECIAL_STATUS_ALL}'; got {status!r}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vesperiki.export",
        description=(
            "Dump a Vesperiki database to a directory of Obsidian-compatible "
            "markdown files."
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
        "--output",
        required=True,
        help="Output directory. Required. Created if missing.",
    )
    parser.add_argument(
        "--with-history",
        action="store_true",
        help=(
            "Also write one .md per revision under .history/{slug}/. "
            "Archival, not for Obsidian consumption."
        ),
    )
    parser.add_argument(
        "--status",
        default=DEFAULT_STATUS,
        help=(
            "Filter by page status. Default: 'active'. Use 'all' to export "
            f"every page. Recognized values: {', '.join(_VALID_STATUSES)}, all."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        summary = export(
            args.db,
            args.output,
            with_history=args.with_history,
            status=args.status,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    rev_part = (
        f", {summary['revisions']} revisions"
        if summary.get("revisions")
        else ""
    )
    print(
        f"Exported {summary['pages']} pages, "
        f"{summary['media']} media files{rev_part} "
        f"to {summary['output_dir']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
