"""MCP stdio server for Vesperiki."""

from __future__ import annotations

import asyncio
import ast
import base64
import binascii
import json
import logging
import os
import re
import sys
from collections.abc import Callable
from typing import Any

import jsonschema
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

import vesperiki.db as db
import vesperiki.sections as sections
import vesperiki.service as service

_DB_PATH: str | None = None

_LOG_LEVEL = os.environ.get("VESPERIKI_LOG_LEVEL", "INFO").upper()
_LOGGER = logging.getLogger("vesperiki.mcp")
if not _LOGGER.handlers:
    _handler = logging.StreamHandler(stream=sys.__stderr__)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    _LOGGER.addHandler(_handler)
    _LOGGER.setLevel(_LOG_LEVEL)
    _LOGGER.propagate = False

server = Server("vesperiki")

_TOOLS = {
    "wiki_read": Tool(
        name="wiki_read",
        description=(
            "Read a wiki page by slug. expand_links returns linked pages' titles. "
            "include_revisions returns revision history. section returns a "
            "single section (slugified heading or 'intro') instead of the "
            "full body."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
                "expand_links": {"type": "boolean", "default": False},
                "include_revisions": {"type": "boolean", "default": False},
                "section": {
                    "type": ["string", "null"],
                    "description": (
                        "Return only this section (slugified heading or "
                        "'intro'). Omit for full page."
                    ),
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    ),
    "wiki_search": Tool(
        name="wiki_search",
        description=(
            "Search wiki text with ranked results and optional tag or type filters. "
            "Results contain snippets unless include_body is true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 10,
                },
                "include_body": {"type": "boolean", "default": False},
                "tag": {"type": ["string", "null"]},
                "type": {"type": ["string", "null"]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    ),
    "wiki_meta": Tool(
        name="wiki_meta",
        description=(
            "Query wiki metadata: page existence or lists, tag counts, recent pages, "
            "outgoing links, and backlinks. Page bodies are not returned."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "exists",
                        "list",
                        "tags",
                        "recent",
                        "stale",
                        "stale_ranked",
                        "links",
                        "backlinks",
                        "corrections",
                    ],
                },
                "slug": {
                    "type": ["string", "null"],
                    "pattern": "^[a-z0-9_-]+$",
                },
                "tag": {"type": ["string", "null"]},
                "type": {"type": ["string", "null"]},
                "status": {"type": ["string", "null"]},
                "include_tags": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Pages must have ALL of these tags (AND). "
                        "Used by list and stale_ranked."
                    ),
                },
                "exclude_tags": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Pages with ANY of these tags are dropped. "
                        "Used by list and stale_ranked."
                    ),
                },
                "limit": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 200,
                },
                "cursor": {"type": ["integer", "null"], "minimum": 0},
                "days": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 3650,
                    "default": 90,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(readOnlyHint=True),
    ),
    "wiki_write": Tool(
        name="wiki_write",
        description=(
            "Create, update, soft-delete, or revive a page, or patch a single "
            "section of a page's body. Create requires title and body and runs "
            "duplicate detection; force overrides duplicate candidates. "
            "create is an upsert: if the slug already exists it updates the "
            "existing page instead of erroring. "
            "update_section requires section_id (the slugified heading or "
            "'intro') and content and replaces the body of one ## section — "
            "the heading is preserved automatically, do NOT include it in "
            "content. revive flips a deprecated page back to active (no-op "
            "if already active)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "update", "delete", "revive", "update_section"]},
                "slug": {"type": "string", "pattern": "^[a-z0-9_-]+$"},
                "title": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "description": "Required for create.",
                },
                "body": {
                    "type": ["string", "null"],
                    "description": "Required for create.",
                },
                "section_id": {
                    "type": ["string", "null"],
                    "description": (
                        "Slugified section heading or 'intro'. "
                        "Required for update_section."
                    ),
                },
                "content": {
                    "type": ["string", "null"],
                    "description": (
                        "New content for the section. Do NOT include the "
                        "heading line — it is preserved automatically."
                    ),
                },
                "tags": {
                    "type": ["array", "null"],
                    "items": {"type": "string", "minLength": 1},
                },
                "type": {"type": ["string", "null"]},
                "sources": {
                    "type": ["array", "null"],
                    "items": {"type": "object"},
                },
                "confidence": {
                    "type": ["number", "null"],
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, bypasses duplicate detection on create "
                        "and forces the write even if duplicate candidates "
                        "were found."
                    ),
                },
                "source_markdown": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Treat body as Markdown: parse a leading YAML "
                        "frontmatter block (title/tags/type) and store only "
                        "the clean body. Malformed frontmatter falls back to "
                        "raw body."
                    ),
                },
            },
            "required": ["action", "slug"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(destructiveHint=True),
    ),
    "wiki_admin": Tool(
        name="wiki_admin",
        description=(
            "Manage explicit links or restore a page revision. Link requires source, "
            "target, and relation; unlink may omit relation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["link", "unlink", "restore"]},
                "source_slug": {
                    "type": ["string", "null"],
                    "pattern": "^[a-z0-9_-]+$",
                },
                "target_slug": {
                    "type": ["string", "null"],
                    "pattern": "^[a-z0-9_-]+$",
                },
                "rel": {"type": ["string", "null"]},
                "context": {"type": ["string", "null"]},
                "slug": {
                    "type": ["string", "null"],
                    "pattern": "^[a-z0-9_-]+$",
                },
                "revision_id": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        annotations=ToolAnnotations(destructiveHint=True),
    ),
    "wiki_media": Tool(
        name="wiki_media",
        description=(
            "Upload, list, or delete media (images) for a page. upload takes "
            "data_base64 (base64-encoded bytes; MCP transports text, not binary) "
            "plus optional filename and mime_type, and deduplicates identical "
            "bytes by sha256. list returns media metadata only (never the binary "
            "data). delete removes a media row by id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["upload", "list", "delete"]},
                "slug": {
                    "type": ["string", "null"],
                    "pattern": "^[a-z0-9_-]+$",
                },
                "data_base64": {
                    "type": ["string", "null"],
                    "description": (
                        "Base64-encoded media bytes. Required for upload."
                    ),
                },
                "filename": {"type": ["string", "null"]},
                "mime_type": {"type": ["string", "null"]},
                "media_id": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": "Required for delete.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
}

_MODE_TO_TOOLS = {
    "full": (
        "wiki_read",
        "wiki_search",
        "wiki_meta",
        "wiki_write",
        "wiki_admin",
        "wiki_media",
    ),
    "author": ("wiki_read", "wiki_search", "wiki_meta", "wiki_write"),
    "readonly": ("wiki_read", "wiki_search", "wiki_meta"),
}


def init_for_test(db_path: str | os.PathLike[str]) -> None:
    """Initialize the database and service path for an in-process test."""
    global _DB_PATH
    _DB_PATH = str(db_path)
    conn = db.init_db(_DB_PATH)
    conn.close()
    service.init_service(_DB_PATH)


def _validation(
    message: str,
    *,
    field: str,
    valid_values: list[Any],
) -> service.ValidationError:
    return service.ValidationError(
        message,
        details={"field": field, "valid_values": valid_values},
    )


def _action(args: dict[str, Any], valid_values: list[str]) -> str:
    value = args.get("action")
    if value not in valid_values:
        raise _validation(
            f"action must be one of {valid_values!r}; got {value!r}",
            field="action",
            valid_values=valid_values,
        )
    return value


def _required(args: dict[str, Any], field: str, action: str | None = None) -> Any:
    if field in args and args[field] is not None:
        return args[field]
    context = f" for action {action!r}" if action is not None else ""
    raise _validation(
        f"{field} is required{context}; got {args.get(field)!r}",
        field=field,
        valid_values=["non-null value"],
    )


def _slug(
    args: dict[str, Any],
    field: str = "slug",
    action: str | None = None,
) -> str:
    value = _required(args, field, action)
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9_-]+", value) is None:
        raise _validation(
            f"{field} {value!r} must match ^[a-z0-9_-]+$; got {value!r}",
            field=field,
            valid_values=["string matching ^[a-z0-9_-]+$"],
        )
    return value


def _writer_identity() -> tuple[str, str]:
    writer = os.environ.get("VESPERIKI_WRITER")
    client = os.environ.get("VESPERIKI_CLIENT")
    defaults: list[str] = []
    if not writer:
        writer = "default"
        defaults.append("VESPERIKI_WRITER='default'")
    if not client:
        client = "unknown"
        defaults.append("VESPERIKI_CLIENT='unknown'")
    if defaults:
        print(
            "warning: writer identity unset; using " + ", ".join(defaults),
            file=sys.stderr,
        )
    return writer, client


def _read(args: dict[str, Any]) -> dict:
    slug = _slug(args)
    section = args.get("section")
    if section is not None:
        # Section-scoped read: fetch the full page, split by ``## ``
        # headings, return the matching section with page context.
        # Mirrors update_section's not-found error so callers get the
        # same shape from both code paths.
        page = service.get_page(slug)
        section_list = sections.parse_sections(page["body"])
        match = next(
            (s for s in section_list if s["id"] == section),
            None,
        )
        valid = [s["id"] for s in section_list]
        if match is None:
            raise service.ValidationError(
                f"section {section!r} not found in page {slug!r}; "
                f"valid section ids: {valid}",
                details={"section_id": section, "valid_section_ids": valid},
            )
        if match["heading"] is None:
            # Intro: heading is None; content has no ``## `` line.
            content = match["content"]
            heading: str | None = None
        else:
            # Non-intro: prepend the heading line so the returned content
            # is a self-contained markdown slice (parser-friendly).
            content = "## " + match["heading"] + "\n" + match["content"]
            heading = match["heading"]
        return {
            "slug": slug,
            "title": page["title"],
            "section_id": match["id"],
            "heading": heading,
            "content": content,
            "valid_section_ids": valid,
        }
    return service.get_page(
        slug,
        expand_links=args.get("expand_links", False),
        include_revisions=args.get("include_revisions", False),
    )


def _search(args: dict[str, Any]) -> list[dict]:
    query = _required(args, "query")
    return service.search_pages(
        query=query,
        limit=args.get("limit", 10),
        include_body=args.get("include_body", False),
        tag=args.get("tag"),
        type=args.get("type"),
    )


def _meta(args: dict[str, Any]) -> dict:
    valid_actions = [
        "exists", "list", "tags", "recent", "stale", "stale_ranked",
        "links", "backlinks", "corrections",
    ]
    action = _action(args, valid_actions)
    if action in ("exists", "links", "backlinks"):
        _slug(args, action=action)
    params = {
        key: args[key]
        for key in (
            "slug", "tag", "type", "status", "limit", "cursor", "days",
            "include_tags", "exclude_tags",
        )
        if key in args and args[key] is not None
    }
    return service.get_meta(action=action, **params)


def _write(args: dict[str, Any]) -> dict:
    action = _action(args, ["create", "update", "delete", "revive", "update_section"])
    slug = _slug(args, action=action)
    changed_by, client = _writer_identity()

    if action == "create":
        # With source_markdown the title may come from frontmatter, so it
        # isn't strictly required at the transport level; service validates
        # the effective title after mapping.
        title = (
            args.get("title")
            if args.get("source_markdown")
            else _required(args, "title", action)
        )
        return service.create_page(
            slug=slug,
            title=title,
            body=_required(args, "body", action),
            tags=args.get("tags") if args.get("tags") is not None else (),
            type=args.get("type") if args.get("type") is not None else "entity",
            sources=args.get("sources") if args.get("sources") is not None else (),
            force=args.get("force", False),
            changed_by=changed_by,
            client=client,
            source_markdown=args.get("source_markdown", False),
        )
    if action == "update":
        return service.update_page(
            slug=slug,
            title=args.get("title"),
            body=args.get("body"),
            tags=args.get("tags"),
            type=args.get("type"),
            sources=args.get("sources"),
            confidence=args.get("confidence"),
            force=args.get("force", False),
            changed_by=changed_by,
            client=client,
            source_markdown=args.get("source_markdown", False),
        )
    if action == "update_section":
        return service.update_section(
            slug=slug,
            section_id=_required(args, "section_id", action),
            content=_required(args, "content", action),
            changed_by=changed_by,
            client=client,
        )
    if action == "revive":
        return service.revive_page(
            slug=slug,
            changed_by=changed_by,
            client=client,
        )
    return service.delete_page(
        slug=slug,
        purge=False,
        changed_by=changed_by,
        client=client,
    )


def _restore(slug: str, revision_id: int) -> dict:
    """Supply defaults to the locked restore API, which reads identity from env."""
    writer, client = _writer_identity()
    old_writer = os.environ.get("VESPERIKI_WRITER")
    old_client = os.environ.get("VESPERIKI_CLIENT")
    os.environ["VESPERIKI_WRITER"] = writer
    os.environ["VESPERIKI_CLIENT"] = client
    try:
        return service.admin_restore(slug=slug, revision_id=revision_id)
    finally:
        if old_writer is None:
            os.environ.pop("VESPERIKI_WRITER", None)
        else:
            os.environ["VESPERIKI_WRITER"] = old_writer
        if old_client is None:
            os.environ.pop("VESPERIKI_CLIENT", None)
        else:
            os.environ["VESPERIKI_CLIENT"] = old_client


def _admin(args: dict[str, Any]) -> dict:
    action = _action(args, ["link", "unlink", "restore"])
    if action == "link":
        return service.admin_link(
            source_slug=_slug(args, "source_slug", action),
            target_slug=_slug(args, "target_slug", action),
            rel=_required(args, "rel", action),
            context=args.get("context"),
        )
    if action == "unlink":
        return service.admin_unlink(
            source_slug=_slug(args, "source_slug", action),
            target_slug=_slug(args, "target_slug", action),
            rel=args.get("rel"),
        )
    return _restore(
        slug=_slug(args, action=action),
        revision_id=_required(args, "revision_id", action),
    )


def _media(args: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
    action = _action(args, ["upload", "list", "delete"])
    if action == "upload":
        slug = _slug(args, action=action)
        data_base64 = _required(args, "data_base64", action)
        if not isinstance(data_base64, str) or not data_base64:
            raise _validation(
                f"data_base64 must be a non-empty base64 string; got {data_base64!r}",
                field="data_base64",
                valid_values=["non-empty base64 string"],
            )
        try:
            data = base64.b64decode(data_base64, validate=False)
        except binascii.Error:
            raise _validation(
                f"data_base64 must be valid base64; got {data_base64!r}",
                field="data_base64",
                valid_values=["valid base64 string"],
            )
        if not data:
            raise _validation(
                f"data_base64 must decode to non-empty bytes; got {data_base64!r}",
                field="data_base64",
                valid_values=["base64 encoding of non-empty bytes"],
            )
        return service.upload_media(
            slug=slug,
            data=data,
            filename=args.get("filename"),
            mime_type=args.get("mime_type"),
        )
    if action == "list":
        return service.list_media(_slug(args, action=action))
    media_id = _required(args, "media_id", action)
    if not isinstance(media_id, int) or isinstance(media_id, bool) or media_id < 1:
        raise _validation(
            f"media_id must be a positive int; got {media_id!r}",
            field="media_id",
            valid_values=["positive integer"],
        )
    return service.delete_media(media_id)


def _text(payload: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload))]


def _error_result(payload: Any) -> CallToolResult:
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=json.dumps(payload))],
    )


def _log_call(name: str, args: dict[str, Any]) -> None:
    """Emit one INFO line per tool call, naming only the discriminative arg.

    Never logs body, data_base64, or any other large/blob field — those can
    be whole pages or image blobs.
    """
    extras: list[str] = [f"tool={name}"]
    if name in ("wiki_meta", "wiki_write", "wiki_admin", "wiki_media"):
        action = args.get("action")
        if action is not None:
            extras.append(f"action={action!r}")
    if name == "wiki_search":
        query = args.get("query")
        if query is not None:
            extras.append(f"query={query!r}")
    elif name == "wiki_read":
        slug = args.get("slug")
        if slug is not None:
            extras.append(f"slug={slug!r}")
    if name in ("wiki_meta", "wiki_write", "wiki_media"):
        slug = args.get("slug")
        if slug is not None:
            extras.append(f"slug={slug!r}")
    elif name == "wiki_admin":
        slug = args.get("slug")
        if slug is not None:
            extras.append(f"slug={slug!r}")
        source_slug = args.get("source_slug")
        if source_slug is not None:
            extras.append(f"source_slug={source_slug!r}")
    _LOGGER.info("call " + " ".join(extras))


def _log_service_error(name: str, exc: service.ServiceError) -> None:
    _LOGGER.warning(f"call tool={name} error code={exc.code} message={exc!s}")


def _log_internal_error(name: str, exc: Exception) -> None:
    _LOGGER.error(f"call tool={name} internal error", exc_info=True)


def _service_error(exc: service.ServiceError) -> dict[str, Any]:
    details = dict(exc.details)
    if isinstance(exc, service.ValidationError):
        message = str(exc)
        field = next(
            (
                candidate
                for candidate in ("slug", "title", "tag", "type", "limit", "action", "rel")
                if message.startswith(candidate) or f"{candidate} required" in message
            ),
            "input",
        )
        details.setdefault("field", field)
        if "valid values:" in message:
            raw_values = message.rsplit("valid values:", 1)[1].strip()
            try:
                valid_values = ast.literal_eval(raw_values)
            except (SyntaxError, ValueError):
                valid_values = [raw_values]
        else:
            valid_values = {
                "slug": ["string matching ^[a-z0-9_-]+$"],
                "title": ["non-empty string"],
                "tag": ["non-empty string"],
                "limit": ["positive integer"],
            }.get(field, ["valid value"])
        details.setdefault("valid_values", valid_values)
    return {"error": exc.code, "message": str(exc), "details": details}


def _validate_schema(name: str, args: dict[str, Any]) -> None:
    try:
        jsonschema.validate(instance=args, schema=_TOOLS[name].inputSchema)
    except jsonschema.ValidationError as exc:
        path = list(exc.absolute_path)
        field = str(path[0]) if path else "input"
        value: Any = exc.instance
        valid_values: list[Any]

        if exc.validator == "required":
            missing = next(
                key for key in exc.validator_value if key not in exc.instance
            )
            field = str(missing)
            value = None
            valid_values = ["required"]
        elif exc.validator == "additionalProperties":
            extras = sorted(set(exc.instance) - set(exc.schema["properties"]))
            field = extras[0]
            value = exc.instance[field]
            valid_values = sorted(exc.schema["properties"])
        elif exc.validator == "enum":
            valid_values = list(exc.validator_value)
        elif exc.validator == "pattern":
            valid_values = [f"string matching {exc.validator_value}"]
        elif exc.validator == "type":
            expected = exc.validator_value
            valid_values = list(expected) if isinstance(expected, list) else [expected]
        elif exc.validator == "minimum":
            valid_values = [f"number >= {exc.validator_value}"]
        elif exc.validator == "maximum":
            valid_values = [f"number <= {exc.validator_value}"]
        elif exc.validator == "minLength":
            valid_values = [f"string length >= {exc.validator_value}"]
        else:
            valid_values = [str(exc.validator_value)]

        raise _validation(
            f"{field} has invalid value {value!r}; valid values: {valid_values!r}",
            field=field,
            valid_values=valid_values,
        ) from exc


async def _handle(
    operation: Callable[[dict[str, Any]], Any],
    args: dict[str, Any],
    name: str,
) -> list[TextContent] | CallToolResult:
    try:
        return _text(operation(args))
    except service.ServiceError as exc:
        _log_service_error(name, exc)
        return _error_result(_service_error(exc))
    except Exception as exc:
        _log_internal_error(name, exc)
        return _error_result(
            {
                "error": "internal",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )


async def _handle_read(args: dict[str, Any]) -> list[TextContent]:
    return await _handle(_read, args, "wiki_read")


async def _handle_search(args: dict[str, Any]) -> list[TextContent]:
    return await _handle(_search, args, "wiki_search")


async def _handle_meta(args: dict[str, Any]) -> list[TextContent]:
    return await _handle(_meta, args, "wiki_meta")


async def _handle_write(args: dict[str, Any]) -> list[TextContent]:
    return await _handle(_write, args, "wiki_write")


async def _handle_admin(args: dict[str, Any]) -> list[TextContent]:
    return await _handle(_admin, args, "wiki_admin")


async def _handle_media(args: dict[str, Any]) -> list[TextContent]:
    return await _handle(_media, args, "wiki_media")


_HANDLERS = {
    "wiki_read": _handle_read,
    "wiki_search": _handle_search,
    "wiki_meta": _handle_meta,
    "wiki_write": _handle_write,
    "wiki_admin": _handle_admin,
    "wiki_media": _handle_media,
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    mode = os.environ.get("VESPERIKI_MODE", "full").lower()
    if mode not in _MODE_TO_TOOLS:
        print(
            f"warning: invalid VESPERIKI_MODE={mode!r}; using 'readonly'",
            file=sys.stderr,
        )
        mode = "readonly"
    return [_TOOLS[name] for name in _MODE_TO_TOOLS[mode]]


@server.call_tool(validate_input=False)
async def call_tool(name: str, args: dict[str, Any] | None) -> list[TextContent] | CallToolResult:
    """Dispatch a tool call through the same handlers used by in-process tests."""
    args = args or {}
    _log_call(name, args)
    try:
        handler = _HANDLERS[name]
        # SDK validation returns a protocol error. Validate here instead so every
        # failure follows Vesperiki's one-TextContent JSON error convention.
        _validate_schema(name, args)
    except service.ServiceError as exc:
        _log_service_error(name, exc)
        return _error_result(_service_error(exc))
    except Exception as exc:
        _log_internal_error(name, exc)
        return _error_result(
            {
                "error": "internal",
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
    return await handler(args)


async def main() -> None:
    mode = os.environ.get("VESPERIKI_MODE", "full").lower()
    if mode not in _MODE_TO_TOOLS:
        mode = "readonly"
    tool_count = len(_MODE_TO_TOOLS[mode])
    init_for_test(os.environ.get("VESPERIKI_DB_PATH", "./vesperiki.db"))
    _LOGGER.info(
        f"startup mode={mode} db={_DB_PATH} tools={tool_count}"
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
