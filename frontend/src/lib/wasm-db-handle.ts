/**
 * Loader + DbHandle factory for WasmDataSource.
 *
 * Two strategies depending on the environment:
 *
 *  - In Node (vitest, scripts): import the package's node.mjs entry
 *    which spins up the WASM module using Node's own WebAssembly
 *    (no OPFS). We open an in-memory DB because per-test isolation
 *    is more important than local persistence.
 *
 *  - In the browser: open the DB inside a Web Worker via the
 *    package's named `sqlite3Worker1Promiser` export. In
 *    3.53.0-build1 that export is the callable v2 factory itself,
 *    though older/typed builds may expose it as a `.v2()` method.
 *    The factory may return the promiser directly or a promise for
 *    it. The worker is the only place where the OPFS VFS can
 *    install: the main-thread `vfsInstallationFeatureCheck` requires
 *    `WorkerGlobalScope` (see node_modules/@sqlite.org/sqlite-wasm/
 *    dist/index.mjs) and silently no-ops otherwise, so
 *    `sqlite3.oo1.OpfsWlDb` / `OpfsDb` never register on the main
 *    thread of @sqlite.org/sqlite-wasm 3.53.0-build1. The promiser
 *    proxies queries between the main thread and the worker, which
 *    keeps the {@link DbHandle} shape unchanged from the caller.
 *
 *    If the promiser path can't start (worker load failure,
 *    COOP/COEP missing, OPFS API absent, the worker falls back to
 *    `:memory:`, etc.) we degrade to the in-memory OO1 path on
 *    the main thread and the data source surfaces a "no
 *    persistence" signal. We never report `persisted: true` for an
 *    in-memory DB.
 *
 * The FTS5 capability is detected at init by attempting
 * `CREATE VIRTUAL TABLE pages_fts USING fts5(...)`. The package's
 * WASM build does include FTS5 in 3.53.0, but the test still
 * matters because the user might serve the SPA from a stripped
 * custom build later.
 */

import type { DbHandle } from './wasm-data-source';

export type RuntimeKind = 'node' | 'browser-opfs' | 'browser-memory';

export interface LoadOptions {
  /**
   * Force a specific persistence backend. Defaults to OPFS on the
   * browser, in-memory under Node. Used by tests.
   */
  filename?: string;
  /** When true, skip OPFS and use an in-memory DB regardless of env. */
  inMemory?: boolean;
}

export interface LoadedDb {
  handle: DbHandle;
  /** Whether the data lives in OPFS / IndexedDB and survives reloads. */
  persisted: boolean;
  /** Which runtime the loader picked — used by the data source banner. */
  kind: RuntimeKind;
}

/**
 * Upper bound on how long we wait for the worker to boot and
 * acknowledge the `open` message. The promiser itself doesn't
 * surface a worker-load failure — if the worker URL 404s or the
 * worker thread crashes before posting `worker1-ready`, the
 * factory's boot result hangs forever and so does the SPA. The timeout
 * lets us degrade to the in-memory OO1 fallback instead of
 * blocking the page on a missing worker file. 10s is far longer
 * than any healthy boot takes in practice.
 */
const WORKER_BOOT_TIMEOUT_MS = 10_000;

/**
 * Detect whether we're in Node (vitest, build script) or the
 * browser. `import.meta.env.SSR` lets the bundler strip Node-only
 * imports from the browser bundle, but we keep the gate defensive
 * in case a future build step changes.
 */
function inNode(): boolean {
  // `process` is not in the TS types for this project (the SPA is a
  // browser app), so we duck-type without naming it.
  const globalProcess = (globalThis as { process?: { versions?: { node?: string } } }).process;
  if (globalProcess?.versions?.node) return true;
  return false;
}

export async function loadDbHandle(opts: LoadOptions = {}): Promise<LoadedDb> {
  if (inNode() || opts.inMemory) {
    return loadNodeDb(opts);
  }
  return loadBrowserDb(opts);
}

/**
 * Load the Node build of sqlite-wasm and wrap it in a DbHandle.
 * Always in-memory — the Node build does not have OPFS, and we
 * don't want test runs to touch the local filesystem.
 */
async function loadNodeDb(_opts: LoadOptions): Promise<LoadedDb> {
  // The package's package.json maps "node" → ./dist/node.mjs, which
  // is what we want here. In a Node environment this resolves
  // automatically.
  const mod = await import('@sqlite.org/sqlite-wasm');
  const initFn = (mod as { default?: () => Promise<unknown> }).default
    ?? (mod as unknown as () => Promise<unknown>);
  const sqlite3 = (await initFn()) as {
    oo1: { DB: new (filename: string) => NodeDb };
  };
  const db = new sqlite3.oo1.DB(':memory:');
  return {
    handle: new NodeDbHandle(db),
    persisted: false,
    kind: 'node',
  };
}

/**
 * Browser build: open the DB inside a Web Worker via
 * `sqlite3Worker1Promiser` and return a {@link DbHandle} that
 * proxies every method over the promiser's message interface.
 *
 * Falls back to the in-memory OO1 path on the main thread when any
 * precondition fails: COOP/COEP not set (crossOriginIsolated),
 * File System Access API absent, the worker bundle fails to load,
 * the promiser init hangs (worker never posts `worker1-ready`), or
 * the worker reports the opened DB as non-persistent. The fallback
 * keeps the app booting — without persistence, but functional —
 * rather than hanging on a half-loaded OPFS handle.
 */
async function loadBrowserDb(opts: LoadOptions): Promise<LoadedDb> {
  // Lazy import so the Node test path never pulls in the worker
  // bundle. We grab both the default init (needed for the in-memory
  // fallback) and the named `sqlite3Worker1Promiser` (needed for
  // the persistent OPFS path) from the same module.
  const mod = await import('@sqlite.org/sqlite-wasm');
  const initFn = (mod as { default?: () => Promise<unknown> }).default
    ?? (mod as unknown as () => Promise<unknown>);
  const sqlite3 = (await initFn()) as {
    oo1: { DB: new (filename: string) => NodeDb };
  };

  const supportsOpfs = await detectOpfs();

  // Try the worker path when OPFS is available. Any failure (init
  // timeout, worker load failure, open rejection, non-persistent
  // DB) falls back to the in-memory OO1 path. We never report
  // `persisted: true` for an in-memory DB.
  if (supportsOpfs && !opts.inMemory) {
    const persisted = await tryOpenWorkerDb(mod, opts.filename ?? 'vesperiki.db');
    if (persisted) return persisted;
  }

  // In-memory fallback. Uses the same WASM init that powers the
  // Node build, but the package.json "browser" mapping points the
  // dynamic import at the ESM entry which exposes the OPFS VFS
  // globals in case a later call decides to use them.
  const db = new sqlite3.oo1.DB(':memory:');
  return {
    handle: new NodeDbHandle(db),
    persisted: false,
    kind: 'browser-memory',
  };
}

/**
 * Try to open `filename` via the worker-promiser and wrap the
 * resulting handle in a {@link WorkerDbHandle}. Returns `null` on
 * any failure so the caller can fall back to the in-memory path
 * without unwrapping a partially-constructed handle.
 *
 * The promiser message API (documented in
 * node_modules/@sqlite.org/sqlite-wasm/dist/index.mjs, "Worker API
 * #1"): the supported message types are `open`, `close`, `exec`,
 * `export`, `config-get`. There is no `select`/`run`/`begin`/
 * `commit`/`rollback` message — every query goes through `exec`,
 * which on the worker side calls `db.exec({sql, ...})` and returns
 * the input options object (with `resultRows` and `columnNames`
 * populated if requested). We exploit that to keep the
 * {@link DbHandle} shape identical to the Node build: `select`
 * passes `{sql, bind, rowMode: 'object', returnValue: 'resultRows'}`,
 * `run` passes `{sql, bind}`, transactions issue `BEGIN`/`COMMIT`/
 * `ROLLBACK` via plain `exec` calls, and `close` posts a `close`
 * message.
 *
 * The `dbId` returned by `open` is opaque to us; the promiser
 * tracks it internally and attaches it to subsequent messages, so
 * we don't need to thread it through every call. We pass it to
 * the wrapper only so it can be reported alongside `close`.
 */
async function tryOpenWorkerDb(
  mod: unknown,
  filename: string,
): Promise<LoadedDb | null> {
  // The named export is declared in
  // node_modules/@sqlite.org/sqlite-wasm/dist/index.d.mts as
  // `Worker1PromiserFactory`. Two shapes exist across builds:
  //   - Unminified / .mjs source: an object whose `.v2(config?)`
  //     method is the v2 factory.
  //   - Minified Vite bundle of @sqlite.org/sqlite-wasm
  //     3.53.0-build1: the v2 factory is re-exported AS the
  //     named export itself (`export { Lr as sqlite3Worker1Promiser
  //     }` where `Lr = sqlite3Worker1Promiser.v2`), so calling
  //     `mod.sqlite3Worker1Promiser.v2` resolves to `undefined`
  //     and the v2 factory must be invoked directly.
  // We grab the export defensively and dispatch to whichever
  // shape is present. A future build could drop the named export
  // entirely while keeping the default — in that case neither
  // shape is available and we fall through to the in-memory
  // fallback.
  const factory = (mod as { sqlite3Worker1Promiser?: unknown })
    .sqlite3Worker1Promiser;
  if (!factory) return null;

  type WorkerPromiserFactory = (
    config?: unknown,
  ) => WorkerPromiser | PromiseLike<WorkerPromiser>;
  const factoryAny = factory as { v2?: WorkerPromiserFactory };
  let createPromiser: WorkerPromiserFactory;
  if (typeof factory === 'function') {
    // sqlite-wasm 3.53.0-build1 exports the v2 factory directly.
    createPromiser = factory as WorkerPromiserFactory;
  } else if (typeof factoryAny.v2 === 'function') {
    // Retain compatibility with builds exposing the typed `.v2` shape.
    createPromiser = factoryAny.v2;
  } else {
    return null;
  }

  let promiser: WorkerPromiser | null = null;
  let dbId: string | null = null;
  try {
    // Invoke the factory inside the raced promise so synchronous
    // factory failures degrade just like asynchronous worker boot
    // failures. Promise resolution accepts both a directly-returned
    // promiser and a promise/thenable for one.
    const promiserPromise = Promise.resolve().then(() => createPromiser({}));
    // Without a config the default worker URL
    // `new Worker(new URL('sqlite3-worker1.mjs', import.meta.url))`
    // is used, which Vite resolves to /assets/sqlite3-worker1-*.js
    // at build time. We race the init against a timeout because the
    // promiser doesn't surface worker-load failures — a missing
    // worker file would otherwise hang forever.
    promiser = await withTimeout(
      promiserPromise,
      WORKER_BOOT_TIMEOUT_MS,
      'sqlite3Worker1Promiser did not resolve within the boot timeout',
    );

    // Open the persistent file. The worker installs its OPFS VFS
    // (worker-side vfsInstallationFeatureCheck sees
    // WorkerGlobalScope) but in the minified 3.53.0-build1 build
    // the worker defaults to `unix-none` (non-persistent) when
    // `vfs` is omitted — verified live: open without vfs returns
    // `{persistent: false, vfs: "unix-none"}`, while open with
    // `{filename, vfs: 'opfs'}` returns `{persistent: true, vfs:
    // "opfs"}` and creates the file in OPFS. We therefore select
    // the proven `'opfs'` VFS explicitly.
    const openResult = await promiser('open', { filename, vfs: 'opfs' });
    // The response envelope is `{type, messageId, dbId, result}`.
    // `.result` is `{filename, dbId, persistent, vfs}` per the
    // worker API docstring (see index.mjs "open" section).
    const openPayload = openResult.result as {
      filename: string;
      dbId: string;
      persistent: boolean;
      vfs: string;
    };
    if (!openPayload.persistent) {
      // The worker fell back to a non-persistent VFS (e.g. :memory:)
      // — close it so we don't leak the worker handle and report
      // the in-memory fallback to the caller instead of lying
      // about persistence.
      try {
        await promiser('close', {});
      } catch {
        /* ignore — we're degrading anyway */
      }
      return null;
    }
    dbId = openPayload.dbId;
    return {
      handle: new WorkerDbHandle(promiser, dbId),
      persisted: true,
      kind: 'browser-opfs',
    };
  } catch (error) {
    // Close any partial open so the worker can be reused on a
    // later retry. Ignore close errors — we already have a
    // primary error to report.
    if (promiser && dbId !== null) {
      try {
        await promiser('close', {});
      } catch {
        /* ignore */
      }
    }
    // eslint-disable-next-line no-console
    console.warn('OPFS open via worker failed, falling back to in-memory:', error);
    return null;
  }
}

/**
 * Minimal local alias for the promiser's callable type. The
 * package exports `Worker1Promiser` as an overloaded callable; we
 * don't need the overload resolution in this file because we only
 * pass strings or plain `{type, args}` envelopes. Defining a
 * structural alias here keeps the wrapper readable without
 * importing the entire Worker1 type machinery.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type WorkerPromiser = (msgOrType: any, maybeArgs?: any) => Promise<any>;

/**
 * Race `promise` against a timeout. Rejects with `message` if the
 * timeout elapses first.
 */
function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

/**
 * Detect the main-thread-observable prerequisites for attempting an
 * OPFS open in sqlite-wasm's dedicated worker. If any of these are
 * missing, the worker cannot install its OPFS VFS and the loader must
 * degrade to an in-memory DB. The conditions:
 *
 *  - The document is cross-origin isolated (COOP/COEP response
 *    headers). The worker inherits the document's isolation, and
 *    sqlite-wasm's OPFS VFS boots an async-proxy worker that needs
 *    SharedArrayBuffer / Atomics.waitAsync.
 *  - The page is in a secure context with the Origin Private File
 *    System API available (`navigator.storage.getDirectory`).
 *  - The low-level primitives observable on the main thread:
 *    SharedArrayBuffer, Atomics, and Atomics.waitAsync, which the
 *    sqlite-wasm OPFS proxy requires.
 *  - The File System Access API constructors observable on the main
 *    thread: FileSystemHandle, FileSystemDirectoryHandle, and
 *    FileSystemFileHandle.
 *
 * `FileSystemFileHandle.prototype.createSyncAccessHandle` is
 * intentionally not checked here because browsers expose it only in
 * dedicated workers. sqlite-wasm's worker-side
 * `vfsInstallationFeatureCheck` checks it while installing the VFS;
 * the worker's `open` envelope then reports whether the resulting DB
 * is persistent, which {@link tryOpenWorkerDb} verifies.
 */
async function detectOpfs(): Promise<boolean> {
  // Missing crossOriginIsolated ⇒ no COOP/COEP ⇒ the OPFS VFS cannot
  // boot. Report OPFS as unavailable so the loader degrades to an
  // in-memory DB instead of trying (and hanging on) the OPFS open.
  if (!globalThis.crossOriginIsolated) return false;
  if (typeof navigator === 'undefined') return false;
  if (typeof navigator.storage?.getDirectory !== 'function') return false;
  if (typeof window !== 'undefined' && (window as Window & { isSecureContext?: boolean }).isSecureContext === false) {
    return false;
  }
  // The remaining gates are OPFS VFS runtime prerequisites that the
  // main thread can observe. Browsers can report
  // crossOriginIsolated=true while still lacking Atomics.waitAsync
  // (observed in Firefox 135 / Camofox); in that case the VFS install
  // fails silently, so report OPFS as unavailable rather than picking
  // WASM and letting `useOfflineStatus` hang on "checking…".
  // `typeof` probes keep this safe where some globals are undefined.
  if (typeof globalThis.SharedArrayBuffer !== 'function') return false;
  if (typeof globalThis.Atomics !== 'object') return false;
  const atomics = globalThis.Atomics as typeof Atomics & { waitAsync?: unknown };
  if (typeof atomics.waitAsync !== 'function') return false;
  if (typeof globalThis.FileSystemHandle !== 'function') return false;
  if (typeof globalThis.FileSystemDirectoryHandle !== 'function') return false;
  if (typeof globalThis.FileSystemFileHandle !== 'function') return false;
  try {
    await navigator.storage.getDirectory();
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Minimal adapter around the sqlite-wasm OO1 API. The package's
// TypeScript surface is large; we use the runtime `exec({ returnValue:
// 'resultRows', rowMode: 'array' })` form so we never have to fight
// overload resolution.
// ---------------------------------------------------------------------------

interface NodeDb {
  exec(input: unknown): unknown;
  prepare(sql: string): NodeStatement;
  close(): void;
}

interface NodeStatement {
  bind(...args: unknown[]): NodeStatement;
  step(): boolean;
  get(rawIndex: number): unknown;
  reset(): NodeStatement;
  finalize(): void;
  /** Available on every statement — populated lazily. */
  getColumnNames(target?: string[]): string[];
}

class NodeDbHandle implements DbHandle {
  constructor(private readonly db: NodeDb) {}

  async exec(sql: string): Promise<unknown> {
    return this.db.exec(sql);
  }

  async select<T = Record<string, unknown>>(sql: string, bind?: unknown[]): Promise<T[]> {
    const stmt = this.db.prepare(sql);
    try {
      this.bindStmt(stmt, bind);
      const rows: T[] = [];
      while (stmt.step()) {
        rows.push(this.rowAsObject(stmt) as T);
      }
      return rows;
    } finally {
      stmt.finalize();
    }
  }

  async run(sql: string, bind?: unknown[]): Promise<void> {
    const stmt = this.db.prepare(sql);
    try {
      this.bindStmt(stmt, bind);
      stmt.step();
    } finally {
      stmt.finalize();
    }
  }

  private bindStmt(stmt: NodeStatement, bind?: unknown[]): void {
    if (!bind || bind.length === 0) return;
    // The sqlite-wasm OO1 `bind` overloads are:
    //   bind(binding: BindingSpec)              // a one-arg array
    //   bind(idx: number, binding: SqlValue)    // two-arg index+value
    // For multi-column prepared statements we pass an array as the
    // single argument. The TypeScript overload for the array form
    // collapses to `never` when called via `apply`, so we cast.
    const bindFn = stmt.bind as unknown as (b: unknown[]) => NodeStatement;
    bindFn.call(stmt, bind);
  }

  async transaction<T>(fn: () => Promise<T> | T): Promise<T> {
    // The sqlite-wasm node build doesn't expose a public BEGIN
    // helper; the cheapest correct option is to wrap in savepoints
    // via plain SQL and rethrow on errors. The package's own
    // recommendations suggest `Database.exec` with multi-statement
    // SQL for atomic updates, which is what we use here.
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
    this.db.close();
  }

  private rowAsObject(stmt: NodeStatement): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    const names = stmt.getColumnNames();
    for (let i = 0; i < names.length; i++) {
      out[names[i]] = stmt.get(i);
    }
    return out;
  }
}

// ---------------------------------------------------------------------------
// WorkerDbHandle — adapter around the sqlite3Worker1Promiser
// message API. Every method posts a message and awaits the
// response; on the worker side each message is dispatched to
// `wMsgHandler[type]` which calls `db.exec({sql, ...})` for the
// 'exec' type. The worker is single-threaded, so messages are
// serialized naturally and we don't need a mutex on top.
// ---------------------------------------------------------------------------

class WorkerDbHandle implements DbHandle {
  /**
   * `closed` flips to `true` after the first `close()` call so
   * later invocations short-circuit instead of throwing — matches
   * the OO1 `DB.close()` behaviour of "calling close() multiple
   * times is harmless".
   */
  private closed = false;

  constructor(
    private readonly promiser: WorkerPromiser,
    /**
     * Opaque identifier the promiser captured from the `open`
     * response and re-attaches to every non-`open` message. We
     * don't need to thread it manually, but keeping it here makes
     * `close()`'s intent obvious in stack traces.
     */
    _dbId: string,
  ) {}

  exec(sql: string): Promise<unknown> {
    this.assertOpen();
    // The response is `{type, messageId, dbId, result}`; the
    // worker returns the input options object. The synchronous
    // `DbHandle.exec` callers don't read the return value, so we
    // just await the response to surface any worker-side error.
    return this.promiser('exec', { sql }).then((envelope: { result: unknown }) => envelope.result);
  }

  async select<T = Record<string, unknown>>(sql: string, bind?: unknown[]): Promise<T[]> {
    this.assertOpen();
    // Worker-side `db.exec({sql, rowMode: 'object',
    // returnValue: 'resultRows'})` populates `resultRows` with
    // objects keyed by column name. The response envelope's
    // `.result.resultRows` is what the OO1 Node handle's
    // `select` would have produced.
    const args: { sql: string; rowMode: 'object'; returnValue: 'resultRows'; bind?: unknown[] } = {
      sql,
      rowMode: 'object',
      returnValue: 'resultRows',
    };
    if (bind && bind.length) args.bind = bind;
    const envelope = await this.promiser('exec', args) as { result: { resultRows?: T[] } };
    return (envelope.result.resultRows ?? []) as T[];
  }

  async run(sql: string, bind?: unknown[]): Promise<void> {
    this.assertOpen();
    // Worker `exec` returns the input options object; we don't
    // need it. The promise still surfaces errors.
    const args: { sql: string; bind?: unknown[] } = { sql };
    if (bind && bind.length) args.bind = bind;
    await this.promiser('exec', args);
  }

  async transaction<T>(fn: () => Promise<T> | T): Promise<T> {
    this.assertOpen();
    // The worker API doesn't expose dedicated begin/commit/
    // rollback message types — only `exec`. Issuing multi-
    // statement SQL via separate `exec` calls serializes them
    // through the worker's single-threaded message queue, which
    // gives us the same guarantee the OO1 Node handle relies on.
    await this.exec('BEGIN');
    try {
      const result = await fn();
      await this.exec('COMMIT');
      return result;
    } catch (error) {
      try {
        await this.exec('ROLLBACK');
      } catch {
        /* ignore — rethrow the original error */
      }
      throw error;
    }
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    // Fire-and-forget: the worker side closes the DB; the
    // promiser clears its cached `dbId` on the close response.
    // We don't await because `close()` is sync in the DbHandle
    // interface, but we still want the message posted before the
    // next event-loop turn so a subsequent open in the same tick
    // doesn't race.
    void this.promiser('close', {});
  }

  private assertOpen(): void {
    if (this.closed) {
      throw new Error('WorkerDbHandle: database is closed');
    }
  }
}
