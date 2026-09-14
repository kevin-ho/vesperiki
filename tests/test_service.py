"""Tests for vesperiki.service: write/read logic + dedup gate.

Every test gets a fresh tmp_path db. Writer identity is supplied via
VESPERIKI_WRITER / VESPERIKI_CLIENT env vars (set in a session-scoped fixture)
so tests read clearly; individual tests may also pass changed_by / client
explicitly to verify the override path.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from vesperiki import db as vdb
from vesperiki import service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _writer_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default writer identity from env. Tests can still pass kwarg overrides.

    autouse=True so every test has the env wired without each test having to
    remember. The tmp_path is per-test, so each gets a fresh DB.
    """
    monkeypatch.setenv("VESPERIKI_WRITER", "test_writer")
    monkeypatch.setenv("VESPERIKI_CLIENT", "pytest")
    service.init_service(tmp_path / "test.db")


def _page_id(conn, slug: str) -> int:
    """Tiny helper: look up a page id directly via a fresh connection."""
    row = conn.execute("SELECT id FROM pages WHERE slug = ?", (slug,)).fetchone()
    assert row is not None, f"page {slug!r} not found"
    return row["id"]


# ---------------------------------------------------------------------------
# Phase 2: write/read
# ---------------------------------------------------------------------------

def test_create_page_basic(tmp_path: Path) -> None:
    r = service.create_page(
        slug="northwind", title="Northwind", body="hello world", tags=("net",)
    )
    assert r["slug"] == "northwind"
    assert r["title"] == "Northwind"
    assert r["body"] == "hello world"
    assert r["type"] == "entity"
    assert r["status"] == "active"
    assert r["tags"] == ["net"]

    g = service.get_page("northwind")
    assert g["slug"] == "northwind"
    assert g["title"] == "Northwind"
    assert g["body"] == "hello world"
    assert g["tags"] == ["net"]


def test_create_page_generates_revision(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="first body")
    g = service.get_page("a", include_revisions=True)
    assert len(g["revisions"]) == 1
    rev = g["revisions"][0]
    assert rev["change_type"] == "create"
    assert rev["body"] == "first body"
    assert rev["changed_by"] == "test_writer"
    assert rev["client"] == "pytest"


def test_create_page_normalizes_title(tmp_path: Path) -> None:
    """title_norm: lowercase, strip everything except a-z0-9.

    "Northwind" -> "northwind", "North Wind" -> "northwind",
    "north-wind" -> "northwind". Stored on the pages row.
    """
    service.create_page(slug="a", title="Northwind", body="x")

    with vdb.connection(tmp_path / "test.db") as conn:
        title_norm = conn.execute(
            "SELECT title_norm FROM pages WHERE slug='a'"
        ).fetchone()["title_norm"]
    assert title_norm == "northwind"

    # "North Wind" and "north-wind" all collapse to "northwind".
    for variant, slug in (("North Wind", "b"), ("north-wind", "c")):
        try:
            service.create_page(slug=slug, title=variant, body="x")
        except service.DuplicateError:
            # Title-norm collision is the whole point — proving the normalization.
            pass
        else:
            pytest.fail(f"expected DuplicateError for {variant!r}")


def test_update_page_creates_revision(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="v1")
    service.update_page(slug="a", body="v2 body")
    g = service.get_page("a", include_revisions=True)
    # Two revisions: most recent first (ORDER BY changed_at DESC).
    assert len(g["revisions"]) == 2
    assert g["revisions"][0]["change_type"] == "update"
    assert g["revisions"][0]["body"] == "v2 body"  # POST-change
    assert g["body"] == "v2 body"  # current page == latest revision


def test_update_page_rebuilds_derived_links(tmp_path: Path) -> None:
    """Removing [[b]] from the body drops the derived link."""
    service.create_page(slug="b", title="B", body="target body")
    service.create_page(slug="a", title="A", body="see [[b]] for details")

    # First confirm the derived link was built.
    g_a = service.get_page("a", expand_links=True)
    assert [l["slug"] for l in g_a["links"]] == ["b"]

    # Update body without the wikilink.
    service.update_page(slug="a", body="plain text, no link")
    g_a2 = service.get_page("a", expand_links=True)
    assert g_a2["links"] == []


def test_update_page_preserves_explicit_links(tmp_path: Path) -> None:
    """Explicit link survives a body update; derived ones do not."""
    service.create_page(slug="a", title="A", body="")
    service.create_page(slug="b", title="B", body="target")
    service.admin_link(source_slug="a", target_slug="b", rel="depends_on")

    # Body update that introduces a different derived link AND keeps a's body.
    service.update_page(slug="a", body="see [[c]]")  # unrelated derived link
    # c doesn't exist, so no derived link is added; the explicit one to b
    # must still be there.
    g = service.get_page("a", expand_links=True)
    slugs = {l["slug"] for l in g["links"]}
    assert "b" in slugs  # explicit link survived


def test_update_page_with_tags(tmp_path: Path) -> None:
    service.create_page(
        slug="a", title="A", body="x", tags=("old", "shared")
    )
    service.update_page(slug="a", tags=("new", "shared"))
    g = service.get_page("a")
    # Tags are sorted by name in _fetch_tags_for.
    assert g["tags"] == ["new", "shared"]


def test_update_page_no_op_skips_write(tmp_path: Path) -> None:
    """update_page with no fields must not bump updated_at or insert a revision.

    A call like update_page(slug='a') with every optional field left None
    is a no-op: the page exists, the six change flags are all False, and
    there is nothing to record. Skipping the write transaction keeps the
    seq counter steady and avoids an empty revision row that would
    otherwise show up in history and sync cursors.
    """
    service.create_page(slug="a", title="A", body="v1")

    before = service.get_page("a", include_revisions=True)
    updated_at_before = before["updated_at"]
    rev_count_before = len(before["revisions"])

    r = service.update_page(slug="a")

    after = service.get_page("a", include_revisions=True)
    assert after["updated_at"] == updated_at_before
    assert len(after["revisions"]) == rev_count_before
    # Returned dict is the unchanged page.
    assert r["slug"] == "a"
    assert r["body"] == "v1"


def test_update_page_title_updates_title_and_norm(tmp_path: Path) -> None:
    """update_page(title=...) changes title + title_norm and records a revision.

    The FTS trigger on pages keeps pages_fts in sync, so a search for the
    new title term should rank the page via its title column.
    """
    service.create_page(slug="a", title="Old Title", body="x")
    service.update_page(slug="a", title="New Title!")

    g = service.get_page("a", include_revisions=True)
    assert g["title"] == "New Title!"
    assert g["revisions"][0]["change_type"] == "update"

    with vdb.connection(tmp_path / "test.db") as conn:
        title_norm = conn.execute(
            "SELECT title_norm FROM pages WHERE slug='a'"
        ).fetchone()["title_norm"]
    assert title_norm == "newtitle"  # lowercase, punctuation/space-stripped

    # FTS title column is in sync — searching for a token in the new title
    # finds it (title is tokenized on whitespace, so 'newtitle' as a single
    # token would NOT match 'New Title!' — search 'new' instead).
    hits = service.search_pages(query="new")
    assert hits[0]["slug"] == "a"


def test_update_page_title_same_value_is_noop(tmp_path: Path) -> None:
    """Passing the current title must not bump updated_at or add a revision."""
    service.create_page(slug="a", title="Same", body="x")
    before = service.get_page("a", include_revisions=True)
    updated_at_before = before["updated_at"]
    rev_count_before = len(before["revisions"])

    service.update_page(slug="a", title="Same")

    after = service.get_page("a", include_revisions=True)
    assert after["updated_at"] == updated_at_before
    assert len(after["revisions"]) == rev_count_before


def test_update_page_title_with_tags_and_body(tmp_path: Path) -> None:
    """Combining title + tags + body in one update applies all three."""
    service.create_page(slug="a", title="Old", body="v1", tags=("old",))
    service.update_page(
        slug="a", title="New", body="v2", tags=("new", "shared")
    )
    g = service.get_page("a")
    assert g["title"] == "New"
    assert g["body"] == "v2"
    assert g["tags"] == ["new", "shared"]


def test_delete_soft(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="x")
    r = service.delete_page(slug="a", purge=False)
    assert r == {"slug": "a", "status": "deprecated", "purged": False}
    # Still readable.
    g = service.get_page("a")
    assert g["status"] == "deprecated"


def test_delete_purge_cascades(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="hello")
    # Add some media and links first so we can verify their cascading.
    with vdb.connection(tmp_path / "test.db") as conn:
        page_id = _page_id(conn, "a")
        conn.execute(
            "INSERT INTO media (page_id, filename, mime_type, byte_size, sha256, data) "
            "VALUES (?, 'f.bin', 'application/octet-stream', 3, 'abc', X'deadbeef')",
            (page_id,),
        )

    r = service.delete_page(slug="a", purge=True)
    assert r["purged"] is True

    with vdb.connection(tmp_path / "test.db") as conn:
        # Page and revisions gone.
        assert conn.execute("SELECT 1 FROM pages WHERE slug='a'").fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM revisions WHERE page_id=?", (page_id,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM media WHERE page_id=?", (page_id,)
        ).fetchone() is None
        # Tombstone present.
        tomb = conn.execute(
            "SELECT 1 FROM tombstones WHERE slug='a'"
        ).fetchone()
        assert tomb is not None


def test_purge_then_recreate_clears_tombstone(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="v1")
    service.delete_page(slug="a", purge=True)

    with vdb.connection(tmp_path / "test.db") as conn:
        assert conn.execute(
            "SELECT 1 FROM tombstones WHERE slug='a'"
        ).fetchone() is not None

    service.create_page(slug="a", title="A2", body="v2")

    with vdb.connection(tmp_path / "test.db") as conn:
        assert conn.execute(
            "SELECT 1 FROM tombstones WHERE slug='a'"
        ).fetchone() is None


def test_revive_page_flips_deprecated_to_active(tmp_path: Path) -> None:
    """revive_page restores a soft-deleted page to active visibility."""
    service.create_page(slug="a", title="A", body="x")
    service.delete_page(slug="a")
    g_dep = service.get_page("a")
    assert g_dep["status"] == "deprecated"

    g_rev = service.revive_page(slug="a")
    assert g_rev["status"] == "active"

    # Page appears in the default list (active only by default).
    listing = service.list_pages()
    slugs = [p["slug"] for p in listing["pages"]]
    assert "a" in slugs


def test_revive_page_already_active_is_noop(tmp_path: Path) -> None:
    """Reviving an active page must not insert a revision or change seq."""
    service.create_page(slug="a", title="A", body="x")
    before = service.get_page("a", include_revisions=True)
    rev_count_before = len(before["revisions"])

    with vdb.connection(str(tmp_path / "test.db")) as conn:
        seq_before = conn.execute(
            "SELECT value FROM change_seq WHERE id = 1"
        ).fetchone()["value"]

    r = service.revive_page(slug="a")
    assert r["status"] == "active"

    after = service.get_page("a", include_revisions=True)
    assert len(after["revisions"]) == rev_count_before

    with vdb.connection(str(tmp_path / "test.db")) as conn:
        seq_after = conn.execute(
            "SELECT value FROM change_seq WHERE id = 1"
        ).fetchone()["value"]
    assert seq_after == seq_before


def test_wikilink_parsing_creates_derived_links(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="intro")
    service.create_page(slug="foo", title="Foo", body="f")
    service.create_page(slug="bar", title="Bar", body="b")
    service.create_page(slug="c", title="C", body="see [[foo]] and [[bar]]")
    g = service.get_page("c", expand_links=True)
    slugs = sorted(l["slug"] for l in g["links"])
    assert slugs == ["bar", "foo"]


def test_get_page_resolves_alias(tmp_path: Path) -> None:
    """If a slug is in slug_aliases, get_page follows it.

    No rename_page exists in v1, so we seed the alias directly. This is the
    mechanism slugs survive page renames — the renamed slug becomes an alias
    pointing at the new page row.
    """
    service.create_page(slug="foo", title="Foo", body="x")
    with vdb.connection(tmp_path / "test.db") as conn:
        page_id = _page_id(conn, "foo")
        conn.execute(
            "INSERT INTO slug_aliases (alias, page_id) VALUES (?, ?)",
            ("old-foo", page_id),
        )
        conn.commit()

    g = service.get_page("old-foo")
    assert g["slug"] == "foo"
    assert g["redirected_from"] == "old-foo"


def test_get_page_404(tmp_path: Path) -> None:
    with pytest.raises(service.NotFoundError):
        service.get_page("nonexistent")


def test_list_pages_pagination(tmp_path: Path) -> None:
    for i in range(10):
        service.create_page(slug=f"p{i:02d}", title=f"Page {i}", body="x")

    seen_ids: list[int] = []
    cursor = None
    pages_with_cursor: list[int] = []
    for _ in range(4):  # 10 pages / limit 3 = 4 calls (3+3+3+1)
        page = service.list_pages(limit=3, cursor=cursor)
        assert len(page["pages"]) <= 3
        seen_ids.extend(p["id"] for p in page["pages"])
        if page["next_cursor"] is not None:
            pages_with_cursor.append(len(page["pages"]))
        cursor = page["next_cursor"]

    # All 10 ids, no duplicates, monotonically increasing.
    assert len(seen_ids) == 10
    assert len(set(seen_ids)) == 10
    assert seen_ids == sorted(seen_ids)

    # First three pages are full (3 each), last has the remainder.
    assert pages_with_cursor == [3, 3, 3]
    assert cursor is None


# ---------------------------------------------------------------------------
# Feature 5: list_pages include/exclude tag filters
# ---------------------------------------------------------------------------

def _make_abc_pages() -> None:
    """Three pages with overlapping tag sets for the filter tests.

    A: ('decommissioned',)
    B: ('reference',)
    C: ('decommissioned', 'reference')
    """
    service.create_page(
        slug="page-a", title="Page A", body="x", tags=("decommissioned",)
    )
    service.create_page(
        slug="page-b", title="Page B", body="x", tags=("reference",)
    )
    service.create_page(
        slug="page-c",
        title="Page C",
        body="x",
        tags=("decommissioned", "reference"),
    )


def test_list_pages_exclude_tags_drops_any_match(tmp_path: Path) -> None:
    """exclude_tags drops any page carrying ANY of the listed tags.

    A and C both have 'decommissioned', so exclude_tags=['decommissioned']
    leaves only B.
    """
    _make_abc_pages()
    listing = service.list_pages(exclude_tags=["decommissioned"])
    slugs = sorted(p["slug"] for p in listing["pages"])
    assert slugs == ["page-b"]


def test_list_pages_no_tag_filter_returns_all(tmp_path: Path) -> None:
    """Without tag filters, all pages (default status='active') are returned."""
    _make_abc_pages()
    listing = service.list_pages()
    slugs = sorted(p["slug"] for p in listing["pages"])
    assert slugs == ["page-a", "page-b", "page-c"]


def test_list_pages_include_tags_single(tmp_path: Path) -> None:
    """include_tags=['reference'] keeps pages that have 'reference': B and C."""
    _make_abc_pages()
    listing = service.list_pages(include_tags=["reference"])
    slugs = sorted(p["slug"] for p in listing["pages"])
    assert slugs == ["page-b", "page-c"]


def test_list_pages_include_tags_requires_all(tmp_path: Path) -> None:
    """include_tags requires ALL tags (AND): only C has both."""
    _make_abc_pages()
    listing = service.list_pages(
        include_tags=["decommissioned", "reference"]
    )
    slugs = [p["slug"] for p in listing["pages"]]
    assert slugs == ["page-c"]


def test_list_pages_empty_tag_lists_behave_like_none(tmp_path: Path) -> None:
    """Empty include_tags / exclude_tags are no-ops, just like None."""
    _make_abc_pages()
    listing = service.list_pages(include_tags=[], exclude_tags=[])
    slugs = sorted(p["slug"] for p in listing["pages"])
    assert slugs == ["page-a", "page-b", "page-c"]


# ---------------------------------------------------------------------------
# Phase 2b: search
# ---------------------------------------------------------------------------

def test_search_basic(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="the quick brown fox jumps")
    results = service.search_pages(query="fox")
    assert len(results) >= 1
    slugs = {r["slug"] for r in results}
    assert "fox" in slugs


def test_search_snippet(tmp_path: Path) -> None:
    service.create_page(
        slug="fox", title="Fox", body="the quick brown fox jumps over the lazy dog"
    )
    results = service.search_pages(query="fox")
    assert len(results) >= 1
    # snippet() returns text with the matched term highlighted by surrounding
    # delimiters (we pass '' '' for the open/close markers, so the result is a
    # plain-text window around the match).
    assert results[0]["snippet"]
    assert "fox" in results[0]["snippet"].lower()


def test_search_tag_filter(tmp_path: Path) -> None:
    service.create_page(
        slug="match1", title="Match", body="alpha beta gamma", tags=("keep",)
    )
    service.create_page(
        slug="match2", title="Other", body="alpha epsilon zeta", tags=("drop",)
    )
    r = service.search_pages(query="alpha", tag="keep")
    slugs = {x["slug"] for x in r}
    assert "match1" in slugs
    assert "match2" not in slugs


def test_search_bm25_title_ranks_higher(tmp_path: Path) -> None:
    """Page with "fox" in the title outranks one with "fox" only in the body.

    bm25 weights title=5x, tags=3x, body=1x, so a title hit pulls a result
    closer to the front of the relevance ordering.
    """
    service.create_page(slug="body", title="Unrelated", body="fox fox fox fox")
    service.create_page(slug="title", title="Fox", body="something else entirely")
    results = service.search_pages(query="fox")
    # title hit ranks ahead of body hit.
    slugs = [r["slug"] for r in results]
    assert slugs.index("title") < slugs.index("body")


# ---------------------------------------------------------------------------
# Phase 3: dedup gate
# ---------------------------------------------------------------------------

def test_dedup_title_norm_match(tmp_path: Path) -> None:
    """Create "Northwind" then "North Wind" → DuplicateError."""
    service.create_page(slug="northwind", title="Northwind", body="hello")
    with pytest.raises(service.DuplicateError) as exc:
        service.create_page(slug="another", title="North Wind", body="hello")
    candidates = exc.value.details["candidates"]
    assert any(c["slug"] == "northwind" for c in candidates)
    assert candidates[0]["reason"] == "title_norm_exact"


def test_dedup_bm25_match(tmp_path: Path) -> None:
    """Different title, but body overlap triggers BM25 hit.

    BM25 needs a non-trivial corpus to return meaningful negative scores
    (IDF is well-defined only when terms don't appear in every document).
    We seed the index with a few unrelated pages before the duplicate check.

    The body carries one more unique token than the head cap (16) needs so
    the AND-tokenized query is exactly the shared body vocabulary: the
    copy's own title word ("Cheatsheet") falls outside the cap, so the
    query matches the original verbatim.
    """
    # Seed corpus so BM25 IDF has something to discriminate against.
    service.create_page(
        slug="seed-a", title="Xylophone", body="Jazz harmony theory and chord voicings"
    )
    service.create_page(
        slug="seed-b", title="Origami", body="Paper folding cranes and tessellation patterns"
    )
    service.create_page(
        slug="seed-c", title="Quasar", body="Active galactic nuclei and relativistic jets"
    )
    body = (
        "Planning doc for the Europe Fall 2026 trip. Itinerary: Berlin, Prague, "
        "Vienna. Flights and hotels for Europe Fall 2026. Budget spreadsheet "
        "attached. Reservations."
    )
    service.create_page(
        slug="original",
        title="Europe Fall 2026 Planning",
        body=body,
    )
    # Different title (avoids the title_norm path), same body verbatim.
    with pytest.raises(service.DuplicateError) as exc:
        service.create_page(
            slug="copy",
            title="Europe Fall 2026 Cheatsheet",
            body=body,
        )
    candidates = exc.value.details["candidates"]
    assert any(c["slug"] == "original" for c in candidates)
    # BM25 path; lower (more negative) = stronger match.
    assert candidates[0]["reason"] == "fts5_bm25"
    assert candidates[0]["score"] < 0
    assert candidates[0]["score"] <= service.DEDUP_BM25_THRESHOLD


def test_dedup_cheatsheet_not_flagged(tmp_path: Path) -> None:
    """Sibling pages sharing family vocabulary must not block a new page.

    Regression for the dogfooding false positive: a 'cheatsheet' page whose
    only overlap with its siblings was the 'Europe Fall 2026' family
    vocabulary was wrongly flagged by the old OR-tokenized BM25 signal. The
    AND signal requires the whole meaningful head to appear in one existing
    page, so shared family vocabulary alone can't trigger it.
    """
    service.create_page(
        slug="europe-fall-2026-planning",
        title="Europe Fall 2026 Planning",
        body=(
            "Planning doc for the Europe Fall 2026 trip. Itinerary: Berlin, "
            "Prague, Vienna. Flights and hotels for Europe Fall 2026. Budget "
            "spreadsheet attached."
        ),
    )
    service.create_page(
        slug="europe-fall-2026-packing",
        title="Europe Fall 2026 Packing",
        body=(
            "Packing list for Europe Fall 2026. Carry-on and checked luggage. "
            "Power adapters for Europe Fall 2026."
        ),
    )
    service.create_page(
        slug="europe-fall-2026-budget",
        title="Europe Fall 2026 Budget",
        body=(
            "Budget for Europe Fall 2026. Flights, hotels, food, and transport "
            "costs for Europe Fall 2026."
        ),
    )
    service.create_page(
        slug="japan-spring-2027",
        title="Japan Spring 2027",
        body="Japan Spring 2027 itinerary. Tokyo, Kyoto, Osaka. Cherry blossom season planning.",
    )
    r = service.create_page(
        slug="europe-fall-2026-cheatsheet",
        title="Europe Fall 2026 Cheatsheet",
        body=(
            "Europe Fall 2026 cheatsheet: what to know before you go. Do and "
            "don't list for Europe Fall 2026 travel. Currency, wifi, safety "
            "tips for Europe Fall 2026."
        ),
    )
    assert r["slug"] == "europe-fall-2026-cheatsheet"
    assert r["title"] == "Europe Fall 2026 Cheatsheet"


def test_dedup_hint_in_message(tmp_path: Path) -> None:
    """Duplicate errors teach the force=true escape hatch."""
    service.create_page(slug="northwind", title="Northwind", body="x")
    with pytest.raises(service.DuplicateError) as exc:
        service.create_page(slug="another", title="North Wind", body="x")
    assert "re-run with force=true if this is intentional" in str(exc.value)
    assert exc.value.details["candidates"]


def test_dedup_force_override(tmp_path: Path) -> None:
    service.create_page(slug="northwind", title="Northwind", body="x")
    # Without force → DuplicateError. We don't even reach here without raising.
    # Just confirm force=True bypasses.
    r = service.create_page(
        slug="northwind2",
        title="Northwind",
        body="x",
        force=True,
    )
    assert r["slug"] == "northwind2"


def test_dedup_no_false_positive(tmp_path: Path) -> None:
    """Two genuinely distinct pages should both create cleanly."""
    service.create_page(slug="a", title="Northwind", body="x")
    r = service.create_page(slug="b", title="Pi", body="x")
    assert r["slug"] == "b"


# ---------------------------------------------------------------------------
# Create = upsert
# ---------------------------------------------------------------------------

def test_create_upsert_updates_existing(tmp_path: Path) -> None:
    """Re-creating an existing slug updates it instead of erroring."""
    service.create_page(slug="foo", title="A", body="one")
    r = service.create_page(slug="foo", title="B", body="two")
    assert r["title"] == "B"
    assert r["body"] == "two"
    with vdb.connection(tmp_path / "test.db") as conn:
        rows = conn.execute("SELECT * FROM pages WHERE slug = ?", ("foo",)).fetchall()
    assert len(rows) == 1


def test_create_upsert_after_soft_delete(tmp_path: Path) -> None:
    """Re-creating a soft-deleted slug succeeds with update semantics."""
    service.create_page(slug="foo", title="A", body="one")
    service.delete_page(slug="foo")
    r = service.create_page(slug="foo", title="B", body="two")
    assert r["title"] == "B"
    assert r["body"] == "two"
    assert r["status"] == "deprecated"  # update semantics on the deprecated row


# ---------------------------------------------------------------------------
# Frontmatter-aware write (source_markdown)
# ---------------------------------------------------------------------------

def test_create_source_markdown_parses(tmp_path: Path) -> None:
    """Frontmatter supplies title/tags/type; only the clean body is stored."""
    r = service.create_page(
        slug="fm",
        body=(
            "---\n"
            "title: From Frontmatter\n"
            "tags: [a, b]\n"
            "type: reference\n"
            "---\n"
            "Body text"
        ),
        source_markdown=True,
    )
    assert r["body"] == "Body text"
    assert r["title"] == "From Frontmatter"
    assert r["tags"] == ["a", "b"]
    assert r["type"] == "reference"


def test_create_source_markdown_no_frontmatter(tmp_path: Path) -> None:
    """No frontmatter block → body kept exactly as given."""
    r = service.create_page(
        slug="plain",
        title="Plain",
        body="no frontmatter here",
        source_markdown=True,
    )
    assert r["body"] == "no frontmatter here"
    assert r["title"] == "Plain"


def test_create_source_markdown_malformed(tmp_path: Path) -> None:
    """Unclosed frontmatter block → raw body fallback, no crash."""
    body = "---\ntitle: x\nbody text without closing fence"
    r = service.create_page(
        slug="mal",
        title="T",
        body=body,
        source_markdown=True,
    )
    assert r["body"] == body
    assert r["title"] == "T"


def test_create_source_markdown_explicit_title_wins(tmp_path: Path) -> None:
    """Caller-passed title beats a frontmatter title."""
    r = service.create_page(
        slug="fm2",
        title="Explicit",
        body="---\ntitle: From Frontmatter\n---\nBody text",
        source_markdown=True,
    )
    assert r["title"] == "Explicit"
    assert r["body"] == "Body text"


def test_create_source_markdown_bad_type_validates(tmp_path: Path) -> None:
    """Frontmatter type is validated against page_types after mapping."""
    with pytest.raises(service.ValidationError) as exc:
        service.create_page(
            slug="fm3",
            body="---\ntitle: T\ntype: bogus\n---\nBody",
            source_markdown=True,
        )
    assert "valid values" in str(exc.value)


def test_create_source_markdown_no_title(tmp_path: Path) -> None:
    """No frontmatter title and no explicit title → ValidationError."""
    with pytest.raises(service.ValidationError) as exc:
        service.create_page(
            slug="fm4",
            body="no frontmatter title here",
            source_markdown=True,
        )
    assert "title" in str(exc.value)


# ---------------------------------------------------------------------------
# Meta + admin
# ---------------------------------------------------------------------------

def test_meta_exists(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="x")
    assert service.get_meta(action="exists", slug="a") == {
        "slug": "a",
        "exists": True,
    }
    assert service.get_meta(action="exists", slug="missing") == {
        "slug": "missing",
        "exists": False,
    }


def test_meta_tags(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="x", tags=("blue", "red"))
    service.create_page(slug="b", title="B", body="x", tags=("blue",))
    service.create_page(slug="c", title="C", body="x", tags=("red",))

    tags = service.get_meta(action="tags")
    by_name = {t["name"]: t["count"] for t in tags}
    assert by_name["blue"] == 2
    assert by_name["red"] == 2


def test_meta_links(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="see [[b]]")
    service.create_page(slug="b", title="B", body="x")
    edges = service.get_meta(action="links", slug="a")
    slugs = [e["slug"] for e in edges]
    assert "b" in slugs


def test_meta_backlinks(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="see [[b]]")
    service.create_page(slug="b", title="B", body="x")
    edges = service.get_meta(action="backlinks", slug="b")
    slugs = [e["slug"] for e in edges]
    assert "a" in slugs


def test_admin_link(tmp_path: Path) -> None:
    """Explicit link survives a body update."""
    service.create_page(slug="a", title="A", body="")
    service.create_page(slug="b", title="B", body="x")
    service.admin_link(source_slug="a", target_slug="b", rel="depends_on")
    # Body update rebuilds derived links, but explicit links are origin='explicit'
    # and survive.
    service.update_page(slug="a", body="new body")
    edges = service.get_meta(action="links", slug="a")
    assert any(e["slug"] == "b" and e["rel"] == "depends_on" for e in edges)


def test_admin_unlink(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="")
    service.create_page(slug="b", title="B", body="x")
    service.admin_link(source_slug="a", target_slug="b", rel="depends_on")
    service.admin_unlink(source_slug="a", target_slug="b", rel="depends_on")
    edges = service.get_meta(action="links", slug="a")
    assert all(e["slug"] != "b" for e in edges)


def test_admin_unlink_increments_change_seq(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="")
    service.create_page(slug="b", title="B", body="x")
    service.admin_link(source_slug="a", target_slug="b", rel="depends_on")

    with vdb.connection(tmp_path / "test.db") as conn:
        pre_unlink_seq = conn.execute(
            "SELECT value FROM change_seq WHERE id = 1"
        ).fetchone()["value"]

    service.admin_unlink(source_slug="a", target_slug="b", rel="depends_on")

    with vdb.connection(tmp_path / "test.db") as conn:
        post_unlink_seq = conn.execute(
            "SELECT value FROM change_seq WHERE id = 1"
        ).fetchone()["value"]
    assert post_unlink_seq == pre_unlink_seq + 1

def test_admin_restore(tmp_path: Path) -> None:
    """Roll back to the first revision: pages.body == first revision body."""
    service.create_page(slug="a", title="A", body="v1")
    first = service.get_page("a", include_revisions=True)["revisions"][0]

    service.update_page(slug="a", body="v2 is different")
    # Sanity: we are not already at v1.
    g_mid = service.get_page("a")
    assert g_mid["body"] == "v2 is different"

    service.admin_restore(slug="a", revision_id=first["id"])

    g = service.get_page("a")
    assert g["body"] == "v1"

def test_admin_restore_records_explicit_writer_identity(tmp_path: Path) -> None:
    service.create_page(slug="a", title="A", body="v1")
    first = service.get_page("a", include_revisions=True)["revisions"][0]
    service.update_page(slug="a", body="v2")

    restoration = service.admin_restore(
        slug="a",
        revision_id=first["id"],
        changed_by="restore_writer",
        client="restore_client",
    )

    with vdb.connection(tmp_path / "test.db") as conn:
        revision = conn.execute(
            "SELECT changed_by, client FROM revisions WHERE id = ?",
            (restoration["id"],),
        ).fetchone()
    assert dict(revision) == {
        "changed_by": "restore_writer",
        "client": "restore_client",
    }

# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_writes_no_busy(tmp_path: Path) -> None:
    """5 threads, each creating a unique page on a shared DB.

    BEGIN IMMEDIATE + busy_timeout=5000 makes writers serialize cleanly
    through the lock; without that you'd see SQLITE_BUSY_SNAPSHOT (rev 13).
    """
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            r = service.create_page(
                slug=f"thread-{i}",
                title=f"Thread {i}",
                body=f"body {i}",
            )
            results.append(r)
        except BaseException as e:  # noqa: BLE001 — propagate after join
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"threads raised: {errors!r}"
    assert len(results) == 5
    slugs = sorted(r["slug"] for r in results)
    assert slugs == [f"thread-{i}" for i in range(5)]


# ---------------------------------------------------------------------------
# Phase 10: staleness detection
# ---------------------------------------------------------------------------

def test_stale_returns_never_verified(tmp_path: Path) -> None:
    """A page with verified_at IS NULL must still appear in stale results.

    NULL < anything is NULL (falsy) in SQLite, so the OR IS NULL branch is
    what pulls never-verified pages in. Without it, a freshly-created page
    would silently be excluded.
    """
    service.create_page(slug="orphan", title="Orphan", body="x")
    # Also backdate updated_at to 120 days ago so the row shows up in the
    # default 90-day window by date arithmetic.
    with vdb.connection(tmp_path / "test.db") as conn:
        conn.execute(
            "UPDATE pages SET updated_at = datetime('now', '-120 days') "
            "WHERE slug = 'orphan'"
        )
        conn.commit()

    rows = service.get_meta(action="stale")
    slugs = [r["slug"] for r in rows]
    assert "orphan" in slugs
    row = next(r for r in rows if r["slug"] == "orphan")
    assert row["verified_at"] is None
    assert row["days_since"] >= 120


def test_stale_returns_old_verified(tmp_path: Path) -> None:
    """A page verified 100 days ago appears in stale at days=90."""
    service.create_page(slug="old", title="Old", body="x")
    with vdb.connection(tmp_path / "test.db") as conn:
        conn.execute(
            "UPDATE pages SET verified_at = datetime('now', '-100 days') "
            "WHERE slug = 'old'"
        )
        conn.commit()

    rows = service.get_meta(action="stale", days=90)
    assert any(r["slug"] == "old" for r in rows)


def test_stale_excludes_recent(tmp_path: Path) -> None:
    """A page verified yesterday does NOT appear at days=90."""
    service.create_page(slug="fresh", title="Fresh", body="x")
    with vdb.connection(tmp_path / "test.db") as conn:
        conn.execute(
            "UPDATE pages SET verified_at = datetime('now', '-1 days') "
            "WHERE slug = 'fresh'"
        )
        conn.commit()

    rows = service.get_meta(action="stale", days=90)
    assert not any(r["slug"] == "fresh" for r in rows)


def test_stale_excludes_deprecated(tmp_path: Path) -> None:
    """Deprecated pages are filtered out regardless of age."""
    service.create_page(slug="dead", title="Dead", body="x")
    service.delete_page(slug="dead", purge=False)
    rows = service.get_meta(action="stale")
    assert not any(r["slug"] == "dead" for r in rows)


def test_stale_excludes_log_type(tmp_path: Path) -> None:
    """The stale query restricts to entity/reference/guide, not log."""
    service.create_page(slug="note1", title="Note", body="x", type="log")
    with vdb.connection(tmp_path / "test.db") as conn:
        conn.execute(
            "UPDATE pages SET updated_at = datetime('now', '-200 days'), "
            "verified_at = NULL WHERE slug = 'note1'"
        )
        conn.commit()

    rows = service.get_meta(action="stale")
    assert not any(r["slug"] == "note1" for r in rows)


def test_stale_days_threshold(tmp_path: Path) -> None:
    """A page verified 50 days ago: excluded at days=90, included at days=30."""
    service.create_page(slug="mid", title="Mid", body="x")
    with vdb.connection(tmp_path / "test.db") as conn:
        conn.execute(
            "UPDATE pages SET verified_at = datetime('now', '-50 days') "
            "WHERE slug = 'mid'"
        )
        conn.commit()

    rows90 = service.get_meta(action="stale", days=90)
    assert not any(r["slug"] == "mid" for r in rows90)

    rows30 = service.get_meta(action="stale", days=30)
    assert any(r["slug"] == "mid" for r in rows30)


# ---------------------------------------------------------------------------
# Feature 7: get_meta action='stale_ranked'
# ---------------------------------------------------------------------------

def test_stale_ranked_returns_oldest_first_with_tags(tmp_path: Path) -> None:
    """stale_ranked returns active pages ordered by updated_at ASC, oldest
    first, with limit honored and tags in the projection."""
    service.create_page(
        slug="fresh", title="Fresh", body="x", tags=("reference",)
    )
    service.create_page(
        slug="older", title="Older", body="x", tags=("guide",)
    )
    service.create_page(
        slug="oldest", title="Oldest", body="x", tags=("decommissioned",)
    )
    # Backdate two of the three so the ordering is deterministic and
    # `limit=2` excludes the freshest page.
    with vdb.connection(tmp_path / "test.db") as conn:
        conn.execute(
            "UPDATE pages SET updated_at = datetime('now', '-30 days') "
            "WHERE slug = 'older'"
        )
        conn.execute(
            "UPDATE pages SET updated_at = datetime('now', '-60 days') "
            "WHERE slug = 'oldest'"
        )
        conn.commit()

    rows = service.get_meta(action="stale_ranked", limit=2)
    slugs = [r["slug"] for r in rows]
    # Oldest two in updated_at ASC order; 'fresh' is excluded by the limit.
    assert slugs == ["oldest", "older"]

    # Projection includes tags and the documented columns.
    first = rows[0]
    assert first["slug"] == "oldest"
    assert first["title"] == "Oldest"
    assert first["type"] == "entity"
    assert first["updated_at"] is not None
    assert first["verified_at"] is None
    assert first["tags"] == ["decommissioned"]


# ---------------------------------------------------------------------------
# Phase 15c: confidence scoring
# ---------------------------------------------------------------------------

def test_confidence_update_and_read(tmp_path: Path) -> None:
    """Setting confidence persists; round-trips through get_page."""
    service.create_page(slug="c", title="C", body="x")
    r = service.update_page(slug="c", confidence=0.42)
    assert r["confidence"] == pytest.approx(0.42)

    g = service.get_page("c")
    assert g["confidence"] == pytest.approx(0.42)

    # Direct SQL confirmation — confidence lives on `pages`.
    with vdb.connection(tmp_path / "test.db") as conn:
        row = conn.execute(
            "SELECT confidence FROM pages WHERE slug = 'c'"
        ).fetchone()
    assert row["confidence"] == pytest.approx(0.42)


def test_confidence_validation(tmp_path: Path) -> None:
    """Out-of-range confidence raises ValidationError; 0.0 and 1.0 are valid."""
    service.create_page(slug="c", title="C", body="x")

    with pytest.raises(service.ValidationError):
        service.update_page(slug="c", confidence=1.5)
    with pytest.raises(service.ValidationError):
        service.update_page(slug="c", confidence=-0.1)

    # Boundaries are valid.
    r0 = service.update_page(slug="c", confidence=0.0)
    assert r0["confidence"] == 0.0
    r1 = service.update_page(slug="c", confidence=1.0)
    assert r1["confidence"] == 1.0


# ---------------------------------------------------------------------------
# Phase 13: section-level patching
# ---------------------------------------------------------------------------

def test_update_section_patches_middle_section(tmp_path: Path) -> None:
    """update_section replaces one section's content and records update revision."""
    service.create_page(
        slug="forgejo",
        title="Forgejo",
        body=(
            "intro\n"
            "## Install\n"
            "old install steps\n"
            "## Configure\n"
            "config steps\n"
        ),
    )

    r = service.update_section(
        slug="forgejo",
        section_id="install",
        content="new install steps",
    )

    # Body now has the patched middle section; intro and Configure are intact.
    assert r["body"] == (
        "intro\n"
        "## Install\n"
        "new install steps\n"
        "## Configure\n"
        "config steps\n"
    )

    # A normal update revision is recorded (change_type='update').
    g = service.get_page("forgejo", include_revisions=True)
    assert len(g["revisions"]) == 2
    latest = g["revisions"][0]
    assert latest["change_type"] == "update"
    assert latest["body"] == r["body"]


def test_update_section_bad_section_id_raises(tmp_path: Path) -> None:
    """Missing section id surfaces a ValidationError naming the section id."""
    service.create_page(
        slug="forgejo",
        title="Forgejo",
        body="## Install\nsteps\n",
    )

    with pytest.raises(service.ValidationError) as exc:
        service.update_section(
            slug="forgejo",
            section_id="nonexistent",
            content="x",
        )
    assert exc.value.details["section_id"] == "nonexistent"
    # Valid section ids are surfaced so the caller can correct the request.
    assert "install" in exc.value.details["valid_section_ids"]


def test_update_section_missing_page_raises(tmp_path: Path) -> None:
    """Updating a section on a non-existent page raises NotFoundError."""
    with pytest.raises(service.NotFoundError):
        service.update_section(
            slug="nonexistent",
            section_id="install",
            content="x",
        )


def test_update_section_preserves_explicit_links(tmp_path: Path) -> None:
    """Derived-link rebuild from update_section keeps explicit links alive."""
    service.create_page(slug="a", title="A", body="")
    service.create_page(slug="b", title="B", body="target")
    service.create_page(
        slug="forgejo",
        title="Forgejo",
        body=("intro\n## Install\n[[b]]\n"),
    )
    service.admin_link(
        source_slug="forgejo", target_slug="b", rel="depends_on"
    )

    service.update_section(
        slug="forgejo",
        section_id="install",
        content="updated body, no wikilink",
    )

    # Explicit link to b survives the section patch.
    edges = service.get_meta(action="links", slug="forgejo")
    slugs = [e["slug"] for e in edges]
    assert "b" in slugs


# ---------------------------------------------------------------------------
# Correction queue
# ---------------------------------------------------------------------------

def test_create_correction_basic(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="the quick fox")

    row = service.create_correction(
        page_slug="fox", selected_text="teh", note="typo"
    )
    assert row["page_slug"] == "fox"
    assert row["page_title"] == "Fox"
    assert row["selected_text"] == "teh"
    assert row["note"] == "typo"
    assert row["status"] == "pending"
    assert row["created_at"] is not None
    assert row["resolved_at"] is None
    assert row["resolved_by"] is None
    assert isinstance(row["id"], int)


def test_create_correction_without_note(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    row = service.create_correction(page_slug="fox", selected_text="teh")
    assert row["note"] is None
    assert row["status"] == "pending"


def test_create_correction_strips_whitespace(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    row = service.create_correction(
        page_slug="fox", selected_text="  teh  "
    )
    assert row["selected_text"] == "teh"


def test_create_correction_missing_page(tmp_path: Path) -> None:
    with pytest.raises(service.NotFoundError):
        service.create_correction(
            page_slug="ghost", selected_text="x"
        )


def test_create_correction_bad_slug(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    with pytest.raises(service.ValidationError):
        service.create_correction(
            page_slug="Bad Slug", selected_text="x"
        )


def test_create_correction_empty_selected_text(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    with pytest.raises(service.ValidationError):
        service.create_correction(page_slug="fox", selected_text="")
    with pytest.raises(service.ValidationError):
        service.create_correction(page_slug="fox", selected_text="   ")


def test_create_correction_bad_note_type(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    with pytest.raises(service.ValidationError):
        service.create_correction(
            page_slug="fox", selected_text="x", note=42
        )


def test_create_correction_does_not_bump_seq(tmp_path: Path) -> None:
    """Flags are client bookkeeping, not page writes — change_seq must not
    advance when a correction is created."""
    service.create_page(slug="fox", title="Fox", body="x")
    with vdb.connection(str(tmp_path / "test.db")) as conn:
        before = conn.execute(
            "SELECT value FROM change_seq WHERE id = 1"
        ).fetchone()["value"]
    service.create_correction(page_slug="fox", selected_text="teh")
    with vdb.connection(str(tmp_path / "test.db")) as conn:
        after = conn.execute(
            "SELECT value FROM change_seq WHERE id = 1"
        ).fetchone()["value"]
    assert before == after


def test_list_corrections_default(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    service.create_correction(page_slug="fox", selected_text="a")
    service.create_correction(page_slug="fox", selected_text="b")
    rows = service.list_corrections()
    assert [r["selected_text"] for r in rows] == ["a", "b"]


def test_list_corrections_filter_by_status(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    a = service.create_correction(page_slug="fox", selected_text="a")
    b = service.create_correction(page_slug="fox", selected_text="b")
    service.resolve_correction(correction_id=a["id"], status="resolved")
    service.resolve_correction(
        correction_id=b["id"], status="dismissed", resolved_by="agent-1"
    )

    pending = service.list_corrections(status="pending")
    assert pending == []
    resolved = service.list_corrections(status="resolved")
    assert [r["selected_text"] for r in resolved] == ["a"]
    dismissed = service.list_corrections(status="dismissed")
    assert [r["resolved_by"] for r in dismissed] == ["agent-1"]


def test_list_corrections_bad_status(tmp_path: Path) -> None:
    with pytest.raises(service.ValidationError):
        service.list_corrections(status="nope")


def test_resolve_correction_resolved(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    c = service.create_correction(page_slug="fox", selected_text="teh")

    out = service.resolve_correction(
        correction_id=c["id"], status="resolved", resolved_by="agent-x"
    )
    assert out["status"] == "resolved"
    assert out["resolved_by"] == "agent-x"
    assert out["resolved_at"] is not None


def test_resolve_correction_dismissed(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    c = service.create_correction(page_slug="fox", selected_text="teh")
    out = service.resolve_correction(
        correction_id=c["id"], status="dismissed"
    )
    assert out["status"] == "dismissed"
    assert out["resolved_at"] is not None
    assert out["resolved_by"] is None


def test_resolve_correction_default_status(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    c = service.create_correction(page_slug="fox", selected_text="teh")
    out = service.resolve_correction(correction_id=c["id"])
    assert out["status"] == "resolved"


def test_resolve_correction_missing_id(tmp_path: Path) -> None:
    with pytest.raises(service.NotFoundError):
        service.resolve_correction(correction_id=9999, status="resolved")


def test_resolve_correction_bad_status(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    c = service.create_correction(page_slug="fox", selected_text="teh")
    with pytest.raises(service.ValidationError):
        service.resolve_correction(correction_id=c["id"], status="nope")


def test_resolve_correction_rejects_pending(tmp_path: Path) -> None:
    """'pending' is the initial state; not a valid resolution target."""
    service.create_page(slug="fox", title="Fox", body="x")
    c = service.create_correction(page_slug="fox", selected_text="teh")
    with pytest.raises(service.ValidationError):
        service.resolve_correction(correction_id=c["id"], status="pending")


def test_resolve_correction_does_not_bump_seq(tmp_path: Path) -> None:
    """Resolving a flag is bookkeeping; the sync cursor must not move."""
    service.create_page(slug="fox", title="Fox", body="x")
    c = service.create_correction(page_slug="fox", selected_text="teh")

    with vdb.connection(str(tmp_path / "test.db")) as conn:
        before = conn.execute(
            "SELECT value FROM change_seq WHERE id = 1"
        ).fetchone()["value"]
    service.resolve_correction(correction_id=c["id"], status="resolved")
    with vdb.connection(str(tmp_path / "test.db")) as conn:
        after = conn.execute(
            "SELECT value FROM change_seq WHERE id = 1"
        ).fetchone()["value"]
    assert before == after


def test_correction_cascades_on_page_purge(tmp_path: Path) -> None:
    service.create_page(slug="fox", title="Fox", body="x")
    service.create_correction(page_slug="fox", selected_text="teh")
    service.delete_page(slug="fox", purge=True)
    assert service.list_corrections() == []
