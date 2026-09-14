import { Link } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { ReactElement, ReactNode } from 'react';
import { isValidElement, useEffect, useMemo, useRef, useState } from 'react';
import {
  Cell,
  Column,
  ColumnResizer,
  ResizableTableContainer,
  Row,
  Table,
  TableBody,
  TableHeader,
} from 'react-aria-components';
import type { SortDescriptor } from 'react-aria-components';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeSlug from 'rehype-slug';
import type { Element as HastElement, Properties } from 'hast';
import { createCorrection } from '../lib/api';
import { getDataSource } from '../lib/data-source';
import { wikilinksToMarkdown } from '../lib/wikilinks';
import { remarkCallouts, type CalloutType } from '../lib/callouts';
import { ErrorState, LoadingState } from '../components/PageList';
import {
  TableOfContents,
  type TocHeading,
} from '../components/TableOfContents';
import type { Correction, Page } from '../lib/types';
import { Route } from './p.$slug';

function getClassNames(properties: Properties | undefined): string[] {
  const className = properties?.className;
  if (Array.isArray(className)) {
    return className.filter((c): c is string => typeof c === 'string');
  }
  if (typeof className === 'string') return [className];
  return [];
}

function calloutTypeFromClassNames(classNames: string[]): CalloutType | null {
  if (!classNames.includes('callout')) return null;
  const found = classNames.find(
    (c) => c.startsWith('callout-') && c !== 'callout',
  );
  if (!found) return 'NOTE';
  return found.slice('callout-'.length).toUpperCase() as CalloutType;
}

function CalloutBlockquote({
  node,
  children,
}: {
  node?: HastElement;
  children?: ReactNode;
}) {
  const type = calloutTypeFromClassNames(getClassNames(node?.properties));
  if (!type) return <blockquote>{children}</blockquote>;
  return (
    <div className="callout" data-callout={type}>
      <div className="callout-label">{type}</div>
      <div className="callout-body">{children}</div>
    </div>
  );
}

/**
 * Markdown `<img>` override. The ingest pipeline rewrites image
 * references to `/api/media/{id}`; the raw URL is enough for the
 * browser to fetch bytes directly via the Vite dev proxy. Adds
 * `loading="lazy"` so off-screen images don't block render and
 * provides a default alt text so the page is never unusable
 * without it.
 */
function MarkdownImage(props: React.ImgHTMLAttributes<HTMLImageElement>) {
  const alt = props.alt && props.alt.trim().length > 0 ? props.alt : 'page image';
  return <img {...props} alt={alt} loading="lazy" />;
}

interface MarkdownTableColumn {
  /** Stable key used as the RAC Column id / sort descriptor column. */
  id: string;
  /** Header text rendered in the column header. */
  label: string;
}

interface MarkdownTableRow {
  id: string;
  cells: string[];
}

/** Locale-aware string comparison for table column sorting. */
const tableCollator = new Intl.Collator(undefined, {
  numeric: true,
  sensitivity: 'base',
});

/** The children of an element (React 19 types keep `props` opaque). */
function elementChildren(node: ReactElement): ReactNode {
  return (node.props as { children?: ReactNode }).children;
}

/** The plain text a ReactNode tree renders (links/code unwrapped). */
function extractCellText(node: ReactNode): string {
  if (node == null || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(extractCellText).join('');
  if (isValidElement(node)) return extractCellText(elementChildren(node));
  return '';
}

/** The tag name of a React element, or null for components/primitives. */
function elementTag(node: ReactNode): string | null {
  if (!isValidElement(node) || typeof node.type !== 'string') return null;
  return node.type;
}

/** All descendant elements whose tag name is one of `tags`. */
function childElements(node: ReactNode, ...tags: string[]): ReactElement[] {
  const found: ReactElement[] = [];
  const visit = (current: ReactNode) => {
    if (Array.isArray(current)) {
      current.forEach(visit);
      return;
    }
    if (isValidElement(current)) {
      const tag = typeof current.type === 'string' ? current.type : null;
      if (tag && tags.includes(tag)) found.push(current);
      else visit(elementChildren(current));
    }
  };
  visit(node);
  return found;
}

/**
 * Parse the GFM `<table>` children react-markdown hands to the `table`
 * override into a plain column/row data model. GFM tables arrive as a
 * `thead`/`tbody` element pair (`<thead><tr><th>…` + `<tbody><tr><td>…`);
 * some renderers put the header row (th cells) in the first tbody row
 * instead, which is handled too. Cell text is extracted (inline links
 * and code flatten to their text) so sorting compares plain strings.
 */
function parseMarkdownTable(children: ReactNode): {
  columns: MarkdownTableColumn[];
  rows: MarkdownTableRow[];
} {
  const parts = Array.isArray(children) ? children : [children];
  const thead = parts.find((part) => elementTag(part) === 'thead');
  const tbody = parts.find((part) => elementTag(part) === 'tbody');

  const headerTrs = thead ? childElements(elementChildren(thead), 'tr') : [];
  const bodyTrs = tbody ? childElements(elementChildren(tbody), 'tr') : [];

  let headerCells = headerTrs[0]
    ? childElements(elementChildren(headerTrs[0]), 'th', 'td')
    : [];
  let dataTrs = bodyTrs;

  // Fallback: when the header row lives in tbody (th cells in the first
  // body row), treat that row as the header instead of a data row.
  if (headerCells.length === 0 && dataTrs[0]) {
    const firstRowCells = childElements(elementChildren(dataTrs[0]), 'th', 'td');
    if (firstRowCells.some((cell) => elementTag(cell) === 'th')) {
      headerCells = firstRowCells;
      dataTrs = dataTrs.slice(1);
    }
  }

  const columns = headerCells.map((cell, index) => ({
    id: `column-${index}`,
    label: extractCellText(cell).trim(),
  }));

  const rows = dataTrs.map((tr, index) => {
    const cellTexts = childElements(elementChildren(tr), 'th', 'td').map((cell) =>
      extractCellText(cell).trim(),
    );
    return {
      id: `row-${index}`,
      // Pad/truncate ragged rows so every Row renders one Cell per Column.
      cells: columns.map((_, columnIndex) => cellTexts[columnIndex] ?? ''),
    };
  });

  return { columns, rows };
}

/**
 * Markdown GFM `<table>` override. The incoming GFM table children are
 * parsed into a column/row data model and rendered as a React Aria
 * table so readers get the full interactive table semantics: sortable
 * columns, drag/keyboard column resizing and arrow-key cell navigation.
 * The RAC `ResizableTableContainer` (the `.table-scroll` element) owns
 * the horizontal scroll for wide tables and is the full-bleed element
 * that breaks out of the content column (see layout.css) — the same
 * viewport pattern the `pre` blocks use. The labelled `<Table>`
 * (accessible name "Markdown table") replaces the old `role="region"`
 * wrapper: RAC's table semantics surface the structure to assistive
 * tech instead (WCAG SC 1.3.1) and its focus management makes the
 * scroll container keyboard-reachable (WCAG SC 2.1.1).
 */
export function MarkdownTable({ children }: { children?: ReactNode }) {
  const { columns, rows } = parseMarkdownTable(children);
  const [sortDescriptor, setSortDescriptor] = useState<
    SortDescriptor | undefined
  >(undefined);

  // Apply the sort in the render path: no sort by default (original
  // markdown order); RAC's onSortChange toggles asc<->desc per column.
  const sortedRows = useMemo(() => {
    if (!sortDescriptor) return rows;
    const columnIndex = columns.findIndex(
      (column) => column.id === sortDescriptor.column,
    );
    if (columnIndex < 0) return rows;
    const factor = sortDescriptor.direction === 'descending' ? -1 : 1;
    return [...rows].sort(
      (a, b) =>
        tableCollator.compare(
          a.cells[columnIndex] ?? '',
          b.cells[columnIndex] ?? '',
        ) * factor,
    );
  }, [rows, columns, sortDescriptor]);

  // No parseable header row (e.g. malformed markdown) — nothing to
  // build a table from.
  if (columns.length === 0) return null;

  return (
    <ResizableTableContainer className="table-scroll">
      <Table
        aria-label="Markdown table"
        selectionMode="none"
        sortDescriptor={sortDescriptor}
        onSortChange={setSortDescriptor}
      >
        <TableHeader>
          {columns.map((column, index) => (
            <Column
              key={column.id}
              id={column.id}
              isRowHeader={index === 0}
              allowsSorting
              defaultWidth="1fr"
              minWidth={100}
              className="relative pr-5 focus-ring"
            >
              {({ sortDirection }) => (
                <>
                  {column.label}
                  {sortDirection === 'ascending' ? (
                    <span aria-hidden="true" className="ml-0.5">↑</span>
                  ) : null}
                  {sortDirection === 'descending' ? (
                    <span aria-hidden="true" className="ml-0.5">↓</span>
                  ) : null}
                  <ColumnResizer
                    aria-label={`Resize ${column.label}`}
                    className="absolute right-0 top-0 h-full w-1 cursor-col-resize bg-transparent hover:bg-bg data-[resizing]:bg-bg"
                  />
                </>
              )}
            </Column>
          ))}
        </TableHeader>
        <TableBody items={sortedRows}>
          {(row) => (
            <Row id={row.id} textValue={row.cells.join(' ')}>
              {row.cells.map((cell, index) => (
                <Cell key={`${row.id}-cell-${index}`}>{cell}</Cell>
              ))}
            </Row>
          )}
        </TableBody>
      </Table>
    </ResizableTableContainer>
  );
}

/**
 * Lower-case, strip whitespace + punctuation. Used to compare a
 * markdown heading line to the page title; imported pages may render
 * `# Forgejo` while the page header renders the canonical `Forgejo`
 * title, so a direct equality check is too strict.
 */
function normaliseHeading(text: string): string {
  return text.trim().toLowerCase().replace(/[\s\p{P}]+/gu, '');
}

/**
 * If the body opens with a single `# ` heading line whose text
 * normalises to the page title, drop that line. The page header
 * already renders the title as `<h1>`, so keeping the body's
 * duplicate would produce two `<h1>`s (WCAG SC 1.3.1 / H42).
 *
 * Stripping (rather than mutating an h1 component) is deterministic
 * and StrictMode-safe: the same input always produces the same
 * markdown, so the rendered tree is identical on both renders.
 *
 * Only the FIRST line is considered and only `# ` is matched (not
 * `##`/`###`); a missing or differently-spelled heading is left
 * untouched.
 */
export function stripLeadingTitleHeading(body: string, pageTitle: string): string {
  const title = normaliseHeading(pageTitle);
  if (!title) return body;
  // Greedy capture of the heading text so the whole heading line is
  // matched in one go. The previous non-greedy `+?` would capture only
  // the first character (e.g. `F` for `# Forgejo`) when no trailing
  // newline was consumed, which broke the title-mismatch guard and
  // left the body unchanged — producing two <h1>s (WCAG SC 1.3.1).
  // The trailing `(?:\n|$)` anchors on either the line break or the
  // end of the body, so the whole heading line is consumed.
  const match = body.match(/^(\s*)#\s+([^\n]+)\s*(?:\n|$)/);
  if (!match) return body;
  // Reject `##`, `###`, etc. — the matched prefix is just the
  // leading whitespace before the heading marker. (The regex already
  // won't match `##` because `\s+` requires whitespace after `#`,
  // but keep the guard as a safety net.)
  if (match[2].startsWith('#')) return body;
  if (normaliseHeading(match[2]) !== title) return body;
  return body.slice(match[0].length);
}

/**
 * Format a 0..1 confidence value as a percentage. `undefined` or
 * 1.0 are treated as fully verified.
 */
export function formatConfidence(value: number | undefined): string | null {
  if (value === undefined || value >= 1) return null;
  return `${Math.round(value * 100)}%`;
}

/**
 * Check whether a Range's closest ancestor element is inside a
 * particular root. Selection API quirks: `commonAncestorContainer`
 * is sometimes a Text node rather than an Element, so we walk up
 * via `parentNode`.
 */
function rangeIsInsideArticle(range: Range, article: Element): boolean {
  let node: Node | null = range.commonAncestorContainer;
  while (node) {
    if (node === article) return true;
    node = node.parentNode;
  }
  return false;
}

/** Lifted-out component so it can be tested directly. */
export function ConfidenceBadge({ value }: { value: number | undefined }) {
  const percent = formatConfidence(value);
  if (percent === null) return null;
  return (
    <span
      data-testid="confidence-badge"
      className="inline-flex items-center border-2 border-border bg-butter px-2 py-1 font-mono text-xs uppercase"
      title="Ingest confidence score; lower values flag this page for review."
    >
      Confidence: {percent}
    </span>
  );
}

interface SelectionPopoverState {
  /** Whitespace-trimmed selected text. */
  text: string;
  /** Pixel coordinates for the popover (top-left in viewport). */
  x: number;
  y: number;
}

function CorrectionPopover({
  page,
  pending,
  onClose,
}: {
  page: Page;
  pending: SelectionPopoverState | null;
  onClose: () => void;
}) {
  const [note, setNote] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const queryClient = useQueryClient();
  const submit = useMutation({
    mutationFn: (payload: { selected_text: string; note: string }) =>
      createCorrection({ page_slug: page.slug, ...payload }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['corrections', page.slug] });
    },
  });

  useEffect(() => {
    setNote('');
    setSubmitted(false);
  }, [pending?.text]);

  if (!pending) return null;
  if (submitted) {
    return (
      <div
        role="status"
        data-testid="correction-confirmation"
        className="fixed z-50 max-w-xs border-3 border-border bg-sage p-3 font-mono text-xs shadow-brutal"
        style={{ left: pending.x, top: pending.y }}
      >
        Correction flagged — an agent will fix it.
      </div>
    );
  }

  return (
    <form
      data-testid="correction-popover"
      aria-label="Flag correction"
      style={{ left: pending.x, top: pending.y }}
      className="fixed z-50 flex max-w-sm flex-col gap-3 border-3 border-border bg-surface p-4 shadow-brutal"
      onSubmit={(event) => {
        event.preventDefault();
        if (submit.isPending) return;
        submit.mutate(
          { selected_text: pending.text, note: note.trim() },
          {
            onSuccess: () => {
              setSubmitted(true);
              setTimeout(onClose, 3000);
            },
          },
        );
      }}
    >
      <p className="font-mono text-xs uppercase text-muted">Selected passage</p>
      <blockquote
        data-testid="correction-selected"
        className="max-h-24 overflow-auto border-2 border-border bg-butter p-2 text-xs"
      >
        {pending.text}
      </blockquote>
      <label className="flex flex-col gap-1 font-mono text-xs uppercase text-muted">
        Correction note
        <textarea
          name="note"
          value={note}
          maxLength={200}
          onChange={(event) => setNote(event.target.value)}
          placeholder="What should it say instead?"
          rows={3}
          data-testid="correction-note"
          className="min-h-11 border-3 border-border bg-bg p-2 font-body text-xs"
        />
      </label>
      {submit.isError ? (
        <p className="font-mono text-xs uppercase text-rose">
          Could not flag: {submit.error.message}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={submit.isPending}
          className="inline-flex min-h-11 items-center border-3 border-border bg-sage px-4 py-2 font-mono font-bold uppercase shadow-brutal transition-transform duration-100 hover:-translate-x-0.5 hover:-translate-y-0.5 disabled:opacity-50 data-[pressed]:translate-x-1 data-[pressed]:translate-y-1 data-[pressed]:shadow-none"
        >
          {submit.isPending ? 'Flagging…' : 'Flag correction'}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="inline-flex min-h-11 items-center border-3 border-border bg-surface px-4 py-2 font-mono uppercase"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

export function PageReader() {
  const { slug } = Route.useParams();
  const page = useQuery({
    queryKey: ['page', slug],
    queryFn: async () => {
      const ds = await getDataSource();
      return ds.getPage(slug);
    },
  });
  const links = useQuery({
    queryKey: ['backlinks', slug],
    queryFn: async () => {
      const ds = await getDataSource();
      return ds.getBacklinks(slug);
    },
  });
  // Use a callback ref so the listener binds as soon as the article
  // actually mounts — using an effect on a stable ref would run
  // while the loading placeholder is on screen and bail because
  // `articleRef.current` is null.
  const [articleEl, setArticleEl] = useState<HTMLElement | null>(null);
  const [selection, setSelection] = useState<SelectionPopoverState | null>(null);
  // Headings are pulled from the rendered article (rehype-slug
  // assigns the ids) rather than re-parsing the markdown, so the
  // TOC always matches what the reader actually sees — including
  // any heading transforms applied by markdown component overrides.
  // Re-derives on every body / article change so route navigation
  // updates the list without a full remount.
  const [headings, setHeadings] = useState<TocHeading[]>([]);

  // Document title — overrides the route-level default set by
  // RootLayout once the page payload resolves. We restore the
  // previous title on unmount so navigating away doesn't leave a
  // stale title.
  useEffect(() => {
    const previousTitle = document.title;
    if (page.data) {
      document.title = `${page.data.title} — vesperiki`;
    }
    return () => {
      document.title = previousTitle;
    };
  }, [page.data]);

  // Capture selection inside the article. The article listener
  // turns a non-collapsed selection inside the article into a
  // popover state; the document-level mousedown listener dismisses
  // the popover when the user clicks anywhere else (with the
  // popover itself carved out so its own clicks don't dismiss it).
  useEffect(() => {
    if (!articleEl) return;
    const handleMouseUp = () => {
      const sel = typeof window !== 'undefined' ? window.getSelection() : null;
      if (!sel || sel.isCollapsed) {
        setSelection(null);
        return;
      }
      const text = sel.toString().trim();
      if (!text) {
        setSelection(null);
        return;
      }
      const range = sel.getRangeAt(0);
      if (!rangeIsInsideArticle(range, articleEl)) {
        setSelection(null);
        return;
      }
      // Real browsers give us a precise viewport rect; in jsdom that
      // method is missing, so fall back to the article's offset
      // coordinates for tests.
      const rectFn = (range as Range & {
        getBoundingClientRect?: () => DOMRect;
      }).getBoundingClientRect;
      let x = articleEl.offsetLeft + 16;
      let y = articleEl.offsetTop + articleEl.offsetHeight + 8;
      if (typeof rectFn === 'function') {
        const rect = rectFn.call(range);
        // The popover is `position: fixed`, which is viewport-relative.
        // getBoundingClientRect() already returns viewport coordinates,
        // so NO scrollX/scrollY adjustment here — adding the scroll
        // offset would push the popover exactly `scrollY` pixels below
        // the selection (off-screen after any scrolling).
        x = rect.left;
        y = rect.bottom + 8;
      }
      setSelection({ text, x, y });
    };
    const handleDismiss = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      if (target.closest('[data-testid="correction-popover"]')) return;
      if (target.closest('[data-testid="correction-confirmation"]')) return;
      setSelection(null);
    };
    articleEl.addEventListener('mouseup', handleMouseUp);
    document.addEventListener('mousedown', handleDismiss);
    return () => {
      articleEl.removeEventListener('mouseup', handleMouseUp);
      document.removeEventListener('mousedown', handleDismiss);
    };
  }, [articleEl]);

  // Walk the rendered article for h2/h3 elements with ids (rehype-slug
  // installs those) and feed the table of contents. Re-runs whenever
  // the article DOM or body source changes so route navigation
  // refreshes the list without a remount; the effect is also a
  // no-op when the article is absent (loading state) so the
  // query-cached `headings` simply stays empty until the next mount.
  useEffect(() => {
    if (!articleEl) {
      setHeadings([]);
      return;
    }
    const collected: TocHeading[] = [];
    const nodes = articleEl.querySelectorAll('h2, h3');
    nodes.forEach((node) => {
      const id = node.id;
      const text = node.textContent ?? '';
      if (!id || !text.trim()) return;
      const level = node.tagName === 'H3' ? 3 : 2;
      collected.push({ id, text: text.trim(), level });
    });
    setHeadings(collected);
  }, [articleEl, page.data?.body, page.data?.title]);

  if (page.isPending) return <LoadingState />;
  if (page.isError) return <ErrorState message={page.error.message} />;
  // ds.getPage resolves `null` for a page absent from the data
  // source (e.g. never synced to the offline copy) — surface the
  // same UX the old HTTP 404 produced instead of crashing on
  // `page.data.body`.
  if (!page.data) return <ErrorState message="Page not found." />;

  const data = page.data;
  const backlinks = links.data ?? [];
  const body = wikilinksToMarkdown(
    stripLeadingTitleHeading(data.body, data.title),
  );

  return (
    <div className="reader-layout" data-testid="reader-layout">
      <article ref={setArticleEl}>
        <header className="mb-8 border-b-3 border-border pb-8">
          <p className="flex flex-wrap items-center gap-3 font-mono text-xs uppercase text-muted">
            <span>/{data.slug} · {data.type}</span>
            <ConfidenceBadge value={data.confidence} />
          </p>
          <h1 className="mt-3 font-display text-3xl leading-tight sm:text-4xl md:text-5xl">
            {data.title}
          </h1>
          {data.tags.length > 0 && (
            <div className="mt-5 flex flex-wrap gap-2">
              {data.tags.map((tag) => (
                <Link
                  key={tag}
                  to="/tags/$tag"
                  params={{ tag }}
                  className="focus-ring inline-flex min-h-11 items-center border-2 border-border bg-butter px-4 py-2 font-mono text-xs uppercase transition-transform duration-100 hover:-translate-x-0.5 hover:-translate-y-0.5 hover:brightness-95 hover:shadow-brutal"
                >
                  #{tag}
                </Link>
              ))}
            </div>
          )}
        </header>
        <div className="markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm, remarkCallouts]}
            rehypePlugins={[rehypeSlug]}
            components={{
              blockquote: CalloutBlockquote,
              img: MarkdownImage,
              table: MarkdownTable,
            }}
          >
            {body}
          </ReactMarkdown>
        </div>
        <aside className="mt-16 border-t-3 border-border pt-8">
          <h2 className="font-display text-2xl">Referenced by</h2>
          {links.isPending ? (
            <LoadingState />
          ) : links.isError ? (
            <ErrorState message={links.error.message} />
          ) : backlinks.length === 0 ? (
            <p className="mt-5 font-mono text-xs text-muted">
              No pages link here yet.
            </p>
          ) : (
            <ul className="mt-5 space-y-3">
              {backlinks.map((link) => (
                <li key={link.slug}>
                  <Link
                    className="focus-ring font-display text-lg underline"
                    to="/p/$slug"
                    params={{ slug: link.slug }}
                  >
                    {link.title || link.slug}
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </aside>
      </article>
      <TableOfContents headings={headings} />
      {selection ? (
        <CorrectionPopover
          page={data}
          pending={selection}
          onClose={() => setSelection(null)}
        />
      ) : null}
    </div>
  );
}

// Re-exported so tests can confirm the type stays identical.
export type { Correction };
