"""Tests for the Vesperiki MCP tool adapter."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from vesperiki import db
from vesperiki import mcp
from vesperiki import service


@pytest.fixture(autouse=True)
def _mcp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "mcp.db"
    monkeypatch.setenv("VESPERIKI_WRITER", "test-writer")
    monkeypatch.setenv("VESPERIKI_CLIENT", "pytest")
    monkeypatch.delenv("VESPERIKI_MODE", raising=False)
    mcp.init_for_test(db_path)
    return db_path


def _call(name: str, args: dict[str, Any]) -> Any:
    result = asyncio.run(mcp.call_tool(name, args))
    # call_tool may return either a bare list[TextContent] (success) or a
    # CallToolResult with .content (error path carrying isError=True).
    content = getattr(result, "content", result)
    assert len(content) == 1
    return json.loads(content[0].text)


def _create(slug: str, title: str, body: str, **kwargs: Any) -> dict:
    return service.create_page(slug=slug, title=title, body=body, **kwargs)


def _tool_names() -> set[str]:
    return {tool.name for tool in asyncio.run(mcp.list_tools())}


def test_wiki_read_basic() -> None:
    _create("foo", "Foo", "hello", tags=("a",))

    data = _call("wiki_read", {"slug": "foo"})

    assert data["slug"] == "foo"
    assert data["title"] == "Foo"
    assert data["body"] == "hello"
    assert data["tags"] == ["a"]


def test_wiki_read_404() -> None:
    data = _call("wiki_read", {"slug": "nonexistent"})

    assert data["error"] == "not_found"
    assert "nonexistent" in data["message"]
    assert data["details"] == {}


def test_wiki_read_expand_links() -> None:
    _create("foo", "Foo", "target")
    _create("bar", "Bar", "See [[foo]].")

    data = _call("wiki_read", {"slug": "bar", "expand_links": True})

    assert data["links"] == [{"slug": "foo", "title": "Foo"}]


# ---------------------------------------------------------------------------
# Feature 6: wiki_read section parameter
# ---------------------------------------------------------------------------

def test_wiki_read_section_returns_matching_section() -> None:
    """A section id matching a ``## `` heading returns that section only,
    with the heading line prepended to content."""
    _create(
        "forgejo",
        "Forgejo",
        "intro\n## Overview\noverview content\n## Details\ndetails content\n",
    )

    data = _call("wiki_read", {"slug": "forgejo", "section": "overview"})

    assert data["slug"] == "forgejo"
    assert data["title"] == "Forgejo"
    assert data["section_id"] == "overview"
    assert data["heading"] == "Overview"
    assert "## Overview" in data["content"]
    assert "overview content" in data["content"]
    # Other section's content must not leak in.
    assert "details content" not in data["content"]
    # Other sections are listed so the caller can navigate.
    assert "details" in data["valid_section_ids"]
    assert "intro" in data["valid_section_ids"]


def test_wiki_read_section_intro_returns_intro() -> None:
    """section='intro' returns the leading text before the first heading."""
    _create(
        "forgejo",
        "Forgejo",
        "intro paragraph\n## Overview\noverview content\n",
    )

    data = _call("wiki_read", {"slug": "forgejo", "section": "intro"})

    assert data["section_id"] == "intro"
    assert data["heading"] is None
    # Intro has no heading line; content is the body text before ``## ``.
    assert data["content"] == "intro paragraph"
    assert "## Overview" not in data["content"]


def test_wiki_read_section_unknown_id_is_validation_error() -> None:
    """An unknown section id surfaces a validation error listing the
    valid ids so the caller can correct the request."""
    _create(
        "forgejo",
        "Forgejo",
        "intro\n## Overview\noverview content\n",
    )

    data = _call("wiki_read", {"slug": "forgejo", "section": "nonexistent"})

    assert data["error"] == "validation"
    assert data["details"]["section_id"] == "nonexistent"
    assert "overview" in data["details"]["valid_section_ids"]


def test_wiki_search_basic() -> None:
    _create("foo", "Foo", "a searchable platypus")

    data = _call("wiki_search", {"query": "platypus"})

    assert [item["slug"] for item in data] == ["foo"]
    assert "body" not in data[0]


def test_wiki_search_empty_query() -> None:
    assert _call("wiki_search", {"query": ""}) == []


def test_wiki_meta_exists() -> None:
    _create("foo", "Foo", "hello")

    assert _call("wiki_meta", {"action": "exists", "slug": "foo"}) == {
        "slug": "foo",
        "exists": True,
    }


def test_wiki_meta_tags() -> None:
    _create("foo", "Foo", "hello", tags=("shared", "only-foo"))
    _create("bar", "Bar", "world", tags=("shared",))

    data = _call("wiki_meta", {"action": "tags"})

    assert {item["name"]: item["count"] for item in data} == {
        "shared": 2,
        "only-foo": 1,
    }


def test_wiki_meta_list_no_body() -> None:
    """wiki_meta action='list' ships metadata only — never bodies.

    The tool's inputSchema has no include_body knob; the list projection
    is slim by default (list_pages include_body=False).
    """
    _create("foo", "Foo", "hello world body")
    _create("bar", "Bar", "another body")

    data = _call("wiki_meta", {"action": "list"})

    assert len(data["pages"]) == 2
    assert all("body" not in p for p in data["pages"])
    # Metadata is intact.
    assert {p["slug"] for p in data["pages"]} == {"foo", "bar"}


def test_wiki_write_create() -> None:
    data = _call(
        "wiki_write",
        {
            "action": "create",
            "slug": "foo",
            "title": "Foo",
            "body": "hello",
            "tags": ["a"],
        },
    )

    assert data["slug"] == "foo"
    assert service.get_page("foo")["tags"] == ["a"]


def test_wiki_write_create_duplicate() -> None:
    _create("first", "North Wind", "router details")

    data = _call(
        "wiki_write",
        {
            "action": "create",
            "slug": "second",
            "title": "Northwind",
            "body": "other details",
        },
    )

    assert data["error"] == "duplicate"
    assert data["details"]["candidates"]
    assert data["details"]["candidates"][0]["slug"] == "first"


def test_wiki_write_create_force() -> None:
    _create("first", "North Wind", "router details")

    data = _call(
        "wiki_write",
        {
            "action": "create",
            "slug": "second",
            "title": "Northwind",
            "body": "other details",
            "force": True,
        },
    )

    assert data["slug"] == "second"


def test_wiki_write_create_duplicate_hint() -> None:
    """Duplicate errors teach the force=true escape hatch."""
    _create("first", "North Wind", "router details")

    data = _call(
        "wiki_write",
        {
            "action": "create",
            "slug": "second",
            "title": "Northwind",
            "body": "other details",
        },
    )

    assert data["error"] == "duplicate"
    assert "re-run with force=true if this is intentional" in data["message"]
    assert data["details"]["candidates"]


def test_wiki_write_source_markdown() -> None:
    """source_markdown stores only the clean body; frontmatter supplies title."""
    data = _call(
        "wiki_write",
        {
            "action": "create",
            "slug": "fm-page",
            "body": (
                "---\n"
                "title: From Frontmatter\n"
                "tags: [a, b]\n"
                "type: reference\n"
                "---\n"
                "Body text"
            ),
            "source_markdown": True,
        },
    )

    assert data["title"] == "From Frontmatter"
    assert data["body"] == "Body text"
    assert data["tags"] == ["a", "b"]
    assert data["type"] == "reference"


def test_wiki_write_create_upsert() -> None:
    """create on an existing slug updates it instead of erroring."""
    _call("wiki_write", {"action": "create", "slug": "foo", "title": "A", "body": "one"})

    data = _call(
        "wiki_write",
        {"action": "create", "slug": "foo", "title": "B", "body": "two"},
    )

    assert data["title"] == "B"
    assert data["body"] == "two"
    assert service.get_page("foo")["title"] == "B"


def test_wiki_write_update() -> None:
    _create("foo", "Foo", "old")

    data = _call(
        "wiki_write",
        {"action": "update", "slug": "foo", "body": "new"},
    )

    assert data["body"] == "new"
    assert service.get_page("foo")["body"] == "new"


def test_wiki_write_delete() -> None:
    _create("foo", "Foo", "hello")

    data = _call("wiki_write", {"action": "delete", "slug": "foo"})

    assert data == {"slug": "foo", "status": "deprecated", "purged": False}
    assert service.get_page("foo")["status"] == "deprecated"


def test_wiki_write_reactive_revive() -> None:
    """wiki_write action='revive' restores a deprecated page to active."""
    created = _call(
        "wiki_write",
        {"action": "create", "slug": "foo", "title": "Foo", "body": "hello"},
    )
    assert created["status"] == "active"

    deleted = _call("wiki_write", {"action": "delete", "slug": "foo"})
    assert deleted == {"slug": "foo", "status": "deprecated", "purged": False}
    assert service.get_page("foo")["status"] == "deprecated"

    revived = _call("wiki_write", {"action": "revive", "slug": "foo"})
    assert revived["status"] == "active"
    assert service.get_page("foo")["status"] == "active"


def test_wiki_admin_link() -> None:
    _create("source", "Source", "hello")
    _create("target", "Target", "world")

    data = _call(
        "wiki_admin",
        {
            "action": "link",
            "source_slug": "source",
            "target_slug": "target",
            "rel": "depends_on",
            "context": "test",
        },
    )

    assert data["origin"] == "explicit"
    assert service.get_meta(action="links", slug="source")[0]["slug"] == "target"


def test_wiki_admin_unlink() -> None:
    _create("source", "Source", "hello")
    _create("target", "Target", "world")
    service.admin_link(
        source_slug="source", target_slug="target", rel="depends_on"
    )

    data = _call(
        "wiki_admin",
        {
            "action": "unlink",
            "source_slug": "source",
            "target_slug": "target",
            "rel": "depends_on",
        },
    )

    assert data["removed"] == 1
    assert service.get_meta(action="links", slug="source") == []


def test_wiki_admin_restore() -> None:
    _create("foo", "Foo", "v1")
    revision_id = service.get_page("foo", include_revisions=True)["revisions"][0][
        "id"
    ]
    service.update_page(slug="foo", body="v2")

    data = _call(
        "wiki_admin",
        {"action": "restore", "slug": "foo", "revision_id": revision_id},
    )

    assert data["body"] == "v1"
    assert service.get_page("foo")["body"] == "v1"


def test_writer_identity_from_env(
    _mcp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VESPERIKI_WRITER", "testwriter")
    monkeypatch.setenv("VESPERIKI_CLIENT", "mcp-test")

    _call(
        "wiki_write",
        {"action": "create", "slug": "foo", "title": "Foo", "body": "hello"},
    )

    with db.connection(_mcp_db) as conn:
        revision = conn.execute(
            "SELECT changed_by, client FROM revisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert dict(revision) == {"changed_by": "testwriter", "client": "mcp-test"}


def test_mode_full_registers_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VESPERIKI_MODE", "full")

    assert _tool_names() == {
        "wiki_read",
        "wiki_search",
        "wiki_meta",
        "wiki_write",
        "wiki_admin",
        "wiki_media",
    }


def test_mode_author_no_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VESPERIKI_MODE", "author")

    names = _tool_names()
    assert "wiki_write" in names
    assert "wiki_admin" not in names


def test_mode_readonly_no_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VESPERIKI_MODE", "readonly")

    names = _tool_names()
    assert names == {"wiki_read", "wiki_search", "wiki_meta"}


def test_validation_error_includes_field() -> None:
    data = _call(
        "wiki_write",
        {"action": "create", "slug": "Bad Slug", "title": "Bad", "body": "x"},
    )

    assert data["error"] == "validation"
    assert data["details"]["field"] == "slug"
    assert data["details"]["valid_values"]
    assert "'Bad Slug'" in data["message"]


def test_service_validation_error_adds_field_and_valid_values() -> None:
    data = _call(
        "wiki_write",
        {
            "action": "create",
            "slug": "foo",
            "title": "Foo",
            "body": "hello",
            "type": "not-a-type",
        },
    )

    assert data["error"] == "validation"
    assert data["details"]["field"] == "type"
    assert "entity" in data["details"]["valid_values"]
    assert "'not-a-type'" in data["message"]


def test_schema_error_is_json_text_content() -> None:
    data = _call("wiki_search", {"query": "foo", "limit": 21})

    assert data["error"] == "validation"
    assert data["details"] == {
        "field": "limit",
        "valid_values": ["number <= 20"],
    }
    assert "21" in data["message"]


def test_optional_nulls_are_accepted() -> None:
    created = _call(
        "wiki_write",
        {
            "action": "create",
            "slug": "foo",
            "title": "Foo",
            "body": "hello",
            "tags": None,
            "type": None,
            "sources": None,
        },
    )
    listed = _call(
        "wiki_meta",
        {
            "action": "list",
            "tag": None,
            "type": None,
            "status": None,
            "limit": None,
            "cursor": None,
        },
    )

    assert created["type"] == "entity"
    assert listed["pages"][0]["slug"] == "foo"


# ---------------------------------------------------------------------------
# Phase 10 + 15c: staleness + confidence over MCP
# ---------------------------------------------------------------------------

def test_wiki_meta_stale(tmp_path: Path) -> None:
    """wiki_meta action='stale' surfaces never-verified pages."""
    _create("orphan", "Orphan", "x")
    # Backdate updated_at so the row is well outside the default 90-day window.
    with db.connection(tmp_path / "mcp.db") as conn:
        conn.execute(
            "UPDATE pages SET updated_at = datetime('now', '-120 days') "
            "WHERE slug = 'orphan'"
        )
        conn.commit()

    data = _call("wiki_meta", {"action": "stale"})

    assert any(r["slug"] == "orphan" for r in data)
    row = next(r for r in data if r["slug"] == "orphan")
    assert row["verified_at"] is None
    assert row["days_since"] >= 120


# ---------------------------------------------------------------------------
# Feature 7: wiki_meta stale_ranked action
# ---------------------------------------------------------------------------

def test_wiki_meta_stale_ranked_oldest_first(tmp_path: Path) -> None:
    """stale_ranked returns active pages ordered by updated_at ASCENDING,
    oldest first, with no time cutoff."""
    _create("fresh", "Fresh", "x")
    _create("old", "Old", "x")
    # Backdate the older page so ordering is deterministic.
    with db.connection(tmp_path / "mcp.db") as conn:
        conn.execute(
            "UPDATE pages SET updated_at = datetime('now', '-60 days') "
            "WHERE slug = 'old'"
        )
        conn.commit()

    data = _call("wiki_meta", {"action": "stale_ranked"})

    slugs = [r["slug"] for r in data]
    # The backdated page is oldest, so it leads the list.
    assert slugs[0] == "old"
    assert "fresh" in slugs
    assert slugs.index("old") < slugs.index("fresh")
    # Projection shape — includes tags so callers can display them.
    assert "tags" in data[0]
    assert "updated_at" in data[0]


def test_wiki_meta_stale_ranked_exclude_tags(tmp_path: Path) -> None:
    """exclude_tags drops pages with any of the listed tags."""
    _create("tagged", "Tagged", "x", tags=("decommissioned",))
    _create("untagged", "Untagged", "x")
    # Backdate both so both are otherwise eligible.
    with db.connection(tmp_path / "mcp.db") as conn:
        for slug in ("tagged", "untagged"):
            conn.execute(
                "UPDATE pages SET updated_at = datetime('now', '-30 days') "
                "WHERE slug = ?",
                (slug,),
            )
        conn.commit()

    data = _call(
        "wiki_meta",
        {"action": "stale_ranked", "exclude_tags": ["decommissioned"]},
    )

    slugs = [r["slug"] for r in data]
    assert "untagged" in slugs
    assert "tagged" not in slugs


def test_wiki_meta_stale_ranked_limit(tmp_path: Path) -> None:
    """The limit kwarg caps the number of returned rows."""
    _create("p1", "One", "x")
    _create("p2", "Two", "x")
    _create("p3", "Three", "x")
    with db.connection(tmp_path / "mcp.db") as conn:
        for slug, days in (("p1", 60), ("p2", 30), ("p3", 1)):
            conn.execute(
                "UPDATE pages SET updated_at = datetime('now', ?) "
                "WHERE slug = ?",
                (f"-{days} days", slug),
            )
        conn.commit()

    data = _call(
        "wiki_meta", {"action": "stale_ranked", "limit": 2}
    )

    assert len(data) == 2
    slugs = [r["slug"] for r in data]
    # The two oldest are returned in updated_at ASC order.
    assert slugs == ["p1", "p2"]


def test_wiki_write_update_confidence(tmp_path: Path) -> None:
    """wiki_write update propagates confidence to the stored page."""
    _create("c", "C", "x")

    data = _call(
        "wiki_write",
        {"action": "update", "slug": "c", "confidence": 0.7},
    )
    assert data["confidence"] == pytest.approx(0.7)

    g = _call("wiki_read", {"slug": "c"})
    assert g["confidence"] == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Phase 13: section-level patching
# ---------------------------------------------------------------------------

def test_wiki_write_update_section() -> None:
    """wiki_write action='update_section' patches one section's content."""
    _create(
        "forgejo",
        "Forgejo",
        "intro\n## Install\nold steps\n## Configure\nconfig\n",
    )

    data = _call(
        "wiki_write",
        {
            "action": "update_section",
            "slug": "forgejo",
            "section_id": "install",
            "content": "new steps",
        },
    )

    assert data["slug"] == "forgejo"
    body = service.get_page("forgejo")["body"]
    assert "new steps" in body
    assert "old steps" not in body
    # Other sections and the heading line are untouched.
    assert "## Configure\nconfig" in body
    assert "## Install\nnew steps" in body


def test_wiki_write_update_section_missing_content() -> None:
    """Omitting content surfaces a validation error naming the field."""
    _create("forgejo", "Forgejo", "## Install\nsteps\n")

    data = _call(
        "wiki_write",
        {
            "action": "update_section",
            "slug": "forgejo",
            "section_id": "install",
        },
    )

    assert data["error"] == "validation"
    assert data["details"]["field"] == "content"


def test_wiki_write_update_section_missing_section_id() -> None:
    """Omitting section_id surfaces a validation error naming the field."""
    _create("forgejo", "Forgejo", "## Install\nsteps\n")

    data = _call(
        "wiki_write",
        {
            "action": "update_section",
            "slug": "forgejo",
            "content": "new",
        },
    )

    assert data["error"] == "validation"
    assert data["details"]["field"] == "section_id"


def test_wiki_write_update_section_unknown_section_id() -> None:
    """A section id not on the page produces a validation error listing the
    valid ids so the caller can correct the request."""
    _create("forgejo", "Forgejo", "## Install\nsteps\n")

    data = _call(
        "wiki_write",
        {
            "action": "update_section",
            "slug": "forgejo",
            "section_id": "nonexistent",
            "content": "x",
        },
    )

    assert data["error"] == "validation"
    assert data["details"]["section_id"] == "nonexistent"
    assert "install" in data["details"]["valid_section_ids"]


# ---------------------------------------------------------------------------
# Correction queue
# ---------------------------------------------------------------------------

def test_wiki_meta_corrections_returns_pending(tmp_path: Path) -> None:
    """wiki_meta action='corrections' is the agent's fix-this surface."""
    _create("fox", "Fox", "the quick fox")
    _create("bar", "Bar", "another page")

    # Two pending flags.
    service.create_correction(page_slug="fox", selected_text="teh")
    service.create_correction(
        page_slug="fox", selected_text="quik", note="missing c"
    )
    # One dismissed — must NOT appear in the MCP view.
    resolved_row = service.create_correction(
        page_slug="bar", selected_text="bad"
    )
    service.resolve_correction(
        correction_id=resolved_row["id"], status="dismissed"
    )

    data = _call("wiki_meta", {"action": "corrections"})
    assert isinstance(data, list)
    selected = [r["selected_text"] for r in data]
    assert "teh" in selected
    assert "quik" in selected
    assert "bad" not in selected
    # Oldest-first ordering.
    assert selected == ["teh", "quik"]


def test_wiki_meta_corrections_empty(tmp_path: Path) -> None:
    """No flags → empty list (never None, never error)."""
    data = _call("wiki_meta", {"action": "corrections"})
    assert data == []


# ---------------------------------------------------------------------------
# Feature: wiki_media upload/list/delete
# ---------------------------------------------------------------------------

def test_wiki_media_upload_returns_metadata() -> None:
    _create("foo", "Foo", "hello")

    data = _call(
        "wiki_media",
        {
            "action": "upload",
            "slug": "foo",
            "data_base64": base64.b64encode(b"fake-image").decode(),
        },
    )

    assert data["id"] >= 1
    assert len(data["sha256"]) == 64
    assert data["byte_size"] == len(b"fake-image")
    assert data["deduped"] is False


def test_wiki_media_upload_dedup_shares_row(tmp_path: Path) -> None:
    """Identical bytes uploaded to a different slug reuse the same media row."""
    _create("foo", "Foo", "hello")
    _create("bar", "Bar", "world")
    payload = base64.b64encode(b"identical-bytes").decode()

    first = _call(
        "wiki_media", {"action": "upload", "slug": "foo", "data_base64": payload}
    )
    second = _call(
        "wiki_media", {"action": "upload", "slug": "bar", "data_base64": payload}
    )

    assert first["deduped"] is False
    assert second["deduped"] is True
    assert second["id"] == first["id"]
    assert second["sha256"] == first["sha256"]
    assert second["byte_size"] == first["byte_size"]
    with db.connection(tmp_path / "mcp.db") as conn:
        count = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    assert count == 1


def test_wiki_media_list_returns_metadata_not_blob() -> None:
    _create("foo", "Foo", "hello")
    _call(
        "wiki_media",
        {
            "action": "upload",
            "slug": "foo",
            "data_base64": base64.b64encode(b"img").decode(),
            "filename": "logo.png",
            "mime_type": "image/png",
        },
    )

    data = _call("wiki_media", {"action": "list", "slug": "foo"})

    assert len(data) == 1
    item = data[0]
    assert item["id"] >= 1
    assert item["filename"] == "logo.png"
    assert item["mime_type"] == "image/png"
    assert item["byte_size"] == 3
    assert "created_at" in item
    # The BLOB must never cross the transport.
    assert "data" not in item


def test_wiki_media_delete_removes_row(tmp_path: Path) -> None:
    _create("foo", "Foo", "hello")
    uploaded = _call(
        "wiki_media",
        {
            "action": "upload",
            "slug": "foo",
            "data_base64": base64.b64encode(b"x").decode(),
        },
    )

    data = _call("wiki_media", {"action": "delete", "media_id": uploaded["id"]})

    assert data == {"deleted": True, "media_id": uploaded["id"]}
    assert _call("wiki_media", {"action": "list", "slug": "foo"}) == []
    again = _call("wiki_media", {"action": "delete", "media_id": uploaded["id"]})
    assert again["error"] == "not_found"


def test_wiki_media_upload_unknown_slug() -> None:
    data = _call(
        "wiki_media",
        {
            "action": "upload",
            "slug": "nope",
            "data_base64": base64.b64encode(b"x").decode(),
        },
    )

    assert data["error"] == "not_found"
    assert "nope" in data["message"]


def test_wiki_media_base64_round_trip(tmp_path: Path) -> None:
    """Uploaded bytes survive base64 transport byte-for-byte."""
    _create("foo", "Foo", "hello")
    original = bytes(range(256)) * 3

    uploaded = _call(
        "wiki_media",
        {
            "action": "upload",
            "slug": "foo",
            "data_base64": base64.b64encode(original).decode(),
        },
    )

    with db.connection(tmp_path / "mcp.db") as conn:
        row = conn.execute(
            "SELECT data FROM media WHERE id = ?", (uploaded["id"],)
        ).fetchone()
    assert bytes(row["data"]) == original


def test_wiki_media_upload_invalid_base64() -> None:
    _create("foo", "Foo", "hello")

    data = _call(
        "wiki_media",
        {"action": "upload", "slug": "foo", "data_base64": "not!base64!"},
    )

    assert data["error"] == "validation"
    assert data["details"]["field"] == "data_base64"


def test_wiki_media_upload_missing_data_base64() -> None:
    _create("foo", "Foo", "hello")

    data = _call("wiki_media", {"action": "upload", "slug": "foo"})

    assert data["error"] == "validation"
    assert data["details"]["field"] == "data_base64"


def test_wiki_media_list_empty_page() -> None:
    _create("foo", "Foo", "hello")

    assert _call("wiki_media", {"action": "list", "slug": "foo"}) == []


# ---------------------------------------------------------------------------
# MCP polish: annotations, isError, stderr logging, payload cap
# ---------------------------------------------------------------------------

def test_tools_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each tool carries the right MCP annotation; wiki_media has none."""
    monkeypatch.setenv("VESPERIKI_MODE", "full")
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}

    read_only = ("wiki_read", "wiki_search", "wiki_meta")
    destructive = ("wiki_write", "wiki_admin")

    for name in read_only:
        ann = tools[name].annotations
        assert ann is not None, name
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is None, name
        # Other annotation fields are out of scope for this polish.
        assert ann.title is None
        assert ann.idempotentHint is None
        assert ann.openWorldHint is None

    for name in destructive:
        ann = tools[name].annotations
        assert ann is not None, name
        assert ann.destructiveHint is True, name
        assert ann.readOnlyHint is None, name
        assert ann.title is None
        assert ann.idempotentHint is None
        assert ann.openWorldHint is None

    # wiki_media has a mixed read/write surface (upload/list/delete); no
    # annotation is the right answer.
    assert tools["wiki_media"].annotations is None


def test_validation_error_has_is_error_flag() -> None:
    """A schema validation failure carries isError=True plus the JSON body."""
    result = asyncio.run(
        mcp.call_tool("wiki_search", {"query": "foo", "limit": 21})
    )
    # Error path returns a CallToolResult with isError=True.
    assert hasattr(result, "isError")
    assert result.isError is True
    assert len(result.content) == 1
    body = json.loads(result.content[0].text)
    assert body["error"] == "validation"
    assert "21" in body["message"]
    assert body["details"]["field"] == "limit"
    assert body["details"]["valid_values"] == ["number <= 20"]


def test_success_path_is_not_error() -> None:
    """A successful wiki_read returns a bare list[TextContent] (no CallToolResult)."""
    _create("foo", "Foo", "hello")
    result = asyncio.run(mcp.call_tool("wiki_read", {"slug": "foo"}))
    # Success stays on the bare list[TextContent] path.
    assert isinstance(result, list)
    assert len(result) == 1
    assert not hasattr(result, "isError")
    body = json.loads(result[0].text)
    assert body["slug"] == "foo"
    assert body["body"] == "hello"


def test_logging_one_stderr_line_per_call(
    capfd: pytest.CaptureFixture,
) -> None:
    """One INFO stderr line per call; zero stdout; bodies never logged."""
    _create("foo", "Foo", "short")
    # A body large enough that any leak would dominate the log line.
    secret_body = "x" * 4096

    # Drop anything the autouse fixture or earlier imports wrote.
    capfd.readouterr()
    asyncio.run(mcp.call_tool("wiki_read", {"slug": "foo"}))
    captured = capfd.readouterr()
    info_lines = [ln for ln in captured.err.splitlines() if " INFO " in ln]
    assert len(info_lines) == 1, captured.err
    assert "tool=wiki_read" in info_lines[0]
    assert "slug='foo'" in info_lines[0]
    # stdout must be empty — the JSON-RPC channel MUST NOT carry log noise.
    assert captured.out == ""

    # wiki_write with a large body: only the discriminative args land in
    # the log line, never the body itself.
    capfd.readouterr()
    asyncio.run(
        mcp.call_tool(
            "wiki_write",
            {
                "action": "create",
                "slug": "big",
                "title": "Big",
                "body": secret_body,
            },
        )
    )
    captured = capfd.readouterr()
    info_lines = [ln for ln in captured.err.splitlines() if " INFO " in ln]
    assert len(info_lines) == 1, captured.err
    assert "tool=wiki_write" in info_lines[0]
    assert "action='create'" in info_lines[0]
    assert "slug='big'" in info_lines[0]
    assert secret_body not in captured.err
    assert captured.out == ""

    # A ServiceError (404 on a missing slug) logs at WARNING with the code.
    capfd.readouterr()
    asyncio.run(mcp.call_tool("wiki_read", {"slug": "nonexistent"}))
    captured = capfd.readouterr()
    warning_lines = [ln for ln in captured.err.splitlines() if " WARNING " in ln]
    assert warning_lines, captured.err
    assert any("not_found" in ln for ln in warning_lines), captured.err


def test_payload_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """tools/list payload stays within +300 chars of the 5,892 baseline."""
    monkeypatch.setenv("VESPERIKI_MODE", "full")
    tools = asyncio.run(mcp.list_tools())
    # The SDK serializes with exclude_none=True; baseline measured the same.
    payload = "".join(
        t.model_dump_json(by_alias=True, exclude_none=True) for t in tools
    )
    # Report the size so the audit trail records the actual delta.
    print(f"tools/list payload size = {len(payload)} chars")
    assert len(payload) <= 6192


    print(f"tools/list payload size = {len(payload)} chars")
    assert len(payload) <= 6192
