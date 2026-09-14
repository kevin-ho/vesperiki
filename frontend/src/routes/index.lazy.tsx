import { useQuery } from '@tanstack/react-query';
import { getDataSource } from '../lib/data-source';
import { ErrorState, LoadingState, PageList } from '../components/PageList';

export function HomePage() {
  const pages = useQuery({
    queryKey: ['recent-pages'],
    queryFn: async () => {
      const ds = await getDataSource();
      return ds.getRecent();
    },
    staleTime: 30_000,
  });

  return (
    <section>
      <h1 className="font-display text-3xl leading-none sm:text-4xl md:text-5xl">
        Recent pages
      </h1>
      <p className="mt-5 max-w-xl text-lg text-muted">
        The newest notes, ideas, and connections in the garden.
      </p>
      <div className="mt-12">
        {pages.isPending ? (
          <LoadingState />
        ) : pages.isError ? (
          <ErrorState message={pages.error.message} />
        ) : (
          <PageList pages={pages.data.pages} />
        )}
      </div>
    </section>
  );
}