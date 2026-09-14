/**
 * WasmDataSource — the offline-first backend for the Vesperiki SPA.
 *
 * Wraps a SQLite-WASM database (mirrored from the server via
 * `/api/sync`) and exposes the same query shape the route components
 * use. The class deliberately stays small: schema bootstrap,
 * apply-delta, query helpers, sync driver. Nothing else.
 *
 * The DB "engine" handle is abstracted behind the {@link DbHandle}
 * interface so the same schema/apply logic can be tested in vitest
 * against the package's Node build (which works under jsdom with
 * in-memory DBs), or pointed at an OPFS-backed database in the
 * browser. The default lazy loader picks the right runtime.
 *
 * What this module deliberately does NOT do:
 *
 *   - It does not call the network. Sync is fetched by the caller
 *     (see `wasm-sync.ts`) and handed in as a SyncDelta. Keeping
 *     `fetch()` out of this file keeps it easy to test and audit.
 *   - It does not surface a UI. The footer indicator is the
 *     consumer's job.
 *   - It does not depend on React or TanStack Query.
 */

import type {
  Graph,
  GraphEdge,
  GraphNode,
  Page,
  PagesResponse,
  SearchHit,
  SyncDelta,
  Tag,
} from './types';

/**
 * The narrow subset of the sqlite3-wasm `Database` API the data
 * source needs. Keeps the production code portable to mocks and to
 * package version bumps.
 *
 * Every method returns a Promise: the production browser path goes
 * through `sqlite3Worker1Promiser`, whose message API is inherently
 * asynchronous (every query is a postMessage round-trip), so the
 * Node build that drives the same `DbHandle` shape must await too
 * for the contract to stay consistent. The Node build wraps the
 * synchronous OO1 API in `Promise.resolve` so the call sites don't
 * have to branch on the runtime.
 */
export interface DbHandle {
  /** Run one or more semicolon-separated SQL statements; return this. */
  exec(sql: string): Promise<unknown>;
  /**
   * Run a SELECT and yield each row as a plain object keyed by
   * column name. The implementation handles prepared statements
   * internally; callers do not deal with `step()` / `finalize()`.
   */
  select<T = Record<string, unknown>>(sql: string, bind?: unknown[]): Promise<T[]>;
  /** Run a parameterised write (INSERT / UPDATE / DELETE). */
  run(sql: string, bind?: unknown[]): Promise<void>;
  /** Wrap a sequence of writes in BEGIN/COMMIT (skips if already in tx). */
  transaction<T>(fn: () => Promise<T> | T): Promise<T>;
  /** Permanently close the underlying handle. */
  close(): Promise<void> | void;
}

/**
 * Search implementation chosen at boot. The browser build almost
 * always supports FTS5; the Node build used by vitest also supports
 * it. We still fall back to LIKE in case a future stripped build
 * removes it — search keeps working either way, just slower.
 */
export type SearchStrategy = 'fts5' | 'like';

/**
 * Lightweight status payload for the footer indicator. Mirrors the
 * public `SyncStatus` type but is computed without React state.
 */
export interface WasmStatus {
  ready: boolean;
  lastSyncAt: string | null;
  cursor: number;
  searchStrategy: SearchStrategy;
  /** True when OPFS is the persistence backend (false → in-memory). */
  persisted: boolean;
}

const PAGE_COLUMNS = `
  slug TEXT PRIMARY KEY,
  id INTEGER,
  title TEXT NOT NULL,
  type TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  tags_json TEXT NOT NULL DEFAULT '[]',
  sources_json TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL DEFAULT '',
  created_at TEXT,
  confidence REAL,
  verified_at TEXT,
  seq INTEGER NOT NULL DEFAULT 0,
  redirected_from TEXT
`.trim();

/** Whole-table schema. Idempotent — safe to run on every init. */
const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS pages (
  ${PAGE_COLUMNS}
);
CREATE TABLE IF NOT EXISTS tags (
  name TEXT PRIMARY KEY,
  count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS page_tags (
  page_slug TEXT NOT NULL,
  tag_name TEXT NOT NULL,
  PRIMARY KEY (page_slug, tag_name)
);
CREATE INDEX IF NOT EXISTS idx_page_tags_tag ON page_tags(tag_name);
CREATE INDEX IF NOT EXISTS idx_page_tags_slug ON page_tags(page_slug);
CREATE TABLE IF NOT EXISTS links (
  source_slug TEXT NOT NULL,
  target_slug TEXT NOT NULL,
  rel TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source_slug);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_slug);
CREATE TABLE IF NOT EXISTS tombstones (
  slug TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  deleted_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS aliases (
  alias TEXT PRIMARY KEY,
  page_slug TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_aliases_slug ON aliases(page_slug);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
`;

const SCHEMA_FTS_SQL = `
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
  slug, title, body,
  tokenize = 'porter unicode61'
);
`;

/**
 * Helper: serialize anything (typically an array of strings) into a
 * JSON string for storage. `null` / `undefined` round-trip to `[]`,
 * matching what the server's `_page_to_dict` does.
 */
function jsonString(value: unknown): string {
  if (value === null || value === undefined) return '[]';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return '[]';
  }
}

/** Inverse of {@link jsonString}. */
function parseJson<T>(raw: string | null | undefined, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

/** Wrap a string so ILIKE-style wildcards in user input don't break the query. */
function likeEscape(input: string): string {
  return input.replace(/[\\%_]/g, '\\$&');
}

const SNIPPET_RADIUS = 80;

/** Build a small snippet around the first match of `query` in `body`. */
function buildSnippet(body: string, query: string): string {
  if (!body) return '';
  if (!query) return body.slice(0, SNIPPET_RADIUS * 2);
  const lower = body.toLowerCase();
  const needle = query.toLowerCase();
  const idx = lower.indexOf(needle);
  if (idx < 0) return body.slice(0, SNIPPET_RADIUS * 2);
  const start = Math.max(0, idx - SNIPPET_RADIUS);
  const end = Math.min(body.length, idx + needle.length + SNIPPET_RADIUS);
  const prefix = start > 0 ? '…' : '';
  const suffix = end < body.length ? '…' : '';
  return `${prefix}${body.slice(start, end)}${suffix}`;
}

/**
 * Concrete WASM-backed data source. Constructed with a ready-made
 * {@link DbHandle}; the loader (`loadWasmDataSource`) handles the
 * platform dance (OPFS on browsers, in-memory under Node).
 */
export class WasmDataSource {
  private readonly db: DbHandle;
  private strategy: SearchStrategy;

  /**
   * @param db The active database handle.
   * @param strategy Optional forced search strategy. When omitted,
   *   `init()` probes the build at runtime: it attempts
   *   `CREATE VIRTUAL TABLE pages_fts USING fts5(...)` and falls
   *   back to LIKE search when the statement throws (e.g. a WASM
   *   build compiled without FTS5). Pass `'like'` explicitly to
   *   skip the probe entirely.
   */
  constructor(db: DbHandle, strategy?: SearchStrategy) {
    this.db = db;
    this.strategy = strategy ?? 'fts5';
  }

  /** Boot the schema and the meta cursor. Idempotent. */
  async init(): Promise<void> {
    await this.db.exec(SCHEMA_SQL);
    if (this.strategy === 'fts5') {
      try {
        await this.db.exec(SCHEMA_FTS_SQL);
      } catch {
        // FTS5 is not compiled into this build. Search still works
        // via the LIKE fallback (slower, but correct). The flag is
        // recorded so the footer can show the honest strategy.
        this.strategy = 'like';
      }
    }
    // Seed the meta table with the canonical keys so reads never
    // need to handle NULL when the DB is brand new.
    await this.db.run(
      "INSERT OR IGNORE INTO meta(key, value) VALUES('cursor', '0')",
    );
    await this.db.run(
      "INSERT OR IGNORE INTO meta(key, value) VALUES('last_sync_at', '')",
    );
    // Local schema version — bump when a shipped bug leaves existing
    // browsers with corrupt local data that only a full re-sync heals.
    // v2 (2026-09-05): sync deltas once shipped pages WITHOUT tag names
    // (the client defaulted the missing field to []), so every synced
    // copy has empty tags_json/page_tags. Resetting the cursor forces
    // one full /api/sync, which now carries tags per page.
    const LOCAL_SCHEMA_VERSION = '2';
    if ((await this.getMeta('schema_version')) !== LOCAL_SCHEMA_VERSION) {
      await this.db.run("UPDATE meta SET value = ? WHERE key = ?", [
        '0',
        'cursor',
      ]);
      await this.db.run(
        `INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '2')`,
      );
    }
  }

  searchStrategy(): SearchStrategy {
    return this.strategy;
  }

  /**
   * Resolve a single page by slug. Returns `null` when the row is
   * missing (do not confuse with the tombstone case — a tombstoned
   * slug is deleted from the table entirely, so `null` covers both
   * "never existed" and "deleted").
   */
  async getPage(slug: string): Promise<Page | null> {
    const pages = await this.db.select<PageRow>('SELECT * FROM pages WHERE slug = ?', [slug]);
    const row = pages[0];
    if (!row) return null;
    // The hidden-by-status rule (matching the server's `filterDeprecatedHits`):
    // clients never surface status='deprecated' rows even if they
    // happen to be present locally.
    if (row.status === 'deprecated') return null;
    return rowToPage(row);
  }

  /**
   * List pages with optional filters. Mirrors the URL params the
   * `/api/pages` endpoint expects so callers can route either backend
   * through the same signature.
   */
  async list(params: {
    tag?: string;
    type?: string;
    status?: string;
    limit?: number;
  }): Promise<PagesResponse> {
    const limit = Math.max(1, Math.min(200, params.limit ?? 50));
    const where: string[] = [];
    const bind: unknown[] = [];
    if (params.status) {
      where.push('p.status = ?');
      bind.push(params.status);
    } else {
      where.push("p.status != 'deprecated'");
    }
    if (params.type) {
      where.push('p.type = ?');
      bind.push(params.type);
    }
    let join = '';
    if (params.tag) {
      join = 'JOIN page_tags pt ON pt.page_slug = p.slug';
      where.push('pt.tag_name = ?');
      bind.push(params.tag);
    }
    const sql = `
      SELECT p.* FROM pages p ${join}
      WHERE ${where.join(' AND ')}
      ORDER BY p.updated_at DESC
      LIMIT ?
    `;
    bind.push(limit);
    const rows = await this.db.select<PageRow>(sql, bind);
    return { pages: rows.map(rowToPage), next_cursor: null };
  }

  /** Shortcut for the home page: 20 most recently updated active pages. */
  async getRecent(limit = 20): Promise<PagesResponse> {
    return this.list({ status: 'active', limit });
  }

  /**
   * Search by free text. The query is matched against title (with
   * weight) and body. Returns ranked hits with a short snippet.
   */
  async search(params: {
    q: string;
    tag?: string;
    type?: string;
    include_body?: boolean;
    limit?: number;
  }): Promise<SearchHit[]> {
    const limit = Math.max(1, Math.min(200, params.limit ?? 50));
    const q = (params.q ?? '').trim();
    if (!q) return [];

    let hits: SearchHit[];
    if (this.strategy === 'fts5') {
      hits = await this.searchFts(q, limit);
    } else {
      hits = await this.searchLike(q, limit);
    }

    // Server-side, tag/type filters apply AFTER relevance ranking.
    // We mirror that here: filter, then truncate.
    let filtered = hits;
    if (params.tag) {
      const allowedSlugs = new Set(
        (
          await this.db.select<{ page_slug: string }>(
            'SELECT page_slug FROM page_tags WHERE tag_name = ?',
            [params.tag],
          )
        ).map((r) => r.page_slug),
      );
      filtered = filtered.filter((h) => allowedSlugs.has(h.slug));
    }
    if (params.type) {
      filtered = filtered.filter((h) => h.type === params.type);
    }
    const deprecatedStatuses = new Map<string, string | null>();
    for (const h of filtered) {
      if (!deprecatedStatuses.has(h.slug)) {
        deprecatedStatuses.set(h.slug, await this.getPageStatus(h.slug));
      }
    }
    filtered = filtered.filter(
      (h) => deprecatedStatuses.get(h.slug) !== 'deprecated',
    );
    return filtered.slice(0, limit);
  }

  async getTags(): Promise<Tag[]> {
    return (
      await this.db.select<TagRow>(
        // Count pages per tag (de-duplicated against page_tags rows
        // that already exist) so the value stays correct after
        // multiple sync cycles refresh page_tags.
        `SELECT t.name AS name, COUNT(pt.page_slug) AS count
         FROM tags t LEFT JOIN page_tags pt ON pt.tag_name = t.name
         GROUP BY t.name ORDER BY count DESC, t.name ASC`,
      )
    ).map((r) => ({ name: r.name, count: Number(r.count) }));
  }

  /**
   * Resolve which pages link to `slug` by walking the cached link
   * graph. Returns minimal `{slug,title,type}` rows so callers can
   * render the same "Referenced by" sidebar the server route uses.
   */
  async getBacklinks(slug: string): Promise<Array<Pick<Page, 'slug' | 'title' | 'type'>>> {
    return (
      await this.db.select<{
        slug: string;
        title: string;
        type: string;
      }>(
        `SELECT p.slug, p.title, p.type
         FROM links l JOIN pages p ON p.slug = l.source_slug
         WHERE l.target_slug = ?
         ORDER BY p.title ASC`,
        [slug],
      )
    ).map((r) => ({ slug: r.slug, title: r.title, type: r.type }));
  }

  /**
   * Build the full page/link graph for the graph view, mirroring
   * `/api/graph`: every non-deprecated page becomes a node and every
   * link between two non-deprecated pages becomes an edge. The local
   * `links` table stores slugs, so edges are resolved to numeric page
   * ids via the node list; edges whose source or target slug has no
   * node (dangling rows, or pages that never got a server id) are
   * dropped.
   */
  async getGraph(): Promise<Graph> {
    const nodeRows = await this.db.select<{
      id: number | null;
      slug: string;
      title: string;
      type: string;
    }>("SELECT id, slug, title, type FROM pages WHERE status != 'deprecated'");
    const nodes: GraphNode[] = [];
    const slugToId = new Map<string, number>();
    for (const row of nodeRows) {
      // GraphNode requires a numeric id; skip pages the server never
      // assigned one (their edges are dropped below via the map).
      if (row.id === null || row.id === undefined) continue;
      slugToId.set(row.slug, row.id);
      nodes.push({ id: row.id, slug: row.slug, title: row.title, type: row.type });
    }
    const edgeRows = await this.db.select<{
      source_slug: string;
      target_slug: string;
      rel: string;
      origin: string;
    }>(
      `SELECT l.source_slug, l.target_slug, l.rel, l.origin
       FROM links l
       JOIN pages ps ON ps.slug = l.source_slug
       JOIN pages pt ON pt.slug = l.target_slug
       WHERE ps.status != 'deprecated'
         AND pt.status != 'deprecated'`,
    );
    const edges: GraphEdge[] = [];
    for (const row of edgeRows) {
      const source = slugToId.get(row.source_slug);
      const target = slugToId.get(row.target_slug);
      if (source === undefined || target === undefined) continue;
      edges.push({ source, target, rel: row.rel, origin: row.origin });
    }
    return { nodes, edges };
  }

  /**
   * Apply a sync delta to the local DB. The delta is delivered by the
   * sync fetcher; the data source doesn't fetch itself.
   *
   * The apply runs in a single transaction so a partial failure
   * leaves the DB exactly as it was (no torn pages).
   */
  async applyDelta(delta: SyncDelta): Promise<void> {
    await this.db.transaction(async () => {
      await this.replaceTags(delta.tags);
      // Pages and tombstones: server returns them ordered by `seq`
      // per table (ascending), but they are independent so a page row
      // and a tombstone for the same slug can appear in the same
      // delta. The plan: merge both streams by seq and apply in
      // order — the higher seq wins for any given slug.
      const merges = mergeBySlugAndSeq(delta.pages, delta.tombstones);
      const updatedSlugs: string[] = [];
      const deletedSlugs: string[] = [];
      for (const event of merges) {
        if (event.kind === 'page') {
          await this.upsertPage(event.page);
          await this.upsertPageTagsFromPage(event.page);
          updatedSlugs.push(event.page.slug);
        } else {
          await this.applyTombstone(event.tombstone);
          deletedSlugs.push(event.tombstone.slug);
        }
      }
      await this.replaceLinks(delta.links, delta.pages, delta.tombstones);
      await this.replaceAliases(delta.aliases);
      // FTS stays in sync with the tables.
      if (this.strategy === 'fts5') {
        for (const slug of updatedSlugs) {
          await this.ftsUpsert(slug);
        }
        for (const slug of deletedSlugs) {
          await this.ftsDelete(slug);
        }
      }
      // Cursor and timestamps are written LAST so a half-applied
      // delta isn't recorded as a successful sync.
      await this.db.run('UPDATE meta SET value = ? WHERE key = ?', [
        String(delta.cursor),
        'cursor',
      ]);
      await this.db.run('UPDATE meta SET value = ? WHERE key = ?', [
        new Date().toISOString(),
        'last_sync_at',
      ]);
    });
  }

  /** Snapshot the current status for the footer indicator. */
  async status(): Promise<WasmStatus> {
    const cursor = await this.getMeta('cursor');
    const lastSyncAt = await this.getMeta('last_sync_at');
    return {
      ready: true,
      cursor: cursor ? Number(cursor) : 0,
      lastSyncAt: lastSyncAt && lastSyncAt !== '' ? lastSyncAt : null,
      searchStrategy: this.strategy,
      // We only know "persisted" if the loader told us; default to
      // false. Callers should treat false here as "didn't check" —
      // the footer indicator uses the navigator API to verify.
      persisted: false,
    };
  }

  /** Close the underlying SQLite handle. Idempotent. */
  close(): void {
    try {
      void this.db.close();
    } catch {
      /* already closed — non-fatal */
    }
  }

  // ----- internal helpers -----

  private async getMeta(key: string): Promise<string | null> {
    const rows = await this.db.select<{ value: string }>(
      'SELECT value FROM meta WHERE key = ?',
      [key],
    );
    return rows[0]?.value ?? null;
  }

  private async getPageStatus(slug: string): Promise<string | null> {
    const rows = await this.db.select<{ status: string }>(
      'SELECT status FROM pages WHERE slug = ?',
      [slug],
    );
    return rows[0]?.status ?? null;
  }

  private async upsertPage(page: Page): Promise<void> {
    // Missing id/slug/title/type signals a malformed delta row;
    // skip rather than write a NULL PK that would break the next
    // upsert.
    if (!page.slug || !page.title || !page.type) return;
    await this.db.run(
      `INSERT INTO pages (
         slug, id, title, type, body, status, tags_json, sources_json,
         updated_at, created_at, confidence, verified_at, seq, redirected_from
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(slug) DO UPDATE SET
         id = excluded.id,
         title = excluded.title,
         type = excluded.type,
         body = excluded.body,
         status = excluded.status,
         tags_json = excluded.tags_json,
         sources_json = excluded.sources_json,
         updated_at = excluded.updated_at,
         created_at = excluded.created_at,
         confidence = excluded.confidence,
         verified_at = excluded.verified_at,
         seq = excluded.seq,
         redirected_from = excluded.redirected_from
      `,
      [
        page.slug,
        page.id ?? null,
        page.title,
        page.type,
        page.body ?? '',
        page.status ?? 'active',
        jsonString(page.tags ?? []),
        jsonString(page.sources ?? []),
        page.updated_at ?? '',
        page.created_at ?? null,
        page.confidence ?? null,
        (page as { verified_at?: string | null }).verified_at ?? null,
        page.seq ?? 0,
        page.redirected_from ?? null,
      ],
    );
  }

  private async upsertPageTagsFromPage(page: Page): Promise<void> {
    // page_tags rows are owned by the page row — when the page is
    // re-written we drop and recreate. Cheap; tag counts are
    // re-aggregated from page_tags at query time.
    if (!page.slug) return;
    await this.db.run('DELETE FROM page_tags WHERE page_slug = ?', [page.slug]);
    for (const tag of page.tags ?? []) {
      await this.db.run(
        'INSERT OR IGNORE INTO page_tags(page_slug, tag_name) VALUES (?, ?)',
        [page.slug, tag],
      );
    }
  }

  private async applyTombstone(tombstone: { slug: string; seq: number; deleted_at: string }): Promise<void> {
    await this.db.run('DELETE FROM pages WHERE slug = ?', [tombstone.slug]);
    await this.db.run('DELETE FROM page_tags WHERE page_slug = ?', [tombstone.slug]);
    await this.db.run(
      `INSERT INTO tombstones(slug, seq, deleted_at)
       VALUES(?, ?, ?)
       ON CONFLICT(slug) DO UPDATE SET
         seq = excluded.seq,
         deleted_at = excluded.deleted_at`,
      [tombstone.slug, tombstone.seq, tombstone.deleted_at ?? ''],
    );
  }

  private async replaceTags(tags: Tag[]): Promise<void> {
    await this.db.run('DELETE FROM tags');
    for (const tag of tags) {
      await this.db.run(
        'INSERT INTO tags(name, count) VALUES(?, ?)',
        // Sync deltas may omit `count` (JSON field absent → undefined);
        // the column is NOT NULL, so default to 0 to keep applyDelta
        // from throwing and rolling the whole delta back.
        [tag.name, tag.count ?? 0],
      );
    }
  }

  /**
   * The server returns `links` (and `page_tags`, `aliases`) as
   * whole-table refreshes keyed by server-internal row IDs. Those
   * IDs are not stable across syncs — we resolve them to slugs
   * using the page rows that came in alongside the link rows (or
   * the previously-cached page table) and the alias table.
   */
  private async replaceLinks(
    rawLinks: unknown[],
    pages: Page[],
    tombstones: { slug: string }[],
  ): Promise<void> {
    await this.db.run('DELETE FROM links');
    if (!Array.isArray(rawLinks) || rawLinks.length === 0) return;

    const idToSlug = await this.buildIdToSlugMap(pages, tombstones);
    for (const raw of rawLinks) {
      const link = raw as {
        source_id?: number;
        target_id?: number;
        rel?: string;
        origin?: string;
      };
      const source = idToSlug.get(link.source_id ?? -1);
      const target = idToSlug.get(link.target_id ?? -1);
      if (!source || !target) continue;
      await this.db.run(
        'INSERT INTO links(source_slug, target_slug, rel, origin) VALUES(?, ?, ?, ?)',
        [source, target, link.rel ?? 'link', link.origin ?? ''],
      );
    }
  }

  private async replaceAliases(rawAliases: unknown[]): Promise<void> {
    await this.db.run('DELETE FROM aliases');
    if (!Array.isArray(rawAliases)) return;
    for (const raw of rawAliases) {
      const alias = raw as { alias?: string; page_id?: number };
      if (!alias.alias || !alias.page_id) continue;
      // Resolve through the cached page table; if the page is not
      // present we still write the alias (rare and harmless — the
      // redirected lookup just falls back to the network).
      const match = await this.db.select<{ slug: string }>(
        'SELECT slug FROM pages WHERE id = ? LIMIT 1',
        [alias.page_id],
      );
      const target = match[0]?.slug ?? `__id:${alias.page_id}`;
      await this.db.run(
        'INSERT INTO aliases(alias, page_slug) VALUES(?, ?)',
        [alias.alias, target],
      );
    }
  }

  /**
   * Build a `(server-side) page_id → slug` map for the in-flight
   * delta. Incoming pages are processed first; if the same page_id
   * appears in `links` but wasn't in this delta's pages list, we
   * look up the cached row (the server only refreshes references
   * whole-table when something changed, so older slugs are still
   * available locally).
   */
  private async buildIdToSlugMap(
    pages: Page[],
    tombstones: { slug: string }[],
  ): Promise<Map<number, string>> {
    const map = new Map<number, string>();
    const tombstoned = new Set(tombstones.map((t) => t.slug));
    for (const page of pages) {
      if (page.id != null) map.set(page.id, page.slug);
    }
    const cached = await this.db.select<{ id: number; slug: string }>(
      'SELECT id, slug FROM pages WHERE id IS NOT NULL',
    );
    for (const row of cached) {
      if (!map.has(row.id) && !tombstoned.has(row.slug)) {
        map.set(row.id, row.slug);
      }
    }
    return map;
  }

  private async ftsUpsert(slug: string): Promise<void> {
    const row = await this.getPage(slug);
    if (!row) {
      await this.ftsDelete(slug);
      return;
    }
    // delete-then-insert: FTS5 content-less tables do not support
    // UPDATE, so the upsert pattern is to remove and re-insert.
    await this.ftsDelete(slug);
    await this.db.run(
      'INSERT INTO pages_fts(slug, title, body) VALUES(?, ?, ?)',
      [row.slug, row.title, row.body ?? ''],
    );
  }

  private async ftsDelete(slug: string): Promise<void> {
    await this.db.run('DELETE FROM pages_fts WHERE slug = ?', [slug]);
  }

  private async searchFts(q: string, limit: number): Promise<SearchHit[]> {
    // FTS5 query syntax: wrap user input in quotes and escape
    // internal double quotes so an attacker can't inject operators.
    const safe = `"${q.replace(/"/g, '""')}"`;
    const rows = await this.db.select<{
      slug: string;
      title: string;
      type: string;
      body: string;
      snippet: string;
      rank: number;
    }>(
      `SELECT p.slug, p.title, p.type, p.body,
              snippet(pages_fts, 2, '<<', '>>', '…', 16) AS snippet,
              rank
         FROM pages_fts JOIN pages p ON p.slug = pages_fts.slug
        WHERE pages_fts MATCH ?
          AND p.status != 'deprecated'
        ORDER BY rank
        LIMIT ?`,
      [safe, limit * 4],
    );
    return rows.map((row) => ({
      slug: row.slug,
      title: row.title,
      type: row.type,
      snippet: row.snippet || buildSnippet(row.body ?? '', q),
      score: -Number(row.rank),
    }));
  }

  private async searchLike(q: string, limit: number): Promise<SearchHit[]> {
    const needle = `%${likeEscape(q)}%`;
    const rows = await this.db.select<PageRow>(
      `SELECT * FROM pages
        WHERE (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')
          AND status != 'deprecated'
        ORDER BY updated_at DESC
        LIMIT ?`,
      [needle, needle, limit * 4],
    );
    return rows.map((row) => ({
      slug: row.slug,
      title: row.title,
      type: row.type,
      snippet: buildSnippet(row.body, q),
      score: scoreByTitle(row.title, q),
    }));
  }
}

// ---------------------------------------------------------------------------
// Row types — SQLite returns objects whose keys are column names;
// these interfaces pin that shape for the typed `select()` helper.
// ---------------------------------------------------------------------------

interface PageRow {
  slug: string;
  id: number | null;
  title: string;
  type: string;
  body: string;
  status: string;
  tags_json: string;
  sources_json: string;
  updated_at: string;
  created_at: string | null;
  confidence: number | null;
  verified_at: string | null;
  seq: number;
  redirected_from: string | null;
}

interface TagRow {
  name: string;
  count: number;
}

/**
 * Hydrate a {@link Page} from a row. Tags/sources JSON columns are
 * parsed back to arrays here, in one place.
 */
function rowToPage(row: PageRow): Page {
  return {
    slug: row.slug,
    id: row.id ?? undefined,
    title: row.title,
    type: row.type,
    tags: parseJson<string[]>(row.tags_json, []),
    sources: parseJson<string[]>(row.sources_json, []),
    body: row.body,
    status: row.status,
    updated_at: row.updated_at,
    created_at: row.created_at ?? undefined,
    confidence: row.confidence ?? undefined,
    verified_at: row.verified_at ?? undefined,
    seq: row.seq,
    redirected_from: row.redirected_from ?? undefined,
  };
}

/**
 * Score title matches higher than body matches in the LIKE fallback so
 * the rank order stays roughly aligned with the FTS5 `rank` value.
 */
function scoreByTitle(title: string, query: string): number {
  const t = title.toLowerCase();
  const q = query.toLowerCase();
  if (t === q) return 100;
  if (t.startsWith(q)) return 50;
  if (t.includes(q)) return 10;
  return 1;
}

/**
 * Apply merged page / tombstone events for the same slug with a
 * single rule: whichever entry has the higher `seq` wins. When they
 * have equal `seq`, the tombstone is treated as authoritative
 * (deletions are explicit; an "edit then immediately delete" race
 * can otherwise keep the page alive forever).
 */
interface PageEvent {
  kind: 'page';
  page: Page;
  seq: number;
}
interface TombstoneEvent {
  kind: 'tombstone';
  tombstone: { slug: string; seq: number; deleted_at: string };
  seq: number;
}
type SyncEvent = PageEvent | TombstoneEvent;

function mergeBySlugAndSeq(
  pages: Page[],
  tombstones: { slug: string; seq: number; deleted_at: string }[],
): SyncEvent[] {
  const out: SyncEvent[] = [];
  for (const page of pages) {
    if (!page.slug) continue;
    out.push({ kind: 'page', page, seq: page.seq ?? 0 });
  }
  for (const tombstone of tombstones) {
    if (!tombstone.slug) continue;
    out.push({ kind: 'tombstone', tombstone, seq: tombstone.seq ?? 0 });
  }
  // Stable sort by seq preserves the server-side order within the
  // same seq so tests and cache ordering stay deterministic.
  out.sort((a, b) => a.seq - b.seq);

  // Collapse same-slug events to a single winner. We iterate in
  // ascending seq order and keep the latest; on equal seq the
  // tombstone wins.
  const winners = new Map<string, SyncEvent>();
  const order: string[] = [];
  for (const event of out) {
    const slug = event.kind === 'page' ? event.page.slug : event.tombstone.slug;
    const previous = winners.get(slug);
    if (!previous) {
      winners.set(slug, event);
      order.push(slug);
      continue;
    }
    const prevSeq = previous.seq;
    if (event.seq < prevSeq) continue;
    if (event.seq === prevSeq && previous.kind === 'page' && event.kind === 'tombstone') {
      winners.set(slug, event);
      continue;
    }
    if (event.seq > prevSeq) {
      winners.set(slug, event);
    }
  }
  return order.map((slug) => winners.get(slug)!).filter(Boolean);
}
