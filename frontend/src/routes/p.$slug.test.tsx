import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import type { Correction, Page } from '../lib/types';

// Mock the data layer BEFORE importing the component under test.
// Vitest hoists `vi.mock` calls above static imports.
vi.mock('../lib/api', () => ({
  getPageBySlug: vi.fn(),
  getBacklinks: vi.fn(),
  createCorrection: vi.fn(),
  resolveCorrection: vi.fn(),
  listCorrections: vi.fn(),
}));

// PageReader pulls its slug from the route module via
// `Route.useParams()`. A synthetic router (the `withRouter` helper
// from PageList.test.tsx) wouldn't carry the real `/p/$slug` route
// context, so we stub `Route.useParams()` directly here. The test
// still renders the real <PageReader /> component, so any regression
// in stripLeadingTitleHeading shows up in the rendered h1 count.
vi.mock('./p.$slug', () => ({
  Route: {
    useParams: () => ({ slug: 'forgejo' }),
  },
}));

import { createCorrection, getBacklinks, getPageBySlug } from '../lib/api';
import {
  ConfidenceBadge,
  formatConfidence,
  PageReader,
  stripLeadingTitleHeading,
} from './p.$slug.lazy';

const FIXED_NOW = new Date('2026-01-15T12:00:00Z').getTime();

const forgejoPage: Page = {
  slug: 'forgejo',
  title: 'Forgejo',
  type: 'note',
  tags: [],
  sources: [],
  body: '# Forgejo\n\nSome content.',
  status: 'published',
  updated_at: new Date(FIXED_NOW - 60_000).toISOString(),
};

function withQueryClient(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeAll(() => {
  vi.setSystemTime(new Date(FIXED_NOW));
});

afterAll(() => {
  vi.useRealTimers();
});

describe('stripLeadingTitleHeading', () => {
  it('strips the leading h1 when the heading text matches the page title', () => {
    const result = stripLeadingTitleHeading('# Forgejo\n\nBody.', 'Forgejo');
    expect(result).not.toContain('# Forgejo');
    expect(result).toContain('Body.');
  });

  it('leaves the heading untouched when the heading text differs from the page title', () => {
    const body = '# Different Title\n\nBody.';
    expect(stripLeadingTitleHeading(body, 'Forgejo')).toBe(body);
  });

  it('does not strip an h2 — leaves ## heading in place', () => {
    const body = '## Forgejo\n\nBody.';
    expect(stripLeadingTitleHeading(body, 'Forgejo')).toBe(body);
  });

  it('strips a multi-word title heading', () => {
    const result = stripLeadingTitleHeading(
      '# Distributed Systems\n\nBody.',
      'Distributed Systems',
    );
    expect(result).not.toContain('# Distributed Systems');
    expect(result).toContain('Body.');
  });

  it('returns the body unchanged when there is no leading heading', () => {
    const body = 'Just a paragraph.\n\nAnother line.';
    expect(stripLeadingTitleHeading(body, 'Forgejo')).toBe(body);
  });

  it('tolerates a leading blank line before the heading', () => {
    const result = stripLeadingTitleHeading(
      '\n# Forgejo\n\nBody.',
      'Forgejo',
    );
    expect(result).not.toContain('# Forgejo');
    expect(result).toContain('Body.');
  });

  it('strips when the heading is the last line with no trailing newline', () => {
    expect(stripLeadingTitleHeading('# Forgejo', 'Forgejo')).toBe('');
  });

  it('strips when the heading is followed by content on the next line', () => {
    const result = stripLeadingTitleHeading(
      '# Forgejo\nSome content.',
      'Forgejo',
    );
    expect(result).not.toContain('# Forgejo');
    expect(result).toContain('Some content.');
  });
});

describe('PageReader', () => {
  it('renders exactly one <h1> even when the body opens with a duplicate title heading (WCAG SC 1.3.1 / H42)', async () => {
    vi.mocked(getPageBySlug).mockResolvedValue(forgejoPage);
    vi.mocked(getBacklinks).mockResolvedValue([]);

    const { container } = withQueryClient(<PageReader />);

    // Wait for the page header h1 (the query resolves on the next microtask).
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'Forgejo' })).toBeInTheDocument();
    });

    // Regression guard: a duplicate `# Forgejo` in the body must not
    // produce a second <h1>. With the previous non-greedy regex, the
    // title-mismatch check failed and the body kept its own h1.
    expect(container.querySelectorAll('h1')).toHaveLength(1);
    expect(screen.getByRole('heading', { level: 1, name: 'Forgejo' })).toHaveTextContent('Forgejo');
  });

  it('sets document.title to include the page title once the page h1 renders (WCAG 2.4.2)', async () => {
    vi.mocked(getPageBySlug).mockResolvedValue(forgejoPage);
    vi.mocked(getBacklinks).mockResolvedValue([]);
    withQueryClient(<PageReader />);
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'Forgejo' })).toBeInTheDocument();
    });
    expect(document.title).toContain('Forgejo');
  });
});

describe('formatConfidence', () => {
  it('returns null for the verified default (1.0 and undefined)', () => {
    expect(formatConfidence(undefined)).toBeNull();
    expect(formatConfidence(1)).toBeNull();
    expect(formatConfidence(1.0)).toBeNull();
  });

  it('formats a 0..1 score as a rounded percentage string', () => {
    expect(formatConfidence(0.7)).toBe('70%');
    expect(formatConfidence(0.42)).toBe('42%');
    expect(formatConfidence(0)).toBe('0%');
  });
});

describe('ConfidenceBadge', () => {
  it('renders nothing when the score is missing or fully verified', () => {
    const { container: c1 } = render(<ConfidenceBadge value={undefined} />);
    const { container: c2 } = render(<ConfidenceBadge value={1} />);
    expect(c1.firstChild).toBeNull();
    expect(c2.firstChild).toBeNull();
  });

  it('renders a "Confidence: NN%" badge when the score is below 1.0', () => {
    render(<ConfidenceBadge value={0.7} />);
    const badge = screen.getByTestId('confidence-badge');
    expect(badge).toHaveTextContent('Confidence: 70%');
  });
});

describe('confidence badge on the page header', () => {
  it('does not render a badge when the page has confidence 1.0', async () => {
    vi.mocked(getPageBySlug).mockResolvedValue({
      ...forgejoPage,
      confidence: 1.0,
    });
    vi.mocked(getBacklinks).mockResolvedValue([]);
    withQueryClient(<PageReader />);
    expect(await screen.findByRole('heading', { level: 1, name: 'Forgejo' })).toBeInTheDocument();
    expect(screen.queryByTestId('confidence-badge')).toBeNull();
  });

  it('renders a confidence badge when confidence < 1.0', async () => {
    vi.mocked(getPageBySlug).mockResolvedValue({
      ...forgejoPage,
      confidence: 0.6,
    });
    vi.mocked(getBacklinks).mockResolvedValue([]);
    withQueryClient(<PageReader />);
    const badge = await screen.findByTestId('confidence-badge');
    expect(badge).toHaveTextContent('Confidence: 60%');
  });
});

describe('media rendering', () => {
  it('renders <img> for a markdown image whose src points at /api/media/{id}', async () => {
    vi.mocked(getPageBySlug).mockResolvedValue({
      ...forgejoPage,
      body: '![A lighthouse diagram](/api/media/42)',
    });
    vi.mocked(getBacklinks).mockResolvedValue([]);
    const { container } = withQueryClient(<PageReader />);
    const img = await waitFor(() => {
      const node = container.querySelector('img');
      expect(node).not.toBeNull();
      return node as HTMLImageElement;
    });
    expect(img.getAttribute('src')).toBe('/api/media/42');
    expect(img.getAttribute('alt')).toBe('A lighthouse diagram');
    expect(img.getAttribute('loading')).toBe('lazy');
  });

  it('falls back to a generic alt when the markdown image has none', async () => {
    vi.mocked(getPageBySlug).mockResolvedValue({
      ...forgejoPage,
      body: '![]( /api/media/8 )',
    });
    vi.mocked(getBacklinks).mockResolvedValue([]);
    const { container } = withQueryClient(<PageReader />);
    const img = await waitFor(() => {
      const node = container.querySelector('img');
      expect(node).not.toBeNull();
      return node as HTMLImageElement;
    });
    expect(img.getAttribute('alt')).toBe('page image');
    expect(img.getAttribute('loading')).toBe('lazy');
  });
});

describe('markdown table rendering', () => {
  const tableBody = '| A | B |\n|---|---|\n| 1 | 2 |';

  function renderTablePage() {
    vi.mocked(getPageBySlug).mockResolvedValue({
      ...forgejoPage,
      body: tableBody,
    });
    vi.mocked(getBacklinks).mockResolvedValue([]);
    return withQueryClient(<PageReader />);
  }

  it('renders the GFM table as a labelled RAC grid with header + row text', async () => {
    renderTablePage();
    // The React Aria Table replaces the old role="region" wrapper: the
    // labelled table itself carries the accessible name + table roles.
    const table = await screen.findByRole('grid', { name: 'Markdown table' });
    // GFM tables: first row becomes the header, each later row a body row.
    const headers = table.querySelectorAll('thead [role="columnheader"]');
    expect(headers).toHaveLength(2);
    expect(headers[0]).toHaveTextContent('A');
    expect(headers[1]).toHaveTextContent('B');
    const rows = table.querySelectorAll('tbody [role="row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toHaveTextContent('1');
    expect(rows[0]).toHaveTextContent('2');
  });

  it('wraps the RAC table in the .table-scroll container the layout.css rules target', async () => {
    const { container } = renderTablePage();
    const wrapper = await waitFor(() => {
      const node = container.querySelector('.table-scroll');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });
    // The wrapper is the RAC ResizableTableContainer — the scroll
    // container that owns horizontal overflow (layout.css); the real
    // <table> must live inside it, never as a sibling.
    const table = wrapper.querySelector('table');
    expect(table).not.toBeNull();
    // Structural assertion only: layout.css sets
    // `.markdown .table-scroll table { font-family: var(--font-display) }`.
    // jsdom cannot compute layout, so we verify the markup shape the
    // rule targets rather than any computed style.
    expect(table!.closest('.markdown')).not.toBeNull();
  });

  it('sorts rows ascending then descending when a column header is clicked', async () => {
    vi.mocked(getPageBySlug).mockResolvedValue({
      ...forgejoPage,
      body: '| Fruit | Qty |\n|---|---|\n| cherry | 3 |\n| apple | 1 |\n| banana | 2 |',
    });
    vi.mocked(getBacklinks).mockResolvedValue([]);
    const { container } = withQueryClient(<PageReader />);
    const user = userEvent.setup();

    // React Aria mounts each Column in multiple Collection contexts
    // (visible table + accessibility bookkeeping), so pick the one
    // actually rendered inside the visible <thead> where the press
    // handler is wired to the DOM node the user sees.
    const fruitHeader = await waitFor(() => {
      const node = container.querySelector(
        'thead [role="columnheader"][data-key="column-0"]',
      );
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });

    const rowTexts = () =>
      [...container.querySelectorAll('tbody [role="row"]')].map(
        (row) => row.textContent ?? '',
      );

    // First click on the header sorts ascending (apple, banana, cherry)
    // and shows the ascending indicator on the active column.
    await user.click(fruitHeader);
    await waitFor(() => {
      expect(rowTexts()[0]).toContain('apple');
      expect(rowTexts()[2]).toContain('cherry');
    });
    expect(fruitHeader).toHaveTextContent('↑');
    expect(fruitHeader).not.toHaveTextContent('↓');

    // Second click on the same header toggles back to descending.
    await user.click(fruitHeader);
    await waitFor(() => {
      expect(rowTexts()[0]).toContain('cherry');
      expect(rowTexts()[2]).toContain('apple');
    });
    expect(fruitHeader).toHaveTextContent('↓');
    expect(fruitHeader).not.toHaveTextContent('↑');
  });
});

describe('correction flag popover', () => {
  it('opens a popover when text is selected inside the article and POSTs the correction', async () => {
    vi.mocked(getPageBySlug).mockResolvedValue(forgejoPage);
    vi.mocked(getBacklinks).mockResolvedValue([]);
    const createdCorrection: Correction = {
      id: 7,
      page_slug: 'forgejo',
      selected_text: 'Forgejo runs on a small VPS',
      note: 'should mention ssh keys',
      status: 'pending',
      created_at: '2026-01-15T12:00:00Z',
    };
    vi.mocked(createCorrection).mockResolvedValue(createdCorrection);

    const { container } = withQueryClient(<PageReader />);
    const article = await waitFor(() => {
      const node = container.querySelector('article');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });
    // Pick a paragraph that lives inside the rendered markdown body
    // (not the header subtitle). The fixture strips its duplicate
    // title heading, leaving a single <p>Some content.</p>.
    const target = article.querySelector('.markdown p');
    expect(target).not.toBeNull();
    const range = document.createRange();
    range.selectNodeContents(target as HTMLElement);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);

    fireEvent.mouseUp(article);

    await waitFor(() => {
      expect(screen.getByTestId('correction-popover')).toBeInTheDocument();
    });
    expect(screen.getByTestId('correction-selected')).toHaveTextContent(
      /Some content/,
    );

    fireEvent.change(screen.getByTestId('correction-note'), {
      target: { value: 'should mention ssh keys' },
    });
    fireEvent.click(screen.getByRole('button', { name: /flag correction/i }));

    await waitFor(() => {
      expect(createCorrection).toHaveBeenCalledWith({
        page_slug: 'forgejo',
        selected_text: 'Some content.',
        note: 'should mention ssh keys',
      });
    });
    expect(
      await screen.findByTestId('correction-confirmation'),
    ).toHaveTextContent(/Correction flagged/);
  });

  it('does not open the popover when the selection is outside the article', async () => {
    vi.mocked(getPageBySlug).mockResolvedValue(forgejoPage);
    vi.mocked(getBacklinks).mockResolvedValue([]);
    const { container } = withQueryClient(<PageReader />);
    await waitFor(() => {
      expect(screen.getByRole('heading', { level: 1, name: 'Forgejo' })).toBeInTheDocument();
    });

    // Set up a selection on a paragraph that lives OUTSIDE the article.
    const outside = document.createElement('p');
    outside.textContent = 'Text that should not trigger the popover.';
    document.body.appendChild(outside);
    const range = document.createRange();
    range.selectNodeContents(outside);
    const selection = window.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);

    const article = container.querySelector('article');
    if (article) fireEvent.mouseUp(article);

    expect(screen.queryByTestId('correction-popover')).toBeNull();
    document.body.removeChild(outside);
  });
});