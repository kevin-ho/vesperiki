import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WasmDataSource } from './wasm-data-source';
import type { DbHandle, SearchStrategy } from './wasm-data-source';
import type { Page, SyncDelta, Tag } from './types';

/**
 * In-memory DbHandle backed by a Map keyed by table → column → value.
 *
 * We avoid pulling in the real sqlite-wasm package here because
 * (a) the loader is exercised in other tests; (b) the goal of this
 * suite is to lock in the schema/apply/query logic, and a tiny
 * SQLite-shaped stub gives cleaner failure messages than WASM
 * errors.
 *
 * Just enough surface for WasmDataSource:
 *   - SELECT with positional binds returning row objects
 *   - INSERT/UPDATE/DELETE with binds
 *   - Transactions (BEGIN/COMMIT/ROLLBACK via a flag)
 *   - The CREATE VIRTUAL TABLE pages_fts call (only used in fts5 mode)
 *
 * The stub keeps things simple but still SQL-flavoured: every
 * statement is matched against a small set of regexes that pull
 * the bound values out of the same array WasmDataSource passes.
 */
class StubDb implements DbHandle {
  /** table → row array (objects keyed by column). */
  readonly tables = new Map<string, Map<string, Record<string, unknown>>>();
  /** Last sequence number per rowId — used for INSERT OR REPLACE. */
  private inTxn = 0;
  /** Names of virtual tables created via `CREATE VIRTUAL TABLE`. */
  readonly virtualTables = new Set<string>();
  /**
   * Knob the suite can flip to inject a runtime failure on a
   * specific SQL keyword. Helpful for testing transaction
   * rollback semantics without monkey-patching the stub.
   */
  failureOnExec: string | null = null;

  constructor(opts: { failOn?: string } = {}) {
    this.failureOnExec = opts.failOn ?? null;
  }

  async exec(sql: string): Promise<unknown> {
    // SCHEMA_SQL is a multi-statement blob. The real sqlite-wasm
    // `exec` walks the statements; the stub mirrors that by
    // splitting on `;` and dispatching each statement through the
    // same logic. Tests that pass a single statement still work
    // because the resulting array has a single entry.
    const trimmed = sql.trim();
    const statements = trimmed
      .split(/;\s*(?:$|\n)/)
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    for (const stmt of statements) {
      this.execOne(stmt);
    }
    return this;
  }

  private execOne(trimmed: string): unknown {
    // CREATE TABLE — match `CREATE TABLE[ IF NOT EXISTS]? name (cols)`
    const createMatch = trimmed.match(
      /^CREATE TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)\s*\(([\s\S]*)\);?$/i,
    );
    if (createMatch) {
      const [, name] = createMatch;
      if (!this.tables.has(name)) this.tables.set(name, new Map());
      return this;
    }
    // CREATE INDEX / CREATE VIRTUAL TABLE — no-op for the stub,
    // unless the failure-on keyword was specifically configured.
    if (
      /^CREATE\s+(VIRTUAL\s+)?(UNIQUE\s+)?INDEX/i.test(trimmed) ||
      /^CREATE\s+VIRTUAL\s+TABLE/i.test(trimmed)
    ) {
      if (this.failureOnExec && trimmed.includes(this.failureOnExec)) {
        throw new Error(`stub: forced failure on ${this.failureOnExec}`);
      }
      // Track the FTS table so its existence can be checked by
      // tests that want to know whether the init took the FTS path.
      const vMatch = trimmed.match(/CREATE\s+VIRTUAL\s+TABLE\s+(\w+)/i);
      if (vMatch) this.virtualTables.add(vMatch[1]);
      return this;
    }
    // BEGIN/COMMIT/ROLLBACK
    if (/^BEGIN/i.test(trimmed)) {
      this.inTxn += 1;
      return this;
    }
    if (/^COMMIT/i.test(trimmed)) {
      this.inTxn = Math.max(0, this.inTxn - 1);
      return this;
    }
    if (/^ROLLBACK/i.test(trimmed)) {
      this.inTxn = Math.max(0, this.inTxn - 1);
      return this;
    }
    if (this.failureOnExec && trimmed.includes(this.failureOnExec)) {
      throw new Error(`stub: forced failure on ${this.failureOnExec}`);
    }
    throw new Error(`stub exec: unrecognized statement ${trimmed}`);
  }

  async select<T>(sql: string, bind: unknown[] = []): Promise<T[]> {
    const trimmed = sql.trim();
    // SELECT * FROM pages WHERE slug = ?
    const singleMatch = trimmed.match(
      /^SELECT\s+\*\s+FROM\s+(\w+)\s+WHERE\s+(\w+)\s*=\s*\?;?$/i,
    );
    if (singleMatch) {
      const [, table, col] = singleMatch;
      const expected = bind[0];
      const rows = this.tableRows(table);
      return rows
        .filter((row) => row[col] === expected)
        .map((row) => row as unknown as T);
    }
    // SELECT * FROM pages WHERE (title LIKE ? ESCAPE '\' OR body LIKE ? ESCAPE '\') AND status != 'deprecated' ORDER BY updated_at DESC LIMIT ?
    const likeSearch = /^SELECT\s+\*\s+FROM\s+pages\s+WHERE\s+\(title\s+LIKE\s+\?\s+ESCAPE\s+'.?'\s+OR\s+body\s+LIKE\s+\?\s+ESCAPE\s+'.?'\)\s+AND\s+status\s+!=\s+'deprecated'\s+ORDER\s+BY\s+updated_at\s+DESC\s+LIMIT\s+\?;?$/i;
    if (likeSearch.test(trimmed)) {
      const needle = String(bind[0] ?? '');
      const stripped = needle.replace(/^%|%$/g, '');
      const rows = this.tableRows('pages');
      return rows
        .filter(
          (row) =>
            row.status !== 'deprecated' &&
            (String(row.title ?? '').includes(stripped) ||
              String(row.body ?? '').includes(stripped)),
        )
        .sort((a, b) =>
          String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')),
        )
        .slice(0, Number(bind[2] ?? 200)) as unknown as T[];
    }
    // SELECT p.* FROM pages p JOIN page_tags pt ON pt.page_slug = p.slug WHERE ... LIMIT ?
    const joinList =
      /^SELECT\s+p\.\*\s+FROM\s+pages\s+p(?:\s+JOIN\s+page_tags\s+pt\s+ON\s+pt\.page_slug\s*=\s*p\.slug)?\s+WHERE\s+(.+?)\s+ORDER\s+BY\s+p\.updated_at\s+DESC\s+LIMIT\s+\?;?$/i;
    if (joinList.test(trimmed)) {
      // Extract binding positions from the WHERE clause: count `?`s in order, but for tests the bind array is the same one the source passes.
      // We support two shapes: status-only, type-only, status+type, status+tag, status+type+tag.
      const limit = Number(bind[bind.length - 1]);
      const candidates = this.tableRows('pages');
      let pos = 0;
      const wantStatus = !/p\.status\s*!=\s*'deprecated'/i.test(trimmed)
        ? null
        : (() => {
            const row: { v: string | null } = { v: null };
            if (/p\.status\s*=\s*\?/.test(trimmed)) {
              row.v = String(bind[pos++] ?? '');
            }
            return row.v;
          })();
      const wantType = /p\.type\s*=\s*\?/i.test(trimmed)
        ? String(bind[pos++] ?? '')
        : null;
      const wantTag = /pt\.tag_name\s*=\s*\?/i.test(trimmed)
        ? String(bind[pos++] ?? '')
        : null;
      const matchingPageSlugs = wantTag
        ? new Set(
            this
              .tableRows('page_tags')
              .filter((r) => r.tag_name === wantTag)
              .map((r) => r.page_slug),
          )
        : null;
      return candidates
        .filter((row) => {
          if (wantStatus !== null && row.status !== wantStatus) return false;
          if (wantStatus === null && row.status === 'deprecated') return false;
          if (wantType && row.type !== wantType) return false;
          if (matchingPageSlugs && !matchingPageSlugs.has(String(row.slug)))
            return false;
          return true;
        })
        .sort((a, b) =>
          String(b.updated_at ?? '').localeCompare(String(a.updated_at ?? '')),
        )
        .slice(0, limit) as unknown as T[];
    }
    // SELECT p.slug, p.title, p.type, p.body, snippet(...) AS snippet, rank FROM pages_fts JOIN pages p ON p.slug = pages_fts.slug WHERE pages_fts MATCH ? AND p.status != 'deprecated' ORDER BY rank LIMIT ?
    const ftsSearch = /^SELECT\s+p\.slug,\s*p\.title,\s*p\.type,\s*p\.body,[\s\S]+?FROM\s+pages_fts\s+JOIN\s+pages\s+p\s+ON\s+p\.slug\s*=\s*pages_fts\.slug\s+WHERE\s+pages_fts\s+MATCH\s+\?\s+AND\s+p\.status\s+!=\s+'deprecated'\s+ORDER\s+BY\s+rank\s+LIMIT\s+\?;?$/i;
    if (ftsSearch.test(trimmed)) {
      const matchTerm = String(bind[0] ?? '').replace(/^"|"$/g, '');
      const limit = Number(bind[1] ?? 50);
      const rows = this.tableRows('pages');
      return rows
        .filter(
          (row) =>
            row.status !== 'deprecated' &&
            (String(row.title ?? '').toLowerCase().includes(matchTerm.toLowerCase()) ||
              String(row.body ?? '').toLowerCase().includes(matchTerm.toLowerCase())),
        )
        .slice(0, limit)
        .map((row) => {
          const body = String(row.body ?? '');
          return {
            slug: row.slug,
            title: row.title,
            type: row.type,
            body: row.body,
            snippet: `<<${body.slice(0, 16)}>>`,
            rank: -1,
          };
        }) as unknown as T[];
    }
    // SELECT value FROM meta WHERE key = ?
    const metaMatch = trimmed.match(
      /^SELECT\s+(\*|\w+(?:,\s*\w+)*)\s+FROM\s+(\w+)\s+WHERE\s+(\w+)\s*=\s*\?;?$/i,
    );
    if (metaMatch) {
      const [, , table, col] = metaMatch;
      const expected = bind[0];
      const rows = this.tableRows(table);
      return rows
        .filter((row) => row[col] === expected)
        .map((row) => row as unknown as T);
    }
    // SELECT id, slug FROM pages WHERE id IS NOT NULL
    if (/^SELECT\s+id,\s*slug\s+FROM\s+pages\s+WHERE\s+id\s+IS\s+NOT\s+NULL/i.test(trimmed)) {
      const rows = this.tableRows('pages');
      return rows.filter((r) => r.id !== null) as unknown as T[];
    }
    if (/^SELECT\s+slug\s+FROM\s+pages\s+WHERE\s+id\s*=\s*\?\s*LIMIT\s+1;?$/i.test(trimmed)) {
      const expected = bind[0];
      const rows = this
        .tableRows('pages')
        .filter((r) => r.id === expected)
        .slice(0, 1);
      return rows as unknown as T[];
    }
    if (/^SELECT\s+page_slug\s+FROM\s+page_tags\s+WHERE\s+tag_name\s*=\s*\?;?$/i.test(trimmed)) {
      const expected = bind[0];
      return this
        .tableRows('page_tags')
        .filter((r) => r.tag_name === expected)
        .map((r) => ({ page_slug: r.page_slug })) as unknown as T[];
    }
    // SELECT id, slug, title, type FROM pages WHERE status != 'deprecated'  (graph nodes)
    if (/^SELECT\s+id,\s*slug,\s*title,\s*type\s+FROM\s+pages\s+WHERE\s+status\s+!=\s+'deprecated';?$/i.test(trimmed)) {
      return this
        .tableRows('pages')
        .filter((row) => row.status !== 'deprecated')
        .map((row) => ({
          id: row.id,
          slug: row.slug,
          title: row.title,
          type: row.type,
        })) as unknown as T[];
    }
    // SELECT l.source_slug, l.target_slug, l.rel, l.origin FROM links l
    // JOIN pages ps ON ps.slug = l.source_slug JOIN pages pt ON pt.slug = l.target_slug
    // WHERE ps.status != 'deprecated' AND pt.status != 'deprecated'  (graph edges)
    if (/^SELECT\s+l\.source_slug,\s*l\.target_slug,\s*l\.rel,\s*l\.origin\s+FROM\s+links\s+l\s+JOIN\s+pages\s+ps\s+ON\s+ps\.slug\s*=\s*l\.source_slug\s+JOIN\s+pages\s+pt\s+ON\s+pt\.slug\s*=\s*l\.target_slug\s+WHERE\s+ps\.status\s+!=\s+'deprecated'\s+AND\s+pt\.status\s+!=\s+'deprecated';?$/i.test(trimmed)) {
      const pages = this.tableRows('pages');
      const statusBySlug = new Map(pages.map((p) => [p.slug, p.status]));
      return this
        .tableRows('links')
        .filter((l) => {
          const sourceStatus = statusBySlug.get(l.source_slug);
          const targetStatus = statusBySlug.get(l.target_slug);
          return (
            sourceStatus !== undefined &&
            targetStatus !== undefined &&
            sourceStatus !== 'deprecated' &&
            targetStatus !== 'deprecated'
          );
        })
        .map((l) => ({
          source_slug: l.source_slug,
          target_slug: l.target_slug,
          rel: l.rel,
          origin: l.origin,
        })) as unknown as T[];
    }
    // SELECT p.slug, p.title, p.type FROM links l JOIN pages p ON p.slug = l.source_slug WHERE l.target_slug = ?
    const linkMatch = trimmed.match(
      /^SELECT\s+p\.slug,\s*p\.title,\s*p\.type\s+FROM\s+links\s+l\s+JOIN\s+pages\s+p\s+ON\s+p\.slug\s*=\s*l\.source_slug\s+WHERE\s+l\.target_slug\s*=\s*\?\s*ORDER\s+BY\s+p\.title\s+ASC;?$/i,
    );
    if (linkMatch) {
      const expected = bind[0];
      const linkRows = this.tableRows('links').filter((r) => r.target_slug === expected);
      const pages = this.tableRows('pages');
      return linkRows
        .map((l) => pages.find((p) => p.slug === l.source_slug))
        .filter((p): p is Record<string, unknown> => Boolean(p))
        .map((p) => ({ slug: p.slug, title: p.title, type: p.type }))
        .sort((a, b) => String(a.title).localeCompare(String(b.title))) as unknown as T[];
    }
    // Tag count query
    if (/^SELECT\s+t\.name\s+AS\s+name,\s*COUNT\(pt\.page_slug\)\s+AS\s+count\s+FROM\s+tags/i.test(trimmed)) {
      const tags = this.tableRows('tags');
      const out: Array<{ name: string; count: number }> = [];
      for (const tag of tags) {
        const name = String(tag.name);
        const count = this
          .tableRows('page_tags')
          .filter((r) => r.tag_name === name).length;
        out.push({ name, count });
      }
      out.sort((a, b) => (b.count - a.count) || a.name.localeCompare(b.name));
      return out as unknown as T[];
    }
    throw new Error(`stub select: unrecognized query ${trimmed}`);
  }

  async run(sql: string, bind: unknown[] = []): Promise<void> {
    const trimmed = sql.trim();
    // The same failure-injection knob works for `run()` too — a
    // tests can request a forced failure on a particular column
    // substring and the stub throws mid-transaction.
    if (this.failureOnExec && trimmed.includes(this.failureOnExec)) {
      throw new Error(`stub: forced failure on ${this.failureOnExec}`);
    }
    // INSERT OR IGNORE INTO meta(key, value) VALUES('cursor', '0')  (literal seeds)
    const metaSeed = trimmed.match(
      /^INSERT\s+OR\s+IGNORE\s+INTO\s+meta\((\w+),\s*(\w+)\)\s+VALUES\s*\('(\w+)',\s*'([^']*)'\);?$/i,
    );
    if (metaSeed) {
      const [, , , key, value] = metaSeed;
      this.ensureTable('meta');
      // Idempotent — only seed if the key isn't yet present.
      if (!this.tables.get('meta')!.has(`k:${key}`)) {
        this.tables.get('meta')!.set(`k:${key}`, { key, value });
      }
      return;
    }
    // DELETE FROM <table> WHERE slug = ?
    const delBySlug = trimmed.match(
      /^DELETE\s+FROM\s+(\w+)\s+WHERE\s+slug\s*=\s*\?;?$/i,
    );
    if (delBySlug) {
      const [, table] = delBySlug;
      this.ensureTable(table);
      const tab = this.tables.get(table)!;
      for (const [key, row] of tab) if (row.slug === bind[0]) tab.delete(key);
      return;
    }
    // DELETE FROM <table> WHERE page_slug = ?
    const delByPageSlug = trimmed.match(
      /^DELETE\s+FROM\s+(\w+)\s+WHERE\s+page_slug\s*=\s*\?;?$/i,
    );
    if (delByPageSlug) {
      const [, table] = delByPageSlug;
      this.ensureTable(table);
      const tab = this.tables.get(table)!;
      for (const [key, row] of tab) if (row.page_slug === bind[0]) tab.delete(key);
      return;
    }
    // DELETE FROM <table>  (whole table)
    const delAll = trimmed.match(/^DELETE\s+FROM\s+(\w+);?$/i);
    if (delAll) {
      const [, table] = delAll;
      if (table === 'pages') {
        // pages table has slug as PK; deleting everything wipes the table
        this.tables.set('pages', new Map());
      } else {
        this.tables.set(table, new Map());
      }
      return;
    }
    // UPDATE meta SET value = ? WHERE key = ?
    const updateMeta = trimmed.match(
      /^UPDATE\s+meta\s+SET\s+value\s*=\s*\?\s+WHERE\s+key\s*=\s*\?;?$/i,
    );
    if (updateMeta) {
      this.ensureTable('meta');
      const value = String(bind[0]);
      const key = String(bind[1]);
      this.tables.get('meta')!.set(`k:${key}`, { key, value });
      return;
    }
    // DELETE FROM pages_fts WHERE slug = ?
    const ftsDel = trimmed.match(
      /^DELETE\s+FROM\s+pages_fts\s+WHERE\s+slug\s*=\s*\?;?$/i,
    );
    if (ftsDel) return;
    // INSERT INTO pages_fts(slug, title, body) VALUES(?, ?, ?)
    const ftsIns = trimmed.match(
      /^INSERT\s+INTO\s+pages_fts\(slug,\s*title,\s*body\)\s+VALUES\s*\(\?,\s*\?,\s*\?\);?$/i,
    );
    if (ftsIns) return;
    // INSERT OR IGNORE INTO page_tags(page_slug, tag_name) VALUES (?, ?)
    const insertPageTag = trimmed.match(
      /^INSERT\s+OR\s+IGNORE\s+INTO\s+page_tags\(page_slug,\s*tag_name\)\s+VALUES\s*\(\?,\s*\?\);?$/i,
    );
    if (insertPageTag) {
      this.ensureTable('page_tags');
      this.tables.get('page_tags')!.set(
        `${bind[0]}|${bind[1]}`,
        { page_slug: bind[0], tag_name: bind[1] },
      );
      return;
    }
    // INSERT INTO tags(name, count) VALUES(?, ?)
    const insertTag = trimmed.match(
      /^INSERT\s+INTO\s+tags\(name,\s*count\)\s+VALUES\s*\(\?,\s*\?\);?$/i,
    );
    if (insertTag) {
      this.ensureTable('tags');
      this.tables.get('tags')!.set(`n:${bind[0]}`, {
        name: bind[0],
        count: bind[1],
      });
      return;
    }
    // INSERT INTO aliases(alias, page_slug) VALUES(?, ?)
    const insertAlias = trimmed.match(
      /^INSERT\s+INTO\s+aliases\(alias,\s*page_slug\)\s+VALUES\s*\(\?,\s*\?\);?$/i,
    );
    if (insertAlias) {
      this.ensureTable('aliases');
      this.tables.get('aliases')!.set(`a:${bind[0]}`, {
        alias: bind[0],
        page_slug: bind[1],
      });
      return;
    }
    // INSERT INTO links(source_slug, target_slug, rel, origin) VALUES(?, ?, ?, ?)
    const insertLink = trimmed.match(
      /^INSERT\s+INTO\s+links\(source_slug,\s*target_slug,\s*rel,\s*origin\)\s+VALUES\s*\(\?,\s*\?,\s*\?,\s*\?\);?$/i,
    );
    if (insertLink) {
      this.ensureTable('links');
      this.tables.get('links')!.set(`${bind[0]}|${bind[1]}`, {
        source_slug: bind[0],
        target_slug: bind[1],
        rel: bind[2],
        origin: bind[3],
      });
      return;
    }
    // INSERT INTO tombstones(slug, seq, deleted_at) VALUES(?, ?, ?) ON CONFLICT ...
    const tombstone = trimmed.match(
      /^INSERT\s+INTO\s+tombstones\(slug,\s*seq,\s*deleted_at\)\s+VALUES\s*\(\?,\s*\?,\s*\?\)\s+ON\s+CONFLICT/i,
    );
    if (tombstone) {
      this.ensureTable('tombstones');
      this.tables.get('tombstones')!.set(`s:${bind[0]}`, {
        slug: bind[0],
        seq: bind[1],
        deleted_at: bind[2],
      });
      return;
    }
    // The big pages upsert. Match the `INSERT INTO pages (...) VALUES (?, ?, ...) ON CONFLICT`
    // shape robustly without trying to count placeholders exactly
    // (regex `+?` across newlines is brittle).
    const pagesUpsert = /^INSERT\s+INTO\s+pages[\s\S]+?VALUES\s+\([\s\S]+\)\s+ON\s+CONFLICT/i.test(
      trimmed,
    );
    if (pagesUpsert) {
      this.ensureTable('pages');
      const [
        slug, id, title, type, body, status,
        tags_json, sources_json, updated_at, created_at,
        confidence, verified_at, seq, redirected_from,
      ] = bind;
      if (!slug || !title || !type) return; // matches source-side guard
      const row = {
        slug, id, title, type, body, status,
        tags_json, sources_json, updated_at, created_at,
        confidence, verified_at, seq, redirected_from,
      };
      this.tables.get('pages')!.set(`p:${slug}`, row);
      return;
    }
    throw new Error(`stub run: unrecognized statement ${trimmed}`);
  }

  async transaction<T>(fn: () => Promise<T> | T): Promise<T> {
    await this.exec('BEGIN');
    try {
      const result = await fn();
      await this.exec('COMMIT');
      return result;
    } catch (error) {
      await this.exec('ROLLBACK');
      throw error;
    }
  }

  close(): void {
    this.tables.clear();
  }

  private ensureTable(name: string): void {
    if (!this.tables.has(name)) this.tables.set(name, new Map());
  }

  /**
   * Public accessor for the in-test verification (e.g. asserting
   * that a particular `INSERT` landed). The stub treats it as
   * internal; tests just poke through.
   */
  tableRows(name: string): Array<Record<string, unknown>> {
    this.ensureTable(name);
    return [...this.tables.get(name)!.values()];
  }
}

function makeStubDb(): StubDb {
  // No-op cycling here — each test picks a strategy explicitly
  // by either using the default `fts5` source (set up in
  // beforeEach) or constructing its own.
  return new StubDb();
}

const basePage = (
  overrides: Partial<Page> = {},
): Page => ({
  slug: 'alpha',
  title: 'Alpha',
  type: 'note',
  body: 'Hello world.',
  status: 'active',
  tags: [],
  sources: [],
  updated_at: '2026-01-15T12:00:00Z',
  seq: 1,
  ...overrides,
});

const baseDelta = (
  overrides: Partial<SyncDelta> = {},
): SyncDelta => ({
  cursor: 1,
  pages: [],
  tombstones: [],
  links: [],
  page_tags: [],
  tags: [],
  aliases: [],
  ...overrides,
});

let stub: StubDb;
let source: WasmDataSource;

beforeEach(async () => {
  stub = makeStubDb();
  source = new WasmDataSource(stub, 'fts5');
  await source.init();
});

afterEach(async () => {
  await source.close();
});

describe('init', () => {
  it('creates the canonical tables and seeds the meta cursor', async () => {
    expect(stub.tables.has('pages')).toBe(true);
    expect(stub.tables.has('tags')).toBe(true);
    expect(stub.tables.has('page_tags')).toBe(true);
    expect(stub.tables.has('links')).toBe(true);
    expect(stub.tables.has('tombstones')).toBe(true);
    expect(stub.tables.has('aliases')).toBe(true);
    expect(stub.tables.has('meta')).toBe(true);
    expect((await source.status()).cursor).toBe(0);
  });

  it('is idempotent — calling init twice keeps the same schema', async () => {
    // The stub only reacts to the column shape; we just confirm no
    // exception is raised on a second pass.
    await expect(source.init()).resolves.toBeUndefined();
  });

  it('resets the cursor once when the stored schema_version is stale', async () => {
    // Simulate a browser whose local copy was synced by the tag-less
    // era: cursor advanced, and no schema_version row (it predates
    // the version marker entirely). beforeEach's init has already
    // stamped the version, so remove it to stand in for the old DB.
    stub.tables.get('meta')!.delete('k:schema_version');
    stub.tables.get('meta')!.set('k:cursor', { key: 'cursor', value: '999' });
    await source.init();
    expect((await source.status()).cursor).toBe(0);
    const version = stub.tables.get('meta')!.get('k:schema_version')?.value;
    expect(version).toBe('2');
  });

  it('does not reset the cursor when schema_version matches', async () => {
    stub.tables.get('meta')!.set('k:cursor', { key: 'cursor', value: '4242' });
    stub.tables
      .get('meta')!
      .set('k:schema_version', { key: 'schema_version', value: '2' });
    await source.init();
    expect((await source.status()).cursor).toBe(4242);
  });
});

describe('applyDelta', () => {
  it('inserts pages and refreshes the meta cursor', async () => {
    const delta = baseDelta({
      cursor: 42,
      pages: [
        basePage({ slug: 'alpha', title: 'Alpha', seq: 1 }),
        basePage({ slug: 'beta', title: 'Beta', seq: 2 }),
      ],
      tags: [{ name: 'garden', count: 2 }],
    });
    await source.applyDelta(delta);
    expect((await source.status()).cursor).toBe(42);
    expect((await source.status()).lastSyncAt).not.toBeNull();
    const alpha = stub.tableRows('pages').find((row) => row.slug === 'alpha');
    expect(alpha?.title).toBe('Alpha');
    const tags = stub.tableRows('tags');
    expect(tags[0]).toMatchObject({ name: 'garden', count: 2 });
  });

  it('applies a delta whose tags omit count and still serves data', async () => {
    // /api/sync deltas can carry tag rows without a `count` field
    // (JSON field absent → undefined). The tags.count column is
    // NOT NULL, so the bind must default to 0 — otherwise the
    // INSERT throws and applyDelta rolls the whole delta back,
    // leaving the local DB empty.
    const delta = baseDelta({
      cursor: 7,
      pages: [basePage({ slug: 'alpha', seq: 7 })],
      tags: [{ name: 'foo' } as unknown as Tag],
    });
    await expect(source.applyDelta(delta)).resolves.toBeUndefined();
    expect((await source.getPage('alpha'))?.title).toBe('Alpha');
    expect((await source.getTags())).toEqual([{ name: 'foo', count: 0 }]);
    expect((await source.list({})).pages.map((p) => p.slug)).toEqual(['alpha']);
    // The rest of the delta still lands and the cursor advances.
    expect((await source.status()).cursor).toBe(7);
  });

  it('passes tags through to the page_tags table for later listing', async () => {
    const delta = baseDelta({
      cursor: 5,
      pages: [basePage({ slug: 'alpha', tags: ['gardens', 'notes'], seq: 5 })],
    });
    await source.applyDelta(delta);
    const rows = stub.tableRows('page_tags').filter((row) => row.page_slug === 'alpha');
    const names = rows.map((r) => r.tag_name).sort();
    expect(names).toEqual(['gardens', 'notes']);
  });

  it('deletes a page when a tombstone with a higher seq arrives in the same delta', async () => {
    const delta = baseDelta({
      cursor: 10,
      pages: [basePage({ slug: 'gone', seq: 9 })],
      tombstones: [{ slug: 'gone', seq: 10, deleted_at: '2026-01-15T12:00:00Z' }],
    });
    await source.applyDelta(delta);
    const pages = stub.tableRows('pages');
    expect(pages.find((p) => p.slug === 'gone')).toBeUndefined();
    expect((await source.getPage('gone'))).toBeNull();
  });

  it('keeps a page when a tombstone with a lower seq is applied after a higher-seq upsert', async () => {
    const earlyTomb = baseDelta({
      cursor: 2,
      tombstones: [{ slug: 'reverse', seq: 3, deleted_at: '2026-01-15T12:00:00Z' }],
    });
    await source.applyDelta(earlyTomb);
    const lateUpdate = baseDelta({
      cursor: 9,
      pages: [basePage({ slug: 'reverse', title: 'Resurrected', seq: 9 })],
    });
    await source.applyDelta(lateUpdate);
    const page = await source.getPage('reverse');
    expect(page?.title).toBe('Resurrected');
  });

  it('rolls back the whole delta when an inner write fails', async () => {
    const setup = baseDelta({
      cursor: 9,
      pages: [basePage({ slug: 'preExisting', seq: 9 })],
    });
    await source.applyDelta(setup);
    expect((await source.status()).cursor).toBe(9);

    // Force a failure on the meta UPDATE — this is the last write
    // the transaction does, so a throw here means the cursor
    // shouldn't advance past 9 even though some pages were upserted.
    stub.failureOnExec = 'UPDATE meta';
    await expect(source.applyDelta(baseDelta({ cursor: 11, pages: [] }))).rejects.toThrow();
    expect((await source.status()).cursor).toBe(9);
  });
});

describe('queries', () => {
  it('returns the page matching a slug, omitting deprecated pages', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 5,
        pages: [
          basePage({ slug: 'live', title: 'Live', status: 'active' }),
          basePage({ slug: 'dead', title: 'Dead', status: 'deprecated' }),
        ],
      }),
    );
    expect((await source.getPage('live'))?.title).toBe('Live');
    expect((await source.getPage('dead'))).toBeNull();
  });

  it('lists pages by tag and type, filtered to status=active by default', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 3,
        pages: [
          basePage({ slug: 'a', tags: ['garden'], type: 'note', updated_at: '2026-02-01T00:00:00Z', seq: 3 }),
          basePage({ slug: 'b', tags: ['other'], type: 'note', updated_at: '2026-01-01T00:00:00Z', seq: 2 }),
          basePage({ slug: 'c', tags: ['garden'], type: 'essay', updated_at: '2025-12-01T00:00:00Z', seq: 1 }),
          basePage({ slug: 'd', tags: ['garden'], status: 'deprecated', updated_at: '2025-11-01T00:00:00Z', seq: 0 }),
        ],
      }),
    );
    const recent = await source.getRecent(10);
    expect(recent.pages.map((p) => p.slug)).toEqual(['a', 'b', 'c']);

    const filtered = await source.list({ tag: 'garden' });
    expect(filtered.pages.map((p) => p.slug)).toEqual(['a', 'c']);

    const byType = await source.list({ type: 'essay' });
    expect(byType.pages.map((p) => p.slug)).toEqual(['c']);
  });

  it('reports tag counts that survive a second sync cycle', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 5,
        pages: [
          basePage({ slug: 'a', tags: ['garden'], seq: 5 }),
          basePage({ slug: 'b', tags: ['garden', 'notes'], seq: 4 }),
        ],
        tags: [{ name: 'garden', count: 2 }, { name: 'notes', count: 1 }],
      }),
    );
    expect((await source.getTags())).toEqual([
      { name: 'garden', count: 2 },
      { name: 'notes', count: 1 },
    ]);
    // A later sync refreshes page_tags because pages are re-applied;
    // counts come from COUNT(page_tags) so they stay correct.
    await source.applyDelta(
      baseDelta({
        cursor: 6,
        pages: [basePage({ slug: 'c', tags: ['garden'], seq: 6 })],
        tags: [{ name: 'garden', count: 3 }, { name: 'notes', count: 1 }],
      }),
    );
    expect((await source.getTags())).toEqual([
      { name: 'garden', count: 3 },
      { name: 'notes', count: 1 },
    ]);
  });

  it('search() does not return matches from deprecated pages', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 2,
        pages: [
          basePage({ slug: 'fresh', body: 'kittens are great', seq: 2 }),
          basePage({
            slug: 'old',
            body: 'kittens were here',
            status: 'deprecated',
            seq: 1,
          }),
        ],
      }),
    );
    const results = await source.search({ q: 'kittens' });
    expect(results.some((r) => r.slug === 'fresh')).toBe(true);
    expect(results.find((r) => r.slug === 'old')).toBeUndefined();
  });

  it('search() returns zero hits on an empty query', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 1,
        pages: [basePage({ slug: 'a', body: 'hello', seq: 1 })],
      }),
    );
    expect((await source.search({ q: '' }))).toEqual([]);
    expect((await source.search({ q: '   ' }))).toEqual([]);
  });

  it('getBacklinks returns pages that link to the target via the cached link graph', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 5,
        pages: [
          basePage({ slug: 'source-a', id: 1, seq: 5 }),
          basePage({ slug: 'source-b', id: 2, seq: 4 }),
          basePage({ slug: 'target', id: 3, seq: 3 }),
        ],
        links: [
          { source_id: 1, target_id: 3, rel: 'link', origin: 'body' },
          { source_id: 2, target_id: 3, rel: 'link', origin: 'body' },
          { source_id: 1, target_id: 2, rel: 'link', origin: 'body' }, // unrelated
        ],
      }),
    );
    const backlinks = await source.getBacklinks('target');
    expect(backlinks.map((b) => b.slug).sort()).toEqual(['source-a', 'source-b']);
  });

  it('skips links whose source/target slugs cannot be resolved', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 5,
        pages: [basePage({ slug: 'src', id: 1, seq: 5 })],
        links: [
          { source_id: 1, target_id: 99, rel: 'link', origin: 'body' }, // dangling target
          { source_id: 77, target_id: 1, rel: 'link', origin: 'body' }, // dangling source
        ],
      }),
    );
    expect((await source.getBacklinks('src'))).toEqual([]);
    expect((await source.getBacklinks('whatever'))).toEqual([]);
  });
});

describe('getGraph', () => {
  it('builds nodes and edges from cached pages and links, mapping slugs to numeric ids', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 5,
        pages: [
          basePage({ slug: 'alpha', id: 1, title: 'Alpha' }),
          basePage({ slug: 'beta', id: 2, title: 'Beta' }),
        ],
        links: [
          { source_id: 1, target_id: 2, rel: 'link', origin: 'body' },
          { source_id: 2, target_id: 1, rel: 'related', origin: 'sidebar' },
        ],
      }),
    );
    const graph = await source.getGraph();
    expect(graph.nodes).toEqual([
      { id: 1, slug: 'alpha', title: 'Alpha', type: 'note' },
      { id: 2, slug: 'beta', title: 'Beta', type: 'note' },
    ]);
    expect(graph.edges).toEqual([
      { source: 1, target: 2, rel: 'link', origin: 'body' },
      { source: 2, target: 1, rel: 'related', origin: 'sidebar' },
    ]);
  });

  it('excludes deprecated pages from both nodes and edges', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 5,
        pages: [
          basePage({ slug: 'alpha', id: 1, title: 'Alpha' }),
          basePage({ slug: 'gone', id: 2, title: 'Gone', status: 'deprecated' }),
          basePage({ slug: 'beta', id: 3, title: 'Beta' }),
        ],
        links: [
          { source_id: 1, target_id: 2, rel: 'link', origin: 'body' }, // to deprecated
          { source_id: 2, target_id: 3, rel: 'link', origin: 'body' }, // from deprecated
          { source_id: 1, target_id: 3, rel: 'link', origin: 'body' }, // live
        ],
      }),
    );
    const graph = await source.getGraph();
    expect(graph.nodes.map((n) => n.slug).sort()).toEqual(['alpha', 'beta']);
    expect(graph.edges).toEqual([
      { source: 1, target: 3, rel: 'link', origin: 'body' },
    ]);
  });

  it('skips edges whose source or target page is absent from the cache', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 5,
        pages: [basePage({ slug: 'alpha', id: 1 })],
      }),
    );
    // Simulate a stale local cache: link rows whose slugs no longer
    // resolve to any page row.
    await stub.run(
      'INSERT INTO links(source_slug, target_slug, rel, origin) VALUES(?, ?, ?, ?)',
      ['alpha', 'ghost', 'link', 'body'],
    );
    await stub.run(
      'INSERT INTO links(source_slug, target_slug, rel, origin) VALUES(?, ?, ?, ?)',
      ['ghost', 'alpha', 'link', 'body'],
    );
    const graph = await source.getGraph();
    expect(graph.nodes).toHaveLength(1);
    expect(graph.edges).toEqual([]);
  });
});

describe('search strategy robustness', () => {
  it('probes FTS5 at init and falls back to LIKE when unavailable', async () => {
    const noFts = new StubDb({ failOn: 'pages_fts' });
    const s = new WasmDataSource(noFts); // no forced strategy → probe
    await s.init();
    expect(s.searchStrategy()).toBe('like');
    await s.applyDelta(
      baseDelta({
        cursor: 1,
        pages: [basePage({ slug: 'a', body: 'fuzzy wuzzy', seq: 1 })],
      }),
    );
    const hits = await s.search({ q: 'fuzzy' });
    expect(hits.some((h) => h.slug === 'a')).toBe(true);
    s.close();
  });

  it('falls back gracefully when FTS is unavailable', async () => {
    const likeDb = new StubDb({ failOn: 'pages_fts' });
    await expect(new WasmDataSource(likeDb, 'like').init()).resolves.toBeUndefined();
    const s = new WasmDataSource(likeDb, 'like');
    await s.init();
    await s.applyDelta(
      baseDelta({
        cursor: 1,
        pages: [basePage({ slug: 'a', body: 'fuzzy wuzzy', seq: 1 })],
      }),
    );
    const hits = await s.search({ q: 'fuzzy' });
    expect(hits.some((h) => h.slug === 'a')).toBe(true);
    s.close();
  });

  it('keeps FTS path in normal operation', async () => {
    // The stub doesn't actually build FTS rows; we assert the SQL
    // path doesn't throw and produces a non-empty result. With the
    // default `fts5` strategy, the FTS row-update is a no-op for
    // the stub but search() falls through to LIKE-like semantics
    // via the stub's generalised SELECT.
    const ftsDb = new StubDb();
    const ftsSource = new WasmDataSource(ftsDb, 'fts5');
    await ftsSource.init();
    await ftsSource.applyDelta(
      baseDelta({
        cursor: 1,
        pages: [basePage({ slug: 'x', body: 'kittens again', seq: 1 })],
      }),
    );
    const hits = await ftsSource.search({ q: 'kittens' });
    expect(Array.isArray(hits)).toBe(true);
    expect(hits.length).toBeGreaterThan(0);
    ftsSource.close();
  });
});

describe('aliases', () => {
  it('persists alias rows from the delta so redirected lookups stay offline', async () => {
    await source.applyDelta(
      baseDelta({
        cursor: 4,
        pages: [basePage({ slug: 'real-slug', id: 11, seq: 4 })],
        aliases: [{ alias: 'old-slug', page_id: 11, created_at: '2026-01-01' }],
      }),
    );
    const aliases = stub.tableRows('aliases');
    expect(aliases[0]).toMatchObject({ alias: 'old-slug', page_slug: 'real-slug' });
  });
});

describe('status', () => {
  it('reports ready=true after init and cursor advances after apply', async () => {
    expect((await source.status()).ready).toBe(true);
    await source.applyDelta(baseDelta({ cursor: 99, pages: [basePage({ slug: 'a', seq: 99 })] }));
    const s = await source.status();
    expect(s.cursor).toBe(99);
    expect(s.lastSyncAt).not.toBeNull();
  });
});

// Exercise the real SQLite-WASM node build end-to-end (no mocks)
// to confirm the production code path works under vitest. The
// goal of THIS file is to lock the logic; the loader is exercised
// in `wasm-db-handle` integration smoke. We import lazily so a
// missing import doesn't bring down the whole suite.
describe('sqlite-wasm node build (smoke)', () => {
  it('opens an in-memory db and applies a delta with FTS5 enabled', async () => {
    const mod = await import('./wasm-db-handle');
    const { handle } = await mod.loadDbHandle();
    const real = new WasmDataSource(handle, 'fts5');
    await real.init();
    await real.applyDelta(
      baseDelta({
        cursor: 1,
        pages: [
          basePage({ slug: 'alpha', body: 'hello world', seq: 1 }),
          basePage({ slug: 'beta', body: 'goodbye world', seq: 1 }),
        ],
        tags: [{ name: 'greeting', count: 1 }],
      }),
    );
    const alpha = await real.getPage('alpha');
    expect(alpha?.title).toBe('Alpha');
    const results = await real.search({ q: 'hello' });
    expect(results.some((r) => r.slug === 'alpha')).toBe(true);
    const recent = await real.getRecent();
    expect(recent.pages).toHaveLength(2);
    real.close();
  });

  it('uses LIKE strategy when FTS5 is rejected and still finds rows', async () => {
    const mod = await import('./wasm-db-handle');
    const { handle } = await mod.loadDbHandle();
    const real = new WasmDataSource(handle, 'like');
    await real.init();
    await real.applyDelta(
      baseDelta({
        cursor: 2,
        pages: [basePage({ slug: 'beta', body: 'unicorn sparkle', seq: 2 })],
      }),
    );
    const hits = await real.search({ q: 'sparkle' });
    expect(hits.some((h) => h.slug === 'beta')).toBe(true);
    real.close();
  });
});
