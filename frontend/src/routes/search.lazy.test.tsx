import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router';
import type { SearchHit } from '../lib/types';

vi.mock('../lib/data-source', () => ({
  getDataSource: vi.fn(),
  resetDataSource: vi.fn(),
}));

import { getDataSource } from '../lib/data-source';
import { SearchPage } from './search.lazy';

const hit: SearchHit = {
  slug: 'forgejo',
  title: 'Forgejo',
  type: 'note',
  snippet: 'An offline-first forge.',
  score: 10,
};

const dataSource = {
  kind: 'wasm' as const,
  getPage: vi.fn(),
  search: vi.fn(),
  list: vi.fn(),
  getRecent: vi.fn(),
  getTags: vi.fn(),
  getBacklinks: vi.fn(),
  getGraph: vi.fn(),
};

beforeEach(() => {
  dataSource.search.mockResolvedValue([hit]);
  dataSource.list.mockResolvedValue({ pages: [], next_cursor: null });
  vi.mocked(getDataSource).mockResolvedValue(dataSource);
});

afterEach(() => {
  vi.clearAllMocks();
});

async function renderSearchPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const rootRoute = createRootRoute({ component: () => <Outlet /> });
  const searchRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/search',
    component: SearchPage,
    validateSearch: (search: Record<string, unknown>) => ({
      q: typeof search.q === 'string' ? search.q : '',
    }),
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([searchRoute]),
    // Start WITH the q= search param: TanStack Router issues a 307 redirect
    // from bare /search → /search?q= (validateSearch normalizes q), and the
    // router stays mid-redirect (matches: []) unless the entry already has q.
    history: createMemoryHistory({ initialEntries: ['/search?q='] }),
  });
  await router.load();
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe('SearchPage accessibility', () => {
  it('labels the search landmark and announces the submitted result count', async () => {
    const user = userEvent.setup();
    await renderSearchPage();

    const form = screen.getByRole('search');
    await user.type(screen.getByRole('searchbox', { name: 'Search query' }), 'offline');
    await user.click(screen.getByRole('button', { name: 'Search' }));

    expect(form).toBeInTheDocument();
    expect(await screen.findByTestId('search-results-count')).toHaveTextContent(
      '1 results for "offline".',
    );
  });
});
