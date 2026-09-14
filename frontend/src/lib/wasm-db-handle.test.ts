import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { loadDbHandle } from './wasm-db-handle';

/**
 * Loader tests for the browser (worker-promiser OPFS) path of
 * `loadDbHandle`.
 *
 * The @sqlite.org/sqlite-wasm module is mocked so we can drive the
 * worker promiser and assert which message types the loader sends
 * and what envelope it returns. The production loader opens the
 * persistent DB inside a Web Worker via the callable
 * `sqlite3Worker1Promiser({})` factory and falls back to the
 * in-memory OO1 path on the main thread when any precondition
 * fails. A legacy `{ v2: factory }` export shape is also covered:
 *
 *   - `crossOriginIsolated` is false (COOP/COEP not set).
 *   - The File System Access API constructor globals are missing
 *     (`FileSystemHandle`, `FileSystemDirectoryHandle`, or
 *     `FileSystemFileHandle`).
 *   - `Atomics.waitAsync` is missing (the sqlite-wasm OPFS proxy
 *     needs it).
 *   - The worker factory rejects.
 *   - The worker opens the DB as non-persistent (returns
 *     `persistent: false`).
 *
 * `createSyncAccessHandle` is intentionally not a main-thread
 * precondition: browsers expose it only in dedicated workers, where
 * sqlite-wasm performs its own VFS installation feature check.
 *
 * The happy-path tests stub `process` away so vitest's jsdom env
 * routes the loader to the browser path, and stub
 * `navigator.storage.getDirectory` so `detectOpfs()` reports OPFS
 * as available. The gating tests flip one of the
 * `vfsInstallationFeatureCheck` prerequisites and assert the
 * in-memory fallback.
 */

const h = vi.hoisted(() => {
  // In-memory OO1 fallback constructor — only invoked on the
  // browser path when `detectOpfs()` returns false (the gating
  // tests) or when `tryOpenWorkerDb` returns null. The worker
  // happy path never calls it.
  const calls: Array<{ name: string; filename: string }> = [];
  const makeCtor = (name: string) =>
    function (this: unknown, filename: string) {
      calls.push({ name, filename });
      return { close() {} };
    } as unknown as new (filename: string) => unknown;

  // Worker promiser fake. `workerMessages` records every call
  // the loader makes through the promiser so tests can assert
  // message types and args. The fake promiser returns the
  // shape documented in
  // node_modules/@sqlite.org/sqlite-wasm/dist/index.mjs ("Worker
  // API #1"): a `{type, messageId, dbId, result}` envelope where
  // `result` for `open` is `{filename, persistent, dbId, vfs}`.
  const workerMessages: Array<{ type: string; args: unknown }> = [];
  const state = {
    /**
     * What the factory and the `open` envelope do:
     *   - 'succeed-persistent' (default): the factory succeeds,
     *     open returns persistent:true → loader reports browser-opfs.
     *   - 'succeed-memory': the factory succeeds, open returns
     *     persistent:false → loader degrades to browser-memory.
     *   - 'reject': the factory rejects → loader degrades to
     *     browser-memory (worker load failure).
     */
    workerBehavior: 'succeed-persistent' as
      | 'succeed-persistent'
      | 'succeed-memory'
      | 'reject',
    factoryShape: 'callable' as 'callable' | 'legacy-v2',
  };
  const fakePromiserFn = vi.fn(async (type: string, args: unknown) => {
    workerMessages.push({ type, args });
    if (type === 'open') {
      const persistent = state.workerBehavior !== 'succeed-memory';
      return {
        type: 'open',
        result: {
          filename: 'vesperiki.db',
          persistent,
          dbId: 'db1',
          vfs: persistent ? 'opfs' : 'memory',
        },
      };
    }
    if (type === 'exec') {
      // The WorkerDbHandle's `select` reads `result.resultRows`;
      // an empty array is the no-rows result.
      return { type: 'exec', result: { resultRows: [] } };
    }
    if (type === 'close') {
      return { type: 'close', result: {} };
    }
    return { type, result: {} };
  });

  // The current runtime shape is callable and can return the
  // promiser directly. The legacy `.v2` mock returns a Promise,
  // exercising both factory result forms as well as both export
  // shapes.
  const fakeCallableFactory = vi.fn(() => {
    if (state.workerBehavior === 'reject') {
      throw new Error('worker load failure');
    }
    return fakePromiserFn;
  });
  const fakeLegacyV2Factory = vi.fn(async () => {
    if (state.workerBehavior === 'reject') {
      throw new Error('worker load failure');
    }
    return fakePromiserFn;
  });

  return {
    calls,
    makeCtor,
    workerMessages,
    fakePromiserFn,
    fakeCallableFactory,
    fakeLegacyV2Factory,
    state,
  };
});

vi.mock('@sqlite.org/sqlite-wasm', () => ({
  default: vi.fn(async () => ({
    oo1: { DB: h.makeCtor('DB') },
  })),
  // Use a getter so each test can expose exactly one runtime shape.
  // The happy path defaults to sqlite-wasm 3.53's callable export;
  // a focused regression test switches to the legacy `.v2` object.
  get sqlite3Worker1Promiser() {
    return h.state.factoryShape === 'callable'
      ? h.fakeCallableFactory
      : { v2: h.fakeLegacyV2Factory };
  },
}));

describe('loadDbHandle browser worker path', () => {
  beforeEach(() => {
    // Force the browser path (vitest's jsdom env normally exposes
    // Node's `process`, which would send the loader to loadNodeDb).
    vi.stubGlobal('process', undefined);
    // OPFS needs a cross-origin isolated document (COOP/COEP); jsdom
    // leaves crossOriginIsolated undefined, so pretend we're isolated.
    vi.stubGlobal('crossOriginIsolated', true);
    // Report OPFS as available so the worker path (not the
    // detectOpfs probe) drives the outcome.
    Object.defineProperty(navigator, 'storage', {
      configurable: true,
      value: { getDirectory: vi.fn().mockResolvedValue({}) },
    });
    // jsdom has none of the File System Access API constructor globals
    // that the main thread can probe, so stub them. Deliberately leave
    // createSyncAccessHandle absent: browsers expose it only inside
    // dedicated workers, where sqlite-wasm checks it while installing
    // the VFS.
    vi.stubGlobal('FileSystemHandle', class FileSystemHandle {});
    vi.stubGlobal('FileSystemDirectoryHandle', class FileSystemDirectoryHandle {});
    vi.stubGlobal('FileSystemFileHandle', class FileSystemFileHandle {});
    h.calls.length = 0;
    h.workerMessages.length = 0;
    h.fakePromiserFn.mockClear();
    h.fakeCallableFactory.mockClear();
    h.fakeLegacyV2Factory.mockClear();
    h.state.workerBehavior = 'succeed-persistent';
    h.state.factoryShape = 'callable';
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete (navigator as { storage?: unknown }).storage;
    vi.restoreAllMocks();
  });

  it('opens the DB in a worker and reports browser-opfs', async () => {
    const loaded = await loadDbHandle();
    expect(loaded.kind).toBe('browser-opfs');
    expect(loaded.persisted).toBe(true);
    // The loader must have posted an `open` with the default
    // filename and an explicit `vfs: 'opfs'` so the worker
    // installs its OPFS VFS on the file (in this build the
    // worker defaults to `unix-none` when `vfs` is omitted, so
    // without this the open would silently fall back to a
    // non-persistent DB and the loader would report
    // `browser-memory`).
    const openMsg = h.workerMessages.find((m) => m.type === 'open');
    expect(openMsg).toBeDefined();
    expect(openMsg?.args).toEqual({ filename: 'vesperiki.db', vfs: 'opfs' });
    expect(h.fakeCallableFactory).toHaveBeenCalledWith({});
    expect(h.fakeLegacyV2Factory).not.toHaveBeenCalled();
    // The in-memory OO1 fallback must NOT have been called — the
    // worker path owns the DB now.
    expect(h.calls).toEqual([]);
  });

  it('opens through the legacy factory.v2 shape', async () => {
    h.state.factoryShape = 'legacy-v2';

    const loaded = await loadDbHandle();

    expect(loaded.kind).toBe('browser-opfs');
    expect(loaded.persisted).toBe(true);
    expect(h.fakeLegacyV2Factory).toHaveBeenCalledWith({});
    expect(h.fakeCallableFactory).not.toHaveBeenCalled();
    const openMsg = h.workerMessages.find((message) => message.type === 'open');
    expect(openMsg?.args).toEqual({ filename: 'vesperiki.db', vfs: 'opfs' });
    expect(h.calls).toEqual([]);
  });

  it('degrades to in-memory when the callable factory opens a non-persistent DB', async () => {
    // The worker is healthy but the OPFS VFS install fell through
    // to ':memory:'. The loader must close the handle and report
    // the in-memory fallback rather than lying about persistence.
    h.state.workerBehavior = 'succeed-memory';
    const loaded = await loadDbHandle();
    expect(loaded.kind).toBe('browser-memory');
    expect(loaded.persisted).toBe(false);
    expect(h.fakeCallableFactory).toHaveBeenCalledWith({});
    // The loader must have issued a `close` to avoid leaking the
    // half-opened worker handle.
    expect(h.workerMessages.some((m) => m.type === 'close')).toBe(true);
    // The in-memory OO1 fallback must have been instantiated.
    expect(h.calls).toEqual([{ name: 'DB', filename: ':memory:' }]);
  });

  it('degrades to in-memory when the legacy v2 factory rejects', async () => {
    // A v2() rejection has the same shape as a 404 on the worker
    // bundle or a crash before `worker1-ready`. The loader must NOT
    // hang (it races v2() against a 10s boot timeout) and must NOT
    // claim persistence.
    h.state.factoryShape = 'legacy-v2';
    h.state.workerBehavior = 'reject';
    const loaded = await loadDbHandle();
    expect(loaded.kind).toBe('browser-memory');
    expect(loaded.persisted).toBe(false);
    expect(h.fakeLegacyV2Factory).toHaveBeenCalledWith({});
    expect(h.calls).toEqual([{ name: 'DB', filename: ':memory:' }]);
  });

  it('ignores OPFS when the document is not cross-origin isolated', async () => {
    // Without COOP/COEP the document is not cross-origin isolated
    // and sqlite-wasm's OPFS VFS cannot boot, so detectOpfs()
    // must report OPFS as unavailable even though the worker
    // promiser and the storage API are present — the loader
    // degrades to :memory: instead of attempting (and hanging on)
    // the OPFS open.
    vi.stubGlobal('crossOriginIsolated', false);
    const loaded = await loadDbHandle();
    expect(loaded.kind).toBe('browser-memory');
    expect(loaded.persisted).toBe(false);
    expect(h.calls).toEqual([{ name: 'DB', filename: ':memory:' }]);
  });

  it('ignores OPFS when Atomics.waitAsync is missing', async () => {
    // The sqlite-wasm OPFS proxy needs Atomics.waitAsync on top of
    // SharedArrayBuffer + Atomics. Some browsers report
    // crossOriginIsolated=true yet still lack waitAsync (observed
    // in Firefox 135 / Camofox); there the VFS install fails
    // silently and the worker can't open the persistent DB, so
    // detectOpfs() must report OPFS as unavailable and the loader
    // must degrade to :memory: — never reaching the worker.
    vi.stubGlobal('Atomics', { waitAsync: undefined });
    const loaded = await loadDbHandle();
    expect(loaded.kind).toBe('browser-memory');
    expect(loaded.persisted).toBe(false);
    expect(h.calls).toEqual([{ name: 'DB', filename: ':memory:' }]);
  });

  it('attempts worker OPFS when createSyncAccessHandle is absent on the main thread', async () => {
    // createSyncAccessHandle is worker-only, so its absence here must
    // not prevent sqlite-wasm's worker from performing the definitive
    // VFS feature check and reporting whether the open is persistent.
    const fileHandlePrototype = globalThis.FileSystemFileHandle.prototype as FileSystemFileHandle & {
      createSyncAccessHandle?: unknown;
    };
    expect(typeof fileHandlePrototype.createSyncAccessHandle).toBe('undefined');
    const loaded = await loadDbHandle();
    expect(loaded.kind).toBe('browser-opfs');
    expect(loaded.persisted).toBe(true);
    const openMsg = h.workerMessages.find((message) => message.type === 'open');
    expect(openMsg?.args).toEqual({ filename: 'vesperiki.db', vfs: 'opfs' });
    expect(h.calls).toEqual([]);
  });
});
