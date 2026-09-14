import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router';
import {
  makeSyncDriver,
  resetDataSource,
  subscribeDataChanged,
} from './data-source';
import type { SyncDelta } from './types';
import { HomePage } from '../routes/index.lazy';

/**
 * Cold-start race regression test — REAL factory, REAL in-memory WASM
 * DB, REAL sync driver, REAL subscription mechanism. No data-source
 * mocks: the goal is to reproduce the regression in a test.
 *
 * Scenario:
 *   1. Cold load: the home route's queryFn reads the (still empty)
 *      WASM DB and caches `{ pages: [] }` (staleTime 30s).
 *   2. The offline hook's mount sync then populates the WASM DB.
 *   3. Without a data-changed notification nothing invalidates the
 *      cached empty result → "No pages yet" forever.
 *
 * The fix under test: a successful sync notifies subscribers, and the
 * subscriber (mirroring App.tsx's module-scope wiring) invalidates
 * the QueryClient, so the home page refetches from the now-populated
 * WASM DB with no further user interaction.
 */

/** One page, the same shape `/api/sync` returns. */
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
      tags: ['garden'],
      sources: [],
      updated_at: '2026-01-15T12:00:00Z',
      seq: 1,
    },
  ],
  tombstones: [],
  links: [],
  page_tags: [],
  tags: [{ name: 'garden', count: 1 }],
  aliases: [],
};

async function withRouter(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const rootRoute = createRootRoute({
    component: () => <Outlet />,
  });
  const route = createRoute({
    getParentRoute: () => rootRoute,
    path: '/',
    component: () => <>{ui}</>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([route]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  });
  await router.load();
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return client;
}

describe('sync → query invalidation (cold-start race)', () => {
  beforeEach(() => {
    resetDataSource();
    localStorage.clear();
    // jsdom reports no secure context by default (undefined → false),
    // which would degrade the selector to HTTP and skip the WASM path
    // entirely. navigator.onLine already defaults to true in jsdom.
    Object.defineProperty(window, 'isSecureContext', {
      configurable: true,
      get: () => true,
    });
  });

  afterEach(() => {
    resetDataSource();
    vi.unstubAllGlobals();
    delete (window as { isSecureContext?: boolean }).isSecureContext;
    localStorage.clear();
  });

  it('refetches the cached empty home query after a successful sync', async () => {
    // 1. Cold load against an empty WASM DB: the home query resolves
    //    with zero pages and renders the empty state.
    const client = await withRouter(<HomePage />);
    expect(
      await screen.findByText('No pages yet. Your knowledge garden is ready.'),
    ).toBeInTheDocument();

    // 2. Sync (what useOfflineStatus's mount boot does) populates the
    //    WASM DB. Mirror App.tsx: a data-changed listener invalidates
    //    the QueryClient, wired BEFORE the sync runs.
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(syncDelta), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const driver = makeSyncDriver();
    const unsubscribe = subscribeDataChanged(() => {
      void client.invalidateQueries();
    });
    try {
      await act(async () => {
        await driver.sync();
      });

      // 3. Without any further user interaction the home page
      //    refetches and renders the page that just landed in the DB.
      expect(
        await screen.findByRole('heading', { level: 2, name: 'Forgejo' }),
      ).toBeInTheDocument();
      // The refetch read from the WASM source, not the network:
      // fetch was only ever called for the sync itself.
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      unsubscribe();
      vi.unstubAllGlobals();
    }
  });
});
