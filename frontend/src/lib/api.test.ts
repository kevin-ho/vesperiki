import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  createCorrection,
  filterDeprecatedHits,
  getGraph,
  getPageBySlug,
  getRecent,
  listCorrections,
  listPages,
  resolveCorrection,
  search,
} from './api';
import { ApiError } from './types';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('listPages', () => {
  it('builds the /api/pages URL with the requested params and parses JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ pages: [], next_cursor: null }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const result = await listPages({ limit: 20, tag: 'alpha' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/pages?limit=20&tag=alpha');
    expect(init.headers).toMatchObject({ accept: 'application/json' });
    expect(result).toEqual({ pages: [], next_cursor: null });
  });
});

describe('getPageBySlug', () => {
  it('encodes the slug segment of /api/pages/:slug', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ slug: 'my notes', title: 'My notes' }));
    vi.stubGlobal('fetch', fetchMock);

    const result = await getPageBySlug('my notes');

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/pages/my%20notes');
    expect(result.slug).toBe('my notes');
  });
});

describe('getRecent', () => {
  it('defaults to the home-page limit of 20, scopes to active pages, and includes the body snippet', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ pages: [], next_cursor: null }));
    vi.stubGlobal('fetch', fetchMock);

    await getRecent();

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      '/api/pages?limit=20&status=active&include_body=true&order=updated',
    );
  });

  it('honours an explicit limit', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ pages: [], next_cursor: null }));
    vi.stubGlobal('fetch', fetchMock);

    await getRecent(5);

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/pages?limit=5&status=active&include_body=true&order=updated');
  });
});

describe('search', () => {
  it('omits empty string params from the query string', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await search({ q: 'foo', tag: '', include_body: false, limit: 10 });

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/search?q=foo&include_body=false&limit=10');
  });
});

describe('filterDeprecatedHits', () => {
  it('returns a new array with deprecated slugs removed', () => {
    const hits = [
      { slug: 'keep' },
      { slug: 'old' },
      { slug: 'keep-too' },
    ];
    const deprecated = new Set(['old']);
    const result = filterDeprecatedHits(hits, deprecated);
    expect(result.map((h) => h.slug)).toEqual(['keep', 'keep-too']);
    // The input array is not mutated.
    expect(hits.map((h) => h.slug)).toEqual(['keep', 'old', 'keep-too']);
  });

  it('returns a shallow copy when the deprecated set is undefined', () => {
    const hits = [{ slug: 'a' }, { slug: 'b' }];
    const result = filterDeprecatedHits(hits, undefined);
    expect(result).toEqual(hits);
    expect(result).not.toBe(hits);
  });

  it('returns an empty array when every hit is deprecated', () => {
    const hits = [{ slug: 'a' }, { slug: 'b' }];
    const deprecated = new Set(['a', 'b']);
    expect(filterDeprecatedHits(hits, deprecated)).toEqual([]);
  });
});

describe('error handling', () => {
  it('throws ApiError with the server payload on a 500', async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        jsonResponse({ code: 'boom', message: 'kaboom', details: { reason: 'disk' } }, 500),
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    await expect(listPages()).rejects.toBeInstanceOf(ApiError);
    await expect(listPages()).rejects.toMatchObject({
      status: 500,
      payload: { code: 'boom', message: 'kaboom', details: { reason: 'disk' } },
    });
  });
});

describe('getGraph', () => {
  it('fetches /api/graph and parses the JSON payload', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ nodes: [{ id: 1, slug: 'a', title: 'A', type: 'note' }], edges: [] }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const graph = await getGraph();

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/graph');
    expect(graph.nodes).toHaveLength(1);
  });
});

describe('listCorrections', () => {
  it('defaults the status filter to pending', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await listCorrections();

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/corrections?status=pending');
  });

  it('forwards the status filter for other values', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([]));
    vi.stubGlobal('fetch', fetchMock);

    await listCorrections('resolved');

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/corrections?status=resolved');
  });
});

describe('createCorrection', () => {
  it('POSTs JSON to /api/corrections and returns the created row', async () => {
    const row = {
      id: 1,
      page_slug: 'forgejo',
      selected_text: 'old text',
      note: 'new note',
      status: 'pending',
      created_at: '2026-01-15T12:00:00Z',
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(row));
    vi.stubGlobal('fetch', fetchMock);

    const result = await createCorrection({
      page_slug: 'forgejo',
      selected_text: 'old text',
      note: 'new note',
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/corrections');
    expect(init.method).toBe('POST');
    expect(init.headers).toMatchObject({ 'content-type': 'application/json' });
    expect(JSON.parse(init.body as string)).toEqual({
      page_slug: 'forgejo',
      selected_text: 'old text',
      note: 'new note',
    });
    expect(result).toEqual(row);
  });
});

describe('resolveCorrection', () => {
  it('PATCHes the status to /api/corrections/:id', async () => {
    const row = { id: 1, status: 'resolved', page_slug: 'forgejo', selected_text: 'x', created_at: '2026-01-15T12:00:00Z' };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(row));
    vi.stubGlobal('fetch', fetchMock);

    const result = await resolveCorrection(1, 'resolved');

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/corrections/1');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body as string)).toEqual({ status: 'resolved' });
    expect(result).toEqual(row);
  });
});
