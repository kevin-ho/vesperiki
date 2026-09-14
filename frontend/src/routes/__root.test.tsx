import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
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

// The offline-status hook is mocked so the test stays focused on
// the root layout footer rendering, not the factory logic (which
// has its own integration suite). When the mock returns a known
// status snapshot, the footer renders deterministically.
vi.mock('../lib/use-offline-status', () => ({
  useOfflineStatus: () => ({
    status: {
      ready: true,
      lastSyncAt: new Date(Date.now() - 60_000).toISOString(),
      cursor: 12,
      isOnline: true,
      syncState: 'ok',
      lastError: null,
      persisted: true,
      searchStrategy: 'fts5',
    },
    syncNow: vi.fn(),
    strategy: 'fts5',
  }),
}));

// The root layout is the actual component under test. `useRouterState`
// requires a router context; the cleanest way to exercise the real
// `RootLayout` (including its `<Outlet />`) is to mount it as the
// root route of a synthetic tree (the same pattern PageList.test.tsx
// uses). The `Route` import below brings in the module's exports so
// we can re-register the layout under a fresh tree.
import { RootLayout } from './__root';

async function withRouter() {
  const rootRoute = createRootRoute({
    component: () => <RootLayout />,
  });
  const indexRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: '/',
    component: () => <Outlet />,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  });
  await router.load();
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date('2026-01-15T12:00:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('RootLayout footer', () => {
  it('renders the offline indicator with the persisted badge', async () => {
    await withRouter();
    expect(await screen.findByTestId('offline-indicator')).toHaveTextContent(/ready/);
    expect(screen.getByTestId('offline-indicator').getAttribute('data-persisted')).toBe('true');
  });

  it('shows the last full sync timestamp when one exists', async () => {
    await withRouter();
    expect(await screen.findByTestId('last-sync')).toHaveTextContent(/Last full sync/);
  });
});

describe('RootLayout accessibility (WCAG 2.2)', () => {
  it('renders a skip link as the first anchor in the document', async () => {
    await withRouter();
    const firstAnchor = document.querySelector('a');
    expect(firstAnchor).not.toBeNull();
    expect(firstAnchor?.getAttribute('href')).toBe('#main-content');
    expect(firstAnchor?.textContent).toMatch(/skip to content/i);
  });

  it('exposes the main landmark with id="main-content"', async () => {
    await withRouter();
    const main = document.querySelector('main');
    expect(main).not.toBeNull();
    expect(main?.id).toBe('main-content');
  });

  it('announces the hamburger as a collapsed menu trigger with aria-controls', async () => {
    await withRouter();
    const hamburger = screen.getByRole('button', { name: /open menu/i });
    expect(hamburger.getAttribute('aria-expanded')).toBe('false');
    expect(hamburger.getAttribute('aria-controls')).toBe('main-nav');
  });

  it('labels the dropdown nav and marks the active route with aria-current="page"', async () => {
    await withRouter();
    const hamburger = screen.getByRole('button', { name: /open menu/i });
    fireEvent.click(hamburger);
    const nav = await screen.findByRole('navigation', { name: /main navigation/i });
    expect(nav.getAttribute('id')).toBe('main-nav');
    const recent = within(nav).getByRole('link', { name: /recent/i });
    expect(recent.getAttribute('aria-current')).toBe('page');
  });
});

describe('RootLayout app shell', () => {
  it('renders the desktop rail with Home/Search/Tags/Graph nav links', async () => {
    await withRouter();
    const rail = await screen.findByTestId('app-rail');
    // Each control gets a 44px+ touch target (layout rule) and a
    // unique aria-label. Assert by aria-label so we don't depend on
    // the internal svg structure of the icons.
    expect(within(rail).getByRole('link', { name: /^home$/i })).toHaveAttribute('href', '/');
    expect(within(rail).getByRole('link', { name: /^search$/i })).toHaveAttribute('href', '/search');
    expect(within(rail).getByRole('link', { name: /^tags$/i })).toHaveAttribute('href', '/tags');
    expect(within(rail).getByRole('link', { name: /^graph$/i })).toHaveAttribute('href', '/graph');
  });

  it('marks the active rail link with aria-current="page" on the matching route', async () => {
    await withRouter();
    const home = screen.getByTestId('rail-link-home');
    const search = screen.getByTestId('rail-link-search');
    // The synthetic router mounts on '/', so only Home is active.
    expect(home).toHaveAttribute('aria-current', 'page');
    expect(search).not.toHaveAttribute('aria-current');
  });

  it('renders an icon-only theme toggle inside the rail (44px target, descriptive aria-label)', async () => {
    await withRouter();
    const toggle = await screen.findByTestId('rail-theme-toggle');
    expect(toggle).toBeInTheDocument();
    // The label flips with the dark state; either value is acceptable
    // here, but the aria-label must always be set (icon-only buttons
    // without one are WCAG 4.1.2 violations).
    expect(toggle.getAttribute('aria-label')).toMatch(/switch to (light|dark) theme/i);
  });

  it('uses md:flex on the rail and md:hidden on the mobile header so the two are breakpoint-mutually-exclusive', async () => {
    await withRouter();
    const rail = await screen.findByTestId('app-rail');
    const mobileHeader = screen.getByTestId('mobile-header');
    // jsdom does not compute layout, so we assert on the class
    // strings directly: the rail's md:flex class switches it on
    // at >=768px, the header's md:hidden switches it off at the
    // same breakpoint. Both elements can coexist in the DOM at
    // any viewport (the rail is inside .app-shell which is a
    // single column <768px, but the rail itself is rendered
    // hidden via the .hidden utility).
    expect(rail.className).toContain('md:flex');
    expect(rail.className).toContain('hidden');
    expect(mobileHeader.className).toContain('md:hidden');
  });

  it('renders the mobile header (logo + hamburger) alongside the rail at every viewport (the rail is the desktop variant)', async () => {
    await withRouter();
    // Both the rail and the mobile header exist in the DOM; CSS
    // visibility is breakpoint-driven. The logo lives only in the
    // mobile header, the wordmark only in the rail — neither is
    // duplicated.
    const mobileHeader = screen.getByTestId('mobile-header');
    const rail = screen.getByTestId('app-rail');
    expect(within(mobileHeader).getByText('vesperiki', { exact: true })).toBeInTheDocument();
    expect(within(rail).getByText('vesperiki', { exact: true })).toBeInTheDocument();
  });

  it('keeps the existing dropdown nav (#main-nav, Main navigation label) intact for mobile users', async () => {
    await withRouter();
    const hamburger = screen.getByRole('button', { name: /open menu/i });
    fireEvent.click(hamburger);
    const nav = await screen.findByRole('navigation', { name: /main navigation/i });
    // Regression guard: the id and label are the contract the
    // hamburger's aria-controls resolves against. If either
    // changed, the hamburger would be lying to screen readers.
    expect(nav.id).toBe('main-nav');
    // The dropdown ToggleButton stays in place for accessibility —
    // the spec is explicit that the rail theme toggle does not
    // replace it for mobile.
    expect(within(nav).getByRole('button', { name: /switch to (light|dark) theme/i })).toBeInTheDocument();
  });
});
