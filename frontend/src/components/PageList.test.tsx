import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from '@tanstack/react-router';
import type { Page } from '../lib/types';
import { EmptyState, PageList } from './PageList';

const FIXED_NOW = new Date('2026-01-15T12:00:00Z').getTime();

const pageAlpha: Page = {
  slug: 'alpha',
  title: 'Alpha Notes',
  type: 'note',
  tags: ['garden', 'core'],
  sources: [],
  body: 'Body for alpha.',
  status: 'published',
  updated_at: new Date(FIXED_NOW - 60_000).toISOString(),
};

const pageBeta: Page = {
  slug: 'beta',
  title: 'Beta Rambles',
  type: 'essay',
  tags: ['thoughts'],
  sources: [],
  body: 'Body for beta.',
  status: 'published',
  updated_at: new Date(FIXED_NOW - 5 * 60_000).toISOString(),
};

async function withRouter(ui: ReactNode) {
  const rootRoute = createRootRoute({
    component: () => (
      <>
        <Outlet />
      </>
    ),
  });
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/',
    component: () => <>{ui}</>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  });
  await router.load();
  return render(<RouterProvider router={router} />);
}

beforeAll(() => {
  vi.setSystemTime(new Date(FIXED_NOW));
});

afterAll(() => {
  vi.useRealTimers();
});

describe('PageList', () => {
  it('renders one card per page with title, slug, type, tag chips, and a relative-time badge', async () => {
    await withRouter(<PageList pages={[pageAlpha, pageBeta]} />);

    expect(await screen.findByText('Alpha Notes')).toBeInTheDocument();
    expect(await screen.findByText('Beta Rambles')).toBeInTheDocument();
    expect(screen.getByText('/alpha')).toBeInTheDocument();
    expect(screen.getByText('/beta')).toBeInTheDocument();
    expect(screen.getByText('note')).toBeInTheDocument();
    expect(screen.getByText('essay')).toBeInTheDocument();
    expect(screen.getByText('#garden')).toBeInTheDocument();
    expect(screen.getByText('#core')).toBeInTheDocument();
    expect(screen.getByText('#thoughts')).toBeInTheDocument();
    expect(screen.getByText('1m ago')).toBeInTheDocument();
    expect(screen.getByText('5m ago')).toBeInTheDocument();
  });

  it('renders EmptyState when given an empty array', async () => {
    await withRouter(<PageList pages={[]} />);
    expect(
      await screen.findByText('No pages yet. Your knowledge garden is ready.'),
    ).toBeInTheDocument();
  });
});

describe('EmptyState', () => {
  it('renders its message verbatim', async () => {
    await withRouter(<EmptyState message="Custom empty message." />);
    expect(await screen.findByText('Custom empty message.')).toBeInTheDocument();
  });
});