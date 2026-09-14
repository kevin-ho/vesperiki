"""Static SPA + PWA serving for the FastAPI app.

The frontend SPA (React 19 + Vite) builds into ``frontend/dist/`` with
fingerprinted assets under ``assets/`` and PWA shell files at the root
(``index.html``, ``manifest.webmanifest``, ``sw.js``, ``icon*.svg``,
``favicon.ico``). ``mount_spa`` wires those files into a FastAPI app so
``python -m vesperiki.api`` serves both the API and the web UI from one
process.

The sqlite-wasm worker files (``sqlite3-worker1.js``,
``sqlite3-worker1.mjs``, ``sqlite3-opfs-async-proxy.js``, ``sqlite3.wasm``)
are served from the URL root with explicit content types so the offline
WASM database's OPFS VFS can boot from its unhashed worker URLs instead
of receiving the SPA's index.html.

Fallback semantics
------------------

The catch-all route at ``/{full_path:path}`` returns ``index.html`` so
the SPA router can take over on client-side routes (``/p/foo``,
``/search``, ``/tags``, …). It must NOT shadow the API, the health
endpoint, the static ``/assets`` mount, or FastAPI's own docs UI
(``/docs``, ``/openapi.json``, ``/redoc``); we check ``full_path`` for
those prefixes and ``raise HTTPException(404)`` so FastAPI's default
404 handler runs and a real 404 comes back (e.g. for ``/api/does-not-exist``).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

# Path prefixes that the SPA catch-all must NOT consume. Keep these in
# sync with the API surface and FastAPI's auto-generated docs.
_NON_SPA_PREFIXES = (
    "api/",
    "healthz",
    "assets/",
    "docs",
    "openapi.json",
    "redoc",
)

# Static PWA shell files served at the URL root, with explicit content
# types so browsers pick the right parser. Order matters only for
# readability; each entry is its own route.
_PWA_FILES: tuple[tuple[str, str], ...] = (
    ("manifest.webmanifest", "application/manifest+json"),
    ("sw.js", "application/javascript"),
    ("icon.svg", "image/svg+xml"),
    ("icon-192.svg", "image/svg+xml"),
    ("icon-512.svg", "image/svg+xml"),
    ("icon-maskable.svg", "image/svg+xml"),
    ("favicon.ico", "image/x-icon"),
)

# sqlite-wasm browser files requested by their UNHASHED root names by the
# SQLite OPFS VFS. If these fell through to the SPA catch-all they would
# come back as index.html and the WASM offline DB would never boot, so
# each one gets an explicit route with the right content type. These
# files additionally need ``Cross-Origin-Resource-Policy: cross-origin``
# and ``Cross-Origin-Embedder-Policy: require-corp`` — see
# ``_CORP_HEADERS`` / ``_COEP_HEADERS`` below.
_SQLITE_FILES: tuple[tuple[str, str], ...] = (
    ("sqlite3-worker1.js", "application/javascript"),
    ("sqlite3-worker1.mjs", "application/javascript"),
    ("sqlite3-opfs-async-proxy.js", "application/javascript"),
    ("sqlite3.wasm", "application/wasm"),
)

# Cross-origin isolation headers for static asset responses. The SPA
# document is served with COEP: require-corp by
# ``api._CrossOriginIsolationMiddleware`` so sqlite-wasm's OPFS VFS can
# boot — its async-proxy worker needs ``Atomics.waitAsync``, which
# Chromium only exposes to cross-origin-isolated documents.
#
# Two headers are required on /assets/ responses and the sqlite3 root
# files so the OPFS worker boots under COEP:
#
#   * ``Cross-Origin-Resource-Policy: cross-origin`` — lets the
#     COEP-protected document load these subresources (Workers, WASM,
#     module scripts) at all.
#   * ``Cross-Origin-Embedder-Policy: require-corp`` — Chromium also
#     blocks the worker fetch itself when the worker is loaded from a
#     COEP-protected document unless the *worker's* response carries its
#     own COEP declaration (CDP: ``blockedReason:
#     "coep-frame-resource-needs-coep-header"``). CORP alone is NOT
#     sufficient.
#
# Verified live: without COEP on the worker response the loader
# degraded to :memory: (net::ERR_BLOCKED_BY_RESPONSE) and OPFS stayed
# empty across reloads. Setting both CORP and COEP on /assets/ and the
# sqlite3 root files lets the worker boot and lets its async-proxy
# fetch the WASM. The SPA document itself does not need CORP (it is
# the embedding context, not a subresource) and is deliberately left
# alone; PWA shell files (sw.js, manifest, icons) are same-origin
# loads that don't need either header.
_CORP_HEADER = "cross-origin-resource-policy"
_CORP_VALUE = "cross-origin"
_COEP_HEADER = "cross-origin-embedder-policy"
_COEP_VALUE = "require-corp"
_CORP_HEADERS: dict[str, str] = {_CORP_HEADER: _CORP_VALUE}
_COEP_HEADERS: dict[str, str] = {_COEP_HEADER: _COEP_VALUE}


class _CORPStaticFiles(StaticFiles):
    """StaticFiles mount that tags every served response with both
    ``Cross-Origin-Resource-Policy: cross-origin`` (so the SPA's
    COEP-protected document can load the subresource) and
    ``Cross-Origin-Embedder-Policy: require-corp`` (so Chromium doesn't
    block a module Worker fetched from this response with
    ``coep-frame-resource-needs-coep-header``). See the comment on
    ``_COEP_HEADERS`` for the full rationale."""

    def __init__(self, *, directory: str) -> None:
        super().__init__(directory=directory)

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers[_CORP_HEADER] = _CORP_VALUE
        response.headers[_COEP_HEADER] = _COEP_VALUE
        return response


def resolve_dist_dir(repo_root: Path | None = None) -> Path | None:
    """Locate the built SPA's ``frontend/dist`` directory.

    Resolution order:
      1. ``repo_root / "frontend" / "dist"`` if ``repo_root`` is given.
      2. ``<cwd>/frontend/dist``.
      3. ``<parent of vesperiki package>/frontend/dist`` — the in-tree
         build, when the server is run from a working copy.

    Returns ``None`` if no candidate exists so callers can degrade
    gracefully (tests without a build, ``pip install`` deployments, …).
    """
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(repo_root / "frontend" / "dist")
    candidates.append(Path.cwd() / "frontend" / "dist")

    try:
        # vesperiki/__init__.py lives at <repo_root>/vesperiki/__init__.py,
        # so the package's parent is the repo root regardless of cwd.
        pkg_root = Path(__file__).resolve().parent
        candidates.append(pkg_root.parent / "frontend" / "dist")
    except NameError:
        # __file__ is not defined in unusual environments (e.g. frozen);
        # the cwd candidate above still applies.
        pass

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """Mount the SPA + PWA shell onto ``app`` from ``dist_dir``.

    Registration order:
      1. Explicit GET routes for unhashed sqlite-wasm aliases under
         ``/assets`` (before the mount, so they take precedence).
      2. ``/assets`` mount for fingerprinted JS/CSS chunks.
      3. Explicit GET routes for PWA shell + root sqlite-wasm worker files.
      4. Catch-all ``GET /{full_path:path}`` returning ``index.html`` for
         SPA client routes, with API/health/assets/docs short-circuits.
    """
    assets_dir = dist_dir / "assets"

    # The mount claims every /assets/* path, including files that are
    # missing from its directory. Register the unhashed sqlite aliases
    # before the mount so these explicit routes can serve the root-dist
    # copies instead of being handled as StaticFiles 404s.
    sqlite_filenames = {filename for filename, _ in _SQLITE_FILES}
    _sqlite_isolation_headers: dict[str, str] = {
        **_CORP_HEADERS,
        **_COEP_HEADERS,
    }
    for filename, media_type in _SQLITE_FILES:
        path = dist_dir / filename
        if not path.is_file():
            continue

        def _make_asset_handler(
            _path: Path = path,
            _media_type: str = media_type,
        ):
            def handler() -> FileResponse:
                return FileResponse(
                    _path,
                    media_type=_media_type,
                    headers=_sqlite_isolation_headers,
                )

            return handler

        app.add_api_route(
            "/assets/" + filename,
            _make_asset_handler(),
            methods=["GET"],
            include_in_schema=False,
        )

    if assets_dir.is_dir():
        # StaticFiles handles HEAD/Range for us and 404s on missing files
        # without escaping the directory (it normalizes the path). The
        # ``_CORPStaticFiles`` subclass tags every served response with
        # both CORP and COEP so the SPA's cross-origin-isolated document
        # can load these subresources AND Chromium doesn't block a
        # module Worker fetched from this response.
        app.mount(
            "/assets",
            _CORPStaticFiles(directory=str(assets_dir)),
            name="assets",
        )

    # sqlite3 root files need CORP + COEP (so module Workers under
    # COEP can boot the OPFS VFS); PWA shell files are same-origin
    # loads that don't need either.
    for filename, media_type in (*_PWA_FILES, *_SQLITE_FILES):
        path = dist_dir / filename
        if not path.is_file():
            continue
        headers = (
            _sqlite_isolation_headers
            if filename in sqlite_filenames
            else None
        )
        # Bind each route in its own closure so ``filename`` is captured
        # correctly across iterations.
        def _make_handler(
            _path: Path = path,
            _media_type: str = media_type,
            _headers: dict[str, str] | None = headers,
        ):
            def handler() -> FileResponse:
                return FileResponse(
                    _path,
                    media_type=_media_type,
                    headers=_headers,
                )

            return handler

        route_path = "/" + filename
        app.add_api_route(
            route_path, _make_handler(), methods=["GET"], include_in_schema=False
        )

    index_html = dist_dir / "index.html"
    if not index_html.is_file():
        # Built tree is missing the entry point — fall back to a 404 from
        # the catch-all so we don't shadow real API endpoints.
        @app.get("/{full_path:path}", include_in_schema=False)
        def _no_spa(full_path: str) -> None:  # pragma: no cover
            raise HTTPException(status.HTTP_404_NOT_FOUND)

        return

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        # FastAPI strips the leading slash from ``full_path`` for the
        # catch-all parameter, so an empty string here means the user hit
        # ``GET /``. API/health/assets/docs must keep their real routes.
        if any(
            full_path == prefix.rstrip("/") or full_path.startswith(prefix)
            for prefix in _NON_SPA_PREFIXES
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return FileResponse(index_html, media_type="text/html")
