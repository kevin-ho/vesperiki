"""Service layer for Vesperiki: page CRUD, search, dedup gate, admin ops.

Public functions return plain dicts so both the MCP transport (Phase 4) and the
REST transport (Phase 5) can serialize them. The service layer is the single
source of truth for write/read logic — validation, dedup, revision-writing, and
link-rebuilding live here, not in either transport.

All write functions follow the schema's mandatory write pattern: BEGIN IMMEDIATE
→ bump change_seq → write rows with seq = (new change_seq value) → COMMIT.
See `vesperiki/db.py` for the rationale.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from typing import Any, Iterable, Sequence

from . import chunking, db, embeddings, sections

# Module-level DB path. Service functions open their own per-call connection.
_DB_PATH: str | None = None

# BM25 returns negative numbers; lower = more relevant.
# A "strong" duplicate has score < DEDUP_BM25_THRESHOLD.
# Tuned for precision over recall: a false positive trains agents to pass
# force=True reflexively, which converts the gate into a speed bump.
DEDUP_BM25_THRESHOLD = -3.0

# Escape-hatch hint appended to DuplicateError messages. Both transports
# serialize str(exc) verbatim (mcp._service_error / api._service_error_payload),
# so teaching the hint here teaches every transport at once.
DEDUP_HINT = "re-run with force=true if this is intentional"

# Meaningless head tokens dropped before the dedup FTS5 query: English
# function words, contraction fragments (the parser splits "don't" into
# "don" "t" — hence "don t s d ll ve re"), and stray single letters.
DEDUP_STOPWORDS = frozenset(
    """the a an and or of to for in on at by with from is are was were \
be been being it its this that these those i you we they he she do does did \
don t s d ll ve re what how when where why which who not no as but if then \
so can will would should could may might there their your our my me up out \
off over under about into than too very just also have has had x""".split()
)

# Slug pattern: lowercase, digits, underscore, hyphen.
_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")

# Wikilink pattern: [[slug]]. Captures the slug.
_WIKILINK_RE = re.compile(r"\[\[([a-z0-9_-]+)\]\]")

# FTS5 query sanitization: strip everything except alphanumeric and space.
# Removes FTS5 syntax characters that would raise a parser error.
_FTS_SAFE_RE = re.compile(r"[^a-zA-Z0-9 ]+")

# Title normalization: lowercase, strip everything except a-z0-9.
# "My Page" -> "mypage", "MY PAGE" -> "mypage", "my-page" -> "mypage".
_TITLE_NORM_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ServiceError(Exception):
    """Base error for the service layer. Subclasses carry a stable code."""

    code: str = "service_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class DuplicateError(ServiceError):
    """Raised when the dedup gate triggers."""

    code = "duplicate"


class NotFoundError(ServiceError):
    """Raised when a page or revision is not found."""

    code = "not_found"


class ValidationError(ServiceError):
    """Raised for input validation failures."""

    code = "validation"


class AliasConflictError(ServiceError):
    """Raised when a slug matches a retired alias."""

    code = "alias_conflict"


class SemanticSearchNotConfigured(ServiceError):
    code = "semantic_not_configured"


class EmbedSpaceMismatch(ServiceError):
    code = "embed_space_mismatch"


class EmbedProviderUnavailable(ServiceError):
    code = "embed_provider_unavailable"


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------


def init_service(db_path: str | os.PathLike) -> None:
    """Store the db path; service functions open their own per-call connection."""
    global _DB_PATH
    _DB_PATH = str(db_path)


def _require_db_path() -> str:
    if _DB_PATH is None:
        raise ServiceError("service not initialized; call init_service(db_path) first")
    return _DB_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_writer(
    changed_by: str | None,
    client: str | None,
) -> tuple[str, str]:
    """Resolve writer identity from kwargs or env, or raise ValidationError.

    The MCP server (Phase 4) will read these from its own environment and pass
    them explicitly; the service layer also accepts VESPERIKI_WRITER and
    VESPERIKI_CLIENT env vars so the REST entrypoint can run in the same
    process without extra plumbing.
    """
    changed_by = changed_by or os.environ.get("VESPERIKI_WRITER")
    client = client or os.environ.get("VESPERIKI_CLIENT")
    missing: list[str] = []
    if not changed_by:
        missing.append("changed_by (or VESPERIKI_WRITER)")
    if not client:
        missing.append("client (or VESPERIKI_CLIENT)")
    if missing:
        raise ValidationError(
            "writer identity required: " + ", ".join(missing)
        )
    assert changed_by is not None and client is not None
    return changed_by, client


def _validate_slug(slug: Any) -> None:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValidationError(
            f"slug {slug!r} must match ^[a-z0-9_-]+$; got {slug!r}"
        )


def _validate_title(title: Any) -> None:
    if not isinstance(title, str) or not title.strip():
        raise ValidationError(
            f"title must be a non-empty string; got {title!r}"
        )


def _normalize_title(title: str) -> str:
    """Lowercase, strip everything except a-z0-9. Anchored by the schema."""
    return _TITLE_NORM_RE.sub("", title.lower())


def _normalize_fts_query(text: str, *, or_separator: bool = False) -> str:
    """Strip non-alphanumeric except space; collapse runs of whitespace.

    When ``or_separator`` is True, terms are joined with " OR " so any term
    matches in FTS5. User-facing search keeps AND semantics by default —
    users typing "fox AND brown" expect both terms to match. The dedup gate
    tokenizes with AND semantics too (see ``_check_duplicate``).
    """
    cleaned = _FTS_SAFE_RE.sub(" ", text)
    terms = re.sub(r"\s+", " ", cleaned).strip().split(" ")
    if not terms or terms == [""]:
        return ""
    return " OR ".join(terms) if or_separator else " ".join(terms)


def _bump_seq(conn: sqlite3.Connection) -> int:
    """Bump change_seq and return the new value. Caller must be in a tx."""
    conn.execute("UPDATE change_seq SET value = value + 1 WHERE id = 1")
    return conn.execute(
        "SELECT value FROM change_seq WHERE id = 1"
    ).fetchone()["value"]


def _resolve_page(
    conn: sqlite3.Connection, slug: str
) -> tuple[sqlite3.Row, str | None]:
    """Resolve a slug to a page row. Follows slug_aliases.

    Returns (page_row, redirected_from). `redirected_from` is the requested
    slug if it was an alias, otherwise None. Raises NotFoundError if neither
    pages nor slug_aliases has the slug.
    """
    row = conn.execute(
        "SELECT * FROM pages WHERE slug = ?", (slug,)
    ).fetchone()
    if row is not None:
        return row, None
    alias = conn.execute(
        "SELECT page_id FROM slug_aliases WHERE alias = ?", (slug,)
    ).fetchone()
    if alias is None:
        raise NotFoundError(f"page {slug!r} not found")
    row = conn.execute(
        "SELECT * FROM pages WHERE id = ?", (alias["page_id"],)
    ).fetchone()
    if row is None:
        # Alias points to a vanished page — treat as not found.
        raise NotFoundError(f"page {slug!r} not found")
    return row, slug


def _sync_tags(
    conn: sqlite3.Connection, page_id: int, tags: Iterable[str]
) -> None:
    """Insert new tags and page_tags rows. Caller's transaction is in flight."""
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            raise ValidationError(
                f"tag must be a non-empty string; got {tag!r}"
            )
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,))
        tag_id = conn.execute(
            "SELECT id FROM tags WHERE name = ?", (tag,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO page_tags (page_id, tag_id) VALUES (?, ?)",
            (page_id, tag_id),
        )


def _rebuild_derived_links(
    conn: sqlite3.Connection, page_id: int, body: str
) -> None:
    """Delete derived links for this page, then re-insert from body wikilinks.

    Explicit links (origin='explicit') survive this: the DELETE filters by
    origin, and the (source_id, target_id, rel) PK causes INSERT OR IGNORE to
    preserve the explicit row when present.

    Unresolvable wikilinks (target page doesn't exist yet) are skipped
    deliberately. A derived link is rebuilt on every body write, so the link
    appears once the target page is created — no backfill pass required.
    """
    conn.execute(
        "DELETE FROM links WHERE source_id = ? AND origin = 'derived'",
        (page_id,),
    )
    for target_slug in set(_WIKILINK_RE.findall(body)):
        target = conn.execute(
            "SELECT id FROM pages WHERE slug = ?", (target_slug,)
        ).fetchone()
        if target is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO links "
            "(source_id, target_id, rel, origin) VALUES (?, ?, 'references', 'derived')",
            (page_id, target["id"]),
        )

def _resolve_back_links(
    conn: sqlite3.Connection, new_page_id: int, new_slug: str
) -> None:
    """Retroactively create derived links pointing at *new_slug*.

    Unresolvable wikilinks resolve retroactively when the target
    page is created. Idempotent (INSERT OR IGNORE on PK). Caller must hold
    the active transaction so other writers never observe a partial state.
    """
    pattern_pct = "%" + "[[" + new_slug + "]]" + "%"
    rows = conn.execute(
        "SELECT id, body FROM pages WHERE id != ? AND body LIKE ?",
        (new_page_id, pattern_pct),
    ).fetchall()
    for r in rows:
        if new_slug in _WIKILINK_RE.findall(r["body"]):
            conn.execute(
                "INSERT OR IGNORE INTO links "
                "(source_id, target_id, rel, origin) "
                "VALUES (?, ?, 'references', 'derived')",
                (r["id"], new_page_id),
            )


def _page_to_dict(
    page: sqlite3.Row, tags: list[str] | None = None
) -> dict:
    """Build the standard page dict. `tags` may be supplied if already fetched."""
    sources: list[Any] = []
    if page["sources"]:
        try:
            parsed = json.loads(page["sources"])
            if isinstance(parsed, list):
                sources = parsed
        except (json.JSONDecodeError, TypeError):
            sources = []
    return {
        "slug": page["slug"],
        "id": page["id"],
        "title": page["title"],
        "type": page["type"],
        "tags": list(tags) if tags is not None else [],
        "sources": sources,
        "body": page["body"],
        "status": page["status"],
        "updated_at": page["updated_at"],
        "confidence": page["confidence"],
    }


def _page_to_slim_dict(
    page: sqlite3.Row, tags: list[str] | None = None
) -> dict:
    """Listing projection: same as `_page_to_dict` minus the body.

    Used by `list_pages` so a metadata sweep over many pages doesn't ship
    every body to the client. The full projection (`_page_to_dict`) still
    feeds `get_page`, sync, and write paths — body is required there.
    """
    d = _page_to_dict(page, tags=tags)
    d.pop("body", None)
    return d


def _sync_page_chunks(conn: sqlite3.Connection, page_id: int, body: str) -> None:
    """Synchronize deterministic chunks and enqueue changed pages.

    This helper is called only while the caller owns BEGIN IMMEDIATE. It never
    contacts an embedding provider.
    """
    old = {
        row["seq"]: row
        for row in conn.execute(
            "SELECT * FROM page_chunks WHERE page_id = ?", (page_id,)
        ).fetchall()
    }
    changed = False
    kept: set[int] = set()
    for item in chunking.chunk_page(body):
        seq = int(item["seq"])
        kept.add(seq)
        previous = old.get(seq)
        same = previous is not None and (
            previous["body"] == item["body"]
            and previous["heading_path"] == item["heading_path"]
        )
        if same:
            continue
        changed = True
        if previous is not None:
            conn.execute("DELETE FROM page_chunks WHERE chunk_id = ?", (previous["chunk_id"],))
        conn.execute(
            "INSERT INTO page_chunks(page_id, seq, heading_path, body, char_count, stale) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (page_id, seq, item["heading_path"], item["body"], item["char_count"]),
        )
    for seq, previous in old.items():
        if seq not in kept:
            changed = True
            conn.execute("DELETE FROM page_chunks WHERE chunk_id = ?", (previous["chunk_id"],))
    if changed:
        conn.execute(
            "INSERT INTO embed_queue(page_id, queued_at) VALUES (?, datetime('now')) "
            "ON CONFLICT(page_id) DO UPDATE SET queued_at=excluded.queued_at",
            (page_id,),
        )


def _fetch_tags_for(
    conn: sqlite3.Connection, page_ids: Sequence[int]
) -> dict[int, list[str]]:
    """Return {page_id: [tag_name, ...]} for the given pages (sorted)."""
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


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------
#
# No pyyaml available in the project venv. A small, focused parser is enough:
# the project uses a predictable subset of YAML. Copied privately from
# vesperiki/migrate.py (entry point renamed) so the service layer doesn't
# couple to the migration module; both sides keep their own copy.
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


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
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


def _map_frontmatter(
    body: str,
    *,
    title: str | None,
    tags: Sequence[str] | None,
    type: str | None,
) -> tuple[str, str | None, Sequence[str] | None, str | None]:
    """Map title/tags/type from a leading YAML frontmatter block.

    Returns (clean_body, effective_title, effective_tags, effective_type).
    Callers translate their own defaults into None for "not passed" so
    frontmatter only fills fields the caller left unset; caller-provided
    values always win. Other frontmatter keys (created, updated, sources,
    ...) are ignored. A missing or malformed frontmatter block leaves the
    body and values untouched — never raises.
    """
    if not isinstance(body, str):
        return body, title, tags, type
    fm, clean_body = _parse_frontmatter(body)
    if not fm:
        return body, title, tags, type
    if title is None and isinstance(fm.get("title"), str) and fm["title"]:
        title = fm["title"]
    if tags is None and isinstance(fm.get("tags"), list) and all(
        isinstance(t, str) for t in fm["tags"]
    ):
        tags = list(fm["tags"])
    if type is None and isinstance(fm.get("type"), str):
        type = fm["type"]
    return clean_body, title, tags, type


# ---------------------------------------------------------------------------
# Dedup gate (Phase 3)
# ---------------------------------------------------------------------------


def _check_duplicate(
    conn: sqlite3.Connection,
    title: str,
    body: str,
    title_norm: str,
) -> list[dict] | None:
    """Return None if no duplicate; list of candidates if duplicate.

    Two signals:
      1. Title-norm exact match — indexed, high precision. Catches title
         variants that normalize identically ('My Page' / 'my-page' / 'MY PAGE').
      2. FTS5 BM25 overlap on body[:200] + title[:50]. Catches conceptual
         duplicates that share vocabulary.

    Signal 2 builds an AND-tokenized query over the meaningful head tokens:
    strip non-alphanumerics, drop stopwords (DEDUP_STOPWORDS), dedupe
    case-insensitively preserving first occurrence, cap at 16 tokens, join
    with " AND ", and check the top BM25 result. AND replaces the old
    OR-tokenized query because OR matched on any single shared term — a
    sibling page sharing only a family vocabulary ("Europe Fall 2026" in
    every planning page) scored below the threshold and blocked legitimate
    creates. AND requires the whole meaningful head to appear in one
    existing page: shared family vocabulary alone can't trigger it, while a
    near-verbatim copy still matches and gets flagged.

    The threshold is DEDUP_BM25_THRESHOLD; lower (more negative) BM25 means
    stronger match. Tuned for precision over recall — see the constant.
    """
    # 1. Title-norm exact match. Excludes deprecated pages so a soft-delete
    # doesn't block an intentional re-create.
    rows = conn.execute(
        "SELECT slug, title FROM pages "
        "WHERE title_norm = ? AND status != 'deprecated' LIMIT 5",
        (title_norm,),
    ).fetchall()
    if rows:
        return [
            {
                "slug": r["slug"],
                "title": r["title"],
                "score": 1.0,
                "reason": "title_norm_exact",
            }
            for r in rows
        ]

    # 2. AND-tokenized FTS5 BM25 overlap on body head + title head.
    head = (body[:200] + " " + title[:50]).strip()
    meaningful: list[str] = []
    seen: set[str] = set()
    for token in _normalize_fts_query(head, or_separator=False).split():
        key = token.lower()
        if key in DEDUP_STOPWORDS or key in seen:
            continue
        seen.add(key)
        meaningful.append(token)
        if len(meaningful) == 16:
            break
    fts_query = " AND ".join(meaningful)
    if not fts_query:
        return None

    try:
        rows = conn.execute(
            "SELECT p.slug, p.title, "
            "bm25(pages_fts, 0.0, 5.0, 3.0, 1.0) AS score "
            "FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
            "WHERE pages_fts MATCH ? ORDER BY score ASC LIMIT 5",
            (fts_query,),
        ).fetchall()
    except sqlite3.OperationalError:
        # FTS5 syntax error (e.g., query parses to a no-op). Treat as no match.
        return None

    if (
        rows
        and rows[0]["score"] is not None
        and rows[0]["score"] < DEDUP_BM25_THRESHOLD
    ):
        return [
            {
                "slug": r["slug"],
                "title": r["title"],
                "score": r["score"],
                "reason": "fts5_bm25",
            }
            for r in rows
        ]
    return None


def _check_no_alias(
    conn: sqlite3.Connection, slug: str, release_alias: bool
) -> None:
    """Raise AliasConflictError if slug is a retired alias and release_alias is False."""
    if release_alias:
        return
    row = conn.execute(
        "SELECT page_id FROM slug_aliases WHERE alias = ?", (slug,)
    ).fetchone()
    if row is not None:
        raise AliasConflictError(
            f"slug {slug!r} is a retired alias; "
            f"pass release_alias=True to reuse it"
        )


# ---------------------------------------------------------------------------
# Public API: write
# ---------------------------------------------------------------------------


def create_page(
    *,
    slug: str,
    title: str | None = None,
    body: str,
    tags: Sequence[str] = (),
    type: str = "entity",
    sources: Sequence[Any] = (),
    force: bool = False,
    changed_by: str | None = None,
    client: str | None = None,
    release_alias: bool = False,
    source_markdown: bool = False,
) -> dict:
    """Create a page, or update it if the slug already exists (upsert).

    Returns the page dict on success. ``source_markdown=True`` treats
    ``body`` as raw Markdown: a leading YAML frontmatter block supplies
    title/tags/type (unless the caller passed them) and only the clean
    body is stored; malformed frontmatter falls back to the raw body.
    """
    _validate_slug(slug)

    # Frontmatter-aware write: map title/tags/type from the raw markdown
    # head and store only the clean body. Caller-provided values win; the
    # create defaults (tags=(), type='entity') are translated to None so
    # "didn't pass" is unambiguous.
    if source_markdown:
        body, title, tags, type = _map_frontmatter(
            body,
            title=title,
            tags=None if tags == () else tags,
            type=None if type == "entity" else type,
        )
    tags = tags if tags is not None else ()
    type = type if type is not None else "entity"

    _validate_title(title)
    assert title is not None
    changed_by, client = _resolve_writer(changed_by, client)

    title_norm = _normalize_title(title)
    sources_json = json.dumps(list(sources))

    # Upsert: an existing slug (any status, including deprecated) delegates
    # to update_page, so create is idempotent — it silently updates instead
    # of erroring, skips the dedup gate and alias checks, and keeps update
    # semantics (no tombstone clearing; _resolve_page finds deprecated rows).
    with db.connection(_require_db_path()) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pages WHERE slug = ?", (slug,)
        ).fetchone()
    if exists is not None:
        return update_page(
            slug=slug,
            title=title,
            body=body,
            tags=tags,
            type=type,
            sources=sources,
            force=force,
            changed_by=changed_by,
            client=client,
        )

    # Read-side: validate type, run dedup gate, check alias conflict.
    with db.connection(_require_db_path()) as conn:
        type_row = conn.execute(
            "SELECT 1 FROM page_types WHERE name = ?", (type,)
        ).fetchone()
        if type_row is None:
            valid = [
                r["name"]
                for r in conn.execute("SELECT name FROM page_types").fetchall()
            ]
            raise ValidationError(
                f"type {type!r} not in page_types; got {type!r}, "
                f"valid values: {valid}"
            )

        candidates = _check_duplicate(conn, title, body, title_norm)
        if candidates and not force:
            raise DuplicateError(
                f"duplicate page: {len(candidates)} candidate(s); {DEDUP_HINT}",
                details={"candidates": candidates},
            )

        _check_no_alias(conn, slug, release_alias)

    # Write-side: BEGIN IMMEDIATE, insert, bump seq, COMMIT.
    with db.connection(_require_db_path()) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")

            # Clear any tombstone for this slug (sync-critical: a client
            # replaying the purge then the create would otherwise delete the
            # freshly created page).
            conn.execute("DELETE FROM tombstones WHERE slug = ?", (slug,))

            if release_alias:
                conn.execute(
                    "DELETE FROM slug_aliases WHERE alias = ?", (slug,)
                )

            new_seq = _bump_seq(conn)
            cursor = conn.execute(
                "INSERT INTO pages "
                "(slug, title, title_norm, body, type, sources, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (slug, title, title_norm, body, type, sources_json, new_seq),
            )
            page_id = cursor.lastrowid

            conn.execute(
                "INSERT INTO revisions "
                "(page_id, body, change_type, changed_by, client) "
                "VALUES (?, ?, 'create', ?, ?)",
                (page_id, body, changed_by, client),
            )

            _sync_tags(conn, page_id, tags)
            _rebuild_derived_links(conn, page_id, body)
            _sync_page_chunks(conn, page_id, body)
            _resolve_back_links(conn, page_id, slug)

            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            msg = str(e).lower()
            if "slug" in msg:
                raise AliasConflictError(
                    f"slug {slug!r} conflict: {e}"
                ) from e
            raise
        except BaseException:
            conn.rollback()
            raise

    # Read back to return the canonical dict (and capture created_at).
    return get_page(slug)


def update_page(
    *,
    slug: str,
    title: str | None = None,
    body: str | None = None,
    tags: Sequence[str] | None = None,
    type: str | None = None,
    sources: Sequence[Any] | None = None,
    verified_at: str | None = None,
    confidence: float | None = None,
    force: bool = False,
    changed_by: str | None = None,
    client: str | None = None,
    source_markdown: bool = False,
) -> dict:
    """Update a page. Returns the updated page dict.

    ``title`` updates both ``title`` and its ``title_norm`` (the FTS
    trigger keeps the search index in sync). Omit any field to leave it
    unchanged; pass ``title=""`` explicitly to clear a title (not
    recommended — the column is NOT NULL). ``source_markdown=True`` treats
    ``body`` as raw Markdown: a leading YAML frontmatter block supplies
    title/tags/type (unless passed explicitly) and only the clean body is
    stored; malformed frontmatter falls back to the raw body.
    """
    changed_by, client = _resolve_writer(changed_by, client)
    if confidence is not None and not (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0.0 <= confidence <= 1.0
    ):
        raise ValidationError(
            f"confidence must be between 0.0 and 1.0; got {confidence!r}"
        )

    # Frontmatter-aware write: map title/tags/type from the raw markdown
    # head and store only the clean body. Caller-provided values win
    # (None means "not passed" on this path).
    if source_markdown and body is not None:
        body, title, tags, type = _map_frontmatter(
            body, title=title, tags=tags, type=type
        )

    with db.connection(_require_db_path()) as conn:
        page, _redirected_from = _resolve_page(conn, slug)
        page_id = page["id"]

        # Determine what actually changed.
        body_changed = body is not None and body != page["body"]
        tags_changed = tags is not None
        type_changed = type is not None and type != page["type"]
        sources_changed = sources is not None
        verified_at_changed = verified_at is not None
        confidence_changed = confidence is not None
        title_changed = title is not None and title != page["title"]

        if type_changed:
            type_row = conn.execute(
                "SELECT 1 FROM page_types WHERE name = ?", (type,)
            ).fetchone()
            if type_row is None:
                valid = [
                    r["name"]
                    for r in conn.execute("SELECT name FROM page_types").fetchall()
                ]
                raise ValidationError(
                    f"type {type!r} not in page_types; got {type!r}, "
                    f"valid values: {valid}"
                )

        # The dedup gate is a create-only check. Body updates don't gate on
        # duplicates — an entity page can legitimately grow similar to another
        # if the user is intentionally merging. (force is accepted for signature
        # parity; ignored here.)
        del force

        # No-op early return: if the caller didn't actually change anything,
        # skip the write transaction entirely — no seq bump, no revision row,
        # no updated_at refresh. tags_changed follows the "tags is not None"
        # rule: passing an explicit empty list still counts as a change
        # (clearing tags) and must NOT short-circuit here.
        if not (
            body_changed
            or tags_changed
            or type_changed
            or sources_changed
            or verified_at_changed
            or confidence_changed
            or title_changed
        ):
            return get_page(slug)

        # Write.
        conn.execute("BEGIN IMMEDIATE")
        new_seq = _bump_seq(conn)

        set_parts = ["updated_at = datetime('now')", "seq = ?"]
        params: list[Any] = [new_seq]

        if body_changed:
            set_parts.append("body = ?")
            params.append(body)
        if type_changed:
            set_parts.append("type = ?")
            params.append(type)
        if sources_changed:
            set_parts.append("sources = ?")
            params.append(json.dumps(list(sources)))
        if verified_at_changed:
            set_parts.append("verified_at = ?")
            params.append(verified_at)
        if confidence_changed:
            set_parts.append("confidence = ?")
            params.append(confidence)

        if title_changed:
            assert title is not None  # title_changed implies title is not None
            set_parts.append("title = ?")
            params.append(title)
            set_parts.append("title_norm = ?")
            params.append(_normalize_title(title))

        params.append(page_id)
        conn.execute(
            f"UPDATE pages SET {', '.join(set_parts)} WHERE id = ?",
            params,
        )

        if tags_changed:
            conn.execute(
                "DELETE FROM page_tags WHERE page_id = ?", (page_id,)
            )
            _sync_tags(conn, page_id, tags)

        if body_changed:
            _rebuild_derived_links(conn, page_id, body)
            _sync_page_chunks(conn, page_id, body)

        # Revision body = POST-change body (schema "body sync convention").
        # `verified_at` lives on `pages`, not `revisions` — already written above
        # if the caller passed it. The verification event itself is recorded as
        # change_type='verify' below when the caller's intent was a verification.
        rev_body = body if body_changed else page["body"]
        rev_change_type = "verify" if verified_at_changed and not body_changed else "update"
        conn.execute(
            "INSERT INTO revisions "
            "(page_id, body, change_type, changed_by, client) "
            "VALUES (?, ?, ?, ?, ?)",
            (page_id, rev_body, rev_change_type, changed_by, client),
        )

        conn.commit()

    return get_page(slug)


def update_section(
    *,
    slug: str,
    section_id: str,
    content: str,
    changed_by: str | None = None,
    client: str | None = None,
) -> dict:
    """Replace the content of a single ``## `` section of a page.

    Reads the current body, splices the new content into the matching
    section, and writes the full body back via :func:`update_page` so a
    normal ``change_type='update'`` revision is recorded. The section's
    original heading line is preserved verbatim; only the lines between
    this heading and the next (or EOF) change.

    Raises:
      NotFoundError: page does not exist.
      ValidationError: section id is not present on the page, or
        ``content`` is ``None``.
    """
    if content is None:
        raise ValidationError(
            "content is required for update_section"
        )

    changed_by, client = _resolve_writer(changed_by, client)

    # Raises NotFoundError naturally if the slug is unknown.
    page = get_page(slug)

    try:
        new_body = sections.replace_section(page["body"], section_id, content)
    except KeyError:
        valid = [s["id"] for s in sections.parse_sections(page["body"])]
        raise ValidationError(
            f"section {section_id!r} not found in page {slug!r}; "
            f"valid section ids: {valid}",
            details={"section_id": section_id, "valid_section_ids": valid},
        )

    return update_page(
        slug=slug,
        body=new_body,
        changed_by=changed_by,
        client=client,
    )


def delete_page(
    *,
    slug: str,
    purge: bool = False,
    changed_by: str | None = None,
    client: str | None = None,
) -> dict:
    """Soft-delete (status='deprecated') or hard-purge a page.

    Soft delete: page row stays, status flips, sync clients filter it out.
    Hard purge: rows cascade-delete (revisions, links, media), a tombstone
    is written so sync clients who haven't seen the page yet delete it.
    """
    # Resolve writer identity even if the slug is missing — the validation
    # error is better than a surprising post-write error.
    changed_by, client = _resolve_writer(changed_by, client)
    del changed_by, client  # tombstones carry no provenance columns

    with db.connection(_require_db_path()) as conn:
        page, _ = _resolve_page(conn, slug)
        page_id = page["id"]

        if not purge:
            conn.execute("BEGIN IMMEDIATE")
            new_seq = _bump_seq(conn)
            conn.execute(
                "UPDATE pages SET status = 'deprecated', "
                "updated_at = datetime('now'), seq = ? WHERE id = ?",
                (new_seq, page_id),
            )
            conn.execute(
                "INSERT INTO embed_queue(page_id) VALUES (?) "
                "ON CONFLICT(page_id) DO UPDATE SET queued_at=datetime('now')",
                (page_id,),
            )
            conn.commit()
            return {"slug": slug, "status": "deprecated", "purged": False}

        # Hard purge.
        conn.execute("BEGIN IMMEDIATE")
        new_seq = _bump_seq(conn)
        # Cascade deletes revisions, links, media. The tombstone table only
        # stores (slug, seq, deleted_at) — no provenance columns.
        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))
        conn.execute(
            "INSERT INTO tombstones (slug, seq) VALUES (?, ?)",
            (slug, new_seq),
        )
        conn.commit()

    return {"slug": slug, "status": "purged", "purged": True}


def revive_page(
    *,
    slug: str,
    changed_by: str | None = None,
    client: str | None = None,
) -> dict:
    """Flip a deprecated page back to status='active'. No-op if already active.

    A soft-delete (status='deprecated') hides the page from sync clients and
    from the default ``list_pages`` filter. Reviving restores it to active
    visibility and records an ``update`` revision carrying the page's
    current body so the history reflects the state transition. Reviving an
    already-active page is a no-op: no write transaction, no seq bump, no
    revision row.
    """
    changed_by, client = _resolve_writer(changed_by, client)

    with db.connection(_require_db_path()) as conn:
        page, _ = _resolve_page(conn, slug)
        page_id = page["id"]

        if page["status"] == "active":
            # Already active: do not bump seq, do not insert a revision.
            return get_page(slug)

        conn.execute("BEGIN IMMEDIATE")
        new_seq = _bump_seq(conn)
        conn.execute(
            "UPDATE pages SET status = 'active', "
            "updated_at = datetime('now'), seq = ? WHERE id = ?",
            (new_seq, page_id),
        )
        conn.execute(
            "INSERT INTO embed_queue(page_id) VALUES (?) "
            "ON CONFLICT(page_id) DO UPDATE SET queued_at=datetime('now')",
            (page_id,),
        )
        conn.execute(
            "INSERT INTO revisions "
            "(page_id, body, change_type, changed_by, client) "
            "VALUES (?, ?, 'update', ?, ?)",
            (page_id, page["body"], changed_by, client),
        )
        conn.commit()

    return get_page(slug)


# ---------------------------------------------------------------------------
# Optional semantic indexing and retrieval
# ---------------------------------------------------------------------------


def _load_vec(conn: sqlite3.Connection) -> Any:
    try:
        import sqlite_vec
        sqlite_vec.load(conn)
        return sqlite_vec
    except (ImportError, AttributeError, sqlite3.Error) as exc:
        raise embeddings.EmbedProviderError("sqlite-vec is unavailable") from exc


def _ensure_vec_table(conn: sqlite3.Connection, dim: int) -> Any:
    vec = _load_vec(conn)
    row = conn.execute("SELECT value FROM embed_config WHERE key='dim'").fetchone()
    if row is not None and int(row["value"]) != dim:
        raise EmbedSpaceMismatch("embedding dimension changed; full re-embed required")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
    )
    return vec


def _embedding_space(config: embeddings.EmbedConfig, conn: sqlite3.Connection, dim: int) -> None:
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM embed_config")}
    if rows.get("model") and rows["model"] != config.model:
        raise EmbedSpaceMismatch("embedding model changed; full re-embed required")
    if rows.get("dim") and int(rows["dim"]) != dim:
        raise EmbedSpaceMismatch("embedding dimension changed; full re-embed required")


def drain_embed_queue(limit: int = 64) -> dict[str, Any]:
    """Embed queued page chunks without holding a SQLite write transaction over HTTP."""
    config = embeddings.get_config()
    if config is None:
        return {"configured": False, "embedded": 0, "queued": _queue_count()}
    with db.connection(_require_db_path()) as conn:
        pages = conn.execute(
            "SELECT page_id FROM embed_queue ORDER BY queued_at, page_id LIMIT ?", (limit,)
        ).fetchall()
        chunks = []
        for page in pages:
            chunks.extend(conn.execute(
                "SELECT chunk_id, body FROM page_chunks WHERE page_id=? ORDER BY seq", (page["page_id"],)
            ).fetchall())
    if not chunks:
        return {"configured": True, "embedded": 0, "queued": 0}
    try:
        vectors = embeddings.embed_texts([row["body"] for row in chunks])
    except embeddings.EmbedProviderUnavailable as exc:
        return {"configured": True, "embedded": 0, "queued": _queue_count(), "error": str(exc)}
    except embeddings.EmbedError:
        raise
    dim = len(vectors[0])
    with db.connection(_require_db_path()) as conn:
        _embedding_space(config, conn, dim)
        vec = _ensure_vec_table(conn, dim)
        blob = vec.serialize_float32
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO embed_config(key,value) VALUES('model',?)", (config.model,))
        conn.execute("INSERT OR REPLACE INTO embed_config(key,value) VALUES('dim',?)", (str(dim),))
        for row, vector in zip(chunks, vectors):
            conn.execute("DELETE FROM chunk_vec WHERE chunk_id=?", (row["chunk_id"],))
            conn.execute("INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)", (row["chunk_id"], blob(vector)))
            conn.execute(
                "UPDATE page_chunks SET embedding_model=?, embedding_dim=?, embedded_at=datetime('now'), stale=0 WHERE chunk_id=?",
                (config.model, dim, row["chunk_id"]),
            )
        for page in pages:
            remaining = conn.execute(
                "SELECT 1 FROM page_chunks WHERE page_id=? AND (embedded_at IS NULL OR stale=1)", (page["page_id"],)
            ).fetchone()
            if remaining is None:
                conn.execute("DELETE FROM embed_queue WHERE page_id=?", (page["page_id"],))
        conn.commit()
    return {"configured": True, "embedded": len(vectors), "queued": _queue_count()}


def _queue_count() -> int:
    with db.connection(_require_db_path()) as conn:
        return conn.execute("SELECT count(*) FROM embed_queue").fetchone()[0]


def _semantic_failure_note(exc: Exception) -> str:
    if isinstance(exc, SemanticSearchNotConfigured):
        return "set VESPERIKI_EMBED_URL and VESPERIKI_EMBED_MODEL to enable"
    if isinstance(exc, EmbedSpaceMismatch):
        return "embedding model or dimension changed; re-embed required"
    return "embedding provider unreachable"


def search_semantic(query: str, *, limit: int = 10, include_body: bool = False,
                    type: str | None = None, tag: str | None = None) -> list[dict]:
    config = embeddings.get_config()
    if config is None:
        raise SemanticSearchNotConfigured("set VESPERIKI_EMBED_URL and VESPERIKI_EMBED_MODEL to enable semantic search")
    if not query.strip():
        return []
    try:
        vector = embeddings.embed_texts([query])[0]
    except embeddings.EmbedProviderUnavailable as exc:
        raise EmbedProviderUnavailable(str(exc)) from exc
    except embeddings.EmbedProviderError as exc:
        raise ServiceError(str(exc)) from exc
    with db.connection(_require_db_path()) as conn:
        rows = conn.execute("SELECT value FROM embed_config WHERE key='dim'").fetchone()
        if rows is None:
            return []
        _embedding_space(config, conn, len(vector))
        vec = _ensure_vec_table(conn, len(vector))
        k = max(limit * 4, limit)
        matches = conn.execute(
            "SELECT chunk_id, distance FROM chunk_vec WHERE embedding MATCH ? AND k = ?",
            (vec.serialize_float32(vector), k),
        ).fetchall()
        results: list[dict] = []
        seen: set[int] = set()
        for match in matches:
            row = conn.execute(
                "SELECT c.*, p.slug, p.title, p.type, p.body, p.status FROM page_chunks c JOIN pages p ON p.id=c.page_id WHERE c.chunk_id=?",
                (match["chunk_id"],),
            ).fetchone()
            if row is None or row["status"] == "deprecated" or row["page_id"] in seen:
                continue
            if type is not None and row["type"] != type:
                continue
            if tag is not None and conn.execute(
                "SELECT 1 FROM page_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.page_id=? AND t.name=?",
                (row["page_id"], tag),
            ).fetchone() is None:
                continue
            seen.add(row["page_id"])
            item = {"slug": row["slug"], "title": row["title"], "type": row["type"],
                    "snippet": row["body"], "distance": match["distance"], "match": "semantic"}
            if include_body:
                item["body"] = row["body"]
            results.append(item)
            if len(results) >= limit:
                break
        return results


def search_hybrid(query: str, *, limit: int = 10, include_body: bool = False,
                  type: str | None = None, tag: str | None = None) -> dict:
    keyword = search_pages(query=query, limit=limit, include_body=include_body, type=type, tag=tag)
    try:
        semantic = search_semantic(query, limit=limit, include_body=include_body, type=type, tag=tag)
    except ServiceError as exc:
        return {"results": keyword, "semantic": False, "note": _semantic_failure_note(exc), "modes": {"keyword": True, "semantic": False}}
    fused: dict[str, tuple[float, dict]] = {}
    for rank, item in enumerate(keyword):
        score, existing = fused.get(item["slug"], (0.0, item))
        fused[item["slug"]] = (score + 1 / (60 + rank + 1), existing)
    for rank, item in enumerate(semantic):
        score, existing = fused.get(item["slug"], (0.0, item))
        fused[item["slug"]] = (score + 1 / (60 + rank + 1), existing)
    results = [item for _, item in sorted(fused.values(), key=lambda pair: -pair[0])][:limit]
    return {"results": results, "semantic": True, "modes": {"keyword": True, "semantic": True}}


# ---------------------------------------------------------------------------
# Public API: read
# ---------------------------------------------------------------------------


def get_page(
    slug: str,
    *,
    expand_links: bool = False,
    include_revisions: bool = False,
) -> dict:
    """Read a page by slug. Raises NotFoundError if not found."""
    with db.connection(_require_db_path()) as conn:
        page, redirected_from = _resolve_page(conn, slug)
        page_id = page["id"]

        tags_by_page = _fetch_tags_for(conn, [page_id])
        result = _page_to_dict(page, tags=tags_by_page.get(page_id, []))

        first_rev = conn.execute(
            "SELECT changed_at FROM revisions "
            "WHERE page_id = ? AND change_type = 'create' "
            "ORDER BY id ASC LIMIT 1",
            (page_id,),
        ).fetchone()
        if first_rev is not None:
            result["created_at"] = first_rev["changed_at"]

        if redirected_from is not None:
            result["redirected_from"] = redirected_from

        if expand_links:
            result["links"] = [
                {"slug": r["slug"], "title": r["title"]}
                for r in conn.execute(
                    "SELECT p.slug, p.title FROM links l "
                    "JOIN pages p ON p.id = l.target_id "
                    "WHERE l.source_id = ? ORDER BY p.slug",
                    (page_id,),
                )
            ]

        if include_revisions:
            result["revisions"] = [
                dict(r) for r in conn.execute(
                    "SELECT id, page_id, body, changed_by, client, changed_at, "
                    "change_type, change_summary, session_id, source_type, "
                    "source_refs, trigger "
                    "FROM revisions WHERE page_id = ? ORDER BY changed_at DESC",
                    (page_id,),
                )
            ]

        return result


def list_pages(
    *,
    tag: str | None = None,
    include_tags: Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    type: str | None = None,
    status: str | None = None,
    limit: int = 50,
    cursor: int | None = None,
    include_body: bool = False,
    order: str = "id",
) -> dict:
    """Cursor-paginated page list. Cursor is the last id returned.

    Pagination key is `id`, not `seq`: seq changes during writes, id is
    stable. Clients pass back the `next_cursor` from the previous response.

    ``order`` selects the sort: ``id`` (default, upstream-compatible,
    cursor-paginated by insertion order) or ``updated`` (most recently
    updated first, for "recent pages" surfaces). Cursor pagination only
    applies to ``id`` order; ``updated`` returns the newest ``limit``
    rows without a cursor.

    By default the per-page projection omits `body` (listing is for picking
    a page; get_page serves bodies). Pass ``include_body=True`` to ship the
    full projection — useful when the caller knows it needs every body
    up front and wants to avoid a second round-trip per page.

    ``include_tags`` requires every listed page to carry ALL of the given
    tags (AND semantics). ``exclude_tags`` drops any page carrying ANY of
    the given tags (NOT-ANY semantics). Both combine with each other and
    with the ``tag``/``type``/``status`` filters via AND. ``None`` and
    empty lists are treated identically as "no filter".
    """
    if limit <= 0:
        raise ValidationError(f"limit must be positive; got {limit!r}")

    where: list[str] = []
    params: list[Any] = []

    if status is not None:
        where.append("status = ?")
        params.append(status)
    if type is not None:
        where.append("type = ?")
        params.append(type)
    if cursor is not None:
        where.append("id > ?")
        params.append(cursor)
    if tag is not None:
        where.append(
            "id IN (SELECT pt.page_id FROM page_tags pt "
            "JOIN tags t ON t.id = pt.tag_id WHERE t.name = ?)"
        )
        params.append(tag)
    if include_tags:
        # AND across the listed tags: GROUP BY the page id, HAVING the
        # count of distinct matched names. The IN clause restricts the
        # candidate tags; the HAVING clause then enforces that every one
        # of the requested tags is present.
        placeholders = ",".join("?" * len(include_tags))
        where.append(
            f"EXISTS (SELECT 1 FROM page_tags pt "
            f"JOIN tags t ON t.id = pt.tag_id "
            f"WHERE pt.page_id = pages.id AND t.name IN ({placeholders}) "
            f"GROUP BY pt.page_id "
            f"HAVING COUNT(DISTINCT t.name) = ?)"
        )
        params.extend(include_tags)
        params.append(len(include_tags))
    if exclude_tags:
        # NOT ANY: drop the page if any of the listed tags is attached.
        placeholders = ",".join("?" * len(exclude_tags))
        where.append(
            f"NOT EXISTS (SELECT 1 FROM page_tags pt "
            f"JOIN tags t ON t.id = pt.tag_id "
            f"WHERE pt.page_id = pages.id AND t.name IN ({placeholders}))"
        )
        params.extend(exclude_tags)

    if order not in ("id", "updated"):
        raise ValidationError(
            f"order must be 'id' or 'updated'; got {order!r}"
        )
    if cursor is not None and order == "updated":
        raise ValidationError(
            "cursor pagination requires order='id'; "
            "order='updated' returns the newest rows only"
        )

    where_clause = "WHERE " + " AND ".join(where) if where else ""
    order_clause = (
        "ORDER BY id ASC" if order == "id" else "ORDER BY updated_at DESC, id DESC"
    )
    sql = (
        f"SELECT * FROM pages {where_clause} "
        f"{order_clause} LIMIT ?"
    )
    # Fetch limit+1 to know whether there's a next page without a second query.
    params.append(limit + 1)

    with db.connection(_require_db_path()) as conn:
        rows = conn.execute(sql, params).fetchall()
        page_ids = [r["id"] for r in rows[:limit]]
        tags_by_page = _fetch_tags_for(conn, page_ids)

    next_cursor: int | None = None
    if len(rows) > limit:
        next_cursor = page_ids[-1]

    project = _page_to_dict if include_body else _page_to_slim_dict
    result_pages = [
        project(r, tags=tags_by_page.get(r["id"], []))
        for r in rows[:limit]
    ]
    return {"pages": result_pages, "next_cursor": next_cursor}


def search_pages(
    *,
    query: str,
    limit: int = 10,
    include_body: bool = False,
    tag: str | None = None,
    type: str | None = None,
) -> list[dict]:
    """FTS5 BM25 search with snippet and optional filters.

    Returns [{slug, title, type, snippet, score, (body if requested)}].
    Most relevant first (BM25 score ascending — lower is more negative).
    """
    sanitized = _normalize_fts_query(query)
    if not sanitized:
        return []

    select_cols = ["p.slug", "p.title", "p.type"]
    if include_body:
        select_cols.append("p.body")
    # Column 3 of pages_fts is `body` (slug=0, title=1, tags=2, body=3).
    select_cols.append(
        "snippet(pages_fts, 3, '', '', '...', 16) AS snippet"
    )
    select_cols.append(
        "bm25(pages_fts, 0.0, 5.0, 3.0, 1.0) AS score"
    )

    sql_parts = [
        "SELECT " + ", ".join(select_cols),
        "FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid",
        "WHERE pages_fts MATCH ?",
    ]
    params: list[Any] = [sanitized]

    if type is not None:
        sql_parts.append("AND p.type = ?")
        params.append(type)
    if tag is not None:
        sql_parts.append(
            "AND EXISTS (SELECT 1 FROM page_tags pt "
            "JOIN tags t ON t.id = pt.tag_id "
            "WHERE pt.page_id = p.id AND t.name = ?)"
        )
        params.append(tag)

    sql_parts.append("ORDER BY score ASC LIMIT ?")
    params.append(limit)
    sql = "\n".join(sql_parts)

    with db.connection(_require_db_path()) as conn:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []

    results: list[dict] = []
    for r in rows:
        item: dict[str, Any] = {
            "slug": r["slug"],
            "title": r["title"],
            "type": r["type"],
            "snippet": r["snippet"],
            "score": r["score"],
        }
        if include_body:
            item["body"] = r["body"]
        results.append(item)
    return results


def get_meta(*, action: str, **params: Any) -> dict | list[dict]:
    """Dispatch on action.

    Actions:
      - exists:    {slug, exists: bool}
      - list:      passes through to list_pages
      - tags:      [{name, count}]
      - recent:    [{slug, title, updated_at}]
      - stale:     [{slug, title, verified_at, updated_at, days_since}]
      - stale_ranked: [{slug, title, type, updated_at, verified_at, tags}]
        active pages ordered oldest-first with no time cutoff; supports
        ``type``, ``include_tags``, ``exclude_tags`` filters.
      - links:     outgoing edges for slug
      - backlinks: incoming edges for slug
      - corrections: pending typo/error flags (status='pending')
    """
    if action == "exists":
        slug = params.get("slug")
        if slug is None:
            raise ValidationError("slug required for action 'exists'")
        with db.connection(_require_db_path()) as conn:
            row = conn.execute(
                "SELECT 1 FROM pages WHERE slug = ?", (slug,)
            ).fetchone()
            if row is not None:
                return {"slug": slug, "exists": True}
            alias = conn.execute(
                "SELECT 1 FROM slug_aliases WHERE alias = ?", (slug,)
            ).fetchone()
        return {"slug": slug, "exists": alias is not None}

    if action == "list":
        return list_pages(
            tag=params.get("tag"),
            include_tags=params.get("include_tags"),
            exclude_tags=params.get("exclude_tags"),
            type=params.get("type"),
            status=params.get("status"),
            limit=params.get("limit", 50),
            cursor=params.get("cursor"),
        )

    if action == "tags":
        with db.connection(_require_db_path()) as conn:
            rows = conn.execute(
                "SELECT t.name, COUNT(pt.page_id) AS count "
                "FROM tags t LEFT JOIN page_tags pt ON pt.tag_id = t.id "
                "GROUP BY t.id ORDER BY count DESC, t.name"
            ).fetchall()
        return [{"name": r["name"], "count": r["count"]} for r in rows]

    if action == "recent":
        limit = int(params.get("limit", 10))
        with db.connection(_require_db_path()) as conn:
            rows = conn.execute(
                "SELECT slug, title, updated_at FROM pages "
                "WHERE status != 'deprecated' "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"slug": r["slug"], "title": r["title"], "updated_at": r["updated_at"]}
            for r in rows
        ]

    if action == "stale":
        days = int(params.get("days", 90))
        with db.connection(_require_db_path()) as conn:
            rows = conn.execute(
                "SELECT p.slug, p.title, p.verified_at, p.updated_at, "
                "CAST(julianday('now') - "
                "julianday(COALESCE(p.verified_at, p.updated_at)) AS INTEGER) "
                "AS days_since "
                "FROM pages p "
                "WHERE p.status = 'active' "
                "AND p.type IN ('entity', 'reference', 'guide') "
                "AND (p.verified_at IS NULL "
                "OR p.verified_at < datetime('now', ?)) "
                "ORDER BY days_since DESC",
                (f"-{days} days",),
            ).fetchall()
        return [dict(r) for r in rows]

    if action == "stale_ranked":
        # Active pages ordered by updated_at ASCENDING (oldest first), with
        # no time cutoff. The "agent's never-reviewed this in a long time"
        # surface. Same tag include/exclude filters as list_pages; the
        # projection is a flat dict per page including tags.
        limit = int(params.get("limit", 10))
        type_ = params.get("type")
        include_tags = params.get("include_tags")
        exclude_tags = params.get("exclude_tags")

        where = ["p.status = 'active'"]
        sql_params: list[Any] = []
        if type_ is not None:
            where.append("p.type = ?")
            sql_params.append(type_)
        if include_tags:
            placeholders = ",".join("?" * len(include_tags))
            where.append(
                f"EXISTS (SELECT 1 FROM page_tags pt "
                f"JOIN tags t ON t.id = pt.tag_id "
                f"WHERE pt.page_id = p.id AND t.name IN ({placeholders}) "
                f"GROUP BY pt.page_id "
                f"HAVING COUNT(DISTINCT t.name) = ?)"
            )
            sql_params.extend(include_tags)
            sql_params.append(len(include_tags))
        if exclude_tags:
            placeholders = ",".join("?" * len(exclude_tags))
            where.append(
                f"NOT EXISTS (SELECT 1 FROM page_tags pt "
                f"JOIN tags t ON t.id = pt.tag_id "
                f"WHERE pt.page_id = p.id AND t.name IN ({placeholders}))"
            )
            sql_params.extend(exclude_tags)

        where_clause = " AND ".join(where)
        sql = (
            f"SELECT p.id, p.slug, p.title, p.type, p.updated_at, "
            f"p.verified_at "
            f"FROM pages p "
            f"WHERE {where_clause} "
            f"ORDER BY p.updated_at ASC LIMIT ?"
        )
        sql_params.append(limit)

        with db.connection(_require_db_path()) as conn:
            rows = conn.execute(sql, sql_params).fetchall()
            page_ids = [r["id"] for r in rows]
            tags_by_page = _fetch_tags_for(conn, page_ids)

        return [
            {
                "slug": r["slug"],
                "title": r["title"],
                "type": r["type"],
                "updated_at": r["updated_at"],
                "verified_at": r["verified_at"],
                "tags": tags_by_page.get(r["id"], []),
            }
            for r in rows
        ]

    if action == "links":
        slug = params.get("slug")
        if slug is None:
            raise ValidationError("slug required for action 'links'")
        with db.connection(_require_db_path()) as conn:
            page, _ = _resolve_page(conn, slug)
            rows = conn.execute(
                "SELECT p.slug, p.title, l.rel, l.origin, l.context "
                "FROM links l JOIN pages p ON p.id = l.target_id "
                "WHERE l.source_id = ? ORDER BY p.slug",
                (page["id"],),
            ).fetchall()
        return [dict(r) for r in rows]

    if action == "backlinks":
        slug = params.get("slug")
        if slug is None:
            raise ValidationError("slug required for action 'backlinks'")
        with db.connection(_require_db_path()) as conn:
            page, _ = _resolve_page(conn, slug)
            rows = conn.execute(
                "SELECT p.slug, p.title, l.rel, l.origin, l.context "
                "FROM links l JOIN pages p ON p.id = l.source_id "
                "WHERE l.target_id = ? ORDER BY p.slug",
                (page["id"],),
            ).fetchall()
        return [dict(r) for r in rows]

    if action == "corrections":
        # The MCP view of the queue is always pending — that's the agent's
        # "what should I fix next" surface. Use REST for resolved/dismissed.
        return list_corrections(status="pending")

    raise ValidationError(f"unknown action {action!r}")


# ---------------------------------------------------------------------------
# Public API: admin
# ---------------------------------------------------------------------------


def admin_link(
    *,
    source_slug: str,
    target_slug: str,
    rel: str = "references",
    context: str | None = None,
) -> dict:
    """Create an explicit link between two pages."""
    with db.connection(_require_db_path()) as conn:
        rel_row = conn.execute(
            "SELECT 1 FROM link_rels WHERE name = ?", (rel,)
        ).fetchone()
        if rel_row is None:
            valid = [
                r["name"]
                for r in conn.execute("SELECT name FROM link_rels").fetchall()
            ]
            raise ValidationError(
                f"rel {rel!r} not in link_rels; got {rel!r}, "
                f"valid values: {valid}"
            )

        source, _ = _resolve_page(conn, source_slug)
        target, _ = _resolve_page(conn, target_slug)

        conn.execute("BEGIN IMMEDIATE")
        _bump_seq(conn)
        conn.execute(
            "INSERT OR IGNORE INTO links "
            "(source_id, target_id, rel, origin, context) "
            "VALUES (?, ?, ?, 'explicit', ?)",
            (source["id"], target["id"], rel, context),
        )
        conn.commit()

    return {
        "source_slug": source_slug,
        "target_slug": target_slug,
        "rel": rel,
        "origin": "explicit",
        "context": context,
    }


def admin_unlink(
    *,
    source_slug: str,
    target_slug: str,
    rel: str | None = None,
) -> dict:
    """Remove links between two pages. If rel is None, removes all rels."""
    with db.connection(_require_db_path()) as conn:
        source, _ = _resolve_page(conn, source_slug)
        target, _ = _resolve_page(conn, target_slug)

        conn.execute("BEGIN IMMEDIATE")
        _bump_seq(conn)
        if rel is None:
            cursor = conn.execute(
                "DELETE FROM links WHERE source_id = ? AND target_id = ?",
                (source["id"], target["id"]),
            )
        else:
            cursor = conn.execute(
                "DELETE FROM links "
                "WHERE source_id = ? AND target_id = ? AND rel = ?",
                (source["id"], target["id"], rel),
            )
        conn.commit()
        removed = cursor.rowcount

    return {
        "source_slug": source_slug,
        "target_slug": target_slug,
        "rel": rel,
        "removed": removed,
    }


def admin_restore(
    *,
    slug: str,
    revision_id: int,
    changed_by: str | None = None,
    client: str | None = None,
) -> dict:
    """Restore a page body to a previous revision.

    Records the new revision as change_type='update' with a change_summary
    that names the rolled-back revision id. The brief spec calls for
    change_type='restore', but the schema's `revisions.change_type` CHECK
    constraint allows only ('create', 'update', 'verify', 'migrate',
    'correct', 'delete') and the Phase 1 schema is locked. The intent is
    preserved in the `change_summary` column and the body of the new
    revision matches the rolled-back revision byte-for-byte.

    The old revision remains in history; a new revision is inserted with the
    restored body.
    """
    with db.connection(_require_db_path()) as conn:
        page, _ = _resolve_page(conn, slug)
        page_id = page["id"]

        old = conn.execute(
            "SELECT id, body, page_id FROM revisions WHERE id = ?",
            (revision_id,),
        ).fetchone()
        if old is None:
            raise NotFoundError(f"revision {revision_id!r} not found")
        if old["page_id"] != page_id:
            raise NotFoundError(
                f"revision {revision_id!r} does not belong to page {slug!r}"
            )

        writer, client_id = _resolve_writer(changed_by, client)

        conn.execute("BEGIN IMMEDIATE")
        new_seq = _bump_seq(conn)
        conn.execute(
            "UPDATE pages SET body = ?, updated_at = datetime('now'), "
            "seq = ? WHERE id = ?",
            (old["body"], new_seq, page_id),
        )
        _rebuild_derived_links(conn, page_id, old["body"])
        _sync_page_chunks(conn, page_id, old["body"])
        cursor = conn.execute(
            "INSERT INTO revisions "
            "(page_id, body, change_type, changed_by, client, change_summary) "
            "VALUES (?, ?, 'update', ?, ?, ?)",
            (
                page_id,
                old["body"],
                writer,
                client_id,
                f"restore to revision {revision_id}",
            ),
        )
        new_revision_id = cursor.lastrowid
        conn.commit()

    with db.connection(_require_db_path()) as conn:
        new_rev = conn.execute(
            "SELECT id, page_id, body, changed_by, client, changed_at, "
            "change_type, change_summary, session_id, source_type, "
            "source_refs, trigger "
            "FROM revisions WHERE id = ?",
            (new_revision_id,),
        ).fetchone()
    return dict(new_rev)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public API: media
# ---------------------------------------------------------------------------
#
# Image upload/list/delete. Media rows store BLOBs; sha256 is uniquely
# indexed so identical bytes deduplicate into one row shared across pages.
# Mirrors the REST /api/media behavior: the dedup check runs BEFORE page
# resolution, and media writes are not page content — no change_seq bump,
# no revision rows.


def upload_media(
    *,
    slug: str,
    data: bytes,
    filename: str | None = None,
    mime_type: str | None = None,
) -> dict:
    """Upload media bytes to a page, deduplicating identical uploads by sha256.

    Mirrors the REST /api/media endpoint: the sha256 dedup check runs before
    page resolution, so re-uploading bytes that already exist returns the
    existing row under its original id even when the target slug differs.
    No change_seq bump and no revision row — media is not page content.
    """
    _validate_slug(slug)
    if not isinstance(data, bytes) or not data:
        raise ValidationError(
            f"data must be non-empty bytes; got {data!r}"
        )
    sha256 = hashlib.sha256(data).hexdigest()

    with db.connection(_require_db_path()) as conn:
        existing = conn.execute(
            "SELECT id, byte_size FROM media WHERE sha256 = ?", (sha256,)
        ).fetchone()
        if existing is not None:
            return {
                "id": existing["id"],
                "sha256": sha256,
                "byte_size": existing["byte_size"],
                "deduped": True,
            }

        page, _ = _resolve_page(conn, slug)

        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "INSERT INTO media "
            "(page_id, filename, mime_type, byte_size, sha256, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                page["id"],
                filename or "upload",
                mime_type or "application/octet-stream",
                len(data),
                sha256,
                data,
            ),
        )
        media_id = cursor.lastrowid
        conn.commit()

    return {
        "id": media_id,
        "sha256": sha256,
        "byte_size": len(data),
        "deduped": False,
    }


def list_media(slug: str) -> list[dict]:
    """List media metadata for a page, oldest row first. Never the BLOB."""
    with db.connection(_require_db_path()) as conn:
        page, _ = _resolve_page(conn, slug)
        rows = conn.execute(
            "SELECT id, filename, mime_type, byte_size, sha256, created_at "
            "FROM media WHERE page_id = ? ORDER BY id ASC",
            (page["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_media(media_id: int) -> dict:
    """Delete a media row by id. No page state changes (no seq bump)."""
    if not isinstance(media_id, int) or isinstance(media_id, bool) or media_id < 1:
        raise ValidationError(
            f"media_id must be a positive int; got {media_id!r}"
        )
    with db.connection(_require_db_path()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "DELETE FROM media WHERE id = ?", (media_id,)
        )
        if cursor.rowcount == 0:
            raise NotFoundError(f"media {media_id} not found")
        conn.commit()
    return {"deleted": True, "media_id": media_id}


# ---------------------------------------------------------------------------
# Public API: correction queue
# ---------------------------------------------------------------------------
#
# One-tap typo/error flagging. The reader flags an issue without
# composing a full agent request. Authorship stays with agents: the actual
# fix is a normal update that the agent records as change_type='correct'
# with the human recorded in the `trigger` field of the new revision.
# The queue is client-facing bookkeeping — it intentionally does NOT bump
# change_seq (no consumer cares about a flag for sync purposes).


_VALID_CORRECTION_STATUSES = ("pending", "resolved", "dismissed")


def _row_to_correction(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "page_slug": row["page_slug"],
        "page_title": row["page_title"],
        "selected_text": row["selected_text"],
        "note": row["note"],
        "status": row["status"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "resolved_by": row["resolved_by"],
    }


def list_corrections(*, status: str | None = None) -> list[dict]:
    """Return corrections, optionally filtered by status. Oldest first.

    A LEFT JOIN keeps the row even if the page was just deprecated (FK still
    holds) — we want to surface the human's flag even when the page is in
    a transitional state. Purged pages cascade-delete the rows, so they
    never appear here.
    """
    where = ""
    params: list[Any] = []
    if status is not None:
        if status not in _VALID_CORRECTION_STATUSES:
            raise ValidationError(
                f"status must be one of {_VALID_CORRECTION_STATUSES!r}; got {status!r}"
            )
        where = "WHERE c.status = ?"
        params.append(status)
    sql = (
        "SELECT c.id, c.page_slug, p.title AS page_title, "
        "       c.selected_text, c.note, c.status, "
        "       c.created_at, c.resolved_at, c.resolved_by "
        "FROM corrections c "
        "LEFT JOIN pages p ON p.slug = c.page_slug "
        f"{where} "
        "ORDER BY c.created_at ASC, c.id ASC"
    )
    with db.connection(_require_db_path()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_correction(r) for r in rows]


def create_correction(
    *,
    page_slug: str,
    selected_text: str,
    note: str | None = None,
) -> dict:
    """Append a pending correction. Raises NotFoundError for unknown pages.

    No change_seq bump: this is client-side bookkeeping, not a page write.
    The transaction is short and read-only w.r.t. change_seq, so a plain
    BEGIN IMMEDIATE + INSERT + COMMIT is correct (avoids the seq-bumping
    helper entirely).
    """
    _validate_slug(page_slug)
    if not isinstance(selected_text, str) or not selected_text.strip():
        raise ValidationError(
            "selected_text must be a non-empty string; got "
            f"{selected_text!r}"
        )
    if note is not None and not isinstance(note, str):
        raise ValidationError(f"note must be a string or null; got {note!r}")

    with db.connection(_require_db_path()) as conn:
        # Resolve the page (follows aliases) so a flag against an old slug
        # lands on the live page. _resolve_page raises NotFoundError if
        # neither the page nor an alias matches.
        page, _ = _resolve_page(conn, page_slug)
        target_slug = page["slug"]

        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO corrections (page_slug, selected_text, note) "
            "VALUES (?, ?, ?)",
            (target_slug, selected_text.strip(), note),
        )
        new_id = cur.lastrowid
        row = conn.execute(
            "SELECT c.id, c.page_slug, p.title AS page_title, "
            "       c.selected_text, c.note, c.status, "
            "       c.created_at, c.resolved_at, c.resolved_by "
            "FROM corrections c "
            "LEFT JOIN pages p ON p.slug = c.page_slug "
            "WHERE c.id = ?",
            (new_id,),
        ).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_correction(row)


def resolve_correction(
    *,
    correction_id: int,
    status: str = "resolved",
    resolved_by: str | None = None,
) -> dict:
    """Mark a correction resolved or dismissed. No page content changes.

    The agent that applies the actual fix is responsible for recording the
    revision with change_type='correct' and the human's identity in the
    `trigger` field; that work happens via update_page, not here.
    """
    if not isinstance(correction_id, int) or isinstance(correction_id, bool):
        raise ValidationError(
            f"correction_id must be an int; got {correction_id!r}"
        )
    if status not in _VALID_CORRECTION_STATUSES:
        raise ValidationError(
            f"status must be one of {_VALID_CORRECTION_STATUSES!r}; got {status!r}"
        )
    if status == "pending":
        # 'pending' is the initial state, not a valid resolution.
        raise ValidationError(
            "status 'pending' is not a valid resolution; use 'resolved' or 'dismissed'"
        )
    if resolved_by is not None and not isinstance(resolved_by, str):
        raise ValidationError(
            f"resolved_by must be a string or null; got {resolved_by!r}"
        )

    with db.connection(_require_db_path()) as conn:
        existing = conn.execute(
            "SELECT id FROM corrections WHERE id = ?", (correction_id,)
        ).fetchone()
        if existing is None:
            raise NotFoundError(f"correction {correction_id} not found")

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE corrections "
            "SET status = ?, resolved_at = datetime('now'), resolved_by = ? "
            "WHERE id = ?",
            (status, resolved_by, correction_id),
        )
        row = conn.execute(
            "SELECT c.id, c.page_slug, p.title AS page_title, "
            "       c.selected_text, c.note, c.status, "
            "       c.created_at, c.resolved_at, c.resolved_by "
            "FROM corrections c "
            "LEFT JOIN pages p ON p.slug = c.page_slug "
            "WHERE c.id = ?",
            (correction_id,),
        ).fetchone()
        conn.commit()
    assert row is not None
    return _row_to_correction(row)
