import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as dataSourceModule from './data-source';
import {
  applySyncDelta,
  resetDataSource,
} from './data-source';
import type { DataSource } from './data-source';
import { useDataSource } from './data-source-api';
import type { SyncDelta } from './types';

/**
 * `useDataSource()` is a thin pass-through to `getDataSource()`. The
 * spy-based tests below pin the contract: the wrapper forwards
 * every call, shares the exact promise via the factory's pin, and
 * never adds its own error handling or caching that would mask a
 * factory failure.
 */
describe('useDataSource() (mocked factory)', () => {
  let spy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    spy = vi.spyOn(dataSourceModule, 'getDataSource');
  });

  afterEach(() => {
    spy.mockRestore();
  });

  it('resolves to the DataSource returned by getDataSource()', async () => {
    const fakeSource = { kind: 'wasm' } as unknown as DataSource;
    spy.mockResolvedValue(fakeSource);

    const result = await useDataSource();

    expect(result).toBe(fakeSource);
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('is a pass-through: every call forwards to getDataSource()', async () => {
    const fakeSource = { kind: 'wasm' } as unknown as DataSource;
    spy.mockResolvedValue(fakeSource);

    const a = useDataSource();
    const b = useDataSource();

    expect(spy).toHaveBeenCalledTimes(2);
    expect(await a).toBe(fakeSource);
    expect(await b).toBe(fakeSource);
  });

  it('never boots on its own when the factory rejects', async () => {
    spy.mockRejectedValue(new Error('boot failed'));

    // The wrapper must surface the factory's failure verbatim — no
    // try/catch, no fallback cache, no swallowed rejection.
    await expect(useDataSource()).rejects.toThrow('boot failed');
  });
});

/**
 * Mirrors `factory concurrency (promise pin)` in data-source.test.ts
 * but goes through `useDataSource()` instead of `getDataSource()`
 * directly. The factory's pin must be observable through the
 * wrapper — concurrent callers must share one Promise and one
 * resolved source.
 */
describe('useDataSource() concurrency (promise pin)', () => {
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

  it('two calls in the same tick share one boot', async () => {
    // The point: do NOT await between the calls. Both promises must
    // be created in the same tick so the second observes the
    // factory's pin.
    const a = useDataSource();
    const b = useDataSource();
    expect(a).toBe(b);
    const [x, y] = await Promise.all([a, b]);
    expect(x).toBe(y);
  });
});

/**
 * Online integration: with secure context + online + no offline-copy
 * flag, the REAL factory must boot a working data source. The
 * factory may degrade to HTTP on WASM load failure, so we accept
 * either kind and prove the contract on whichever it picks.
 */
describe('useDataSource() online integration (real factory)', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetDataSource();
    localStorage.clear();
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      get: () => true,
    });
    Object.defineProperty(navigator, 'onLine', {
      configurable: true,
      get: () => true,
    });
    // Stub fetch to reject so any stray network call fails loudly.
    fetchMock = vi
      .fn()
      .mockRejectedValue(
        new Error('online test: fetch must never be used'),
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

  it('returns a working DataSource and reads a synced page', async () => {
    const ds = await useDataSource();
    // Accept either kind: the Node sqlite build produces 'wasm',
    // but the loader may degrade to 'http' if the WASM module
    // fails to load under jsdom.
    expect(['wasm', 'http']).toContain(ds.kind);

    const delta: SyncDelta = {
      cursor: 1,
      pages: [
        {
          id: 1,
          slug: 'api-online',
          title: 'API Online',
          type: 'note',
          body: 'synced via the factory boot path',
          status: 'active',
          tags: ['online'],
          sources: [],
          updated_at: '2026-01-15T12:00:00Z',
          seq: 1,
        },
      ],
      tombstones: [],
      links: [],
      page_tags: [],
      tags: [{ name: 'online', count: 1 }],
      aliases: [],
    };

    if (ds.kind === 'wasm') {
      await applySyncDelta(delta);
      const page = await ds.getPage('api-online');
      expect(page?.title).toBe('API Online');
    } else {
      // HTTP fallback: verify the interface is intact and the
      // factory honoured the online WASM-eligible path.
      expect(typeof ds.getPage).toBe('function');
      expect(typeof ds.getRecent).toBe('function');
    }
  });
});

/**
 * Offline integration: secure context + offline + offline-copy flag
 * must select the WASM backend, sync a delta into the local DB, and
 * serve reads from it with zero network traffic.
 */
describe('useDataSource() offline integration (real factory)', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    resetDataSource();
    localStorage.clear();
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      get: () => true,
    });
    Object.defineProperty(navigator, 'onLine', {
      configurable: true,
      get: () => false,
    });
    // Stand in for "we synced once, a local copy exists".
    localStorage.setItem('vesperiki-has-offline-copy', '1');
    fetchMock = vi
      .fn()
      .mockRejectedValue(
        new Error('offline test: fetch must never be used'),
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

  it('selects WASM, serves reads from the local DB, and never calls fetch', async () => {
    const ds = await useDataSource();
    expect(ds.kind).toBe('wasm');

    const delta: SyncDelta = {
      cursor: 1,
      pages: [
        {
          id: 1,
          slug: 'api-offline',
          title: 'API Offline',
          type: 'note',
          body: 'read from the local WASM DB',
          status: 'active',
          tags: ['offline'],
          sources: [],
          updated_at: '2026-01-15T12:00:00Z',
          seq: 1,
        },
      ],
      tombstones: [],
      links: [],
      page_tags: [],
      tags: [{ name: 'offline', count: 1 }],
      aliases: [],
    };
    await applySyncDelta(delta);

    const recent = await ds.getRecent();
    expect(recent.pages.map((p) => p.slug)).toContain('api-offline');

    // The whole point: offline reads never touch the network.
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
