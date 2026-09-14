/**
 * Data source selection — picks between the live HTTP API and the
 * offline WASM cache based on the runtime environment and sync state.
 *
 * The factory is intentionally explicit and testable: the
 * `selectDataSource()` function is a pure decision over a small
 * snapshot of the world, and `getDataSource()` is the IO-bound
 * factory that turns the decision into a real {@link DataSource}.
 *
 * The interface mirrors the parts of the HTTP API the routes use
 * today (getPage / search / list / getRecent / getTags /
 * getBacklinks / getGraph). Adding a new query path on either side
 * happens in two places: `api.ts` and here.
 */

import type { Graph, Page, PagesResponse, SearchHit, SyncDelta, Tag } from './types';
import { loadDbHandle } from './wasm-db-handle';
import {
  WasmDataSource,
  type SearchStrategy,
  type WasmStatus,
} from './wasm-data-source';

/**
 * The narrow contract the routes will eventually consume. Today the
 * routes still call `api.ts` directly, but the indirection is in
 * place so switching to offline-first is a one-commit change once
 * the WASM data is hot enough to be authoritative.
 */
export interface DataSource {
  /** Where this implementation lives — useful in logs and the footer. */
  readonly kind: 'http' | 'wasm';
  /** Read a single page by slug. Returns `null` when missing. */
  getPage(slug: string): Promise<Page | null>;
  /** Search; mirrors the HTTP API's `/api/search` parameter shape. */
  search(params: {
    q: string;
    tag?: string;
    type?: string;
    include_body?: boolean;
    limit?: number;
  }): Promise<SearchHit[]>;
  /** List pages with optional filters; mirrors `/api/pages`. */
  list(params: {
    tag?: string;
    type?: string;
    status?: string;
    limit?: number;
  }): Promise<PagesResponse>;
  /** Shortcut for the home page — most recent active pages. */
  getRecent(limit?: number): Promise<PagesResponse>;
  /** Tag list with counts; mirrors `/api/tags`. */
  getTags(): Promise<Tag[]>;
  /** Pages that link to `slug`; mirrors the `getBacklinks` route. */
  getBacklinks(
    slug: string,
  ): Promise<Array<Pick<Page, 'slug' | 'title' | 'type'>>>;
  /** Full graph of pages and links; mirrors the `/api/graph` route. */
  getGraph(): Promise<Graph>;
}

/**
 * Snapshot of the environment the selector needs to make its
 * decision. Picking is a pure function of these four values; the
 * factory collects them once via the `useSelectionInput()` helper.
 */
export interface SelectionInput {
  /** `window.isSecureContext` — OPFS lives here. */
  isSecureContext: boolean;
  /** `navigator.onLine` — the browser's last-known connectivity. */
  isOnline: boolean;
  /**
   * Whether a local WASM DB file already exists. The factory learns
   * this by trying to open OPFS and read the schema row count. When
   * `null` is passed, the selector assumes the caller doesn't care
   * (used by tests).
   */
  hasWasmDb: boolean | null;
  /**
   * Whether the WasmDataSource module is even available. In tests
   * that mock this away, we'd degrade to HTTP regardless of the
   * other inputs.
   */
  wasmModuleAvailable: boolean;
}

/**
 * Pure decision: which backend should serve this request given the
 * snapshot of the world?
 *
 * Rules, in priority order:
 *
 *   1. The WASM module isn't loadable → HTTP.
 *   2. OPFS not available (insecure context) → HTTP. We never run
 *      a WASM DB that can't persist.
 *   3. We have a WASM DB and we're offline → WASM (offline is the
 *      whole reason this exists).
 *   4. We're online with a working WASM DB → WASM. The HTTP path
 *      stays as a fallback only when WASM is unavailable.
 *
 * The empty state (offline + no WASM DB) surfaces as HTTP; the
 * caller is expected to render the empty state when both layers
 * return no data.
 */
export function selectDataSource(input: SelectionInput): 'wasm' | 'http' {
  if (!input.wasmModuleAvailable) return 'http';
  if (!input.isSecureContext) return 'http';
  // Secure context + online → WASM, even when no local copy exists
  // yet: the factory boots the (initially empty) DB and the sync
  // driver builds it from the first delta.
  if (input.isOnline) return 'wasm';
  // Offline: use the local copy when we've synced at least once;
  // otherwise the caller renders an honest empty state (HTTP would
  // only fail offline).
  if (input.hasWasmDb === true) return 'wasm';
  return 'http';
}

// ---------------------------------------------------------------------------
// Factory: turn the selection into a concrete DataSource.
// ---------------------------------------------------------------------------

let activeSource: DataSource | null = null;
let activeWasmSource: WasmDataSource | null = null;
/**
 * Promise pin for the in-flight boot. While a WASM DB is being
 * loaded, two concurrent callers (e.g. a sync writer and a route
 * reader) would otherwise each see `activeSource === null`, both
 * decide to boot a fresh DB, and end up talking to two separate
 * sqlite instances — sync writes to one, reads from the other. This
 * promise is set synchronously *before* any await so the second
 * caller picks up the same in-flight boot and the same resolved
 * source. Cleared by `resetDataSource`.
 */
let activeSourcePromise: Promise<DataSource> | null = null;
/** localStorage key the sync driver sets after the first successful delta. */
const HAS_LOCAL_COPY_KEY = 'vesperiki-has-offline-copy';
/**
 * Whether the local DB lives in OPFS (survives reloads) or memory.
 * The loader reports this; the driver surfaces it in status so the
 * footer can label the offline copy accurately.
 */
let activeWasmPersisted = false;

/**
 * Build / return the cached data source for the current page
 * lifetime. Safe to call from multiple effect sites — the same
 * instance is reused.
 *
 * The factory doesn't trigger sync on its own; the caller wires
 * the sync driver once the source is in hand.
 */
export function getDataSource(): Promise<DataSource> {
  // Promise pin: if a boot is already in flight (or just resolved),
  // return *the same* promise so concurrent callers share one
  // sqlite instance instead of racing to boot two. Returning a
  // non-async function is what preserves the promise identity — an
  // `async` wrapper would always allocate a fresh Promise.
  if (activeSourcePromise) return activeSourcePromise;
  // Assign the promise *before* any await so a synchronous second
  // call observes it. The IIFE is the existing boot body.
  activeSourcePromise = (async () => {
    if (activeSource) return activeSource;
    const input = await readSelectionInput();
    const choice = selectDataSource(input);
    if (choice === 'wasm') {
      try {
        const loaded = await loadDbHandle();
        // Strategy is verified at runtime inside init() (probe FTS5,
        // fall back to LIKE) — no hardcoding here.
        const source = new WasmDataSource(loaded.handle);
        await source.init();
        activeSource = makeWasmAdapter(source);
        activeWasmSource = source;
        activeWasmPersisted = loaded.persisted;
        return activeSource;
      } catch (error) {
        // OPFS may be present in storage but locked by another tab, or
        // the WASM module may fail to load in a Safari Technology
        // Preview. Degrade to HTTP rather than blocking the page.
        console.warn('Falling back to HTTP data source:', error);
        activeSource = makeHttpAdapter();
        return activeSource;
      }
    }
    activeSource = makeHttpAdapter();
    return activeSource;
  })();
  return activeSourcePromise;
}

/**
 * Reset the cached data source — used by tests and by the footer
 * "Sync now" button after a forced cold-restart of the WASM DB.
 *
 * Closing is best-effort: the WASM handle's `close()` is
 * idempotent and the caller doesn't need to wait for the worker
 * to acknowledge it before booting a fresh source. The next
 * `getDataSource()` call observes the cleared cache immediately.
 */
export function resetDataSource(): void {
  if (activeWasmSource) {
    try {
      activeWasmSource.close();
    } catch {
      /* already closed or worker gone — non-fatal */
    }
  }
  activeSource = null;
  activeWasmSource = null;
  activeWasmPersisted = false;
  // Drop the pin so the next call boots a fresh DB; without this,
  // a post-reset caller would receive the previously-resolved
  // (and now closed) source.
  activeSourcePromise = null;
}

// ---------------------------------------------------------------------------
// Concrete adapters.
// ---------------------------------------------------------------------------

function makeHttpAdapter(): DataSource {
  return {
    kind: 'http',
    async getPage(slug) {
      const mod = await import('./api');
      try {
        return await mod.getPageBySlug(slug);
      } catch (error) {
        if (isNotFound(error)) return null;
        throw error;
      }
    },
    async search(params) {
      const mod = await import('./api');
      return mod.search(params);
    },
    async list(params) {
      const mod = await import('./api');
      return mod.listPages(params);
    },
    async getRecent(limit = 20) {
      const mod = await import('./api');
      return mod.getRecent(limit);
    },
    async getTags() {
      const mod = await import('./api');
      return mod.listTags();
    },
    async getBacklinks(slug) {
      const mod = await import('./api');
      return mod.getBacklinks(slug);
    },
    async getGraph() {
      const mod = await import('./api');
      return mod.getGraph();
    },
  };
}

function makeWasmAdapter(source: WasmDataSource): DataSource {
  return {
    kind: 'wasm',
    async getPage(slug) {
      return source.getPage(slug);
    },
    async search(params) {
      return source.search(params);
    },
    async list(params) {
      return source.list(params);
    },
    async getRecent(limit) {
      return source.getRecent(limit);
    },
    async getTags() {
      return source.getTags();
    },
    async getBacklinks(slug) {
      return source.getBacklinks(slug);
    },
    async getGraph() {
      return source.getGraph();
    },
  };
}

function isNotFound(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const e = error as { status?: number; name?: string };
  return e.status === 404 || e.name === 'NotFoundError';
}

// ---------------------------------------------------------------------------
// Data-changed notifications — sync success → cache invalidation.
//
// Deliberately framework-free: a tiny pub/sub so the sync driver can
// tell the rest of the app (TanStack Query) that the local DB changed
// without importing React or React Query into this module.
// ---------------------------------------------------------------------------

export type DataChangedListener = () => void;

const dataChangedListeners = new Set<DataChangedListener>();

/**
 * Subscribe to "a sync just succeeded" notifications. The callback
 * runs synchronously after every successful sync — i.e. after the
 * delta has been applied to the local DB — and never on failure.
 * Returns an unsubscribe function.
 */
export function subscribeDataChanged(listener: DataChangedListener): () => void {
  dataChangedListeners.add(listener);
  return () => {
    dataChangedListeners.delete(listener);
  };
}

// ---------------------------------------------------------------------------
// Sync driver — orchestrates /api/sync → applyDelta.
// ---------------------------------------------------------------------------

export interface SyncDriver {
  /** Run a one-shot sync against the server. No-op if WASM is not active. */
  sync(force?: boolean): Promise<void>;
  /**
   * Last completed status; null when nothing has run yet. The read
   * itself hits the underlying WASM source and is therefore
   * asynchronous — the worker-promiser path the source is built on
   * is a postMessage round-trip, not a synchronous local call.
   */
  status(): Promise<WasmStatus | null>;
}

/**
 * Build a sync driver that talks to the server and applies deltas to
 * the active WASM data source. The factory lazy-imports `./api` so
 * the data-source module has no top-level dependency on `fetch`.
 */
export function makeSyncDriver(): SyncDriver {
  let lastResult: { ok: true } | { ok: false; error: string } | null = null;
  return {
    async sync(force = false) {
      if (!activeWasmSource) {
        lastResult = { ok: false, error: 'no WASM data source' };
        return;
      }
      try {
        const mod = await import('./api');
        let cursor = (await activeWasmSource.status()).cursor;
        // The first sync pulls the whole DB; subsequent pulls use
        // the last-known cursor. The "force" flag is reserved for
        // future use (cache eviction → reseed).
        if (force) cursor = 0;
        const delta = await mod.sync(cursor);
        await activeWasmSource.applyDelta(delta);
        // Record that a local copy exists so the offline selection
        // logic can decide between "read from cache" and "empty
        // state" next cold start. localStorage survives reloads and
        // is opaque to the OPFS layout sqlite-wasm manages.
        try {
          localStorage.setItem(HAS_LOCAL_COPY_KEY, '1');
        } catch {
          /* storage quota / private mode — non-fatal */
        }
        lastResult = { ok: true };
        // The local DB just changed — notify subscribers. The app
        // invalidates its TanStack Query cache in response, which is
        // what keeps a cold-start first render (a route queryFn reads
        // an empty WASM DB and caches the result) from showing stale
        // data forever once the sync populates the DB.
        for (const listener of dataChangedListeners) listener();
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        lastResult = { ok: false, error: message };
      }
    },
    async status() {
      if (!activeWasmSource) return null;
      return {
        ...(await activeWasmSource.status()),
        persisted: activeWasmPersisted,
      };
    },
  };
}

/**
 * Apply a sync delta to the currently-active WASM data source.
 * Exposed for tests and for the manual sync pathway in the footer.
 */
export async function applySyncDelta(delta: SyncDelta): Promise<void> {
  await activeWasmSource?.applyDelta(delta);
}

// ---------------------------------------------------------------------------
// Environment snapshot.
// ---------------------------------------------------------------------------

/**
 * Collect a {@link SelectionInput} from the current page. Centralises
 * the browser-API probes so tests can mock them in one place.
 */
export async function readSelectionInput(): Promise<SelectionInput> {
  return {
    isSecureContext: readIsSecureContext(),
    isOnline: readIsOnline(),
    hasWasmDb: await readHasWasmDb(),
    wasmModuleAvailable: true,
  };
}

function readIsSecureContext(): boolean {
  if (typeof window === 'undefined') return false;
  return Boolean((window as Window & { isSecureContext?: boolean }).isSecureContext);
}

function readIsOnline(): boolean {
  if (typeof navigator === 'undefined') return true;
  return navigator.onLine !== false;
}

/**
 * Has the WASM DB ever been written? The sync driver sets a
 * localStorage flag after the first successful delta; the flag
 * survives reloads and is opaque to the OPFS directory layout that
 * sqlite-wasm manages internally (probing `navigator.storage` for a
 * file name would be fragile and version-dependent).
 *
 * Returns `null` when storage is unavailable (e.g. under Node)
 * so the selector can treat the state as unknown.
 */
async function readHasWasmDb(): Promise<boolean | null> {
  if (typeof localStorage === 'undefined') return null;
  try {
    return localStorage.getItem(HAS_LOCAL_COPY_KEY) === '1';
  } catch {
    return null;
  }
}
