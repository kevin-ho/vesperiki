"""Tests for vesperiki.migrate: markdown-to-SQLite importer.

Each test gets a fresh tmp_path DB. The migration script uses
``service.create_page`` under the hood, which requires
VESPERIKI_WRITER and VESPERIKI_CLIENT; the autouse fixture below wires
those env vars (the CLI defaults to the same values when run from the
shell, so the tests and the shipped script agree on the writer identity).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from vesperiki import db
from vesperiki import migrate as migrate_mod
from vesperiki import service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _writer_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VESPERIKI_WRITER", "test_writer")
    monkeypatch.setenv("VESPERIKI_CLIENT", "pytest")
    service.init_service(tmp_path / "migrate.db")


def _build_wiki(root: Path) -> Path:
    """Build a representative wiki directory tree under ``root``.

    Covers every shape the importer must handle: folder-to-tag mapping,
    frontmatter parsing, no-frontmatter fallback, wikilink prefix
    stripping, sources as strings vs dicts, and the files/dirs that
    must be skipped.
    """
    wiki = root / "wiki"
    wiki.mkdir()

    # entities/forgejo.md — full frontmatter, wikilinks to several targets
    (wiki / "entities").mkdir()
    (wiki / "entities" / "forgejo.md").write_text(
        "---\n"
        "title: Forgejo\n"
        "type: entity\n"
        "tags: [git, self-hosted]\n"
        "sources:\n"
        "  - url: https://forgejo.org\n"
        "    title: Forgejo Homepage\n"
        "  - url: https://docs.forgejo.org\n"
        "    title: Forgejo Docs\n"
        "date: 2024-01-15\n"
        "---\n"
        "Body. Links to [[article]] and [[infrastructure/edge-router]].\n"
    )

    # entities/with-display.md — wikilink with folder prefix AND display text
    (wiki / "entities" / "with-display.md").write_text(
        "---\n"
        "title: With Display\n"
        "---\n"
        "See [[entities/forgejo|The Forgejo Page]] for context.\n"
    )

    # infrastructure/edge-router.md
    (wiki / "infrastructure").mkdir()
    (wiki / "infrastructure" / "edge-router.md").write_text(
        "---\n"
        "title: Edge Router\n"
        "tags: [router, network]\n"
        "---\n"
        "Infrastructure body.\n"
    )

    # knowledge/article.md
    (wiki / "knowledge").mkdir()
    (wiki / "knowledge" / "article.md").write_text(
        "---\n"
        "title: Knowledge Article\n"
        "---\n"
        "Knowledge body.\n"
    )

    # comparisons/ — folder whose name is not in the special mapping.
    # The folder name becomes a tag verbatim.
    (wiki / "comparisons").mkdir()
    (wiki / "comparisons" / "vs-foo.md").write_text(
        "---\n"
        "title: Vs Foo\n"
        "---\n"
        "Comparison body.\n"
    )

    # Root-level file: no folder tag.
    (wiki / "root-page.md").write_text(
        "---\n"
        "title: Root Page\n"
        "---\n"
        "Root body.\n"
    )

    # No frontmatter — title comes from the first H1.
    (wiki / "from-heading.md").write_text(
        "# Heading Title\n"
        "Body without frontmatter.\n"
    )

    # No frontmatter AND no heading — title falls back to filename stem.
    (wiki / "bare-page.md").write_text(
        "Just a body, no frontmatter, no heading.\n"
    )

    # sources as a block list of bare URL strings.
    (wiki / "string-sources.md").write_text(
        "---\n"
        "title: String Sources\n"
        "sources:\n"
        "  - https://a.com\n"
        "  - https://b.com\n"
        "---\n"
        "Body.\n"
    )

    # _archive/ — must be skipped wholesale.
    (wiki / "_archive").mkdir()
    (wiki / "_archive" / "old.md").write_text(
        "---\n"
        "title: Old Archived\n"
        "---\n"
        "Should not be imported.\n"
    )

    # Quartz bookkeeping at the root — must be skipped by filename.
    (wiki / "index.md").write_text("# Index\n\nIndex body.\n")
    (wiki / "log.md").write_text("# Log\n\nLog body.\n")
    (wiki / "SCHEMA.md").write_text("# Schema\n\nSchema body.\n")

    return wiki


def _conn(db_path: Path) -> sqlite3.Connection:
    return db.connection(db_path)


# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


def test_parse_frontmatter_basic() -> None:
    text = "---\ntitle: Foo\ntype: entity\n---\nBody."
    fm, body = migrate_mod.parse_frontmatter(text)
    assert fm == {"title": "Foo", "type": "entity"}
    assert body == "Body."


def test_parse_frontmatter_missing() -> None:
    text = "# Heading\nBody with no frontmatter."
    fm, body = migrate_mod.parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_parse_frontmatter_unclosed() -> None:
    """Unclosed fence is treated as no frontmatter (whole file is body)."""
    text = "---\ntitle: Foo\nno closing fence"
    fm, body = migrate_mod.parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_parse_frontmatter_complex_sources() -> None:
    text = (
        "---\n"
        "title: T\n"
        "sources:\n"
        "  - url: https://a.com\n"
        "    title: A\n"
        "  - https://b.com\n"
        "  - url: https://c.com\n"
        "    title: C\n"
        "    note: with a colon: in it\n"
        "---\n"
        "body"
    )
    fm, _ = migrate_mod.parse_frontmatter(text)
    assert fm["sources"] == [
        {"url": "https://a.com", "title": "A"},
        "https://b.com",
        {"url": "https://c.com", "title": "C", "note": "with a colon: in it"},
    ]


def test_parse_frontmatter_quoted_titles() -> None:
    text = '---\ntitle: "Page: Subtitle"\n---\nbody'
    fm, _ = migrate_mod.parse_frontmatter(text)
    assert fm["title"] == "Page: Subtitle"


def test_parse_frontmatter_inline_list() -> None:
    text = "---\ntags: [foo, bar, baz]\n---\nbody"
    fm, _ = migrate_mod.parse_frontmatter(text)
    assert fm["tags"] == ["foo", "bar", "baz"]


def test_slug_from_path_root() -> None:
    slug, tag = migrate_mod.slug_from_path(
        Path("/wiki/root.md"), Path("/wiki")
    )
    assert slug == "root"
    assert tag is None


def test_slug_from_path_entities() -> None:
    slug, tag = migrate_mod.slug_from_path(
        Path("/wiki/entities/foo.md"), Path("/wiki")
    )
    assert slug == "foo"
    assert tag == "entity"


def test_slug_from_path_infrastructure() -> None:
    slug, tag = migrate_mod.slug_from_path(
        Path("/wiki/infrastructure/bar.md"), Path("/wiki")
    )
    assert slug == "bar"
    assert tag == "infrastructure"


def test_slug_from_path_knowledge() -> None:
    slug, tag = migrate_mod.slug_from_path(
        Path("/wiki/knowledge/baz.md"), Path("/wiki")
    )
    assert slug == "baz"
    assert tag == "knowledge"


def test_slug_from_path_other_folder_uses_folder_name() -> None:
    slug, tag = migrate_mod.slug_from_path(
        Path("/wiki/comparisons/vs-x.md"), Path("/wiki")
    )
    assert slug == "vs-x"
    assert tag == "comparisons"


def test_slug_from_path_normalizes_case() -> None:
    slug, _ = migrate_mod.slug_from_path(
        Path("/wiki/MyPage.md"), Path("/wiki")
    )
    assert slug == "mypage"


def test_wikilink_target_strip_prefix() -> None:
    assert migrate_mod.wikilink_target("entities/forgejo") == "forgejo"
    assert migrate_mod.wikilink_target("infrastructure/x") == "x"
    assert migrate_mod.wikilink_target("knowledge/y") == "y"
    assert migrate_mod.wikilink_target("plain") == "plain"


def test_transform_wikilinks_strips_prefix_and_display() -> None:
    body = "See [[entities/forgejo|Display]] and [[infrastructure/x]] and [[plain]]."
    out = migrate_mod.transform_wikilinks(body)
    assert out == "See [[forgejo]] and [[x]] and [[plain]]."


def test_derive_title_from_heading() -> None:
    text = "intro\n# Real Title\nmore body"
    assert migrate_mod.derive_title(text, "fallback") == "Real Title"


def test_derive_title_skips_h2() -> None:
    text = "## Subhead\n# Top\nmore"
    assert migrate_mod.derive_title(text, "fallback") == "Top"


def test_derive_title_falls_back() -> None:
    text = "no heading here"
    assert migrate_mod.derive_title(text, "fallback") == "fallback"


def test_normalize_date_iso() -> None:
    assert migrate_mod.normalize_date("2024-01-15") == "2024-01-15"
    assert migrate_mod.normalize_date("2024-01-15T12:00:00") == "2024-01-15T12:00:00"
    assert migrate_mod.normalize_date("2024-01-15T12:00:00Z") == "2024-01-15T12:00:00+00:00"


def test_normalize_date_unparseable() -> None:
    assert migrate_mod.normalize_date("January 15, 2024") is None
    assert migrate_mod.normalize_date("") is None
    assert migrate_mod.normalize_date("not a date") is None


# ---------------------------------------------------------------------------
# End-to-end migration tests
# ---------------------------------------------------------------------------


def test_migrate_page_count_and_slugs(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    summary = migrate_mod.migrate(
        wiki, str(tmp_path / "test.db"), writer="migrate", client="migration-script"
    )
    # 8 content files: forgejo, with-display, edge-router, article, vs-foo,
    # root-page, from-heading, bare-page, string-sources = 9.
    assert summary["pages_imported"] == 9
    assert summary["pages_errored"] == 0
    assert summary["errors"] == []

    with _conn(tmp_path / "test.db") as conn:
        slugs = {
            r["slug"]
            for r in conn.execute("SELECT slug FROM pages ORDER BY slug")
        }
    assert slugs == {
        "forgejo",
        "with-display",
        "edge-router",
        "article",
        "vs-foo",
        "root-page",
        "from-heading",
        "bare-page",
        "string-sources",
    }


def test_migrate_folder_to_tag_mapping(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        tags_by_slug: dict[str, set[str]] = {}
        for r in conn.execute(
            "SELECT p.slug, t.name FROM pages p "
            "JOIN page_tags pt ON pt.page_id = p.id "
            "JOIN tags t ON t.id = pt.tag_id"
        ):
            tags_by_slug.setdefault(r["slug"], set()).add(r["name"])

    # entities/ -> 'entity' tag
    assert "entity" in tags_by_slug["forgejo"]
    # infrastructure/ -> 'infrastructure' tag
    assert "infrastructure" in tags_by_slug["edge-router"]
    # knowledge/ -> 'knowledge' tag
    assert "knowledge" in tags_by_slug["article"]
    # comparisons/ -> folder name verbatim
    assert "comparisons" in tags_by_slug["vs-foo"]
    # root-level -> no folder tag, no frontmatter tags: no tag rows at all.
    assert "root-page" not in tags_by_slug


def test_migrate_frontmatter_tags_merged_with_folder_tag(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        tags = {
            r["name"]
            for r in conn.execute(
                "SELECT t.name FROM page_tags pt "
                "JOIN tags t ON t.id = pt.tag_id "
                "JOIN pages p ON p.id = pt.page_id "
                "WHERE p.slug = 'forgejo'"
            )
        }
    # forgejo: folder 'entity' + frontmatter [git, self-hosted]
    assert tags == {"entity", "git", "self-hosted"}


def test_migrate_wikilinks_resolved(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        # forgejo's body references [[article]] (in knowledge/) and
        # [[infrastructure/edge-router]] — both should be linked.
        links = conn.execute(
            "SELECT pt.slug FROM links l "
            "JOIN pages ps ON ps.id = l.source_id "
            "JOIN pages pt ON pt.id = l.target_id "
            "WHERE ps.slug = 'forgejo' ORDER BY pt.slug"
        ).fetchall()
        link_slugs = [r["slug"] for r in links]
    assert "article" in link_slugs
    assert "edge-router" in link_slugs


def test_migrate_wikilink_prefix_stripping(tmp_path: Path) -> None:
    """[[entities/forgejo|Display]] resolves to a link to forgejo."""
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        link = conn.execute(
            "SELECT pt.slug, l.origin FROM links l "
            "JOIN pages ps ON ps.id = l.source_id "
            "JOIN pages pt ON pt.id = l.target_id "
            "WHERE ps.slug = 'with-display'"
        ).fetchone()
    assert link is not None
    assert link["slug"] == "forgejo"
    # Service marks derived links as such.
    assert link["origin"] == "derived"


def test_migrate_wikilink_body_normalized(tmp_path: Path) -> None:
    """The stored body has the prefix stripped and display text dropped."""
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        body = conn.execute(
            "SELECT body FROM pages WHERE slug = 'with-display'"
        ).fetchone()["body"]
    # Original: [[entities/forgejo|The Forgejo Page]]
    # After transform: [[forgejo]]
    assert "[[forgejo]]" in body
    assert "entities/" not in body
    assert "The Forgejo Page" not in body


def test_migrate_skip_dirs(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    summary = migrate_mod.migrate(wiki, str(tmp_path / "test.db"))
    # _archive/old.md must not be imported.
    with _conn(tmp_path / "test.db") as conn:
        slugs = {
            r["slug"]
            for r in conn.execute("SELECT slug FROM pages")
        }
    assert "old" not in slugs
    assert summary["pages_imported"] == 9


def test_migrate_skip_files(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        slugs = {
            r["slug"]
            for r in conn.execute("SELECT slug FROM pages")
        }
    # Quartz bookkeeping files at the root must not become pages.
    assert "index" not in slugs
    assert "log" not in slugs
    assert "schema" not in slugs


def test_migrate_custom_skip_dirs(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    # Add a file in a custom skip dir.
    (wiki / "drafts").mkdir()
    (wiki / "drafts" / "wip.md").write_text(
        "---\ntitle: WIP\n---\nBody."
    )

    summary = migrate_mod.migrate(
        wiki,
        str(tmp_path / "test.db"),
        skip_dirs=("_archive", "raw", "drafts"),
    )
    with _conn(tmp_path / "test.db") as conn:
        slugs = {
            r["slug"] for r in conn.execute("SELECT slug FROM pages")
        }
    assert "wip" not in slugs
    assert summary["pages_imported"] == 9


def test_migrate_no_frontmatter_title_from_heading(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        title = conn.execute(
            "SELECT title FROM pages WHERE slug = 'from-heading'"
        ).fetchone()["title"]
    assert title == "Heading Title"


def test_migrate_no_frontmatter_title_from_filename(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        title = conn.execute(
            "SELECT title FROM pages WHERE slug = 'bare-page'"
        ).fetchone()["title"]
    # No frontmatter, no heading — falls back to filename stem.
    assert title == "bare-page"


def test_migrate_type_default_reference(tmp_path: Path) -> None:
    """Pages without a `type:` field default to 'reference'."""
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        types = {
            r["slug"]: r["type"]
            for r in conn.execute("SELECT slug, type FROM pages")
        }
    # Files with no `type:` in frontmatter.
    assert types["edge-router"] == "reference"
    assert types["article"] == "reference"
    assert types["root-page"] == "reference"
    # forgejo explicitly set type: entity.
    assert types["forgejo"] == "entity"


def test_migrate_sources_as_dicts(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        sources_json = conn.execute(
            "SELECT sources FROM pages WHERE slug = 'forgejo'"
        ).fetchone()["sources"]
    parsed = json.loads(sources_json)
    assert parsed == [
        {"url": "https://forgejo.org", "title": "Forgejo Homepage"},
        {"url": "https://docs.forgejo.org", "title": "Forgejo Docs"},
    ]


def test_migrate_sources_as_strings(tmp_path: Path) -> None:
    """String entries are wrapped to {"title": s} for shape uniformity."""
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        sources_json = conn.execute(
            "SELECT sources FROM pages WHERE slug = 'string-sources'"
        ).fetchone()["sources"]
    parsed = json.loads(sources_json)
    assert parsed == [
        {"title": "https://a.com"},
        {"title": "https://b.com"},
    ]


def test_migrate_updated_at_preserved(tmp_path: Path) -> None:
    """Frontmatter `date` becomes pages.updated_at exactly."""
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        updated_at = conn.execute(
            "SELECT updated_at FROM pages WHERE slug = 'forgejo'"
        ).fetchone()["updated_at"]
    assert updated_at == "2024-01-15"


def test_migrate_change_type_migrate(tmp_path: Path) -> None:
    """The inserted revision is stamped change_type='migrate', source_type='migration'."""
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        rev = conn.execute(
            "SELECT change_type, source_type, changed_by, client "
            "FROM revisions WHERE page_id = (SELECT id FROM pages WHERE slug='forgejo')"
        ).fetchone()
    assert rev["change_type"] == "migrate"
    assert rev["source_type"] == "migration"
    assert rev["changed_by"] == "migrate"
    assert rev["client"] == "migration-script"


def test_migrate_fts_search_works(tmp_path: Path) -> None:
    """FTS5 must remain queryable after the import — pages_fts is populated
    by the live triggers that fire on each service.create_page call.
    """
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        rows = conn.execute(
            "SELECT p.slug FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
            "WHERE pages_fts MATCH ?",
            ("forgejo",),
        ).fetchall()
    slugs = {r["slug"] for r in rows}
    assert "forgejo" in slugs


def test_migrate_dry_run_writes_nothing(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    db_path = tmp_path / "dry.db"

    summary = migrate_mod.migrate(
        wiki, str(db_path), dry_run=True
    )
    # Parsed but not written.
    assert summary["pages_imported"] == 9
    assert summary["pages_errored"] == 0
    assert len(summary["pages"]) == 9
    # No DB file should exist.
    assert not db_path.exists()
    # Even sidecar files (WAL, SHM) shouldn't be left behind.
    assert not (tmp_path / "dry.db-wal").exists()
    assert not (tmp_path / "dry.db-shm").exists()


def test_migrate_dry_run_returns_parsed_pages(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    summary = migrate_mod.migrate(
        wiki, str(tmp_path / "x.db"), dry_run=True
    )
    slugs = {p.slug for p in summary["pages"]}
    assert "forgejo" in slugs
    assert "edge-router" in slugs
    assert "vs-foo" in slugs


def test_migrate_force_bypasses_dedup(tmp_path: Path) -> None:
    """Two files with the same title both land — migration uses force=True."""
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "a.md").write_text("---\ntitle: Same Title\n---\nbody 1")
    (wiki / "b.md").write_text("---\ntitle: Same Title\n---\nbody 2")

    summary = migrate_mod.migrate(wiki, str(tmp_path / "test.db"))
    assert summary["pages_imported"] == 2

    with _conn(tmp_path / "test.db") as conn:
        titles = sorted(
            r["title"] for r in conn.execute("SELECT title FROM pages")
        )
    assert titles == ["Same Title", "Same Title"]


def test_migrate_returns_link_and_tag_counts(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    summary = migrate_mod.migrate(wiki, str(tmp_path / "test.db"))
    # At least the wikilinks between forgejo and article/edge-router.
    assert summary["links"] >= 2
    # Multiple unique tags (entity, infrastructure, knowledge, comparisons,
    # git, self-hosted, router, network).
    assert summary["tags"] >= 6


def test_migrate_missing_source_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        migrate_mod.migrate(
            tmp_path / "nonexistent", str(tmp_path / "x.db")
        )


def test_migrate_writes_to_pages_fts_tags_column(tmp_path: Path) -> None:
    """The denormalized `tags` column in pages_fts is populated so that
    tag-name searches hit the index.
    """
    wiki = _build_wiki(tmp_path)
    migrate_mod.migrate(wiki, str(tmp_path / "test.db"))

    with _conn(tmp_path / "test.db") as conn:
        # FTS5 MATCH on a tag name should find the page that carries it.
        rows = conn.execute(
            "SELECT p.slug FROM pages_fts JOIN pages p ON p.id = pages_fts.rowid "
            "WHERE pages_fts MATCH ?",
            ("entity",),
        ).fetchall()
    slugs = {r["slug"] for r in rows}
    # forgejo and with-display are in entities/ and carry the 'entity' tag.
    assert "forgejo" in slugs
    assert "with-display" in slugs


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


def test_cli_requires_source(tmp_path: Path) -> None:
    """`python -m vesperiki.migrate` without --source exits 2."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vesperiki.migrate",
            "--db",
            str(tmp_path / "x.db"),
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 2
    assert "--source is required" in result.stderr


def test_cli_dry_run_via_subprocess(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    db_path = tmp_path / "cli.db"
    env = {"PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vesperiki.migrate",
            "--source",
            str(wiki),
            "--db",
            str(db_path),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "Imported 9 pages" in result.stdout
    assert not db_path.exists()


def test_cli_source_from_env(tmp_path: Path) -> None:
    wiki = _build_wiki(tmp_path)
    db_path = tmp_path / "env.db"
    env = {
        "PATH": "/usr/bin:/bin",
        "VESPERIKI_MIGRATE_SOURCE": str(wiki),
        "VESPERIKI_DB_PATH": str(db_path),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "vesperiki.migrate",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert db_path.exists()
    with _conn(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    assert count == 9
