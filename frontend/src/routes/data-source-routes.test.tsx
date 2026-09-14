import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
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
import type { Graph, Page, SearchHit } from '../lib/types';

// Mock the DataSource factory BEFORE importing the route components so
// every queryFn resolves through a fake data source instead of the
// real factory (whose HTTP path would hit the network). The fake is
// replaced per test with fixtures, and `fetch` is stubbed so a route
// that sneaks past the abstraction (calling raw fetch) fails loudly.
vi.mock('../lib/data-source', () => ({
  getDataSource: vi.fn(),
  resetDataSource: vi.fn(),
}));

// Route modules are only used for `Route.useParams()`; stub them like
// p.$slug.test.tsx / graph.test.tsx do so each route component can
// read its fixture params without a full file-based router.
vi.mock('./p.$slug', () => ({
  Route: { useParams: () => ({ slug: 'forgejo' }) },
}));
vi.mock('./graph', () => ({
  Route: { useParams: () => ({}) },
}));
vi.mock('./tags.$tag', () => ({
  Route: { useParams: () => ({ tag: 'garden' }) },
}));

import { getDataSource } from '../lib/data-source';
import { GraphPage } from './graph.lazy';
import { HomePage } from './index.lazy';
import { PageReader } from './p.$slug.lazy';
import { SearchPage } from './search.lazy';
import { TagPage } from './tags.$tag.lazy';
import { TagsPage } from './tags.lazy';

const fixturePage: Page = {
  slug: 'forgejo',
  title: 'Forgejo',
  type: 'note',
  tags: ['garden'],
  sources: [],
  body: '# Forgejo\n\nAn offline-first forge.',
  status: 'active',
  updated_at: '2026-01-15T12:00:00Z',
};

const fixtureHit: SearchHit = {
  slug: 'forgejo',
  title: 'Forgejo',
  type: 'note',
  snippet: 'An offline-first forge.',
  score: 10,
};

const fixtureGraph: Graph = {
  nodes: [
    { id: 1, slug: 'forgejo', title: 'Forgejo', type: 'note' },
    { id: 2, slug: 'alpha', title: 'Alpha', type: 'note' },
  ],
  edges: [{ source: 1, target: 2, rel: 'references', origin: 'agent' }],
};

/** A fake DataSource whose query methods are spies returning fixtures. */
function makeFakeDataSource() {
  return {
    kind: 'wasm' as const,
    getPage: vi.fn(),
    search: vi.fn(),
    list: vi.fn(),
    getRecent: vi.fn(),
    getTags: vi.fn(),
    getBacklinks: vi.fn(),
    getGraph: vi.fn(),
  };
}

let fakeDs: ReturnType<typeof makeFakeDataSource>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fakeDs = makeFakeDataSource();
  vi.mocked(getDataSource).mockResolvedValue(fakeDs);
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function withRouter(ui: ReactNode, initialEntries: string[] = ['/']) {
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
    history: createMemoryHistory({ initialEntries }),
  });
  await router.load();
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

// SearchPage reads the query from the URL via `useSearch({from:
// '/search'})`, so the synthetic router needs a real `/search` route
// with the same validateSearch the file route uses.
async function withSearchRouter() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const rootRoute = createRootRoute({
    component: () => <Outlet />,
  });
  const searchRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/search',
    component: () => <SearchPage />,
    validateSearch: (search: Record<string, unknown>) => ({
      q: typeof search.q === 'string' ? search.q : '',
    }),
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([searchRoute]),
    history: createMemoryHistory({ initialEntries: ['/search?q=offline'] }),
  });
  await router.load();
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe('routes read through getDataSource (never raw fetch)', () => {
  it('HomePage: getRecent comes from the data source', async () => {
    fakeDs.getRecent.mockResolvedValue({ pages: [fixturePage], next_cursor: null });

    await withRouter(<HomePage />);

    expect(
      await screen.findByRole('heading', { level: 2, name: 'Forgejo' }),
    ).toBeInTheDocument();
    expect(getDataSource).toHaveBeenCalled();
    expect(fakeDs.getRecent).toHaveBeenCalledWith();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('SearchPage: search + deprecated-pages list come from the data source', async () => {
    fakeDs.search.mockResolvedValue([fixtureHit]);
    fakeDs.list.mockResolvedValue({ pages: [], next_cursor: null });

    await withSearchRouter();

    expect(
      await screen.findByRole('link', { name: 'Forgejo' }),
    ).toBeInTheDocument();
    expect(fakeDs.search).toHaveBeenCalledWith({
      q: 'offline',
      include_body: true,
      limit: 50,
    });
    expect(fakeDs.list).toHaveBeenCalledWith({ status: 'deprecated', limit: 500 });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('PageReader: getPage + getBacklinks come from the data source', async () => {
    fakeDs.getPage.mockResolvedValue(fixturePage);
    fakeDs.getBacklinks.mockResolvedValue([]);

    await withRouter(<PageReader />);

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Forgejo' }),
    ).toBeInTheDocument();
    expect(fakeDs.getPage).toHaveBeenCalledWith('forgejo');
    expect(fakeDs.getBacklinks).toHaveBeenCalledWith('forgejo');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('TagsPage: getTags comes from the data source', async () => {
    fakeDs.getTags.mockResolvedValue([{ name: 'garden', count: 2 }]);

    await withRouter(<TagsPage />);

    expect(await screen.findByText('#garden')).toBeInTheDocument();
    expect(fakeDs.getTags).toHaveBeenCalledWith();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('TagPage: the filtered page list comes from the data source', async () => {
    fakeDs.list.mockResolvedValue({ pages: [fixturePage], next_cursor: null });

    await withRouter(<TagPage />);

    expect(
      await screen.findByRole('heading', { level: 1, name: '#garden' }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole('link', { name: 'Forgejo' }),
    ).toBeInTheDocument();
    expect(fakeDs.list).toHaveBeenCalledWith({
      tag: 'garden',
      status: 'active',
      limit: 100,
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('GraphPage: getGraph comes from the data source', async () => {
    fakeDs.getGraph.mockResolvedValue(fixtureGraph);

    await withRouter(<GraphPage />);

    expect(await screen.findByTestId('graph-svg')).toBeInTheDocument();
    expect(screen.getByTestId('graph-node-forgejo')).toBeInTheDocument();
    expect(fakeDs.getGraph).toHaveBeenCalledWith();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
