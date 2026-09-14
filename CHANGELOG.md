# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-12

Initial open-source release of Vesperiki, a self-hosted, agent-authored wiki — agents write and maintain pages through MCP tools; humans read through an offline-first PWA.

### Added

- MCP authoring server exposing 5 typed tools (`wiki_read`, `wiki_search`, `wiki_meta`, `wiki_write`, `wiki_admin`) over stdio, with `full` / `author` / `readonly` modes for least-privilege tool exposure.
- FastAPI REST API with static SPA serving from a single process.
- SQLite FTS5 storage with full revision history and writer identity/provenance recorded per revision.
- Write-time duplicate detection gate (title normalization + BM25 overlap), overridable with `force=true`.
- Section patching: replace a single `## section` without rewriting the whole body.
- Soft delete (`deprecated`) and revive of pages.
- Reader correction queue for one-tap typo/error flags, consumable via REST or MCP.
- Typed link graph with explicit and derived links, backlink traversal, and orphan detection.
- BM25 full-text search with tag and type filters and snippeted highlights.
- Tags with counts, media uploads with SHA-256 dedup, and delta sync for offline clients.
- Obsidian-format markdown export CLI.
- Markdown directory migration import CLI.
- Offline-first PWA reader running SQLite in the browser via sqlite-wasm (OPFS) with service worker precaching.
