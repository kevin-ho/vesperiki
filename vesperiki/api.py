"""REST API for Vesperiki: FastAPI over the service layer.

Entry point: ``python -m vesperiki.api``.

This module is the HTTP transport only. All validation, dedup, revision
writing, and link rebuilding live in ``vesperiki.service``.
FastAPI is chosen over MCP because browsers cannot speak stdio MCP and the
web frontend needs standard HTTP verbs, status codes, and exception
semantics. The service layer is the single source of truth; both
transports serialize the same dicts.

Sync endpoint contract:
  Cursor is ``change_seq.value`` — an integer, never a timestamp.
  Pages and tombstones come as delta (``WHERE seq > since``); the four
  small reference tables (links, page_tags, tags, aliases) come whole
  every call. The client interleaves pages and tombstones by ``seq`` to
  replay any purge/re-create/purge sequence correctly; phase-ordering
  (tombstones-first) only happens to work for the common case.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import (
    Body,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

import vesperiki.db as db
import vesperiki.service as service
import vesperiki.static_serving as static_serving


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7420
DEFAULT_DB_PATH = "./vesperiki.db"


# ---------------------------------------------------------------------------
# Sync delta lives here rather than in service.py: it is a read-only
# projection over the change log, not a write-path concern.
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def _sync_delta(path: str, since: int, limit: int) -> dict[str, Any]:
    """Build a sync response shape from the database.

    Order in this function matters: cursor is read AFTER the deltas, so
    it reflects what the client has just received plus any in-flight
    commits.
    """
    with db.connection(path) as conn:
        cursor = conn.execute(
            "SELECT value AS v FROM change_seq WHERE id = 1"
        ).fetchone()["v"]

        page_rows = conn.execute(
            "SELECT id, slug, title, title_norm, body, type, status, "
            "sources, metadata, updated_at, verified_at, confidence, seq "
            "FROM pages WHERE seq > ? AND status != 'deprecated' "
            "ORDER BY seq ASC LIMIT ?",
            (since, limit),
        ).fetchall()

        # Each page is enriched with created_at from its first revision.
        # `pages.created_at` was intentionally removed in rev 13
        # (denormalization drift on edit). The join is keyed on page_id;
        # one row per page is enough.
        page_ids = [r["id"] for r in page_rows]
        created_by_id: dict[int, str | None] = {}
        if page_ids:
            placeholders = ",".join("?" * len(page_ids))
            for r in conn.execute(
                f"SELECT page_id, changed_at FROM revisions "
                f"WHERE change_type = 'create' AND page_id IN ({placeholders}) "
                f"GROUP BY page_id",
                page_ids,
            ).fetchall():
                created_by_id[r["page_id"]] = r["changed_at"]

        pages: list[dict[str, Any]] = []
        # Attach tag NAMES to each page row. The client stores tags on the
        # page row itself (tags_json) and rebuilds its page_tags table from
        # page.tags on every delta — a delta without `tags` silently wipes
        # them locally (the client defaults the missing field to []).
        tags_by_page: dict[int, list[str]] = {}
        if page_ids:
            placeholders = ", ".join("?" * len(page_ids))
            for r in conn.execute(
                f"SELECT pt.page_id, t.name FROM page_tags pt "
                f"JOIN tags t ON t.id = pt.tag_id "
                f"WHERE pt.page_id IN ({placeholders}) ORDER BY t.name",
                page_ids,
            ):
                tags_by_page.setdefault(r["page_id"], []).append(r["name"])
        for row in page_rows:
            d = _row_to_dict(row)
            d["created_at"] = created_by_id.get(row["id"])
            d["tags"] = tags_by_page.get(row["id"], [])
            pages.append(d)

        tombstone_rows = conn.execute(
            "SELECT slug, seq, deleted_at FROM tombstones "
            "WHERE seq > ? ORDER BY seq ASC",
            (since,),
        ).fetchall()

        links = [_row_to_dict(r) for r in conn.execute(
            "SELECT source_id, target_id, rel, origin, context FROM links"
        ).fetchall()]
        page_tags = [_row_to_dict(r) for r in conn.execute(
            "SELECT page_id, tag_id FROM page_tags"
        ).fetchall()]
        tags = [_row_to_dict(r) for r in conn.execute(
            "SELECT id, name FROM tags"
        ).fetchall()]
        aliases = [_row_to_dict(r) for r in conn.execute(
            "SELECT alias, page_id, created_at FROM slug_aliases"
        ).fetchall()]

    next_cursor = cursor if len(pages) == limit else None
    return {
        "cursor": cursor,
        "next_cursor": next_cursor,
        "pages": pages,
        "tombstones": [_row_to_dict(r) for r in tombstone_rows],
        "links": links,
        "page_tags": page_tags,
        "tags": tags,
        "aliases": aliases,
    }


# ---------------------------------------------------------------------------
# Cross-origin isolation (COOP/COEP) for the SPA document
# ---------------------------------------------------------------------------

_COOP_HEADER = (b"cross-origin-opener-policy", b"same-origin")
_COEP_HEADER = (b"cross-origin-embedder-policy", b"require-corp")


class _CrossOriginIsolationMiddleware:
    """Set COOP/COEP on SPA document responses so sqlite-wasm's OPFS VFS
    (``opfs-wl``) can boot: its async-proxy worker needs
    ``Atomics.waitAsync``, which Chromium only enables for cross-origin
    isolated documents.

    Only SPA document responses get the headers. The catch-all and
    ``/index.html`` are the app's sole ``text/html`` responses (docs UI is
    disabled), so the response content-type is the exact discriminator:
    API (JSON), ``/assets`` chunks, and the PWA/sqlite worker files keep
    their existing headers, leaving API CORS and same-origin asset
    handling untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_isolation(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                content_type = b""
                for name, value in message["headers"]:
                    if name.lower() == b"content-type":
                        content_type = value
                        break
                if content_type.startswith(b"text/html"):
                    message["headers"] = [
                        *message["headers"],
                        _COOP_HEADER,
                        _COEP_HEADER,
                    ]
            await send(message)

        await self.app(scope, receive, send_with_isolation)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(db_path: str) -> FastAPI:
    """Build a FastAPI app bound to the given DB path. ``db.init_db`` runs in
    the lifespan so the schema is initialized for the test path before the
    first request.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    # Initialize eagerly so TestClient(app) without ``with`` works and so
    # the first request can't race startup. db.init_db returns an open
    # connection; close it immediately — every request opens its own.
    eager_conn = db.init_db(db_path)
    eager_conn.close()
    service.init_service(db_path)

    app = FastAPI(
        title="Vesperiki",
        lifespan=lifespan,
        # Disable the auto-generated docs UI / OpenAPI schema. The SPA
        # catches all non-API paths and would otherwise conflict with
        # /docs and /openapi.json, and the product surface is the JSON
        # API — the product surface is the JSON API, not Swagger UI.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # CORS: local development is localhost-only. ``*`` is fine here because
    # the backend is not internet-facing by default;
    # tightening back to specific origins is one environment-flag change.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://localhost:8080",
        ],
        allow_origin_regex=r"https://.*",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Cross-origin isolation for the SPA document: without COOP/COEP,
    # sqlite-wasm's OPFS VFS can't use Atomics.waitAsync in its worker and
    # the offline DB silently falls back to in-memory. See
    # _CrossOriginIsolationMiddleware for what exactly gets the headers.
    app.add_middleware(_CrossOriginIsolationMiddleware)

    _register_exception_handlers(app)
    _register_routes(app, db_path)

    # Mount the built SPA + PWA shell from frontend/dist/ AFTER the API
    # routes so /api/*, /healthz, /docs, and /assets all win over the
    # catch-all SPA fallback. resolve_dist_dir returns None if the build
    # is absent (e.g. in a CI wheel or a test without a fake dist tree),
    # and we degrade to API-only rather than crashing.
    dist_dir = static_serving.resolve_dist_dir()
    if dist_dir is not None:
        static_serving.mount_spa(app, dist_dir)
    else:
        import logging
        logging.getLogger(__name__).warning(
            "vesperiki: frontend/dist not found, SPA not served"
        )

    return app


# ---------------------------------------------------------------------------
# Exception mapping
# ---------------------------------------------------------------------------

def _service_error_payload(exc: service.ServiceError) -> dict[str, Any]:
    return {
        "code": exc.code,
        "message": str(exc),
        "details": dict(exc.details),
    }


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(service.NotFoundError)
    def _not_found(_request: Request, exc: service.NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=_service_error_payload(exc),
        )

    @app.exception_handler(service.ValidationError)
    def _validation(
        _request: Request, exc: service.ValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_service_error_payload(exc),
        )

    @app.exception_handler(service.DuplicateError)
    def _duplicate(
        _request: Request, exc: service.DuplicateError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_service_error_payload(exc),
        )

    @app.exception_handler(service.AliasConflictError)
    def _alias_conflict(
        _request: Request, exc: service.AliasConflictError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=_service_error_payload(exc),
        )

    @app.exception_handler(service.ServiceError)
    def _other_service(
        _request: Request, exc: service.ServiceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_service_error_payload(exc),
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _register_routes(app: FastAPI, db_path: str) -> None:

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        with db.connection(db_path) as conn:
            seq = conn.execute(
                "SELECT value AS v FROM change_seq WHERE id = 1"
            ).fetchone()["v"]
        return {"status": "ok", "seq": seq}

    # ----- pages -----

    @app.get("/api/pages")
    def list_pages(
        limit: int = Query(50, ge=1, le=200),
        cursor: int | None = Query(None),
        tag: str | None = Query(None),
        type: str | None = Query(None),
        status_filter: str | None = Query(None, alias="status"),
        include_body: bool = Query(False),
        order: str = Query("id", pattern="^(id|updated)$"),
    ) -> dict[str, Any]:
        # ``status`` shadows the imported ``fastapi.status`` module locally,
        # so the parameter is renamed above and forwarded as ``status=``.
        # ``include_body`` defaults to False so the default listing payload
        # stays slim (metadata only); callers that need every body pass
        # ``?include_body=true`` and accept the larger response.
        # ``order``: "id" (default, cursor-paginated insertion order) or
        # "updated" (most recently updated first; no cursor).
        return service.list_pages(
            limit=limit,
            cursor=cursor,
            tag=tag,
            type=type,
            status=status_filter,
            include_body=include_body,
            order=order,
        )

    @app.get("/api/pages/{slug}")
    def get_page(slug: str) -> Response:
        page = service.get_page(slug)
        if page.get("redirected_from"):
            return Response(
                status_code=status.HTTP_301_MOVED_PERMANENTLY,
                headers={"Location": f"/api/pages/{page['slug']}"},
            )
        return JSONResponse(content=page)

    @app.put("/api/pages/{slug}", status_code=status.HTTP_201_CREATED)
    def put_page(slug: str, body: dict[str, Any] = Body(...)) -> Any:
        # Upsert: PUT on an existing slug updates it (create_page delegates
        # to update_page) — the status stays 201 for both. With
        # source_markdown the title may come from frontmatter, so a missing
        # top-level title is only rejected when the caller didn't opt in.
        source_markdown = bool(body.get("source_markdown", False))
        title = body.get("title")
        if not body.get("body"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "validation",
                    "message": "body is required",
                    "details": {},
                },
            )
        if not source_markdown and not title:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "validation",
                    "message": "title and body are required",
                    "details": {},
                },
            )
        return service.create_page(
            slug=slug,
            title=title,
            body=body["body"],
            tags=body.get("tags") or (),
            type=body.get("type") or "entity",
            sources=body.get("sources") or (),
            force=bool(body.get("force", False)),
            source_markdown=source_markdown,
        )

    @app.patch("/api/pages/{slug}")
    def patch_page(slug: str, body: dict[str, Any] = Body(...)) -> Any:
        return service.update_page(
            slug=slug,
            title=body.get("title"),
            body=body.get("body"),
            tags=body.get("tags"),
            type=body.get("type"),
            sources=body.get("sources"),
            verified_at=body.get("verified_at"),
            confidence=body.get("confidence"),
            force=bool(body.get("force", False)),
            source_markdown=bool(body.get("source_markdown", False)),
        )

    @app.delete("/api/pages/{slug}")
    def delete_page(slug: str, purge: bool = Query(False)) -> Any:
        return service.delete_page(slug=slug, purge=purge)

    @app.patch("/api/pages/{slug}/section/{section_id}")
    def patch_section(
        slug: str, section_id: str, body: dict[str, Any] = Body(...)
    ) -> Any:
        return service.update_section(
            slug=slug,
            section_id=section_id,
            content=body.get("content", ""),
        )

    @app.get("/api/pages/{slug}/revisions")
    def list_revisions(slug: str) -> list[dict[str, Any]]:
        page = service.get_page(slug, include_revisions=True)
        return list(page.get("revisions") or [])

    @app.post("/api/pages/{slug}/revisions/{revision_id}/restore")
    def restore_revision(slug: str, revision_id: int) -> Any:
        return service.admin_restore(slug=slug, revision_id=revision_id)

    # ----- search / metadata -----

    @app.get("/api/search")
    def search(
        q: str = Query(..., min_length=1),
        mode: str = Query("keyword", pattern="^(keyword|semantic|hybrid)$"),
        limit: int = Query(10, ge=1, le=100),
        include_body: bool = Query(False),
        tag: str | None = Query(None),
        type: str | None = Query(None),
    ) -> Any:
        keyword = service.search_pages(query=q, limit=limit, include_body=include_body, tag=tag, type=type)
        if mode == "keyword":
            return keyword
        try:
            if mode == "semantic":
                return {"results": service.search_semantic(query=q, limit=limit, include_body=include_body, tag=tag, type=type), "semantic": True, "served_by": "semantic"}
            return service.search_hybrid(query=q, limit=limit, include_body=include_body, tag=tag, type=type)
        except service.ServiceError as exc:
            return {
                "results": keyword,
                "semantic": False,
                "served_by": "keyword",
                "note": ("set VESPERIKI_EMBED_URL and VESPERIKI_EMBED_MODEL to enable" if isinstance(exc, service.SemanticSearchNotConfigured) else str(exc)),
            }

    @app.get("/api/search/semantic")
    def search_semantic_alias(
        q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=100),
        include_body: bool = Query(False), tag: str | None = Query(None), type: str | None = Query(None),
    ) -> Any:
        return search(q=q, mode="semantic", limit=limit, include_body=include_body, tag=tag, type=type)

    @app.get("/api/stale")
    def stale(days: int = Query(90, ge=1, le=3650)) -> list[dict[str, Any]]:
        return service.get_meta(action="stale", days=days)

    @app.get("/api/tags")
    def tags() -> list[dict[str, Any]]:
        return service.get_meta(action="tags")

    @app.get("/api/graph")
    def graph() -> dict[str, list[Any]]:
        with db.connection(db_path) as conn:
            page_rows = conn.execute(
                "SELECT id, slug, title, type FROM pages "
                "WHERE status != 'deprecated'"
            ).fetchall()
            edge_rows = conn.execute(
                "SELECT l.source_id, l.target_id, l.rel, l.origin, "
                "       ps.slug AS source_slug, pt.slug AS target_slug "
                "FROM links l "
                "JOIN pages ps ON ps.id = l.source_id "
                "JOIN pages pt ON pt.id = l.target_id "
                "WHERE ps.status != 'deprecated' "
                "  AND pt.status != 'deprecated'"
            ).fetchall()
        nodes = [
            {"id": r["id"], "slug": r["slug"], "title": r["title"], "type": r["type"]}
            for r in page_rows
        ]
        edges = [
            {
                "source": r["source_id"],
                "target": r["target_id"],
                "rel": r["rel"],
                "origin": r["origin"],
            }
            for r in edge_rows
        ]
        return {"nodes": nodes, "edges": edges}

    @app.get("/api/orphans")
    def orphans() -> list[dict[str, Any]]:
        with db.connection(db_path) as conn:
            rows = conn.execute(
                "SELECT slug, title, type FROM pages "
                "WHERE status != 'deprecated' "
                "  AND id NOT IN (SELECT target_id FROM links) "
                "ORDER BY slug"
            ).fetchall()
        return [dict(r) for r in rows]

    # ----- correction queue -----
    # One-tap typo/error flag from the reader. Default GET is pending-only
    # (the agent's "what should I fix next" surface). PATCH sets an explicit
    # status; DELETE is a shorthand for "mark resolved" so a single click
    # clears the row from the queue.

    @app.get("/api/corrections")
    def list_corrections(
        status: str = Query("pending"),
    ) -> list[dict[str, Any]]:
        # Default to pending — the queue's whole point is surfacing things
        # to fix. Pass ?status=resolved|dismissed|all to see the rest.
        if status == "all":
            return service.list_corrections()
        return service.list_corrections(status=status)

    @app.post("/api/corrections", status_code=status.HTTP_201_CREATED)
    def post_correction(body: dict[str, Any] = Body(...)) -> Any:
        page_slug = body.get("page_slug")
        selected_text = body.get("selected_text")
        if not isinstance(page_slug, str) or not page_slug:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "validation",
                    "message": "page_slug is required",
                    "details": {},
                },
            )
        if not isinstance(selected_text, str) or not selected_text.strip():
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "validation",
                    "message": "selected_text is required and must be non-empty",
                    "details": {},
                },
            )
        return service.create_correction(
            page_slug=page_slug,
            selected_text=selected_text,
            note=body.get("note"),
        )

    @app.patch("/api/corrections/{correction_id}")
    def patch_correction(
        correction_id: int, body: dict[str, Any] = Body(...)
    ) -> Any:
        target_status = body.get("status", "resolved")
        return service.resolve_correction(
            correction_id=correction_id,
            status=target_status,
            resolved_by=body.get("resolved_by"),
        )

    @app.delete("/api/corrections/{correction_id}")
    def delete_correction(correction_id: int) -> Any:
        # DELETE = the one-tap "I handled this" gesture. Equivalent to
        # PATCH {"status": "resolved"} but doesn't require a body.
        return service.resolve_correction(
            correction_id=correction_id, status="resolved"
        )

    # ----- media -----

    @app.get("/api/media/{media_id}")
    def get_media(media_id: int) -> Response:
        with db.connection(db_path) as conn:
            row = conn.execute(
                "SELECT data, mime_type FROM media WHERE id = ?",
                (media_id,),
            ).fetchone()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "media not found")
        data = row["data"]
        if isinstance(data, (bytes, bytearray, memoryview)):
            body_bytes = bytes(data)
        else:
            body_bytes = data.encode("utf-8") if isinstance(data, str) else b""
        return Response(
            content=body_bytes,
            media_type=row["mime_type"] or "application/octet-stream",
        )

    @app.post("/api/media", status_code=status.HTTP_201_CREATED)
    async def post_media(
        file: UploadFile = File(...),
        slug: str = Form(...),
    ) -> dict[str, Any]:
        data = await file.read()
        sha = hashlib.sha256(data).hexdigest()
        # MIME guessing is intentionally simple: trust the client's content_type
        # and fall back to application/octet-stream. The brief leaves server-side
        # MIME detection out of scope.
        mime_type = file.content_type or "application/octet-stream"
        filename = file.filename or "upload"

        with db.connection(db_path) as conn:
            existing = conn.execute(
                "SELECT id, byte_size FROM media WHERE sha256 = ?", (sha,)
            ).fetchone()
            if existing is not None:
                return {
                    "id": existing["id"],
                    "sha256": sha,
                    "byte_size": existing["byte_size"],
                }

            page = conn.execute(
                "SELECT id FROM pages WHERE slug = ?", (slug,)
            ).fetchone()
            if page is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, f"page {slug!r} not found"
                )

            cursor = conn.execute(
                "INSERT INTO media "
                "(page_id, filename, mime_type, byte_size, sha256, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (page["id"], filename, mime_type, len(data), sha, data),
            )
            media_id = cursor.lastrowid
            conn.commit()

        return {"id": media_id, "sha256": sha, "byte_size": len(data)}

    # ----- sync -----

    @app.get("/api/sync")
    def sync(
        since: int = Query(0, ge=0),
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict[str, Any]:
        return _sync_delta(db_path, since, limit)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Run uvicorn against the configured DB path."""
    import uvicorn

    host = os.environ.get("VESPERIKI_HOST", DEFAULT_HOST)
    port = int(os.environ.get("VESPERIKI_PORT", str(DEFAULT_PORT)))
    path = os.environ.get("VESPERIKI_DB_PATH", DEFAULT_DB_PATH)

    app = create_app(path)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
