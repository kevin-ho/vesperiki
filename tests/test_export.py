"""Tests for vesperiki.export: SQLite-to-markdown exporter.

Each test gets a fresh tmp_path DB. The exporter uses ``db.connection`` for
reads (not the service layer), so the writer-identity env vars aren't strictly
required — but ``service.create_page`` is used to build the fixture DB, and
it does require them. The autouse fixture wires the same default values
test_migrate.py / test_service.py use, so the three suites agree on writer
identity.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from vesperiki import db
from vesperiki import export as export_mod
from vesperiki import migrate as migrate_mod
from vesperiki import service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _writer_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VESPERIKI_WRITER", "test_writer")
    monkeypatch.setenv("VESPERIKI_CLIENT", "pytest")
    service.init_service(tmp_path / "export.db")


def _build_fixture_db(tmp_path: Path) -> Path:
    """Build the small wiki described in the spec test plan.

    Returns the db path. Creates three pages with tags, wikilinks between
    them, an explicit depends_on link, a source entry, one page with
    verified_at + confidence set, and a media row inserted directly via SQL.

    Body shapes:
      - page-1 ("forgejo"): wikilink to page-2, image ref to media id 1
      - page-2 ("alpha"):   wikilink to page-3, both markdown and HTML link
                            to page-1 (so we exercise both rewriters)
      - page-3 ("omega"):   plain body, becomes the depends_on target
    """
    db_path = tmp_path / "export.db"

    service.create_page(
        slug="forgejo",
        title="Forgejo",
        body="See [[alpha]] for context.\n\nAnd an image: ![](/api/media/1)\n",
        tags=("infrastructure", "git"),
        type="entity",
        sources=(
            {"type": "research", "ref": "agent-xyz"},
            {"type": "conversation", "ref": "session-123"},
        ),
        changed_by="testwriter",
        client="claude-code",
    )

    service.create_page(
        slug="alpha",
        title="Alpha",
        body=(
            "Links to [[omega]] and a markdown link [omega page](/p/omega) "
            "and an HTML anchor <a href=\"/p/omega\">Omega HTML</a>.\n"
        ),
        tags=("guide",),
        type="guide",
        changed_by="testwriter",
        client="claude-code",
    )

    service.create_page(
        slug="omega",
        title="Omega",
        body="Tail page.\n",
        tags=("concept",),
        type="concept",
        changed_by="testwriter",
        client="claude-code",
    )

    # Explicit depends_on link from forgejo → alpha (deliberately not a wikilink,
    # so we know the explicit-link query path is exercised).
    service.admin_link(
        source_slug="forgejo", target_slug="alpha", rel="depends_on"
    )

    # Verified page with custom confidence. Note: service.update_page doesn't
    # accept a `client` kwarg, so the verify revision uses the fixture's
    # default client ("pytest").
    service.update_page(
        slug="alpha",
        verified_at="2026-07-15T10:00:00Z",
        confidence=0.85,
        changed_by="testwriter",
        client="pytest",
    )

    # Insert a media row directly via SQL. The spec instructs this (the
    # /api/media POST route attaches media to a page slug; we sidestep it
    # and insert a row tied to forgejo's id).
    payload = b"\x89PNG\r\n\x1a\n-fake-png-bytes"
    sha = hashlib.sha256(payload).hexdigest()
    with db.connection(db_path) as conn:
        forgejo_id = conn.execute(
            "SELECT id FROM pages WHERE slug = 'forgejo'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO media "
            "(page_id, filename, mime_type, byte_size, sha256, data) "
            "VALUES (?, ?, 'image/png', ?, ?, ?)",
            (forgejo_id, "icon.png", len(payload), sha, payload),
        )
        conn.commit()

    return db_path


def _conn(db_path: Path) -> sqlite3.Connection:
    return db.connection(db_path)


def _read_page(out_dir: Path, slug: str) -> tuple[dict, str]:
    """Read a page file and split it into (frontmatter_dict, body)."""
    text = (out_dir / f"{slug}.md").read_text(encoding="utf-8")
    fm, body = migrate_mod.parse_frontmatter(text)
    return fm, body


# ---------------------------------------------------------------------------
# Pure-function tests: YAML emitter
# ---------------------------------------------------------------------------


def test_yaml_scalar_plain_string() -> None:
    assert export_mod.yaml_scalar("forgejo") == "forgejo"
    assert export_mod.yaml_scalar("hello world") == "hello world"


def test_yaml_scalar_empty_string() -> None:
    assert export_mod.yaml_scalar("") == '""'


def test_yaml_scalar_quoted_when_colon() -> None:
    assert export_mod.yaml_scalar("Page: Subtitle") == '"Page: Subtitle"'


def test_yaml_scalar_quoted_when_quote() -> None:
    assert export_mod.yaml_scalar('She said "hi"') == '"She said \\"hi\\""'


def test_yaml_scalar_quoted_when_hash() -> None:
    assert export_mod.yaml_scalar("color #ff0000") == '"color #ff0000"'


def test_yaml_scalar_quoted_when_brackets() -> None:
    assert export_mod.yaml_scalar("[a, b]") == '"[a, b]"'
    assert export_mod.yaml_scalar("{a: 1}") == '"{a: 1}"'


def test_yaml_scalar_quoted_when_leading_dash() -> None:
    assert export_mod.yaml_scalar("- leading dash") == '"- leading dash"'


def test_yaml_scalar_quoted_when_keyword() -> None:
    assert export_mod.yaml_scalar("true") == '"true"'
    assert export_mod.yaml_scalar("null") == '"null"'
    assert export_mod.yaml_scalar("yes") == '"yes"'


def test_yaml_scalar_quoted_when_whitespace() -> None:
    assert export_mod.yaml_scalar(" leading") == '" leading"'
    assert export_mod.yaml_scalar("trailing ") == '"trailing "'


def test_yaml_scalar_quoted_when_newline() -> None:
    # The emitter produces a YAML double-quoted form with the newline
    # escaped as the two-character sequence `\n`. The migrate.py parser
    # doesn't unescape, so we don't round-trip — we check the raw output.
    assert export_mod.yaml_scalar("line1\nline2") == '"line1\\nline2"'


def test_yaml_scalar_numbers() -> None:
    assert export_mod.yaml_scalar(0) == "0"
    assert export_mod.yaml_scalar(42) == "42"
    assert export_mod.yaml_scalar(1.0) == "1.0"
    assert export_mod.yaml_scalar(0.5) == "0.5"


def test_yaml_scalar_bools_and_none() -> None:
    assert export_mod.yaml_scalar(True) == "true"
    assert export_mod.yaml_scalar(False) == "false"
    assert export_mod.yaml_scalar(None) == "null"


def test_yaml_scalar_rejects_unknown_type() -> None:
    with pytest.raises(TypeError):
        export_mod.yaml_scalar([1, 2])
    with pytest.raises(TypeError):
        export_mod.yaml_scalar({"a": 1})


def test_render_frontmatter_basic_keys() -> None:
    text = export_mod.render_frontmatter(
        {"title": "Foo", "type": "entity", "tags": ["a", "b"], "confidence": 1.0}
    )
    assert text.startswith("---\n")
    assert "\n---\n" in text
    assert "title: Foo" in text
    assert "type: entity" in text
    assert "tags: [a, b]" in text
    assert "confidence: 1.0" in text


def test_render_frontmatter_block_list_of_dicts() -> None:
    text = export_mod.render_frontmatter(
        {"title": "T", "sources": [{"type": "x", "ref": "y"}]}
    )
    assert "sources:\n  - type: x\n    ref: y\n" in text


def test_render_frontmatter_block_dict() -> None:
    text = export_mod.render_frontmatter(
        {"title": "T", "origin": {"changed_by": "testwriter", "session_id": "abc"}}
    )
    assert "origin:\n  changed_by: testwriter\n  session_id: abc\n" in text


def test_render_frontmatter_round_trip_through_migrate_parser(tmp_path: Path) -> None:
    """Round-trip test: emit YAML, parse it back with the migrate parser."""
    meta = {
        "title": "Forgejo",
        "type": "entity",
        "tags": ["infrastructure", "git"],
        "status": "active",
        "sources": [{"type": "migration", "ref": "quartz-import"}],
        "updated_at": "2026-08-01T12:00:00Z",
        "verified_at": "2026-07-15T10:00:00Z",
        "confidence": 1.0,
        "origin": {"changed_by": "testwriter", "session_id": "abc123"},
        "depends_on": ["alpha", "beta"],
    }
    text = export_mod.render_frontmatter(meta)
    parsed, body = migrate_mod.parse_frontmatter(text)

    assert body == ""
    assert parsed["title"] == "Forgejo"
    assert parsed["type"] == "entity"
    assert parsed["tags"] == ["infrastructure", "git"]
    assert parsed["status"] == "active"
    assert parsed["sources"] == [{"type": "migration", "ref": "quartz-import"}]
    assert parsed["updated_at"] == "2026-08-01T12:00:00Z"
    assert parsed["verified_at"] == "2026-07-15T10:00:00Z"
    assert parsed["confidence"] == 1.0
    assert parsed["origin"] == {"changed_by": "testwriter", "session_id": "abc123"}
    assert parsed["depends_on"] == ["alpha", "beta"]


def test_render_frontmatter_special_chars_escaped_in_output() -> None:
    """Special chars in titles must be properly escaped in the YAML output.

    We can't round-trip through ``migrate_mod.parse_frontmatter`` because
    that parser strips quotes but doesn't unescape escape sequences inside
    them (an acceptable limitation for parsing user-authored markdown —
    Obsidian uses a full YAML parser that does unescape).
    """
    meta = {
        "title": 'Page: "Quoted" Title [with] stuff',
        "type": "entity",
        "tags": ["a:b", 'has "quote"'],
    }
    text = export_mod.render_frontmatter(meta)
    # The colon forces quoting; the inner quote is backslash-escaped.
    assert '"Page: \\"Quoted\\" Title [with] stuff"' in text
    # Tags list with a colon and quote are quoted.
    assert '"a:b"' in text
    assert '"has \\"quote\\""' in text


def test_render_frontmatter_special_chars_no_newline_regression() -> None:
    """A title containing a colon but no quotes is still quoted cleanly."""
    meta = {"title": "Page: Subtitle", "type": "entity"}
    text = export_mod.render_frontmatter(meta)
    parsed, _ = migrate_mod.parse_frontmatter(text)
    # No escapes needed for `:` alone, so the migrate parser handles this.
    assert parsed["title"] == "Page: Subtitle"


def test_render_frontmatter_unicode() -> None:
    """Unicode in titles and tags survives a round-trip."""
    meta = {"title": "Ünïcödé ✨", "tags": ["日本語", "español"]}
    text = export_mod.render_frontmatter(meta)
    parsed, _ = migrate_mod.parse_frontmatter(text)
    assert parsed["title"] == "Ünïcödé ✨"
    assert parsed["tags"] == ["日本語", "español"]


# ---------------------------------------------------------------------------
# Pure-function tests: link rewriter
# ---------------------------------------------------------------------------


def test_rewrite_markdown_link_with_text() -> None:
    body = "See [the welcome doc](/p/onboarding) for context."
    assert export_mod.rewrite_markdown_links(body) == \
        "See [[onboarding|the welcome doc]] for context."


def test_rewrite_markdown_link_text_equals_slug() -> None:
    body = "Link [onboarding](/p/onboarding) here."
    assert export_mod.rewrite_markdown_links(body) == \
        "Link [[onboarding]] here."


def test_rewrite_markdown_link_empty_text() -> None:
    body = "Bare link []( /p/x ) is weird."
    # We don't expect this exact case in practice; sanity-check the regex
    # doesn't accidentally match whitespace-prefixed paths.
    assert export_mod.rewrite_markdown_links(body) == body


def test_rewrite_markdown_link_multiple() -> None:
    body = "See [alpha](/p/alpha) and [beta](/p/beta) and [gamma](/p/gamma)."
    out = export_mod.rewrite_markdown_links(body)
    assert out == "See [[alpha]] and [[beta]] and [[gamma]]."


def test_rewrite_html_link_with_text() -> None:
    body = '<a href="/p/forgejo">Forgejo Page</a>'
    assert export_mod.rewrite_html_links(body) == "[[forgejo|Forgejo Page]]"


def test_rewrite_html_link_text_equals_slug() -> None:
    body = '<a href="/p/forgejo">forgejo</a>'
    assert export_mod.rewrite_html_links(body) == "[[forgejo]]"


def test_rewrite_html_link_with_extra_attrs() -> None:
    body = '<a class="external" href="/p/forgejo" target="_blank">Go</a>'
    assert export_mod.rewrite_html_links(body) == "[[forgejo|Go]]"


def test_rewrite_links_does_not_touch_wikilinks() -> None:
    body = "Already [[linked]] and [[other|with text]] stay."
    assert export_mod.rewrite_links(body) == body


def test_rewrite_links_does_not_touch_plain_text() -> None:
    """`/p/slug` in plain text (not a link shape) stays as-is."""
    body = "Use /p/forgejo as a path; mention /p/notes in prose."
    assert export_mod.rewrite_links(body) == body


def test_rewrite_links_does_not_touch_url_containing_p() -> None:
    """A URL like https://example.com/p/foo (no markdown link shape) is untouched."""
    body = "Visit https://example.com/p/foo for docs."
    assert export_mod.rewrite_links(body) == body


def test_rewrite_links_handles_both_shapes_in_one_body() -> None:
    body = (
        "Markdown [alpha](/p/alpha) and HTML "
        '<a href="/p/beta">B Page</a> together.'
    )
    assert export_mod.rewrite_links(body) == \
        "Markdown [[alpha]] and HTML [[beta|B Page]] together."


def test_rewrite_links_skips_non_slug_href() -> None:
    """URLs whose path segment isn't a valid slug (e.g. percent-encoded,
    has spaces) are left alone — only schema-shaped slugs match."""
    body = '<a href="/p/some%20name">Spaces</a>'
    assert export_mod.rewrite_links(body) == body


# ---------------------------------------------------------------------------
# Pure-function tests: media rewriter
# ---------------------------------------------------------------------------


def test_rewrite_media_markdown_known_id() -> None:
    body = "Image: ![](/api/media/3)"
    out = export_mod.rewrite_media_refs(body, {3: "png"})
    assert out == "Image: ![](media/3.png)"


def test_rewrite_media_markdown_alt_text() -> None:
    body = "Image: ![icon](/api/media/3)"
    out = export_mod.rewrite_media_refs(body, {3: "png"})
    assert out == "Image: ![icon](media/3.png)"


def test_rewrite_media_markdown_alternate_path() -> None:
    """Both /api/media/ and /media/ are recognized."""
    body = "![a](/media/7) and ![b](/api/media/8)"
    out = export_mod.rewrite_media_refs(body, {7: "jpg", 8: "webp"})
    assert out == "![a](media/7.jpg) and ![b](media/8.webp)"


def test_rewrite_media_markdown_unknown_id_left_alone() -> None:
    body = "Image: ![](/api/media/999)"
    out = export_mod.rewrite_media_refs(body, {})
    assert out == body


def test_rewrite_media_html_known_id_no_alt() -> None:
    body = '<img src="/api/media/3">'
    out = export_mod.rewrite_media_refs(body, {3: "png"})
    assert out == "![](media/3.png)"


def test_rewrite_media_html_preserves_alt() -> None:
    body = '<img alt="graph" src="/api/media/3" />'
    out = export_mod.rewrite_media_refs(body, {3: "png"})
    assert out == "![graph](media/3.png)"


def test_rewrite_media_custom_prefix() -> None:
    """The .history files use ../../media/ to reach the media dir."""
    body = "![](/api/media/1)"
    out = export_mod.rewrite_media_refs(body, {1: "png"}, prefix="../../media/")
    assert out == "![](../../media/1.png)"


# ---------------------------------------------------------------------------
# End-to-end export tests
# ---------------------------------------------------------------------------


def test_export_writes_one_md_per_active_page(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"

    summary = export_mod.export(db_path, out_dir)

    assert summary["pages"] == 3
    assert (out_dir / "forgejo.md").exists()
    assert (out_dir / "alpha.md").exists()
    assert (out_dir / "omega.md").exists()


def test_export_summary_counts_match_disk(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"

    summary = export_mod.export(db_path, out_dir)

    md_count = sum(1 for _ in out_dir.glob("*.md"))
    assert summary["pages"] == md_count
    media_count = sum(1 for _ in (out_dir / "media").iterdir())
    assert summary["media"] == media_count
    assert summary["output_dir"] == str(out_dir)


def test_export_writes_media_with_correct_extension(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"

    export_mod.export(db_path, out_dir)

    media_path = out_dir / "media" / "1.png"
    assert media_path.exists()
    # Bytes round-trip exactly.
    assert media_path.read_bytes() == b"\x89PNG\r\n\x1a\n-fake-png-bytes"


def test_export_status_default_excludes_deprecated(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    service.delete_page(slug="omega")  # soft delete: status='deprecated'

    out_dir = tmp_path / "out"
    summary = export_mod.export(db_path, out_dir)

    assert summary["pages"] == 2
    assert not (out_dir / "omega.md").exists()


def test_export_status_all_includes_deprecated(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    service.delete_page(slug="omega")

    out_dir = tmp_path / "out"
    summary = export_mod.export(db_path, out_dir, status="all")

    assert summary["pages"] == 3
    assert (out_dir / "omega.md").exists()


def test_export_status_specific_value(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    service.delete_page(slug="omega")

    out_dir = tmp_path / "out"
    summary = export_mod.export(db_path, out_dir, status="deprecated")

    assert summary["pages"] == 1
    assert (out_dir / "omega.md").exists()
    assert not (out_dir / "forgejo.md").exists()


def test_export_invalid_status_raises(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    with pytest.raises(ValueError):
        export_mod.export(db_path, tmp_path / "out", status="bogus")


# ---------------------------------------------------------------------------
# Frontmatter shape per page
# ---------------------------------------------------------------------------


def test_export_frontmatter_basic_shape(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    fm, _ = _read_page(out_dir, "forgejo")
    assert fm["title"] == "Forgejo"
    assert fm["type"] == "entity"
    assert fm["status"] == "active"
    # Tags come back sorted alphabetically by the service's _fetch_tags.
    assert fm["tags"] == ["git", "infrastructure"]
    assert fm["confidence"] == 1.0
    assert "updated_at" in fm


def test_export_frontmatter_sources(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    fm, _ = _read_page(out_dir, "forgejo")
    assert fm["sources"] == [
        {"type": "research", "ref": "agent-xyz"},
        {"type": "conversation", "ref": "session-123"},
    ]


def test_export_frontmatter_verified_and_confidence(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    fm, _ = _read_page(out_dir, "alpha")
    # alpha got verified_at + confidence=0.85 via update_page.
    assert fm["verified_at"] == "2026-07-15T10:00:00Z"
    assert fm["confidence"] == 0.85


def test_export_frontmatter_omits_verified_at_when_null(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    fm, _ = _read_page(out_dir, "forgejo")
    # forgejo was never verified.
    assert "verified_at" not in fm


def test_export_frontmatter_origin_from_first_revision(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    fm, _ = _read_page(out_dir, "forgejo")
    # forgejo was created with changed_by='testwriter', client='claude-code';
    # session_id was None (not passed explicitly).
    origin = fm["origin"]
    assert origin["changed_by"] == "testwriter"
    assert origin["client"] == "claude-code"
    assert "session_id" not in origin


def test_export_frontmatter_typed_relations(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    # forgejo has an explicit depends_on link to alpha (set via admin_link).
    fm, _ = _read_page(out_dir, "forgejo")
    assert fm["depends_on"] == ["alpha"]

    # No 'references' key for forgejo because the forgejo→alpha link is
    # explicit only via depends_on. (Wikilinks in the body produce derived
    # links, which we deliberately don't emit.)
    assert "references" not in fm


def test_export_frontmatter_omits_empty_relations(tmp_path: Path) -> None:
    """Pages with no explicit links should not have any rel-* keys."""
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    fm, _ = _read_page(out_dir, "omega")
    for rel in ("references", "depends_on", "part_of", "supersedes", "contradicts"):
        assert rel not in fm


# ---------------------------------------------------------------------------
# Body rewrites
# ---------------------------------------------------------------------------


def test_export_body_wikilink_unchanged(tmp_path: Path) -> None:
    """`[[alpha]]` (already a wikilink) stays a wikilink after rewrite."""
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    _, body = _read_page(out_dir, "forgejo")
    assert "[[alpha]]" in body
    # No `/p/` paths in the rewritten body (the markdown-link rewriter
    # would have converted them to wikilinks).
    assert "/p/alpha" not in body


def test_export_body_markdown_link_rewritten(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    _, body = _read_page(out_dir, "alpha")
    # `[omega page](/p/omega)` → `[[omega|omega page]]`
    assert "[[omega|omega page]]" in body
    assert "/p/omega" not in body


def test_export_body_html_link_rewritten(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    _, body = _read_page(out_dir, "alpha")
    # `<a href="/p/omega">Omega HTML</a>` → `[[omega|Omega HTML]]`
    assert "[[omega|Omega HTML]]" in body
    assert '<a href="/p/' not in body


def test_export_body_media_ref_rewritten(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    _, body = _read_page(out_dir, "forgejo")
    # `![](/api/media/1)` → `![](media/1.png)`
    assert "![](media/1.png)" in body
    assert "/api/media/" not in body


# ---------------------------------------------------------------------------
# Obsidian compatibility
# ---------------------------------------------------------------------------


def test_export_obsidian_tags_are_list(tmp_path: Path) -> None:
    """Obsidian requires tags: [a, b] (a list), not tags: a, b."""
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    text = (out_dir / "forgejo.md").read_text(encoding="utf-8")
    # Flow form for the tag list (alphabetically sorted by the service).
    assert "tags: [git, infrastructure]" in text


def test_export_obsidian_wikilinks_use_bare_slugs(tmp_path: Path) -> None:
    """Wikilinks use bare slugs, no prefixes or url-encoding."""
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    text_alpha = (out_dir / "alpha.md").read_text(encoding="utf-8")
    # No url-encoded or prefixed paths inside wikilinks.
    assert "[[omega" in text_alpha
    assert "%20" not in text_alpha
    assert "entities/" not in text_alpha


def test_export_frontmatter_uses_double_quoted_when_needed(tmp_path: Path) -> None:
    """updated_at contains a colon -> the scalar must be quoted."""
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir)

    text = (out_dir / "alpha.md").read_text(encoding="utf-8")
    # verified_at was set to an ISO datetime string with colons.
    assert 'verified_at: "2026-07-15T10:00:00Z"' in text


# ---------------------------------------------------------------------------
# Revision history
# ---------------------------------------------------------------------------


def test_export_with_history_writes_revision_files(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"

    summary = export_mod.export(db_path, out_dir, with_history=True)

    # Each page has at least 1 revision (the create). alpha has 2 (create +
    # the verify-only update). forgejo has 1 (create only).
    assert summary["revisions"] >= 3

    history = out_dir / ".history"
    assert (history / "forgejo").is_dir()
    assert (history / "alpha").is_dir()

    # At least one file per page.
    forgejo_revs = list((history / "forgejo").glob("r*.md"))
    alpha_revs = list((history / "alpha").glob("r*.md"))
    assert len(forgejo_revs) >= 1
    assert len(alpha_revs) >= 2


def test_export_revision_frontmatter_shape(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"
    export_mod.export(db_path, out_dir, with_history=True)

    rev_path = next((out_dir / ".history" / "forgejo").glob("r*.md"))
    text = rev_path.read_text(encoding="utf-8")

    fm, body = migrate_mod.parse_frontmatter(text)
    assert fm["slug"] == "forgejo"
    assert fm["changed_by"] == "testwriter"
    assert fm["client"] == "claude-code"
    assert fm["change_type"] == "create"
    assert "changed_at" in fm
    assert "revision_id" in fm
    # Body was the original forgejo body (with [[alpha]] intact).
    assert "[[alpha]]" in body


def test_export_without_history_omits_history_dir(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    out_dir = tmp_path / "out"

    summary = export_mod.export(db_path, out_dir, with_history=False)
    assert summary["revisions"] == 0
    assert not (out_dir / ".history").exists()


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_requires_output(tmp_path: Path) -> None:
    """`python -m vesperiki.export` without --output exits 2."""
    result = subprocess.run(
        [
            sys.executable, "-m", "vesperiki.export",
            "--db", str(tmp_path / "x.db"),
        ],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 2
    assert "--output" in result.stderr


def test_cli_invalid_status_exits_2(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    result = subprocess.run(
        [
            sys.executable, "-m", "vesperiki.export",
            "--db", str(db_path),
            "--output", str(tmp_path / "out"),
            "--status", "bogus",
        ],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 2
    assert "--status" in result.stderr or "bogus" in result.stderr


def test_cli_happy_path(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    result = subprocess.run(
        [
            sys.executable, "-m", "vesperiki.export",
            "--db", str(db_path),
            "--output", str(tmp_path / "out"),
        ],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "Exported 3 pages" in result.stdout
    assert "1 media files" in result.stdout
    assert (tmp_path / "out" / "forgejo.md").exists()


def test_cli_with_history(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    result = subprocess.run(
        [
            sys.executable, "-m", "vesperiki.export",
            "--db", str(db_path),
            "--output", str(tmp_path / "out"),
            "--with-history",
        ],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "revisions" in result.stdout
    assert (tmp_path / "out" / ".history" / "forgejo").is_dir()


def test_cli_db_path_from_env(tmp_path: Path) -> None:
    """VESPERIKI_DB_PATH supplies the --db argument."""
    db_path = _build_fixture_db(tmp_path)
    result = subprocess.run(
        [
            sys.executable, "-m", "vesperiki.export",
            "--output", str(tmp_path / "out"),
        ],
        capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "VESPERIKI_DB_PATH": str(db_path),
        },
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "out" / "forgejo.md").exists()


def test_cli_status_all_includes_deprecated(tmp_path: Path) -> None:
    db_path = _build_fixture_db(tmp_path)
    service.delete_page(slug="omega")
    result = subprocess.run(
        [
            sys.executable, "-m", "vesperiki.export",
            "--db", str(db_path),
            "--output", str(tmp_path / "out"),
            "--status", "all",
        ],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "Exported 3 pages" in result.stdout
    assert (tmp_path / "out" / "omega.md").exists()
