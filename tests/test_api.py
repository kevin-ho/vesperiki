"""Tests for vesperiki.api: the FastAPI REST transport over the service layer.

Each test runs against a fresh tmp_path db. Writer identity is set via env
so service-layer writers carry through into revision metadata. The API
itself takes the db path explicitly via the create_app factory.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from vesperiki import db as vdb
from vesperiki.api import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    db_path = str(tmp_path / "t.db")
    os.environ["VESPERIKI_DB_PATH"] = db_path
    os.environ["VESPERIKI_WRITER"] = "test"
    os.environ["VESPERIKI_CLIENT"] = "pytest"
    app = create_app(db_path)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _writer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the service-test fixture's writer identity. Tests below also
    rely on these via the client fixture; the autouse case here ensures any
    direct service call from this module also picks them up."""
    monkeypatch.setenv("VESPERIKI_WRITER", "test")
    monkeypatch.setenv("VESPERIKI_CLIENT", "pytest")


def _conn(db_path: str) -> sqlite3.Connection:
    """Open a raw connection (row factory set) for assertions against state
    the API doesn't expose directly (e.g. aliases)."""
    conn = vdb.connection(db_path)
    return conn


def _path_from_client(client: TestClient) -> str:
    """Recover the db path the app was built against from the open server."""
    # create_app stored db_path via closure; we tracked it externally via
    # the fixture's tmp_path / "t.db". Look it up from the app's startup.
    # Simpler: the fixture writes VESPERIKI_DB_PATH; read it.
    return os.environ["VESPERIKI_DB_PATH"]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["seq"] == 0


def test_cors_allow_origins_excludes_dead_wildcard_string(client: TestClient) -> None:
    cors = next(
        middleware
        for middleware in client.app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    assert "https://*" not in cors.kwargs["allow_origins"]
    assert "http://localhost:5173" in cors.kwargs["allow_origins"]
    assert "http://localhost:8080" in cors.kwargs["allow_origins"]
    assert cors.kwargs["allow_origin_regex"] == r"https://.*"

# ---------------------------------------------------------------------------
# Page CRUD
# ---------------------------------------------------------------------------

def test_create_page(client: TestClient) -> None:
    r = client.put(
        "/api/pages/foo",
        json={"title": "Foo", "body": "hello world"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["slug"] == "foo"
    assert data["title"] == "Foo"
    assert "id" in data


def test_get_page(client: TestClient) -> None:
    client.put("/api/pages/foo", json={"title": "Foo", "body": "hello"})
    r = client.get("/api/pages/foo")
    assert r.status_code == 200
    data = r.json()
    assert data["slug"] == "foo"
    assert data["body"] == "hello"


def test_get_page_404(client: TestClient) -> None:
    r = client.get("/api/pages/nonexistent")
    assert r.status_code == 404


def test_update_page(client: TestClient) -> None:
    client.put("/api/pages/foo", json={"title": "Foo", "body": "v1"})
    r = client.patch("/api/pages/foo", json={"body": "v2 body"})
    assert r.status_code == 200
    assert r.json()["body"] == "v2 body"


def test_delete_soft(client: TestClient) -> None:
    client.put("/api/pages/foo", json={"title": "Foo", "body": "x"})
    r = client.delete("/api/pages/foo")
    assert r.status_code == 200
    assert r.json() == {"slug": "foo", "status": "deprecated", "purged": False}
    g = client.get("/api/pages/foo")
    assert g.status_code == 200
    assert g.json()["status"] == "deprecated"


def test_delete_purge(client: TestClient) -> None:
    client.put("/api/pages/foo", json={"title": "Foo", "body": "x"})
    r = client.delete("/api/pages/foo?purge=true")
    assert r.status_code == 200
    assert r.json()["purged"] is True
    g = client.get("/api/pages/foo")
    assert g.status_code == 404


def test_alias_redirect(client: TestClient) -> None:
    """foo exists; alias 'old-foo' → foo. GET /old-foo must 301 to /foo."""
    client.put("/api/pages/foo", json={"title": "Foo", "body": "x"})
    # Seed the alias directly — service.py has no rename op in v1.
    with _conn(_path_from_client(client)) as conn:
        page_id = conn.execute(
            "SELECT id FROM pages WHERE slug = ?", ("foo",)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO slug_aliases (alias, page_id) VALUES (?, ?)",
            ("old-foo", page_id),
        )
        conn.commit()

    r = client.get("/api/pages/old-foo", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"].endswith("/api/pages/foo")


def test_dedup_409(client: TestClient) -> None:
    client.put("/api/pages/foo", json={"title": "Fox", "body": "hello"})
    r = client.put(
        "/api/pages/bar",
        json={"title": "fox", "body": "hello"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "duplicate"
    candidates = body["details"]["candidates"]
    assert any(c["slug"] == "foo" for c in candidates)


def test_dedup_force(client: TestClient) -> None:
    client.put("/api/pages/foo", json={"title": "Fox", "body": "hello"})
    r = client.put(
        "/api/pages/bar",
        json={"title": "fox", "body": "hello", "force": True},
    )
    assert r.status_code == 201


def test_dedup_409_hint(client: TestClient) -> None:
    """Duplicate 409s teach the force=true escape hatch."""
    client.put("/api/pages/foo", json={"title": "Fox", "body": "hello"})
    r = client.put(
        "/api/pages/bar",
        json={"title": "fox", "body": "hello"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "duplicate"
    assert "re-run with force=true if this is intentional" in body["message"]
    assert body["details"]["candidates"]


def test_put_source_markdown(client: TestClient) -> None:
    """PUT with source_markdown parses frontmatter; no top-level title needed."""
    r = client.put(
        "/api/pages/fm",
        json={
            "body": (
                "---\n"
                "title: From Frontmatter\n"
                "tags: [a]\n"
                "type: reference\n"
                "---\n"
                "Body text"
            ),
            "source_markdown": True,
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["body"] == "Body text"
    assert data["title"] == "From Frontmatter"

    g = client.get("/api/pages/fm")
    assert g.status_code == 200
    page = g.json()
    assert page["body"] == "Body text"
    assert page["title"] == "From Frontmatter"
    assert page["tags"] == ["a"]
    assert page["type"] == "reference"


def test_put_upsert(client: TestClient) -> None:
    """PUT on an existing slug updates it; both calls return 201."""
    r1 = client.put("/api/pages/foo", json={"title": "A", "body": "one"})
    assert r1.status_code == 201

    r2 = client.put("/api/pages/foo", json={"title": "B", "body": "two"})
    assert r2.status_code == 201
    assert r2.json()["title"] == "B"
    assert r2.json()["body"] == "two"

    g = client.get("/api/pages/foo").json()
    assert g["title"] == "B"
    assert g["body"] == "two"


# ---------------------------------------------------------------------------
# List + pagination
# ---------------------------------------------------------------------------

def test_list_pages(client: TestClient) -> None:
    for i in range(5):
        client.put(
            f"/api/pages/p{i}",
            json={"title": f"Page {i}", "body": "x"},
        )
    r = client.get("/api/pages")
    assert r.status_code == 200
    data = r.json()
    assert len(data["pages"]) == 5
    assert data["next_cursor"] is None
    # Default listing projection omits body so a sweep stays slim.
    assert all("body" not in p for p in data["pages"])


def test_list_pages_pagination(client: TestClient) -> None:
    for i in range(10):
        client.put(
            f"/api/pages/p{i:02d}",
            json={"title": f"Page {i}", "body": "x"},
        )
    r1 = client.get("/api/pages?limit=3")
    assert r1.status_code == 200
    d1 = r1.json()
    assert len(d1["pages"]) == 3
    assert d1["next_cursor"] is not None
    cursor = d1["next_cursor"]
    r2 = client.get(f"/api/pages?limit=3&cursor={cursor}")
    assert r2.status_code == 200
    d2 = r2.json()
    assert len(d2["pages"]) == 3
    # The two pages don't overlap (id-based keyset).
    ids1 = {p["id"] for p in d1["pages"]}
    ids2 = {p["id"] for p in d2["pages"]}
    assert ids1.isdisjoint(ids2)


def test_list_pages_no_body_by_default(client: TestClient) -> None:
    """Default /api/pages omits body; ?include_body=true restores it."""
    client.put(
        "/api/pages/foo",
        json={"title": "Foo", "body": "hello world body"},
    )
    client.put(
        "/api/pages/bar",
        json={"title": "Bar", "body": "another body"},
    )

    r = client.get("/api/pages")
    assert r.status_code == 200
    pages = r.json()["pages"]
    assert len(pages) == 2
    assert all("body" not in p for p in pages)
    # Metadata fields are still present.
    for p in pages:
        assert {
            "slug",
            "id",
            "title",
            "type",
            "tags",
            "sources",
            "status",
            "updated_at",
            "confidence",
        } <= p.keys()

    # include_body=true re-enables the full projection.
    r_full = client.get("/api/pages?include_body=true")
    assert r_full.status_code == 200
    full_pages = r_full.json()["pages"]
    assert len(full_pages) == 2
    bodies = {p["slug"]: p["body"] for p in full_pages}
    assert bodies == {"foo": "hello world body", "bar": "another body"}


# ---------------------------------------------------------------------------
# Search + tags
# ---------------------------------------------------------------------------

def test_search_basic(client: TestClient) -> None:
    client.put(
        "/api/pages/fox",
        json={"title": "Fox", "body": "foxes are mammals"},
    )
    r = client.get("/api/search?q=fox")
    assert r.status_code == 200
    slugs = [x["slug"] for x in r.json()]
    assert "fox" in slugs


def test_search_tag_filter(client: TestClient) -> None:
    client.put(
        "/api/pages/one",
        json={"title": "One", "body": "alpha beta gamma", "tags": ["foo"]},
    )
    client.put(
        "/api/pages/two",
        json={"title": "Two", "body": "alpha delta epsilon", "tags": ["bar"]},
    )
    r = client.get("/api/search?q=alpha&tag=foo")
    assert r.status_code == 200
    slugs = [x["slug"] for x in r.json()]
    assert "one" in slugs
    assert "two" not in slugs


def test_meta_tags(client: TestClient) -> None:
    client.put(
        "/api/pages/a",
        json={"title": "A", "body": "x", "tags": ["blue", "red"]},
    )
    client.put(
        "/api/pages/b",
        json={"title": "B", "body": "x", "tags": ["blue"]},
    )
    r = client.get("/api/tags")
    assert r.status_code == 200
    by_name = {t["name"]: t["count"] for t in r.json()}
    assert by_name["blue"] == 2
    assert by_name["red"] == 1


# ---------------------------------------------------------------------------
# Graph + orphans
# ---------------------------------------------------------------------------

def test_graph(client: TestClient) -> None:
    client.put("/api/pages/a", json={"title": "A", "body": "x"})
    client.put("/api/pages/b", json={"title": "B", "body": "see [[a]]"})
    r = client.get("/api/graph")
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data and "edges" in data
    slugs = {n["slug"] for n in data["nodes"]}
    assert {"a", "b"} == slugs
    assert len(data["edges"]) == 1
    edge = data["edges"][0]
    # Edge references use page id, not slug.
    assert {"source", "target", "rel", "origin"} <= edge.keys()


def test_orphans(client: TestClient) -> None:
    # 'a' has no incoming links; 'b' links to 'c' so 'c' is not orphaned.
    client.put("/api/pages/a", json={"title": "A", "body": "x"})
    client.put("/api/pages/b", json={"title": "B", "body": "x"})
    client.put("/api/pages/c", json={"title": "C", "body": "see [[a]]"})

    r = client.get("/api/orphans")
    assert r.status_code == 200
    slugs = {p["slug"] for p in r.json()}
    # a is referenced from c → not orphan. b and c are orphans.
    assert "a" not in slugs
    assert "b" in slugs
    assert "c" in slugs


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

def test_media_upload(client: TestClient) -> None:
    client.put("/api/pages/host", json={"title": "Host", "body": "x"})
    payload = b"hello-bytes"
    r = client.post(
        "/api/media",
        files={"file": ("h.bin", payload, "application/octet-stream")},
        data={"slug": "host"},
    )
    assert r.status_code == 201
    data = r.json()
    assert "id" in data
    assert data["byte_size"] == len(payload)
    # 64-char hex sha256.
    assert len(data["sha256"]) == 64


def test_media_get(client: TestClient) -> None:
    client.put("/api/pages/host", json={"title": "Host", "body": "x"})
    payload = b"abc-content"
    upload = client.post(
        "/api/media",
        files={"file": ("h.txt", payload, "text/plain")},
        data={"slug": "host"},
    )
    media_id = upload.json()["id"]

    r = client.get(f"/api/media/{media_id}")
    assert r.status_code == 200
    assert r.content == payload
    assert r.headers["content-type"].startswith("text/plain")


def test_media_dedup(client: TestClient) -> None:
    """Uploading identical bytes twice returns the same media id."""
    client.put("/api/pages/host", json={"title": "Host", "body": "x"})
    payload = b"the-same-bytes"
    r1 = client.post(
        "/api/media",
        files={"file": ("a.bin", payload, "application/octet-stream")},
        data={"slug": "host"},
    )
    r2 = client.post(
        "/api/media",
        files={"file": ("b.bin", payload, "application/octet-stream")},
        data={"slug": "host"},
    )
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def test_sync_full(client: TestClient) -> None:
    client.put("/api/pages/one", json={"title": "One", "body": "x"})
    client.put("/api/pages/two", json={"title": "Two", "body": "y"})

    r = client.get("/api/sync?since=0")
    assert r.status_code == 200
    data = r.json()
    # 2 page creates + nothing else (no links/tags/aliases yet).
    slugs = {p["slug"] for p in data["pages"]}
    assert {"one", "two"} <= slugs
    assert data["tombstones"] == []
    assert data["links"] == []
    assert data["page_tags"] == []
    assert data["tags"] == []
    assert data["aliases"] == []
    assert isinstance(data["cursor"], int) and data["cursor"] >= 2


def test_sync_delta(client: TestClient) -> None:
    """After syncing once, an update is the only row in the next batch."""
    client.put("/api/pages/one", json={"title": "One", "body": "x"})
    client.put("/api/pages/two", json={"title": "Two", "body": "y"})

    first = client.get("/api/sync?since=0").json()
    cursor = first["cursor"]

    client.patch("/api/pages/one", json={"body": "x but updated"})

    second = client.get(f"/api/sync?since={cursor}").json()
    # Only 'one' should appear with a higher seq; 'two' is unchanged.
    assert len(second["pages"]) == 1
    assert second["pages"][0]["slug"] == "one"
    assert second["pages"][0]["body"] == "x but updated"


def test_sync_tombstone(client: TestClient) -> None:
    """A purge produces a tombstone that surfaces in the next sync."""
    client.put("/api/pages/doomed", json={"title": "Doomed", "body": "x"})
    first = client.get("/api/sync?since=0").json()
    cursor = first["cursor"]

    client.delete("/api/pages/doomed?purge=true")

    second = client.get(f"/api/sync?since={cursor}").json()
    tomb_slugs = {t["slug"] for t in second["tombstones"]}
    assert "doomed" in tomb_slugs
    # All tombstones carry seq — the client interleaves by seq.
    assert all("seq" in t for t in second["tombstones"])
    assert all("changed_by" in t or "deleted_at" in t for t in second["tombstones"])


def test_sync_pagination(client: TestClient) -> None:
    """When more pages exist than the limit, sync returns next_cursor."""
    for i in range(50):
        client.put(
            f"/api/pages/p{i:02d}",
            json={"title": f"Page {i}", "body": "x"},
        )

    r = client.get("/api/sync?since=0&limit=20")
    assert r.status_code == 200
    data = r.json()
    assert len(data["pages"]) == 20
    assert data["next_cursor"] is not None


def test_sync_excludes_deprecated_pages(client: TestClient) -> None:
    """Soft-deleted pages never ride along in sync deltas.

    The page row stays (status='deprecated') and its seq is bumped by the
    soft delete, so without the filter it would appear in a since=0 sync.
    The sync query must exclude it; the tombstone table is the only
    deletion signal clients should see for purges.
    """
    client.put("/api/pages/keeper", json={"title": "Keeper", "body": "x"})
    client.put("/api/pages/doomed", json={"title": "Doomed", "body": "y"})
    client.delete("/api/pages/doomed")  # soft-delete -> status='deprecated'

    r = client.get("/api/sync?since=0")
    assert r.status_code == 200
    data = r.json()
    slugs = {p["slug"] for p in data["pages"]}
    assert "keeper" in slugs
    assert "doomed" not in slugs
    # The deprecated page still exists server-side (soft delete, not purge).
    g = client.get("/api/pages/doomed")
    assert g.status_code == 200
    assert g.json()["status"] == "deprecated"


# ---------------------------------------------------------------------------
# Revisions + restore
# ---------------------------------------------------------------------------

def test_writer_identity_in_revision(client: TestClient) -> None:
    """Revisions carry the writer identity from VESPERIKI_WRITER/_CLIENT env."""
    client.put("/api/pages/foo", json={"title": "Foo", "body": "v1"})
    r = client.get("/api/pages/foo/revisions")
    assert r.status_code == 200
    revs = r.json()
    assert len(revs) == 1
    latest = revs[0]
    assert latest["changed_by"] == "test"
    assert latest["client"] == "pytest"


def test_restore_revision(client: TestClient) -> None:
    """Restore writes a new revision with the rolled-back body."""
    client.put("/api/pages/foo", json={"title": "Foo", "body": "v1"})
    revs = client.get("/api/pages/foo/revisions").json()
    original_id = revs[0]["id"]

    client.patch("/api/pages/foo", json={"body": "v2 different"})

    r = client.post(f"/api/pages/foo/revisions/{original_id}/restore")
    assert r.status_code == 200
    body_now = client.get("/api/pages/foo").json()
    assert body_now["body"] == "v1"


# ---------------------------------------------------------------------------
# Phase 10: GET /api/stale
# ---------------------------------------------------------------------------

def test_stale_endpoint(client: TestClient) -> None:
    """GET /api/stale lists pages past the verification threshold."""
    client.put("/api/pages/old", json={"title": "Old", "body": "x"})
    db_path = _path_from_client(client)
    with vdb.connection(db_path) as conn:
        conn.execute(
            "UPDATE pages SET verified_at = datetime('now', '-100 days') "
            "WHERE slug = 'old'"
        )
        conn.commit()

    r = client.get("/api/stale")
    assert r.status_code == 200
    data = r.json()
    assert any(row["slug"] == "old" for row in data)
    assert all(
        {"slug", "title", "verified_at", "updated_at", "days_since"}
        <= row.keys()
        for row in data
    )

    # days param narrows the window: days=50 is wider than the default 90
    # in the OTHER direction, so the 100-day-old page is still stale.
    r_wide = client.get("/api/stale?days=50")
    assert r_wide.status_code == 200
    assert any(row["slug"] == "old" for row in r_wide.json())


# ---------------------------------------------------------------------------
# Phase 15c: PATCH confidence
# ---------------------------------------------------------------------------

def test_patch_confidence(client: TestClient) -> None:
    """PATCH /api/pages/{slug} accepts confidence and surfaces it in GET."""
    client.put("/api/pages/foo", json={"title": "Foo", "body": "x"})

    r = client.patch("/api/pages/foo", json={"confidence": 0.5})
    assert r.status_code == 200
    assert r.json()["confidence"] == pytest.approx(0.5)

    g = client.get("/api/pages/foo")
    assert g.status_code == 200
    assert g.json()["confidence"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Phase 13: section-level patching
# ---------------------------------------------------------------------------

def test_patch_section(client: TestClient) -> None:
    """PATCH /api/pages/{slug}/section/{section_id} patches one section."""
    client.put(
        "/api/pages/forgejo",
        json={
            "title": "Forgejo",
            "body": "intro\n## Install\nold\n## Configure\nconfig\n",
        },
    )

    r = client.patch(
        "/api/pages/forgejo/section/install",
        json={"content": "new install steps"},
    )

    assert r.status_code == 200
    body = r.json()["body"]
    assert "## Install\nnew install steps" in body
    assert "## Configure\nconfig" in body
    # The other section and intro are intact.
    assert body.startswith("intro\n")


def test_patch_section_404_on_missing_page(client: TestClient) -> None:
    """PATCH on a non-existent page returns 404."""
    r = client.patch(
        "/api/pages/nonexistent/section/install",
        json={"content": "x"},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_patch_section_400_on_missing_section(client: TestClient) -> None:
    """PATCH with an unknown section id returns 400 and lists valid ids."""
    client.put(
        "/api/pages/forgejo",
        json={"title": "Forgejo", "body": "## Install\nold\n"},
    )

    r = client.patch(
        "/api/pages/forgejo/section/nonexistent",
        json={"content": "x"},
    )

    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "validation"
    assert body["details"]["section_id"] == "nonexistent"
    assert "install" in body["details"]["valid_section_ids"]


# ---------------------------------------------------------------------------
# Correction queue
# ---------------------------------------------------------------------------

def test_corrections_get_default_pending(client: TestClient) -> None:
    client.put("/api/pages/fox", json={"title": "Fox", "body": "x"})
    client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "teh"},
    )
    # Resolve one so the default GET (pending only) excludes it.
    create = client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "quik"},
    )
    client.patch(
        f"/api/corrections/{create.json()['id']}",
        json={"status": "dismissed"},
    )

    r = client.get("/api/corrections")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["selected_text"] == "teh"
    assert data[0]["status"] == "pending"
    assert data[0]["page_slug"] == "fox"
    assert data[0]["page_title"] == "Fox"


def test_corrections_get_status_filter(client: TestClient) -> None:
    client.put("/api/pages/fox", json={"title": "Fox", "body": "x"})
    a = client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "a"},
    ).json()
    client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "b"},
    )
    client.patch(
        f"/api/corrections/{a['id']}",
        json={"status": "resolved", "resolved_by": "agent-1"},
    )

    pending = client.get("/api/corrections?status=pending").json()
    assert [r["selected_text"] for r in pending] == ["b"]

    resolved = client.get("/api/corrections?status=resolved").json()
    assert [r["selected_text"] for r in resolved] == ["a"]
    assert resolved[0]["resolved_by"] == "agent-1"

    all_rows = client.get("/api/corrections?status=all").json()
    assert {r["selected_text"] for r in all_rows} == {"a", "b"}


def test_corrections_post_201(client: TestClient) -> None:
    client.put("/api/pages/fox", json={"title": "Fox", "body": "x"})
    r = client.post(
        "/api/corrections",
        json={
            "page_slug": "fox",
            "selected_text": "teh",
            "note": "typo",
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["id"] >= 1
    assert data["page_slug"] == "fox"
    assert data["status"] == "pending"
    assert data["note"] == "typo"


def test_corrections_post_missing_page(client: TestClient) -> None:
    r = client.post(
        "/api/corrections",
        json={"page_slug": "ghost", "selected_text": "x"},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_corrections_post_validation(client: TestClient) -> None:
    client.put("/api/pages/fox", json={"title": "Fox", "body": "x"})
    # Missing page_slug.
    r1 = client.post("/api/corrections", json={"selected_text": "x"})
    assert r1.status_code == 400
    # Empty selected_text.
    r2 = client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": ""},
    )
    assert r2.status_code == 400
    # Whitespace-only.
    r3 = client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "   "},
    )
    assert r3.status_code == 400


def test_corrections_patch_resolved(client: TestClient) -> None:
    client.put("/api/pages/fox", json={"title": "Fox", "body": "x"})
    c = client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "teh"},
    ).json()

    r = client.patch(
        f"/api/corrections/{c['id']}",
        json={"status": "resolved", "resolved_by": "agent-2"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "resolved"
    assert data["resolved_by"] == "agent-2"
    assert data["resolved_at"] is not None


def test_corrections_patch_dismissed(client: TestClient) -> None:
    client.put("/api/pages/fox", json={"title": "Fox", "body": "x"})
    c = client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "teh"},
    ).json()

    r = client.patch(
        f"/api/corrections/{c['id']}",
        json={"status": "dismissed"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "dismissed"


def test_corrections_patch_404(client: TestClient) -> None:
    r = client.patch(
        "/api/corrections/9999",
        json={"status": "resolved"},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "not_found"


def test_corrections_patch_bad_status(client: TestClient) -> None:
    client.put("/api/pages/fox", json={"title": "Fox", "body": "x"})
    c = client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "teh"},
    ).json()
    r = client.patch(
        f"/api/corrections/{c['id']}",
        json={"status": "nope"},
    )
    assert r.status_code == 400


def test_corrections_delete_marks_resolved(client: TestClient) -> None:
    """DELETE is the one-tap "I handled this" gesture → status=resolved."""
    client.put("/api/pages/fox", json={"title": "Fox", "body": "x"})
    c = client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "teh"},
    ).json()

    r = client.delete(f"/api/corrections/{c['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"
    assert r.json()["resolved_at"] is not None

    # The default GET (pending only) no longer shows it.
    assert client.get("/api/corrections").json() == []


def test_corrections_delete_404(client: TestClient) -> None:
    r = client.delete("/api/corrections/9999")
    assert r.status_code == 404


def test_corrections_list_includes_page_title(client: TestClient) -> None:
    client.put("/api/pages/fox", json={"title": "Fox", "body": "x"})
    client.post(
        "/api/corrections",
        json={"page_slug": "fox", "selected_text": "teh"},
    )
    rows = client.get("/api/corrections").json()
    assert rows[0]["page_title"] == "Fox"


# ---------------------------------------------------------------------------
# order=updated (recent-pages listing)
# ---------------------------------------------------------------------------


def test_list_pages_order_updated_newest_first(client: TestClient) -> None:
    """The home page's "Recent pages" needs newest-edited first.

    The default `order=id` is insertion order (cursor-pagination
    stable); `order=updated` sorts by updated_at DESC.
    """
    client.put("/api/pages/old", json={"title": "Old", "body": "x"})
    client.put("/api/pages/mid", json={"title": "Mid", "body": "x"})
    client.put("/api/pages/new", json={"title": "New", "body": "x"})
    # Make `old` the most recently edited (1-second clock resolution
    # in datetime('now'), so sleep apart the edits).
    time.sleep(1.1)
    client.patch("/api/pages/old", json={"body": "x but touched later"})

    r = client.get("/api/pages?order=updated&status=active")
    assert r.status_code == 200
    slugs = [p["slug"] for p in r.json()["pages"]]
    assert slugs[0] == "old"


def test_list_pages_order_updated_carries_tags(client: TestClient) -> None:
    client.put(
        "/api/pages/tagged",
        json={"title": "Tagged", "body": "x", "tags": ["alpha", "beta"]},
    )
    r = client.get("/api/pages?order=updated&status=active")
    page = next(p for p in r.json()["pages"] if p["slug"] == "tagged")
    assert sorted(page["tags"]) == ["alpha", "beta"]


def test_list_pages_order_rejects_unknown_value(client: TestClient) -> None:
    r = client.get("/api/pages?order=bogus")
    assert r.status_code == 422  # Query pattern rejects it at the wall


def test_list_pages_order_updated_rejects_cursor(client: TestClient) -> None:
    r = client.get("/api/pages?order=updated&cursor=3")
    assert r.status_code == 400  # service-level ValidationError


def test_list_pages_default_order_id_unchanged(client: TestClient) -> None:
    """No order param = upstream behavior: insertion order + cursor."""
    client.put("/api/pages/first", json={"title": "First", "body": "x"})
    client.put("/api/pages/second", json={"title": "Second", "body": "x"})
    data = client.get("/api/pages?status=active").json()
    slugs = [p["slug"] for p in data["pages"]]
    assert slugs.index("first") < slugs.index("second")
    assert data["next_cursor"] is None


# ---------------------------------------------------------------------------
# sync payload carries tags per page
# ---------------------------------------------------------------------------


def test_sync_pages_carry_tag_names(client: TestClient) -> None:
    """The OPFS client rebuilds its page_tags table from page.tags on
    every delta. A delta page without `tags` silently wipes them
    locally — the payload must carry the names (2026-09-05 fix)."""
    client.put(
        "/api/pages/slate",
        json={"title": "Slate", "body": "x", "tags": ["infra", "tool"]},
    )

    data = client.get("/api/sync?since=0").json()
    page = next(p for p in data["pages"] if p["slug"] == "slate")
    assert sorted(page["tags"]) == ["infra", "tool"]


def test_sync_pages_without_tags_carry_empty_list(client: TestClient) -> None:
    client.put("/api/pages/untagged", json={"title": "Untagged", "body": "x"})
    data = client.get("/api/sync?since=0").json()
    page = next(p for p in data["pages"] if p["slug"] == "untagged")
    assert page["tags"] == []
