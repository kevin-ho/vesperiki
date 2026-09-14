import { Link, useSearch, useNavigate } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { Button, SearchField, Form, Input } from 'react-aria-components';
import { useMemo, useState } from 'react';
import { filterDeprecatedHits } from '../lib/api';
import { getDataSource } from '../lib/data-source';
import { EmptyState, ErrorState, LoadingState } from '../components/PageList';

export function SearchPage() {
  // Read the initial query from the URL (?q=...) so the top-bar
  // search can drive this page via navigation.
  const urlSearch = useSearch({ from: '/search' });
  const initialQ = typeof urlSearch.q === 'string' ? urlSearch.q : '';
  const [submitted, setSubmitted] = useState(initialQ);
  const navigate = useNavigate();

  const results = useQuery({
    queryKey: ['search', submitted],
    queryFn: async () => {
      const ds = await getDataSource();
      return ds.search({ q: submitted, include_body: true, limit: 50 });
    },
    enabled: submitted.length > 0,
  });
  const deprecatedPages = useQuery({
    queryKey: ['pages', { status: 'deprecated' }],
    queryFn: async () => {
      const ds = await getDataSource();
      return ds.list({ status: 'deprecated', limit: 500 });
    },
  });
  const deprecatedSlugs = useMemo(() => {
    if (!deprecatedPages.data) return undefined;
    return new Set(deprecatedPages.data.pages.map((p) => p.slug));
  }, [deprecatedPages.data]);
  const hits = filterDeprecatedHits(results.data ?? [], deprecatedSlugs);

  return (
    <section>
      <h1 className="font-display text-3xl sm:text-4xl">Search the garden</h1>
      <Form
        role="search"
        className="mt-8 flex flex-col gap-3 sm:flex-row"
        onSubmit={(event) => {
          event.preventDefault();
          const data = new FormData(event.currentTarget);
          const value = String(data.get('q') ?? '').trim();
          setSubmitted(value);
        }}
      >
        <SearchField
          name="q"
          aria-label="Search query"
          className="flex min-w-0 flex-1 flex-col"
          defaultValue={initialQ}
        >
          <Input
            placeholder="Try 'distributed systems'"
            className="focus-ring min-h-11 border-3 border-border bg-surface px-4 py-3 font-mono"
          />
        </SearchField>
        <Button
          type="submit"
          className="focus-ring inline-flex min-h-11 items-center justify-center border-3 border-border bg-sage px-5 font-mono font-bold uppercase shadow-brutal transition-transform duration-100 hover:-translate-x-0.5 hover:-translate-y-0.5 data-[pressed]:translate-x-1 data-[pressed]:translate-y-1 data-[pressed]:shadow-none"
        >
          Search
        </Button>
      </Form>
      <div aria-live="polite" className="mt-12">
        {submitted && results.isSuccess && !results.isError && (
          <p className="sr-only" data-testid="search-results-count">
            {hits.length} results for "{submitted}".
          </p>
        )}
        {!submitted ? (
          <EmptyState message="Enter a phrase to search titles, tags, and page text." />
        ) : results.isPending ? (
          <LoadingState />
        ) : results.isError ? (
          <ErrorState message={results.error.message} />
        ) : !hits.length ? (
          <EmptyState message="No pages matched that search." />
        ) : (
          <ul className="space-y-5">
            {hits.map((hit) => (
              <li
                key={hit.slug}
                className="border-3 border-border bg-surface p-5 shadow-brutal transition-transform duration-100 hover:-translate-x-0.5 hover:-translate-y-0.5 hover:shadow-brutal"
              >
                <Link
                  className="focus-ring font-display text-xl underline"
                  to="/p/$slug"
                  params={{ slug: hit.slug }}
                >
                  {hit.title}
                </Link>
                <p className="mt-2 text-muted">{hit.snippet}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
