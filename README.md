# Vesperiki

[![CI](https://github.com/kevin-ho/vesperiki/actions/workflows/ci.yml/badge.svg)](https://github.com/kevin-ho/vesperiki/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/kevin-ho/vesperiki/graph/badge.svg)](https://codecov.io/gh/kevin-ho/vesperiki)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**The wiki that AI maintains. The wiki that humans enjoy reading.**

You figure something out once. Vesperiki keeps it figured out — written,
cross-linked, and kept current by your agent, readable by you anywhere,
even offline.

> **/vesperiki** Where are the best photograph spots in Venice?
>
> **/vesperiki** Map my home network infrastructure

It builds on [Karpathy's LLM-wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f):
don't re-derive knowledge from raw sources on every query — compile it
once into persistent, interlinked pages and keep it current. Projects
like [obsidian-wiki](https://github.com/Ar9av/obsidian-wiki) apply that
pattern to markdown (a compromise format) files that both humans and agents read and edit.

**But do humans really want to maintain a wiki?**

Vesperiki has a simpler philosophy: **agents write, humans read.** 

- **Agents write and query it** through typed tools over SQLite —
  structured, transactional, full-text search. No markdown parsing, no
  token tax. They author asynchronously: while you're away, the wiki
  keeps growing.
- **Humans read it** as a clean, offline-first PWA — real pages, search,
  works with no server and no wifi signal.

And because agents are the authors, maintenance is built in: readers
flag mistakes straight back to the agent as a work queue, and pages
carry confidence scores and staleness dates so neglected content
surfaces itself.

Self-hosted, one Python process, one SQLite file. Harness-agnostic: any
MCP-capable agent plugs in — Claude Desktop, OpenAI Codex, and friends —
plus a read-only REST API for everything else.

## Why

- **Your wiki, your file.** Everything lives in one SQLite database you can back up, copy, and inspect with standard tools. No SaaS, no accounts, no lock-in.
- **Nothing is ever lost.** Every edit records who made it and why. Roll back to any revision; deleting a page hides it rather than destroying it.
- **Readers keep it honest.** A one-tap flag on any page hands typos and mistakes back to the agent as a work queue; pages carry confidence scores and staleness dates, so neglected content surfaces itself instead of rotting quietly.
- **Easy in, easy out.** Import an existing markdown folder (frontmatter and wikilinks included) with one command; export the whole wiki to Obsidian-format markdown anytime.
- **Small and auditable.** One Python process, one schema, 545 tests, and a WCAG 2.2 AA reader UI.

Full tool and API details are in [MCP integration](#mcp-integration) below.

## Quick start

### Backend

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
python -m vesperiki.api              # :7420, serves API + built SPA if dist/ exists
```

With `uv`: `uv venv && uv pip install -e . && python -m vesperiki.api`.

### Frontend

```bash
cd frontend
pnpm install
pnpm dev                             # vite on :5173, proxies /api and /healthz to :7420
```

Browse to <http://localhost:5173>. For production: `pnpm build` emits
`frontend/dist/` with the PWA shell, sqlite-wasm worker files, and the
`sw-assets.js` precache manifest. FastAPI serves the built SPA itself,
so production is just `python -m vesperiki.api` on <http://localhost:7420>.

If the API is hosted at a different origin from the built SPA, set
`VITE_API_BASE` at build time so the SPA prepends it to every call.

## Architecture

A single Python process serves the REST API and the built SPA. The MCP
server is a separate stdio process talking to the same SQLite file. The
browser optionally talks to the API in dev and to an offline
sqlite-wasm database once installed.

```
+----------------+   stdio   +---------------+        +---------------+
| MCP-capable    |---------->| vesperiki/mcp |        | Browser (PWA) |
| authoring      |           | 5 tools       |        | React 19 SPA  |
| agent          |           | full|author|ro|        | + sqlite-wasm |
+----------------+           +-------+-------+        | + service W.  |
                                    |                 +-------+-------+
                                    v                         | HTTP
                            +-------+-------+                 v
                            | SQLite (FTS5) |<------- vesperiki/api
                            | pages, revs,  |        | FastAPI + SPA
                            | links, tags,  |        | :7420 (dist/)
                            | media, sync   |        +---------------+
                            +---------------+
```

| Component       | Path                          | Role                                       |
| --------------- | ----------------------------- | ------------------------------------------ |
| Backend         | `vesperiki/service.py`        | Domain logic, validation, dedup, link graph|
| Database        | `vesperiki/db.py`             | Schema, FTS5, `change_seq`, FK, WAL        |
| REST API        | `vesperiki/api.py`            | FastAPI transport + static SPA serving     |
| MCP server      | `vesperiki/mcp.py`            | stdio JSON-RPC, 5 tools, modes             |
| Export CLI      | `vesperiki/export.py`         | Dump DB to Obsidian-format markdown        |
| Migration CLI   | `vesperiki/migrate.py`        | Import a markdown directory into the DB    |
| Static serving  | `vesperiki/static_serving.py` | SPA fallback + PWA / sqlite-wasm routes    |
| Reader SPA      | `frontend/`                   | React 19 + Vite 6 + TanStack Router/Query  |
| Service worker  | `frontend/public/sw.js`       | Net-first API, cache-first nav, SWR assets |
| Offline DB      | `@sqlite.org/sqlite-wasm` 3.53| OPFS VFS, full SQL reader offline          |

## Tech stack

| Layer                | Technology                                                 |
| -------------------- | ---------------------------------------------------------- |
| Runtime              | Python 3.11+                                               |
| HTTP framework       | FastAPI + uvicorn                                          |
| Storage              | SQLite (WAL, FK, FTS5, `change_seq` cursor)                |
| MCP transport        | MCP Python SDK (`mcp>=1.10,<2`), stdio JSON-RPC            |
| Frontend framework   | React 19 + TypeScript                                      |
| Build tool           | Vite 6                                                     |
| Routing / data       | TanStack Router, TanStack Query                            |
| Components / styles  | React Aria Components, Tailwind v4                         |
| Markdown             | react-markdown + remark-gfm                               |
| Offline DB           | @sqlite.org/sqlite-wasm 3.53.0 (OPFS via `opfs-wl`)        |
| Tests                | pytest (backend), vitest + Testing Library + jsdom (FE)    |
| Frontend package mgr | pnpm                                                       |

## Semantic search (optional)

Semantic search is opt-in. Configure any OpenAI-compatible embeddings endpoint;
when unset, keyword search and all writes behave exactly as before.

```bash
# Ollama-style local endpoint
VESPERIKI_EMBED_URL=http://localhost:11434/v1 VESPERIKI_EMBED_MODEL=embedding-model
# llama.cpp started with its embeddings endpoint enabled
VESPERIKI_EMBED_URL=http://localhost:8080/v1 VESPERIKI_EMBED_MODEL=embedding-model
# Hosted OpenAI-compatible API
VESPERIKI_EMBED_URL=https://embedding.example/v1 VESPERIKI_EMBED_MODEL=embedding-model VESPERIKI_EMBED_API_KEY=...
```

Use `python -m vesperiki.reembed --status` to inspect coverage and
`python -m vesperiki.reembed --full` after changing model or dimension. The
client batches requests, never logs the API key, and writes queue state before
any provider call. Keep chunks sized for the selected provider's input limit.

MCP clients pass the same values through `mcpServers.vesperiki.env` alongside
`VESPERIKI_DB_PATH` and the writer identity.

## Configuration

| Variable                  | Default              | Purpose                                              |
| ------------------------- | -------------------- | ---------------------------------------------------- |
| `VESPERIKI_DB_PATH`       | `./vesperiki.db`     | SQLite database file path.                           |
| `VESPERIKI_WRITER`        | `default`            | Writer identity recorded on every revision.          |
| `VESPERIKI_CLIENT`        | `unknown`            | Client identity recorded on every revision.          |
| `VESPERIKI_MODE`          | `full`               | MCP tool exposure: `full`, `author`, or `readonly`.  |
| `VESPERIKI_HOST`          | `127.0.0.1`          | uvicorn bind address.                                |
| `VESPERIKI_PORT`          | `7420`               | uvicorn bind port.                                   |
| `VESPERIKI_MIGRATE_SOURCE`| (unset)              | Default `--source` for `python -m vesperiki.migrate`.|
| `VITE_API_BASE`           | (unset, same origin) | Build-time API base URL for the SPA.                 |

Mode tool surface:

| Mode       | Tools                                                        |
| ---------- | ------------------------------------------------------------ |
| `full`     | `wiki_read`, `wiki_search`, `wiki_meta`, `wiki_write`, `wiki_admin` |
| `author`   | `wiki_read`, `wiki_search`, `wiki_meta`, `wiki_write`         |
| `readonly` | `wiki_read`, `wiki_search`, `wiki_meta`                       |

## MCP integration

Wire the MCP server into your MCP-capable agent (Claude Desktop, generic
JSON clients):

```json
{
  "mcpServers": {
    "vesperiki": {
      "command": "python",
      "args": ["-m", "vesperiki.mcp"],
      "cwd": "/path/to/vesperiki",
      "env": {
        "VESPERIKI_DB_PATH": "/path/to/vesperiki/vesperiki.db",
        "VESPERIKI_WRITER": "agent-name",
        "VESPERIKI_CLIENT": "agent-runtime",
        "VESPERIKI_MODE": "full",
        "VESPERIKI_EMBED_URL": "http://localhost:11434/v1",
        "VESPERIKI_EMBED_MODEL": "embedding-model"
      }
    }
  }
}
```

| Tool         | Purpose                                                                                 |
| ------------ | --------------------------------------------------------------------------------------- |
| `wiki_read`  | Read a page by slug; `expand_links`, `include_revisions`, or `section` (return a single section by slugified heading or `intro` instead of the full body). |
| `wiki_search`| BM25 search with snippets; tag/type filters and `include_body`.                        |
| `wiki_meta`  | `exists`, `list`, `tags`, `recent`, `stale`, `stale_ranked`, `links`, `backlinks`, `corrections`. `list` and `stale_ranked` accept `include_tags` (AND) / `exclude_tags` (NOT-ANY) tag-combination filters plus `limit` and `type`; `stale` takes `days`; `recent` and `list` take `limit`; `list` takes `status` and `cursor`. |
| `wiki_write` | `create`, `update`, `delete`, `revive`, `update_section`. `create` is an upsert (existing slug updates instead of erroring) and runs duplicate detection, overridden by `force=true`. With `source_markdown=true` it parses a leading YAML frontmatter block (title/tags/type) and stores only the clean body; the duplicate error message includes the force hint. `revive` flips a soft-deleted page back to active (no-op if already active). `update_section` (section id = slugified heading or `intro`) replaces one section's body — the heading is preserved automatically, do NOT include it. `update` with no changed fields is a silent no-op (no seq bump, no revision row, no `updated_at` refresh). |
| `wiki_admin` | `link` / `unlink` explicit edges, `restore` revisions.                                  |

Inputs are JSON-schema validated; bad input returns a structured error
with the offending field and valid values.

## Deployment

`python -m vesperiki.api` serves both the API and the built SPA from one
process.

```ini
[Unit]
Description=Vesperiki wiki
After=network.target

[Service]
WorkingDirectory=/opt/vesperiki
ExecStart=/opt/vesperiki/.venv/bin/python -m vesperiki.api
Environment=VESPERIKI_HOST=127.0.0.1
Environment=VESPERIKI_PORT=7421
Environment=VESPERIKI_DB_PATH=/opt/vesperiki/vesperiki.db
# Optional semantic search:
# Environment=VESPERIKI_EMBED_URL=http://localhost:11434/v1
# Environment=VESPERIKI_EMBED_MODEL=embedding-model
# Environment=VESPERIKI_EMBED_API_KEY=
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Expose on your tailnet with Tailscale Serve (TLS + tailnet-only auth):

```bash
tailscale serve --bg --https=7420 --set-path=/ http://127.0.0.1:7421
```

Browse to `https://<host>.<tailnet>.ts.net:7420` from any tailnet device.
No separate static host is required. API-only mode kicks in if
`frontend/dist/` is absent (CI wheel, test environment).

## Development

```bash
pytest                                # backend, 320 tests
cd frontend && pnpm test              # frontend, 138 tests (vitest + jsdom)
cd frontend && pnpm typecheck        # tsc --noEmit, strict tsconfig
cd frontend && pnpm build             # emits dist/ with PWA + sqlite-wasm
```

CSS is split by audience: `frontend/src/styles/layout.css` carries the
structural styles, Tailwind v4 wiring, markdown typography, and focus
rings. `frontend/src/styles/theme.css` carries the themable custom
properties (colors, drop-shadow, font stacks) plus the dark-mode
override. The light/dark theme toggle persists in `localStorage`.

## Contributing

Issues and pull requests are welcome. Backend public functions live in
`vesperiki/service.py` and return plain dicts; tests live in `tests/`
under pytest. Frontend code lives next to the code it exercises
(`src/lib/*.test.ts`, `src/components/*.test.tsx`) under vitest, jsdom,
and Testing Library. Run both suites before opening a PR: `pytest` at
the repo root and `pnpm test` in `frontend/`.

## License

MIT — see [LICENSE](LICENSE).
