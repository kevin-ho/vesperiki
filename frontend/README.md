# vesperiki frontend

A small, read-only React 19 + TypeScript SPA for browsing the [vesperiki]
knowledge garden. Built with Vite 6, TanStack Router, TanStack Query, and
Tailwind v4 with a neobrutalist theme.

## Quick start

```bash
npm install
npm run dev        # start the dev server (proxies /api + /healthz to the backend)
```

Open <http://localhost:5173>. The dev server forwards `/api/*` and `/healthz`
to the FastAPI backend on `localhost:7420` (see `vite.config.ts`).

## Scripts

| Command            | What it does                                          |
| ------------------ | ----------------------------------------------------- |
| `npm run dev`      | Vite dev server with HMR and the `/api` proxy.        |
| `npm run typecheck`| `tsc --noEmit` against the strict `tsconfig.json`.     |
| `npm run build`    | Production build into `dist/`.                        |
| `npm test`         | Vitest single-run (jsdom + Testing Library).          |

## Environment

- `VITE_API_BASE` *(optional)* — base URL prepended to every API call.
  Defaults to `""` so requests go to the same origin. In dev, Vite's proxy
  routes `/api/*` and `/healthz` to the backend on `localhost:7420`, which
  is why no value is normally needed. Set this when deploying the built
  bundle to a host that exposes the API at a different origin.

## Styling: `layout.css` vs `theme.css`

The frontend splits CSS into two files for two different audiences:

- `src/styles/layout.css` — **structural** styles. Tailwind v4 import, the
  `--color-*` → `--color-*` indirection in `@theme inline`, the
  `.markdown` typography rules, and `.focus-ring`. Touch only when changing
  structure or Tailwind wiring.
- `src/styles/theme.css` — **themeable** CSS custom properties. Every
  colour, the brutalist drop-shadow, and the three font stacks live here,
  with a `:root[data-theme='dark']` override for the dark theme. The
  designer iterates on this file; the rest of the app reads
  the variables and stays untouched.

The toggle in the header (`☼/☾ theme`) flips `data-theme` on `<html>` and
persists the choice in `localStorage`.

## API notes

The backend exposes the read-only surface this SPA consumes:

- `GET /api/pages?limit=N&cursor=&tag=&type=&status=` — paginated page list.
  **The service orders by `id ASC`**, so the home page shows the *oldest*
  pages first (the spec was "recent by insertion order", not newest-first).
- `GET /api/pages/{slug}` — single page (handles 301 redirects).
- `GET /api/search?q=…&tag=&type=&include_body=&limit=` — full-text search.
- `GET /api/tags`, `GET /api/orphans`, `GET /api/graph`, `GET /api/sync`.

The backend does **not** expose a dedicated `/api/recent` or
`/api/backlinks/{slug}` endpoint. The home page therefore calls
`/api/pages?limit=20` (`getRecent`), and the backlinks panel computes
incoming links client-side from the full `/api/graph` payload
(`getBacklinks`).

## Wikilink rendering

Page bodies use `[[slug]]` and `[[slug|label]]` wikilinks. The rewriter
lives in `src/lib/wikilinks.ts` (`wikilinksToMarkdown`) and produces
`[label](/p/{slug})` links — the slug is `encodeURIComponent`-escaped so
spaces and other reserved characters survive. See `wikilinks.test.ts` for
the exact contract.

## PWA / service worker

`public/sw.js` ships with the app and is registered from `src/main.tsx`.
Its strategy:

- **API GETs** — *network-first*, falling back to the cached response when
  the network is down. Only the read-only `/api/*` endpoints are cached.
- **Navigation requests** — *cache-first* with an `/index.html` fallback
  so deep links work offline.
- **Same-origin asset GETs** — *stale-while-revalidate* so updated hashes
  roll in on the next request.
- `POST`/`PUT`/`PATCH`/`DELETE` and any cross-origin request bypass the
  service worker entirely.

## Tests

`npm test` runs the Vitest suite under jsdom. Coverage lives next to the
code it exercises:

- `src/lib/wikilinks.test.ts` — wikilink → markdown contract.
- `src/lib/api.test.ts` — fetch URL building, slug encoding, empty-param
  pruning, and the `ApiError` shape on a 500 (fetch is stubbed per test).
- `src/components/PageList.test.tsx` — renders two fake pages inside a
  minimal in-memory TanStack Router and asserts the title, slug path,
  type chip, tag chips, and the relative-time badge; plus the empty-array
  fallback and `EmptyState` rendering.

The relative-time badge is tested against a frozen system time
(`vi.setSystemTime`) so assertions are deterministic.