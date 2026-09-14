import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  isPinned,
  mediaReferencesIn,
  PIN_STORAGE_KEY,
  prefetchMedia,
  readPinnedSlugs,
  togglePin,
  writePinnedSlugs,
} from './pin';

/**
 * `pin.test.ts` — covers the localStorage + media-prefetch helpers
 * used by the page reader. The browser-only Cache API is stubbed
 * via a minimal `caches` shim that records `put()` calls.
 */

function clearStorage() {
  localStorage.clear();
}

beforeEach(() => {
  clearStorage();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('read/write pinned slugs', () => {
  it('returns an empty list when nothing has been pinned', () => {
    expect(readPinnedSlugs()).toEqual([]);
  });

  it('round-trips a list through localStorage', () => {
    writePinnedSlugs(['alpha', 'beta']);
    expect(readPinnedSlugs()).toEqual(['alpha', 'beta']);
  });

  it('survives corrupted localStorage payloads without throwing', () => {
    localStorage.setItem(PIN_STORAGE_KEY, '{not valid json');
    expect(readPinnedSlugs()).toEqual([]);
  });

  it('filters non-string entries out of the persisted list', () => {
    localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(['alpha', 42, null]));
    expect(readPinnedSlugs()).toEqual(['alpha']);
  });
});

describe('togglePin / isPinned', () => {
  it('adds an unpinned slug and removes a pinned slug', () => {
    expect(isPinned('alpha')).toBe(false);
    expect(togglePin('alpha').pinned).toBe(true);
    expect(isPinned('alpha')).toBe(true);
    expect(togglePin('alpha').pinned).toBe(false);
    expect(isPinned('alpha')).toBe(false);
  });

  it('records the slug in pin order (newest last) for consistent UI render', () => {
    togglePin('alpha');
    togglePin('beta');
    expect(readPinnedSlugs()).toEqual(['alpha', 'beta']);
  });
});

describe('mediaReferencesIn', () => {
  it('extracts distinct media ids from markdown image references', () => {
    const body = [
      '![first](/api/media/3)',
      '![second](/api/media/8)',
      '![dup](/api/media/3)',
      '![absolute](https://example.com/x.png)',
    ].join('\n\n');
    expect(mediaReferencesIn(body).sort((a, b) => a - b)).toEqual([3, 8]);
  });

  it('returns an empty list for bodies with no media references', () => {
    expect(mediaReferencesIn('Just text, no images.')).toEqual([]);
  });

  it('ignores non-numeric references', () => {
    expect(mediaReferencesIn('![oops](/api/media/notanint)')).toEqual([]);
  });
});

describe('prefetchMedia (Cache API stub)', () => {
  it('caches each media URL via fetch + cache.put, returning the success count', async () => {
    const calls: Array<{ url: string }> = [];
    const cache = {
      async put(req: Request | string, _res: Response) {
        calls.push({ url: String(req) });
      },
    };
    const cacheStore = new Map<string, unknown>();
    cacheStore.set('vesperiki-media-v1', cache);
    (globalThis as { caches?: { open: (n: string) => Promise<unknown> } }).caches = {
      open: async () => cache,
    };
    const fetchMock = vi.fn().mockImplementation((url: string) =>
      Promise.resolve(
        new Response('binary', { status: 200, headers: { 'content-type': 'image/png' } }),
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const ok = await prefetchMedia([1, 2]);
    expect(ok).toBe(2);
    expect(calls.map((c) => c.url)).toEqual(['/api/media/1', '/api/media/2']);
  });

  it('does not throw when the cache API is missing', async () => {
    (globalThis as { caches?: unknown }).caches = undefined;
    const ok = await prefetchMedia([1]);
    expect(ok).toBe(0);
  });

  it('skips URLs whose fetch fails (offline) and continues with the rest', async () => {
    const cache = {
      async put(_req: Request | string, _res: Response) {
        /* noop */
      },
    };
    (globalThis as { caches?: { open: (n: string) => Promise<unknown> } }).caches = {
      open: async () => cache,
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).endsWith('/2')) return Promise.reject(new Error('offline'));
      return Promise.resolve(
        new Response('ok', { status: 200, headers: { 'content-type': 'image/png' } }),
      );
    });
    vi.stubGlobal('fetch', fetchMock);
    const ok = await prefetchMedia([1, 2]);
    expect(ok).toBe(1);
  });
});
