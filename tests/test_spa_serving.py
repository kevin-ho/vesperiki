"""Tests for SPA + PWA serving from frontend/dist via FastAPI.

Each test builds a fake ``frontend/dist`` tree in ``tmp_path`` and
monkey-patches ``vesperiki.static_serving.resolve_dist_dir`` so the
app factory picks it up. The fake tree mirrors what Vite actually emits
so content-type and fallback semantics can be exercised end-to-end
without depending on a real ``npm run build``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import vesperiki.static_serving as static_serving
from vesperiki.api import create_app

INDEX_HTML = """<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <link rel=\"manifest\" href=\"/manifest.webmanifest\" />
    <title>Vesperiki</title>
  </head>
  <body>
    <div id=\"root\"></div>
    <script type=\"module\" src=\"/assets/index-abc123.js\"></script>
  </body>
</html>
"""

MANIFEST_JSON = {
    "name": "Vesperiki",
    "short_name": "Vesperiki",
    "start_url": "/",
    "display": "standalone",
    "icons": [
        {"src": "/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml"},
    ],
}

SW_JS = "// service-worker stub\nself.addEventListener('install', () => {});\n"
ICON_SVG = "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\"></svg>\n"
FAVICON_ICO = b"\x00\x00\x01\x00\x01\x00\x10\x10"  # not a real ICO; just bytes
INDEX_JS = "console.log('index-abc123');\n"
INDEX_CSS = "body { color: black; }\n"
SQLITE3_WASM = b"\x00asm\x01\x00\x00\x00"  # wasm magic + version, just bytes
SQLITE3_WORKER_JS = "// sqlite3 worker stub\n"
SQLITE3_OPFS_PROXY_JS = "// opfs async proxy stub\n"


def _build_fake_dist(tmp_path: Path) -> Path:
    """Lay down the files the SPA tests expect, mirroring a Vite build."""
    dist = tmp_path / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML)
    (dist / "manifest.webmanifest").write_text(json.dumps(MANIFEST_JSON))
    (dist / "sw.js").write_text(SW_JS)
    (dist / "icon.svg").write_text(ICON_SVG)
    (dist / "icon-192.svg").write_text(ICON_SVG)
    (dist / "icon-512.svg").write_text(ICON_SVG)
    (dist / "icon-maskable.svg").write_text(ICON_SVG)
    (dist / "favicon.ico").write_bytes(FAVICON_ICO)
    (dist / "sqlite3-worker1.js").write_text(SQLITE3_WORKER_JS)
    (dist / "sqlite3-worker1.mjs").write_text(SQLITE3_WORKER_JS)
    (dist / "sqlite3-opfs-async-proxy.js").write_text(SQLITE3_OPFS_PROXY_JS)
    (dist / "sqlite3.wasm").write_bytes(SQLITE3_WASM)
    (dist / "assets" / "index-abc123.js").write_text(INDEX_JS)
    (dist / "assets" / "index-abc123.css").write_text(INDEX_CSS)
    return dist


@pytest.fixture
def fake_dist_dir(tmp_path: Path) -> Path:
    return _build_fake_dist(tmp_path)


@pytest.fixture
def client(
    tmp_path: Path,
    fake_dist_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("VESPERIKI_DB_PATH", db_path)
    monkeypatch.setattr(
        static_serving, "resolve_dist_dir", lambda repo_root=None: fake_dist_dir
    )
    return TestClient(create_app(db_path))


@pytest.fixture
def client_no_spa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """A client where ``resolve_dist_dir`` returns None — SPA is not
    served, but the API still works (graceful degradation)."""
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("VESPERIKI_DB_PATH", db_path)
    monkeypatch.setattr(static_serving, "resolve_dist_dir", lambda repo_root=None: None)
    return TestClient(create_app(db_path))


# ---------------------------------------------------------------------------
# SPA fallback (index.html)
# ---------------------------------------------------------------------------

def test_root_serves_index_html(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in r.text


def test_index_html_path_serves_index_html(client: TestClient) -> None:
    r = client.get("/index.html")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in r.text


def test_spa_route_falls_back_to_index(client: TestClient) -> None:
    r = client.get("/p/foo")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_search_route_falls_back_to_index(client: TestClient) -> None:
    r = client.get("/search")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_tags_route_falls_back_to_index(client: TestClient) -> None:
    r = client.get("/tags")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


# ---------------------------------------------------------------------------
# Cross-origin isolation headers (COOP/COEP) for the SPA document
# ---------------------------------------------------------------------------
# sqlite-wasm's OPFS VFS needs Atomics.waitAsync in its worker, which
# Chromium gates behind cross-origin isolation. The SPA document must
# therefore come back with COOP: same-origin + COEP: require-corp.


def test_root_sets_coop_coep(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert r.headers["cross-origin-embedder-policy"] == "require-corp"


def test_deep_spa_route_sets_coop_coep(client: TestClient) -> None:
    r = client.get("/p/foo")
    assert r.status_code == 200
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert r.headers["cross-origin-embedder-policy"] == "require-corp"


def test_api_responses_do_not_set_coop_coep(client: TestClient) -> None:
    r = client.get("/api/pages")
    assert r.status_code == 200
    assert "cross-origin-opener-policy" not in r.headers
    assert "cross-origin-embedder-policy" not in r.headers


def test_subresources_do_not_set_coop(client: TestClient) -> None:
    # COOP belongs only on the SPA document — subresources are not
    # browsing-context containers and don't need cross-origin opener
    # isolation. Keep the blast radius limited to the HTML document.
    for path in (
        "/assets/index-abc123.js",
        "/assets/index-abc123.css",
        "/sw.js",
        "/manifest.webmanifest",
        "/icon.svg",
        "/sqlite3-worker1.js",
        "/sqlite3.wasm",
    ):
        r = client.get(path)
        assert r.status_code == 200
        assert "cross-origin-opener-policy" not in r.headers


def test_pwa_shell_does_not_set_coep(client: TestClient) -> None:
    # PWA shell files are same-origin loads from the SPA document and
    # don't need their own COEP declaration (the document is what gets
    # isolated, not the shell). Setting COEP on the service worker in
    # particular would actively break SW scope handling.
    for path in (
        "/sw.js",
        "/manifest.webmanifest",
        "/icon.svg",
        "/icon-192.svg",
        "/favicon.ico",
    ):
        r = client.get(path)
        assert r.status_code == 200
        assert "cross-origin-embedder-policy" not in r.headers


# ---------------------------------------------------------------------------
# Cross-Origin-Resource-Policy (CORP) and COEP for static assets
# ---------------------------------------------------------------------------
# The SPA document carries COEP: require-corp so sqlite-wasm's OPFS VFS
# can boot (Atomics.waitAsync in the worker). Under COEP, module Workers
# and cross-origin subresources must carry an explicit CORP or the
# response is blocked (net::ERR_BLOCKED_BY_RESPONSE). Additionally, a
# module Worker fetched from a COEP-protected document is itself blocked
# unless its own response declares COEP (CDP
# ``coep-frame-resource-needs-coep-header``); CORP alone is not
# sufficient. Static asset responses from /assets/ and the sqlite3 root
# files are tagged with BOTH CORP: cross-origin and COEP: require-corp;
# the SPA document itself does not need CORP and PWA shell files
# (sw.js, manifest, icons) don't need either.


def test_sqlite3_worker_files_set_corp_and_coep(client: TestClient) -> None:
    for path in (
        "/sqlite3-worker1.js",
        "/sqlite3-worker1.mjs",
        "/sqlite3-opfs-async-proxy.js",
    ):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["cross-origin-resource-policy"] == "cross-origin"
        assert r.headers["cross-origin-embedder-policy"] == "require-corp"


def test_sqlite3_wasm_sets_corp_and_coep(client: TestClient) -> None:
    r = client.get("/sqlite3.wasm")
    assert r.status_code == 200
    assert r.headers["cross-origin-resource-policy"] == "cross-origin"
    assert r.headers["cross-origin-embedder-policy"] == "require-corp"


def test_unhashed_sqlite_assets_are_served_under_assets(client: TestClient) -> None:
    cases = (
        ("/assets/sqlite3.wasm", "application/wasm", SQLITE3_WASM),
        (
            "/assets/sqlite3-opfs-async-proxy.js",
            "application/javascript",
            SQLITE3_OPFS_PROXY_JS.encode(),
        ),
    )
    for path, media_type, body in cases:
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith(media_type)
        assert r.headers["cross-origin-resource-policy"] == "cross-origin"
        assert r.headers["cross-origin-embedder-policy"] == "require-corp"
        assert r.content == body


def test_assets_files_set_corp_and_coep(client: TestClient) -> None:
    # /assets/ is mounted via a StaticFiles subclass that tags every
    # served response with CORP and COEP. Without CORP, the SPA's
    # COEP-enabled document cannot load module scripts from /assets/
    # at all; without COEP, a module Worker fetched from this response
    # is blocked with coep-frame-resource-needs-coep-header.
    for path in ("/assets/index-abc123.js", "/assets/index-abc123.css"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.headers["cross-origin-resource-policy"] == "cross-origin"
        assert r.headers["cross-origin-embedder-policy"] == "require-corp"


# ---------------------------------------------------------------------------
# PWA shell files (explicit routes)
# ---------------------------------------------------------------------------

def test_manifest_has_correct_content_type(client: TestClient) -> None:
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    body = r.json()
    assert body["name"] == "Vesperiki"
    assert body["start_url"] == "/"


def test_sw_has_correct_content_type(client: TestClient) -> None:
    r = client.get("/sw.js")
    assert r.status_code == 200
    ct = r.headers["content-type"]
    assert ct.startswith("application/javascript") or ct.startswith(
        "text/javascript"
    )
    assert "service-worker" in r.text


def test_icon_svg_content_type(client: TestClient) -> None:
    r = client.get("/icon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in r.text


def test_icon_192_content_type(client: TestClient) -> None:
    r = client.get("/icon-192.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")


# ---------------------------------------------------------------------------
# sqlite-wasm worker files (explicit routes, correct MIME types)
# ---------------------------------------------------------------------------

def test_sqlite3_wasm_content_type(client: TestClient) -> None:
    r = client.get("/sqlite3.wasm")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/wasm")
    assert r.content == SQLITE3_WASM


def test_sqlite3_worker_js_content_type(client: TestClient) -> None:
    for path in ("/sqlite3-worker1.js", "/sqlite3-worker1.mjs"):
        r = client.get(path)
        assert r.status_code == 200
        ct = r.headers["content-type"]
        assert ct.startswith("application/javascript") or ct.startswith(
            "text/javascript"
        )
        assert "worker" in r.text


def test_sqlite3_opfs_proxy_content_type(client: TestClient) -> None:
    r = client.get("/sqlite3-opfs-async-proxy.js")
    assert r.status_code == 200
    ct = r.headers["content-type"]
    assert ct.startswith("application/javascript") or ct.startswith(
        "text/javascript"
    )
    assert "proxy" in r.text


def test_missing_sqlite_file_falls_back_to_index(client: TestClient) -> None:
    # A sqlite-named path with no matching file must still hit the SPA
    # fallback (index.html), not a 404 — fallback semantics unchanged.
    r = client.get("/sqlite3-worker1.wasm")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert '<div id="root"></div>' in r.text


# ---------------------------------------------------------------------------
# Built assets (Vite chunks under /assets)
# ---------------------------------------------------------------------------

def test_assets_js_served(client: TestClient) -> None:
    r = client.get("/assets/index-abc123.js")
    assert r.status_code == 200
    ct = r.headers["content-type"]
    assert ct.startswith("application/javascript") or ct.startswith(
        "text/javascript"
    )
    assert "index-abc123" in r.text


def test_assets_css_served(client: TestClient) -> None:
    r = client.get("/assets/index-abc123.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "color: black" in r.text


# ---------------------------------------------------------------------------
# API + healthz must still work and beat the SPA fallback
# ---------------------------------------------------------------------------

def test_api_routes_still_work(client: TestClient) -> None:
    assert client.get("/api/pages").status_code == 200
    # Real API 404 (page not found) — must NOT be swallowed by SPA fallback.
    assert client.get("/api/pages/foo").status_code == 404
    assert client.get("/api/tags").status_code == 200


def test_healthz_still_works(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["seq"], int)


# ---------------------------------------------------------------------------
# Graceful degradation when no build is present
# ---------------------------------------------------------------------------

def test_missing_dist_graceful(client_no_spa: TestClient) -> None:
    # SPA not served — root 404s.
    assert client_no_spa.get("/").status_code == 404
    # API still works.
    assert client_no_spa.get("/api/pages").status_code == 200
    assert client_no_spa.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# Security: path traversal must be blocked by StaticFiles
# ---------------------------------------------------------------------------

def test_assets_path_traversal_blocked(client: TestClient) -> None:
    # StaticFiles normalizes the path and 404s on escape attempts. The
    # important assertion is that /etc/passwd is NOT in the response.
    r = client.get("/assets/../../../etc/passwd", follow_redirects=False)
    if r.status_code == 200:
        # If anything leaked, it must not be /etc/passwd contents.
        assert "root:" not in r.text
    else:
        assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# FastAPI's own docs UI must beat the SPA fallback
# ---------------------------------------------------------------------------

def test_fallback_skips_docs(client: TestClient) -> None:
    r = client.get("/docs")
    assert r.status_code == 404
