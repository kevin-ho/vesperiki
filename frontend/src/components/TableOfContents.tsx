import { useEffect, useMemo, useState } from 'react';

/** A markdown heading extracted from the rendered article. */
export interface TocHeading {
  /** rehype-slug id (h2[id], h3[id]) — the fragment the link targets. */
  id: string;
  /** Heading text. */
  text: string;
  /** Heading level: 2 or 3. */
  level: number;
}

/** Offset from the viewport top a heading must clear to count as
 *  "current". Desktop has no sticky top bar, so a small offset is
 *  enough; the sticky TOC itself lives in a separate column. */
const ACTIVE_OFFSET = 96;

/**
 * Subscribe to a CSS media query. The TOC is only rendered when
 * `(min-width: 1200px)` matches; below that there is intentionally
 * NO drawer or collapsed fallback — the component returns null.
 * The state is kept in React (instead of relying on CSS alone) so
 * tests can drive visibility with a matchMedia mock.
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () =>
      typeof window !== 'undefined' &&
      typeof window.matchMedia === 'function' &&
      window.matchMedia(query).matches,
  );

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return;
    }
    const mql = window.matchMedia(query);
    const onChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    if (typeof mql.addEventListener === 'function') {
      mql.addEventListener('change', onChange);
      setMatches(mql.matches);
      return () => mql.removeEventListener('change', onChange);
    }
    // Older engines only expose the deprecated listener API.
    mql.addListener(onChange);
    setMatches(mql.matches);
    return () => mql.removeListener(onChange);
  }, [query]);

  return matches;
}

/**
 * Scroll-spy: track which heading is currently in view. Uses an
 * IntersectionObserver looking at the top band of the viewport when
 * the API exists; falls back to a scroll-position estimate (the last
 * heading above the offset) otherwise, which also keeps the active
 * link correct at the bottom of the page where no heading sits in
 * the band. In jsdom neither API computes layout, so the estimate
 * yields a deterministic active heading.
 */
export function useActiveHeading(ids: string[]): string | null {
  const [active, setActive] = useState<string | null>(ids[0] ?? null);

  useEffect(() => {
    if (ids.length === 0) {
      setActive(null);
      return;
    }
    const elements = ids
      .map((id) => document.getElementById(id))
      .filter((el): el is HTMLElement => el !== null);
    if (elements.length === 0) return;

    const compute = () => {
      let current: string | null = elements[0]?.id ?? null;
      for (const el of elements) {
        if (el.getBoundingClientRect().top - ACTIVE_OFFSET <= 0) {
          current = el.id;
        }
      }
      setActive(current);
    };

    compute();
    window.addEventListener('scroll', compute, { passive: true });
    window.addEventListener('resize', compute);

    if (typeof IntersectionObserver !== 'undefined') {
      const observer = new IntersectionObserver(
        (entries) => {
          const visible = entries
            .filter((entry) => entry.isIntersecting)
            .map((entry) => entry.target.id);
          if (visible.length > 0) {
            // Topmost visible heading in document order wins.
            const topmost = elements.find((el) => visible.includes(el.id));
            if (topmost) setActive(topmost.id);
          } else {
            compute();
          }
        },
        { rootMargin: `-${ACTIVE_OFFSET}px 0px -70% 0px`, threshold: 0 },
      );
      elements.forEach((el) => observer.observe(el));
      return () => {
        observer.disconnect();
        window.removeEventListener('scroll', compute);
        window.removeEventListener('resize', compute);
      };
    }

    return () => {
      window.removeEventListener('scroll', compute);
      window.removeEventListener('resize', compute);
    };
  }, [ids]);

  return active;
}

/**
 * Sticky right-hand table of contents for reader pages. Built from
 * the headings of the CURRENT page's rendered output (h2/h3 with
 * rehype-slug ids) — never a separate markdown parse. Visible only
 * at >=1200px; below that it is simply not rendered (no drawer, no
 * collapsed fallback). The nav's own title is an h2 (sr-only) so
 * the page keeps exactly one h1 (WCAG SC 1.3.1 / H42).
 */
export function TableOfContents({ headings }: { headings: TocHeading[] }) {
  const visible = useMediaQuery('(min-width: 1200px)');
  const ids = useMemo(() => headings.map((heading) => heading.id), [headings]);
  const activeId = useActiveHeading(ids);

  if (!visible || headings.length === 0) return null;

  return (
    <nav aria-label="Table of contents" className="reader-toc">
      <h2 className="sr-only">Table of contents</h2>
      <ul className="flex flex-col gap-1">
        {headings.map((heading) => {
          const isActive = activeId === heading.id;
          return (
            <li key={heading.id}>
              <a
                href={`#${heading.id}`}
                aria-current={isActive ? 'true' : undefined}
                className={
                  'focus-ring flex min-h-11 items-center border-l-2 py-1 pr-2 font-mono text-xs leading-snug ' +
                  (heading.level === 3 ? 'pl-6 ' : 'pl-4 ') +
                  (isActive
                    ? 'border-border bg-butter font-semibold text-text'
                    : 'border-transparent text-muted hover:border-border hover:text-text')
                }
              >
                {heading.text}
              </a>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}