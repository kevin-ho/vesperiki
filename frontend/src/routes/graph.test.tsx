import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
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
import type { Graph } from '../lib/types';

// Mock the DataSource factory BEFORE importing the component under test so
// the route renders synchronously with our fixtures (no fetch in
// jsdom).
vi.mock('../lib/data-source', () => ({
  getDataSource: vi.fn(),
  resetDataSource: vi.fn(),
}));

// Stub the route module so `Route.useParams()` returns an empty
// object — the graph route has no params.
vi.mock('./graph', () => ({
  Route: {
    useParams: () => ({}),
  },
}));

import { getDataSource } from '../lib/data-source';
import { GraphPage } from './graph.lazy';

const simpleGraph: Graph = {
  nodes: [
    { id: 1, slug: 'alpha', title: 'Alpha', type: 'note' },
    { id: 2, slug: 'beta', title: 'Beta', type: 'note' },
    { id: 3, slug: 'hub', title: 'Hub', type: 'note' },
  ],
  edges: [
    { source: 1, target: 3, rel: 'references', origin: 'agent' },
    { source: 2, target: 3, rel: 'depends_on', origin: 'agent' },
  ],
};

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
  fakeDs.getGraph.mockResolvedValue(simpleGraph);
  vi.mocked(getDataSource).mockResolvedValue(fakeDs);
  fetchMock = vi.fn();
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function withQueryClient(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

async function withRouter(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const rootRoute = createRootRoute({
    component: () => <Outlet />,
  });
  const homeRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/',
    component: () => <>{ui}</>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([homeRoute]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  });
  await router.load();
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe('GraphPage', () => {
  it('renders one node per page and one edge per link', async () => {
    await withRouter(<GraphPage />);

    const graphSvg = await screen.findByTestId('graph-svg');
    expect(graphSvg).not.toHaveAttribute('role');
    expect(graphSvg).not.toHaveAttribute('aria-label');
    expect(await screen.findByTestId('graph-node-alpha')).toBeInTheDocument();
    expect(await screen.findByTestId('graph-node-beta')).toBeInTheDocument();
    expect(await screen.findByTestId('graph-node-hub')).toBeInTheDocument();

    // Hub has 2 incoming edges in the fixture (degree 2), the others
    // have degree 1. Each Link produces an <a>; the SVG also
    // contains <line> for edges.
    await waitFor(() => {
      const svg = screen.getByTestId('graph-svg');
      expect(svg.querySelectorAll('line')).toHaveLength(2);
    });
    // Title labels next to each circle.
    expect(screen.getByText('Alpha')).toBeInTheDocument();
    expect(screen.getByText('Beta')).toBeInTheDocument();
    expect(screen.getByText('Hub')).toBeInTheDocument();
  });

  it('maps each graph node onto a TanStack Router <Link> pointing at /p/$slug', async () => {
    await withRouter(<GraphPage />);

    const alphaLink = await screen.findByTestId('graph-node-alpha');
    // The component wraps the circle in a TanStack <Link>. In the
    // simplest stubbed router that Link renders as an <a>, so check
    // the href ref carries the right slug.
    const anchor = alphaLink.closest('a');
    expect(anchor).not.toBeNull();
    expect(anchor?.getAttribute('href')).toContain('/p/alpha');
  });

  it('shows an empty-state message when there are no nodes', async () => {
    fakeDs.getGraph.mockResolvedValueOnce({ nodes: [], edges: [] });
    withQueryClient(<GraphPage />);
    expect(
      await screen.findByText(/The garden has no pages yet\./i),
    ).toBeInTheDocument();
  });

  it('GraphPage: getGraph comes from the data source', async () => {
    await withRouter(<GraphPage />);

    expect(await screen.findByTestId('graph-svg')).toBeInTheDocument();
    expect(screen.getByTestId('graph-node-alpha')).toBeInTheDocument();
    expect(getDataSource).toHaveBeenCalled();
    expect(fakeDs.getGraph).toHaveBeenCalledWith();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
