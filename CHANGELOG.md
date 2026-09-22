# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-09-21

### Added

- Optional semantic search layer: env-defined OpenAI-compatible embeddings endpoint, section-based chunking, sqlite-vec kNN over a `chunk_vec` table, `search_semantic` and `search_hybrid` (reciprocal-rank fusion) with page-level aggregation.
- `mode=` parameter (`keyword` / `semantic` / `hybrid`) on the REST `/api/search` endpoint and the MCP `wiki_search` tool. Default `keyword` is byte-identical to previous behavior; unset embed config degrades gracefully (useful results + honest markers, never errors).
- `vesperiki reembed` CLI for (re)embedding the corpus; queue-based async embedding with opportunistic drain at API startup.
- Code coverage measurement in CI with Codecov reporting.

### Fixed

- Backend CI now installs the package itself (`pip install -e .`) instead of a hand-typed dependency list that had silently drifted behind `pyproject.toml` when `sqlite-vec` landed — 10 contract tests ran red on GitHub CI while green locally.



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
