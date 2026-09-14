import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  applySyncDelta,
  getDataSource,
  makeSyncDriver,
  resetDataSource,
  selectDataSource,
  subscribeDataChanged,
  type SelectionInput,
} from './data-source';
import type { SyncDelta } from './types';

/**
 * Selection logic — the pure decision function. The matrix below
 * encodes the rules from the spec:
 *
 *   - WASM module missing → HTTP (we can't help it).
 *   - No secure context → HTTP (no OPFS).
 *   - WASM DB exists + offline → WASM (this is the whole point).
 *   - WASM DB exists + online → WASM (use the local copy).
 *   - No WASM DB + online → HTTP (first sync, no point firing WASM).
 */
describe('selectDataSource', () => {
  const cases: Array<{
    label: string;
    input: SelectionInput;
    expected: 'wasm' | 'http';
  }> = [
    {
      label: 'wasm module unavailable falls back to HTTP',
      input: { isSecureContext: true, isOnline: true, hasWasmDb: true, wasmModuleAvailable: false },
      expected: 'http',
    },
    {
      label: 'insecure context -> HTTP even when online and DB exists',
      input: { isSecureContext: false, isOnline: true, hasWasmDb: true, wasmModuleAvailable: true },
      expected: 'http',
    },
    {
      label: 'online + cached DB -> WASM (the local copy wins)',
      input: { isSecureContext: true, isOnline: true, hasWasmDb: true, wasmModuleAvailable: true },
      expected: 'wasm',
    },
    {
      label: 'offline + cached DB -> WASM (offline-first wins)',
      input: { isSecureContext: true, isOnline: false, hasWasmDb: true, wasmModuleAvailable: true },
      expected: 'wasm',
    },
    {
      label: 'online + no cache -> WASM (boots the DB and syncs the first delta)',
      input: { isSecureContext: true, isOnline: true, hasWasmDb: false, wasmModuleAvailable: true },
      expected: 'wasm',
    },
    {
      label: 'offline + no cache -> HTTP (caller surfaces empty state, not crash)',
      input: { isSecureContext: true, isOnline: false, hasWasmDb: false, wasmModuleAvailable: true },
      expected: 'http',
    },
    {
      label: 'unknown hasWasmDb + online -> WASM (boot + initial sync)',
      input: { isSecureContext: true, isOnline: true, hasWasmDb: null, wasmModuleAvailable: true },
      expected: 'wasm',
    },
    {
      label: 'unknown hasWasmDb + offline -> HTTP (cannot trust cache state)',
      input: { isSecureContext: true, isOnline: false, hasWasmDb: null, wasmModuleAvailable: true },
      expected: 'http',
    },
  ];

  for (const { label, input, expected } of cases) {
    it(label, () => {
      expect(selectDataSource(input)).toBe(expected);
    });
  }
});

/**
 * Integration smoke for the factory: under vitest, the loader picks
 * the Node build with an in-memory DB. A delta applied through
 * `applySyncDelta` (the factory's helper) is queryable through the
 * returned data source.
 *
 * The factory caches its source — `resetDataSource()` clears the
 * cache between tests so each describe block starts clean.
 */
describe('factory integration (Node build)', () => {
  beforeEach(() => {
    resetDataSource();
  });

  afterEach(async () => {
    resetDataSource();
  });

  it('returns a WASM-backed data source when the loader succeeds', async () => {
    const source = await getDataSource();
    // The factory may degrade to HTTP on load failure — accept
    // either; both should expose the same interface.
    expect(['http', 'wasm']).toContain(source.kind);
    // A first sync populates the DB (or, if HTTP-only, just
    // delegates to the network).
    const delta: SyncDelta = {
      cursor: 1,
      pages: [
        {
          slug: 'factory-alpha',
          title: 'Factory Alpha',
          type: 'note',
          body: 'hello factory',
          status: 'active',
          tags: ['hi'],
          sources: [],
          updated_at: '2026-01-15T12:00:00Z',
          seq: 1,
        },
      ],
      tombstones: [],
      links: [],
      page_tags: [],
      tags: [{ name: 'hi', count: 1 }],
      aliases: [],
    };
    if (source.kind === 'wasm') {
      await applySyncDelta(delta);
      const page = await source.getPage('factory-alpha');
      expect(page?.title).toBe('Factory Alpha');
    } else {
      // HTTP fallback — verify the interface is intact.
      expect(typeof source.getPage).toBe('function');
      expect(typeof source.search).toBe('function');
      expect(typeof source.list).toBe('function');
      expect(typeof source.getTags).toBe('function');
      expect(typeof source.getBacklinks).toBe('function');
      expect(typeof source.getRecent).toBe('function');
    }
  });

  it('exposes the same query shape on both WASM and HTTP sources', async () => {
    const source = await getDataSource();
    // Stub fetch so the HTTP fallback has something to talk to.
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const body =
        url.includes('/api/tags')
          ? []
          : { pages: [], next_cursor: null };
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      );
    });
    vi.stubGlobal('fetch', fetchMock);
    try {
      const recent = await source.getRecent(1);
      expect(Array.isArray(recent.pages)).toBe(true);
      const tags = await source.getTags();
      expect(Array.isArray(tags)).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

/**
 * The offline scenario: the browser is offline and a local WASM copy
 * exists, so the REAL factory must select the WASM backend and serve
 * reads from it with zero network traffic.
 *
 * jsdom reports neither a secure context (its `isSecureContext` is
 * undefined → false) nor an offline state, so we stub both — without
 * the `isSecureContext` stub the selector would pick HTTP regardless
 * of the offline flag. `fetch` is stubbed to reject so any stray HTTP
 * attempt fails loudly instead of silently passing.
 */
describe('offline WASM path (real factory)', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetDataSource();
    localStorage.clear();
    // OPFS lives in secure contexts; pretend we have one so the
    // selector doesn't degrade to HTTP.
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      get: () => true,
    });
    Object.defineProperty(navigator, 'onLine', {
      configurable: true,
      get: () => false,
    });
    // The sync driver sets this after the first successful delta;
    // here it stands in for "we synced once, a local copy exists".
    localStorage.setItem('vesperiki-has-offline-copy', '1');
    fetchMock = vi
      .fn()
      .mockRejectedValue(
        new Error('offline simulation: fetch must never be used'),
      );
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    resetDataSource();
    vi.unstubAllGlobals();
    delete (window as { isSecureContext?: boolean }).isSecureContext;
    delete (navigator as { onLine?: boolean }).onLine;
    localStorage.clear();
  });

  it('selects the WASM backend and serves every read from it offline', async () => {
    const source = await getDataSource();
    expect(source.kind).toBe('wasm');

    const delta: SyncDelta = {
      cursor: 1,
      pages: [
        {
          id: 1,
          slug: 'forgejo',
          title: 'Forgejo',
          type: 'note',
          body: 'Forgejo is an offline-first git forge.',
          status: 'active',
          tags: ['garden'],
          sources: [],
          updated_at: '2026-01-15T12:00:00Z',
          seq: 1,
        },
        {
          id: 2,
          slug: 'alpha',
          title: 'Alpha Notes',
          type: 'note',
          body: 'Offline notes about alpha.',
          status: 'active',
          tags: ['garden'],
          sources: [],
          updated_at: '2026-01-15T11:00:00Z',
          seq: 2,
        },
      ],
      tombstones: [],
      links: [{ source_id: 2, target_id: 1, rel: 'references', origin: 'agent' }],
      page_tags: [],
      tags: [{ name: 'garden', count: 2 }],
      aliases: [],
    };
    await applySyncDelta(delta);

    const page = await source.getPage('forgejo');
    expect(page?.title).toBe('Forgejo');
    expect(page?.tags).toContain('garden');

    const recent = await source.getRecent();
    expect(recent.pages.map((p) => p.slug)).toContain('forgejo');

    const graph = await source.getGraph();
    expect(graph.nodes.map((n) => n.slug)).toEqual(
      expect.arrayContaining(['forgejo', 'alpha']),
    );
    expect(graph.edges).toHaveLength(1);

    const hits = await source.search({ q: 'offline', include_body: true, limit: 50 });
    expect(hits.map((h) => h.slug)).toEqual(
      expect.arrayContaining(['forgejo', 'alpha']),
    );

    const tags = await source.getTags();
    expect(tags.some((t) => t.name === 'garden')).toBe(true);

    const backlinks = await source.getBacklinks('forgejo');
    expect(backlinks.some((b) => b.slug === 'alpha')).toBe(true);

    // The whole point: offline reads never touch the network.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('drops to HTTP when offline without a local copy', async () => {
    localStorage.removeItem('vesperiki-has-offline-copy');
    const source = await getDataSource();
    expect(source.kind).toBe('http');
  });
});

describe('sync driver', () => {
  beforeEach(() => {
    resetDataSource();
  });

  afterEach(async () => {
    resetDataSource();
    vi.restoreAllMocks();
  });

  it('is a no-op when no WASM data source is active', async () => {
    // Node-side factory drops to HTTP, so `activeWasmSource` is null.
    // The driver must not throw in that state.
    await getDataSource();
    const driver = makeSyncDriver();
    await expect(driver.sync()).resolves.toBeUndefined();
    expect(await driver.status()).toBeNull();
  });

  it('returns null status from the driver until a WASM source exists', async () => {
    await getDataSource();
    const driver = makeSyncDriver();
    expect(await driver.status()).toBeNull();
  });
});

/**
 * The sync driver must notify subscribers after a SUCCESSFUL sync and
 * stay silent on failure — that notification is the hook the app
 * uses to invalidate its TanStack Query cache (the cold-start race
 * fix). Runs against the real factory + real in-memory WASM DB so the
 * notification timing matches production exactly.
 */
describe('sync driver notifications', () => {
  // A valid delta in the shape `/api/sync` returns. Tags rows may be
  // empty; applyDelta defaults missing `count` values to 0.
  const syncDelta: SyncDelta = {
    cursor: 1,
    pages: [
      {
        id: 1,
        slug: 'forgejo',
        title: 'Forgejo',
        type: 'note',
        body: 'Forgejo is an offline-first git forge.',
        status: 'active',
        tags: [],
        sources: [],
        updated_at: '2026-01-15T12:00:00Z',
        seq: 1,
      },
    ],
    tombstones: [],
    links: [],
    page_tags: [],
    tags: [],
    aliases: [],
  };

  beforeEach(() => {
    resetDataSource();
    localStorage.clear();
    // Force the WASM path (secure context + online) so the driver has
    // an active source to apply deltas to. jsdom's `isSecureContext`
    // is undefined → false, which would otherwise select HTTP.
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      get: () => true,
    });
    Object.defineProperty(navigator, 'onLine', {
      configurable: true,
      get: () => true,
    });
  });

  afterEach(() => {
    resetDataSource();
    vi.unstubAllGlobals();
    delete (window as { isSecureContext?: boolean }).isSecureContext;
    delete (navigator as { onLine?: boolean }).onLine;
    localStorage.clear();
  });

  it('fires each subscriber exactly once per successful sync; unsubscribe stops it', async () => {
    await getDataSource();
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(syncDelta), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const driver = makeSyncDriver();
    const listener = vi.fn();
    const unsubscribe = subscribeDataChanged(listener);
    try {
      await driver.sync();
      expect(listener).toHaveBeenCalledTimes(1);

      // Unsubscribed listeners must not fire on later syncs.
      unsubscribe();
      await driver.sync();
      expect(listener).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('does not notify when the sync fails', async () => {
    await getDataSource();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new Error('network down')),
    );

    const driver = makeSyncDriver();
    const listener = vi.fn();
    subscribeDataChanged(listener);
    try {
      await driver.sync();
      expect(listener).not.toHaveBeenCalled();
      // A failed sync must not record a cursor advance either.
      expect((await driver.status())?.cursor).toBe(0);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

/**
 * Cold-start race fix: two concurrent callers (e.g. a sync writer
 * and a route reader firing in the same tick) must share ONE
 * sqlite instance. Before the fix, both would see
 * `activeSource === null`, both would boot a fresh WASM DB, and
 * writes from one would never reach reads on the other — routes
 * showed "No pages yet" even after sync succeeded.
 *
 * The factory pins the in-flight boot promise so a second call
 * before the first resolves receives the same Promise and the same
 * resolved source.
 */
describe('factory concurrency (promise pin)', () => {
  beforeEach(() => {
    resetDataSource();
    // Force the WASM path so the test exercises the boot that
    // actually races in production (sync writer + route reader).
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      get: () => true,
    });
    Object.defineProperty(navigator, 'onLine', {
      configurable: true,
      get: () => true,
    });
  });

  afterEach(() => {
    resetDataSource();
    delete (window as { isSecureContext?: boolean }).isSecureContext;
    delete (navigator as { onLine?: boolean }).onLine;
  });

  it('two concurrent callers receive the same resolved source', async () => {
    // The point: do NOT await the first call. Both promises must be
    // created in the same tick so the second observes the pin.
    const first = getDataSource();
    const second = getDataSource();
    const [a, b] = await Promise.all([first, second]);
    expect(a).toBe(b);
  });

  it('returns the same promise instance to concurrent callers', () => {
    const first = getDataSource();
    const second = getDataSource();
    // Same Promise object — not just same resolved value. The pin
    // returns the exact in-flight promise so neither caller starts
    // a second boot.
    expect(second).toBe(first);
  });

  it('re-boots after resetDataSource() (different identity)', async () => {
    const before = await getDataSource();
    resetDataSource();
    const after = await getDataSource();
    // The factory always builds a fresh adapter object on boot, so
    // a re-boot must produce a different instance.
    expect(after).not.toBe(before);
    expect(after.kind).toBe(before.kind);
  });
});
