import { Link } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { getDataSource } from '../lib/data-source';
import { ErrorState, EmptyState, LoadingState } from '../components/PageList';
import { Route } from './tags.$tag';

export function TagPage() {
  const { tag } = Route.useParams();
  const pages = useQuery({
    queryKey: ['tag', tag],
    queryFn: async () => {
      const ds = await getDataSource();
      return ds.list({ tag, status: 'active', limit: 100 });
    },
  });
  return (
    <section>
      <p className="font-mono uppercase text-muted">Filtered pages</p>
      <h1 className="font-display text-3xl sm:text-4xl">#{tag}</h1>
      <div className="mt-10">
        {pages.isPending ? (
          <LoadingState />
        ) : pages.isError ? (
          <ErrorState message={pages.error.message} />
        ) : !pages.data.pages.length ? (
          <EmptyState message="No pages carry this tag." />
        ) : (
          <ul className="space-y-5">
            {pages.data.pages.map((page) => (
              <li
                key={page.slug}
                className="border-3 border-border bg-surface p-5 shadow-brutal transition-transform duration-100 hover:-translate-x-0.5 hover:-translate-y-0.5 hover:shadow-brutal"
              >
                <Link
                  className="focus-ring font-display text-xl underline"
                  to="/p/$slug"
                  params={{ slug: page.slug }}
                >
                  {page.title}
                </Link>
                <p className="mt-1 font-mono text-xs text-muted">/{page.slug}</p>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}