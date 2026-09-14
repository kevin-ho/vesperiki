import { Link, Outlet, createRootRoute, useRouterState, useNavigate } from '@tanstack/react-router';
import { Button, ToggleButton, SearchField, Form, Input } from 'react-aria-components';
import { useEffect, useState, useRef } from 'react';
import { useOfflineStatus } from '../lib/use-offline-status';
import { relativeTime } from '../lib/format';

export const Route = createRootRoute({
  component: RootLayout,
});

/**
 * The rail shows a stack of icon-only nav buttons at the top and a
 * vertical wordmark + theme toggle at the bottom. The list is small
 * enough to keep as constants next to the component — promoting to a
 * config map would buy nothing and add an indirection.
 *
 * `aria-current` is the source of truth for the active route: the
 * spec is explicit that the rail uses page-level activeness, not
 * hover/focus. We compare against the current pathname (router
 * state) so the indicator matches the hamburger dropdown that uses
 * the same source.
 */
interface RailLink {
  to: '/' | '/search' | '/tags' | '/graph';
  label: string;
  isActive: (pathname: string) => boolean;
  icon: () => React.ReactNode;
}

function HomeIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5 9.5V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.5" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  );
}

function TagsIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="4" y1="9" x2="20" y2="9" />
      <line x1="4" y1="15" x2="20" y2="15" />
      <line x1="10" y1="3" x2="8" y2="21" />
      <line x1="16" y1="3" x2="14" y2="21" />
    </svg>
  );
}

function GraphIcon() {
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="6" cy="6" r="2.5" />
      <circle cx="18" cy="6" r="2.5" />
      <circle cx="6" cy="18" r="2.5" />
      <circle cx="18" cy="18" r="2.5" />
      <line x1="8.2" y1="7" x2="15.8" y2="17" />
      <line x1="15.8" y1="7" x2="8.2" y2="17" />
      <line x1="7" y1="8.2" x2="17" y2="15.8" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="4" />
      <line x1="12" y1="2" x2="12" y2="5" />
      <line x1="12" y1="19" x2="12" y2="22" />
      <line x1="2" y1="12" x2="5" y2="12" />
      <line x1="19" y1="12" x2="22" y2="12" />
      <line x1="4.5" y1="4.5" x2="6.6" y2="6.6" />
      <line x1="17.4" y1="17.4" x2="19.5" y2="19.5" />
      <line x1="4.5" y1="19.5" x2="6.6" y2="17.4" />
      <line x1="17.4" y1="6.6" x2="19.5" y2="4.5" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg
      aria-hidden="true"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
    </svg>
  );
}

const RAIL_LINKS: RailLink[] = [
  { to: '/', label: 'Home', isActive: (p) => p === '/', icon: HomeIcon },
  { to: '/search', label: 'Search', isActive: (p) => p === '/search', icon: SearchIcon },
  {
    to: '/tags',
    label: 'Tags',
    isActive: (p) => p === '/tags' || p.startsWith('/tags/'),
    icon: TagsIcon,
  },
  { to: '/graph', label: 'Graph', isActive: (p) => p === '/graph', icon: GraphIcon },
];

export function RootLayout() {
  const [dark, setDark] = useState(
    () => typeof window !== 'undefined' && localStorage.getItem('vesperiki-theme') === 'dark',
  );
  const [menuOpen, setMenuOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    try {
      localStorage.setItem('vesperiki-theme', dark ? 'dark' : 'light');
    } catch {
      /* private mode — non-fatal */
    }
  }, [dark]);

  // Close popovers on route change
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  useEffect(() => {
    setMenuOpen(false);
    setSearchOpen(false);
  }, [pathname]);

  // Document title per route. The page reader overrides this with
  // the page title once its query resolves.
  useEffect(() => {
    if (pathname === '/') {
      document.title = 'Recent pages — vesperiki';
    } else if (pathname === '/tags' || pathname.startsWith('/tags/')) {
      document.title = 'Tags — vesperiki';
    } else if (pathname === '/graph') {
      document.title = 'Link graph — vesperiki';
    } else if (pathname === '/search') {
      document.title = 'Search — vesperiki';
    } else if (pathname.startsWith('/p/')) {
      document.title = 'Reading — vesperiki';
    }
  }, [pathname]);

  // Focus the mobile search input when it opens
  useEffect(() => {
    if (searchOpen && searchInputRef.current) {
      searchInputRef.current.focus();
    }
  }, [searchOpen]);

  const { status } = useOfflineStatus();
  const navigate = useNavigate();

  return (
    <div className="app-shell min-h-screen">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:min-h-11 focus:items-center focus:border-3 focus:border-border focus:bg-surface focus:px-4 focus:py-2 focus:font-mono"
      >
        Skip to content
      </a>

      {/*
        Desktop rail (>=768px). The mobile header below is hidden
        at this breakpoint so the two never render at the same
        time. `data-testid="app-rail"` lets the regression tests
        target the element without depending on internal class
        names. The rail is the FIRST interactive element after the
        skip link so keyboard users hit it before the article.
      */}
      <aside
        className="app-rail hidden flex-col items-center justify-between py-6 md:flex"
        data-testid="app-rail"
        aria-label="Primary navigation"
      >
        <ul className="flex flex-col items-center gap-3">
          {RAIL_LINKS.map((link) => {
            const active = link.isActive(pathname);
            const Icon = link.icon;
            return (
              <li key={link.to}>
                <Link
                  to={link.to}
                  aria-label={link.label}
                  aria-current={active ? 'page' : undefined}
                  data-testid={`rail-link-${link.label.toLowerCase()}`}
                  className={
                    'focus-ring inline-flex min-h-11 min-w-11 items-center justify-center border-2 border-border ' +
                    (active
                      ? 'bg-butter text-text shadow-brutal'
                      : 'bg-surface text-text hover:brightness-95')
                  }
                >
                  <Icon />
                </Link>
              </li>
            );
          })}
        </ul>
        <div className="flex flex-col items-center gap-3">
          <div
            className="app-rail-wordmark font-display text-sm"
            aria-hidden="true"
          >
            vesperiki
          </div>
          <Button
            type="button"
            onPress={() => setDark((v) => !v)}
            aria-label={dark ? 'Switch to light theme' : 'Switch to dark theme'}
            data-testid="rail-theme-toggle"
            className="focus-ring inline-flex min-h-11 min-w-11 items-center justify-center border-2 border-border bg-surface"
          >
            {dark ? <SunIcon /> : <MoonIcon />}
          </Button>
        </div>
      </aside>

      <header
        data-testid="mobile-header"
        className="flex flex-col border-b-3 border-border bg-peach shadow-brutal md:hidden"
      >
        <div className="px-4">
          {/* Row 1: title | search + hamburger */}
          <div className="flex items-center justify-between gap-3 py-4 md:py-5">
            <Link
              to="/"
              className="focus-ring inline-flex min-h-11 shrink-0 items-center font-display text-xl tracking-tight sm:text-2xl"
            >
              vesperiki
            </Link>

            {/* Desktop: full-width search bar inline */}
            <Form
              role="search"
              aria-label="Search the garden"
              className="hidden min-w-0 flex-1 md:block"
              onSubmit={(e) => {
                e.preventDefault();
                const data = new FormData(e.currentTarget);
                const q = String(data.get('q') ?? '').trim();
                if (q) navigate({ to: '/search', search: { q } });
              }}
            >
              <SearchField aria-label="Search" className="flex min-w-0 flex-1">
                <Input
                  name="q"
                  placeholder="Search the garden…"
                  className="focus-ring min-h-11 w-full border-3 border-border bg-surface px-4 py-2 font-mono text-xs"
                />
              </SearchField>
            </Form>

            {/* Mobile: search toggle button */}
            <Button
              type="button"
              aria-label={searchOpen ? 'Close search' : 'Open search'}
              onPress={() => setSearchOpen((v) => !v)}
              className="focus-ring inline-flex min-h-11 shrink-0 items-center justify-center border-2 border-border bg-surface px-3 md:hidden data-[pressed]:translate-x-px data-[pressed]:translate-y-px"
            >
              <SearchIcon />
            </Button>

            {/* Hamburger — always visible (all viewports) */}
            <Button
              type="button"
              aria-label="Open menu"
              aria-expanded={menuOpen}
              aria-controls="main-nav"
              onPress={() => setMenuOpen((v) => !v)}
              className="focus-ring inline-flex min-h-11 shrink-0 items-center justify-center border-2 border-border bg-surface px-3 data-[pressed]:translate-x-px data-[pressed]:translate-y-px"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                {menuOpen ? (
                  <>
                    <line x1="18" y1="6" x2="6" y2="18" />
                    <line x1="6" y1="6" x2="18" y2="18" />
                  </>
                ) : (
                  <>
                    <line x1="3" y1="12" x2="21" y2="12" />
                    <line x1="3" y1="6" x2="21" y2="6" />
                    <line x1="3" y1="18" x2="21" y2="18" />
                  </>
                )}
              </svg>
            </Button>
          </div>

          {/* Row 2 (mobile only): collapsible full-width search */}
          {searchOpen && (
            <div className="pb-4 md:hidden">
              <Form
                role="search"
                aria-label="Search the garden"
                onSubmit={(e) => {
                  e.preventDefault();
                  const data = new FormData(e.currentTarget);
                  const q = String(data.get('q') ?? '').trim();
                  if (q) navigate({ to: '/search', search: { q } });
                }}
              >
                <SearchField aria-label="Search" className="flex min-w-0 flex-1">
                  <Input
                    ref={searchInputRef}
                    name="q"
                    placeholder="Search the garden…"
                    className="focus-ring min-h-11 w-full border-3 border-border bg-surface px-4 py-2 font-mono text-xs"
                  />
                </SearchField>
              </Form>
            </div>
          )}

          {/* Hamburger dropdown — all viewports */}
          {menuOpen && (
            <nav
              id="main-nav"
              aria-label="Main navigation"
              className="flex flex-col gap-1 pb-4"
            >
              <Link
                to="/"
                activeOptions={{ exact: true }}
                aria-current={pathname === '/' ? 'page' : undefined}
                className="focus-ring inline-flex min-h-11 items-center px-3 py-3 font-mono text-xs font-medium uppercase"
              >
                Recent
              </Link>
              <Link
                to="/tags"
                aria-current={pathname.startsWith('/tags') ? 'page' : undefined}
                className="focus-ring inline-flex min-h-11 items-center px-3 py-3 font-mono text-xs font-medium uppercase"
              >
                Tags
              </Link>
              <Link
                to="/graph"
                aria-current={pathname === '/graph' ? 'page' : undefined}
                className="focus-ring inline-flex min-h-11 items-center px-3 py-3 font-mono text-xs font-medium uppercase"
              >
                Graph
              </Link>
              <ToggleButton
                isSelected={dark}
                onChange={setDark}
                aria-label={dark ? 'Switch to light theme' : 'Switch to dark theme'}
                className="focus-ring inline-flex min-h-11 items-center px-3 py-3 font-mono text-xs font-medium uppercase"
              >
                {dark ? '☼ Light mode' : '☾ Dark mode'}
              </ToggleButton>
            </nav>
          )}
        </div>
      </header>
      <main
        id="main-content"
        className="min-w-0 flex-1 px-4 py-8 md:px-8 md:py-12"
      >
        <Outlet />
      </main>
      <footer
        data-testid="offline-footer"
        className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-4 py-8 font-mono text-xs uppercase text-muted md:px-8 md:py-10"
      >
        <div data-testid="offline-status" className="flex flex-wrap gap-x-4 gap-y-1">
          <span>Read-only · Offline-first</span>
          <OfflineIndicator status={status} />
          {status.lastSyncAt ? (
            <span data-testid="last-sync">
              Last full sync: {relativeTime(status.lastSyncAt)}
            </span>
          ) : null}
        </div>
      </footer>
    </div>
  );
}

/**
 * Small status badge for the offline copy. Two states: when the
 * browser has granted `navigator.storage.persist()` we render a
 * green check; otherwise we show a yellow warning so the user knows
 * the offline copy could be evicted under storage pressure. The
 * search-strategy info is kept off the footer by default (the
 * compile-time flag is already implied — FTS5 was the goal) but
 * stays available as a `data-` attribute for diagnostics.
 */
function OfflineIndicator({ status }: { status: ReturnType<typeof useOfflineStatus>['status'] }) {
  const grantLabel =
    status.persisted === true
      ? 'Offline copy: ready \u2705'
      : status.persisted === false
        ? 'Offline copy: not protected \u26a0\ufe0f'
        : 'Offline copy: checking\u2026';
  return (
    <span
      data-testid="offline-indicator"
      data-search-strategy={status.searchStrategy}
      data-persisted={status.persisted === null ? 'unknown' : String(status.persisted)}
      aria-live="polite"
    >
      {grantLabel}
    </span>
  );
}
