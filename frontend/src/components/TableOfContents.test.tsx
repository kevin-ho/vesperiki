import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import {
  TableOfContents,
  useActiveHeading,
  useMediaQuery,
  type TocHeading,
} from './TableOfContents';

/**
 * `window.matchMedia` is undefined in jsdom by default. The TOC
 * guards the call site, so the default test environment exercises
 * the "no matchMedia" branch (matches=false → null). To assert
 * the visible-rail behaviour we install a stub that reports the
 * viewport state we want and emits change events the hook
 * subscribes to. The stub lives on `window` so the same listener
 * the production code attaches is the one we drive.
 */
type Listener = (event: { matches: boolean; media: string }) => void;

function installMatchMedia(initialMatches: boolean) {
  const listeners = new Set<Listener>();
  const mql = {
    matches: initialMatches,
    media: '',
    onchange: null,
    addEventListener: (_: string, listener: Listener) => {
      listeners.add(listener);
    },
    removeEventListener: (_: string, listener: Listener) => {
      listeners.delete(listener);
    },
    addListener: (listener: Listener) => listeners.add(listener),
    removeListener: (listener: Listener) => listeners.delete(listener),
    dispatchEvent: () => true,
  };
  // matchMedia is a function on the window — it takes the query
  // and returns a fresh MediaQueryList. We return the same mql
  // for every call so the test can drive it via setMatches below.
  const stub = vi.fn().mockImplementation(() => mql);
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: stub,
  });
  return {
    setMatches(matches: boolean) {
      mql.matches = matches;
      listeners.forEach((listener) => listener({ matches, media: mql.media }));
    },
    stub,
  };
}

afterEach(() => {
  // Remove the stub so subsequent tests start from jsdom's default
  // (undefined) matchMedia — important because the TOC's first-render
  // state initialiser reads window.matchMedia at module init.
  delete (window as { matchMedia?: unknown }).matchMedia;
});

describe('useMediaQuery', () => {
  it('returns false when matchMedia is unavailable (jsdom default)', () => {
    // matchMedia was cleaned up by afterEach.
    const { result } = renderHookOnce(() => useMediaQuery('(min-width: 1200px)'));
    expect(result.current).toBe(false);
  });

  it('reports the current matchMedia value when the API is present', () => {
    installMatchMedia(true);
    const { result } = renderHookOnce(() => useMediaQuery('(min-width: 1200px)'));
    expect(result.current).toBe(true);
  });

  it('flips when matchMedia fires a change event', () => {
    const { setMatches } = installMatchMedia(true);
    const { result } = renderHookOnce(() => useMediaQuery('(min-width: 1200px)'));
    expect(result.current).toBe(true);
    act(() => setMatches(false));
    expect(result.current).toBe(false);
  });
});

describe('useActiveHeading', () => {
  it('starts with the first heading id when no scroll has occurred', () => {
    // No DOM elements with the same ids exist — the effect's
    // `elements` list is empty, so the early-return preserves
    // the useState's initial value (ids[0]). This matches the
    // TOC component's contract: when there is nothing to scroll-
    // spy against, the first heading is the active one.
    const { result } = renderHookOnce(() => useActiveHeading(['intro', 'background']));
    expect(result.current).toBe('intro');
  });

  it('returns null when given an empty list of ids', () => {
    const { result } = renderHookOnce(() => useActiveHeading([]));
    expect(result.current).toBeNull();
  });
});

describe('TableOfContents', () => {
  const sampleHeadings: TocHeading[] = [
    { id: 'intro', text: 'Introduction', level: 2 },
    { id: 'background', text: 'Background', level: 3 },
    { id: 'results', text: 'Results', level: 2 },
  ];

  it('returns null (renders nothing) when matchMedia is unavailable', () => {
    const { container } = render(<TableOfContents headings={sampleHeadings} />);
    expect(container.firstChild).toBeNull();
  });

  it('returns null when matchMedia reports the viewport is below 1200px', () => {
    installMatchMedia(false);
    const { container } = render(<TableOfContents headings={sampleHeadings} />);
    expect(container.firstChild).toBeNull();
  });

  it('returns null when the headings list is empty, even at >=1200px', () => {
    installMatchMedia(true);
    const { container } = render(<TableOfContents headings={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders a nav with the accessible name "Table of contents" and the sr-only h2', () => {
    installMatchMedia(true);
    render(<TableOfContents headings={sampleHeadings} />);
    const nav = screen.getByRole('navigation', { name: /table of contents/i });
    expect(nav).toBeInTheDocument();
    // WCAG H42 / SC 1.3.1: page must keep exactly one h1. The TOC
    // contributes an h2 (sr-only) — that is the only h2 it owns, so
    // the page h2 count elsewhere stays intact.
    const srOnlyHeading = within(nav).getByRole('heading', {
      level: 2,
      name: /table of contents/i,
    });
    expect(srOnlyHeading).toHaveClass('sr-only');
  });

  it('lists each heading as a link and distinguishes h2 vs h3 via padding-left', () => {
    installMatchMedia(true);
    render(<TableOfContents headings={sampleHeadings} />);
    const nav = screen.getByRole('navigation', { name: /table of contents/i });
    const links = within(nav).getAllByRole('link');
    expect(links).toHaveLength(sampleHeadings.length);
    expect(links[0]).toHaveTextContent('Introduction');
    expect(links[1]).toHaveTextContent('Background');
    expect(links[2]).toHaveTextContent('Results');
    // h2 → pl-4, h3 → pl-6 (matches the component's branch).
    expect(links[0].className).toContain('pl-4');
    expect(links[0].className).not.toContain('pl-6');
    expect(links[1].className).toContain('pl-6');
    expect(links[1].className).not.toContain('pl-4');
  });

  it('marks the first heading as aria-current="true" on initial mount', () => {
    installMatchMedia(true);
    render(<TableOfContents headings={sampleHeadings} />);
    const nav = screen.getByRole('navigation', { name: /table of contents/i });
    const links = within(nav).getAllByRole('link');
    // The first heading is the active one on mount: useActiveHeading
    // initialises to ids[0] and jsdom never advances scroll, so the
    // estimate falls through to that same id.
    expect(links[0]).toHaveAttribute('aria-current', 'true');
    expect(links[1]).not.toHaveAttribute('aria-current');
    expect(links[2]).not.toHaveAttribute('aria-current');
  });
});

/**
 * Tiny ad-hoc renderer for the hooks. We can't use the React
 * Testing Library `renderHook` (it lives in a separate
 * `@testing-library/react` export that isn't pulled in here) so we
 * drive the hook with a one-line test component that mirrors the
 * value back onto a mutable ref we read after commit. The effect
 * runs on every render so re-renders triggered by the hook's
 * internal setState (e.g. when matchMedia fires) are reflected
 * in the ref before the test reads it.
 */
import { useEffect, useRef, type ReactNode } from 'react';
function renderHookOnce<T>(useHook: () => T): { result: { current: T } } {
  const ref: { current: T | undefined } = { current: undefined };
  function Probe() {
    const value = useHook();
    useEffect(() => {
      ref.current = value;
    });
    return null as unknown as ReactNode;
  }
  render(<Probe />);
  return { result: ref as { current: T } };
}
