import { Link } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { getDataSource } from '../lib/data-source';
import { ErrorState, EmptyState, LoadingState } from '../components/PageList';

export function TagsPage() {
  const tags = useQuery({
    queryKey: ['tags'],
    queryFn: async () => {
      const ds = await getDataSource();
      return ds.getTags();
    },
  });
  return (
    <section>
      <h1 className="font-display text-3xl sm:text-4xl">Explore tags</h1>
      {tags.isPending ? (
        <div className="mt-10">
          <LoadingState />
        </div>
      ) : tags.isError ? (
        <div className="mt-10">
          <ErrorState message={tags.error.message} />
        </div>
      ) : !tags.data.length ? (
        <div className="mt-10">
          <EmptyState message="No tags have been added yet." />
        </div>
      ) : (
        <ul className="mt-10 grid grid-cols-1 gap-5 sm:grid-cols-2">
          {tags.data.map((tag) => (
            <li key={tag.name}>
              <Link
                to="/tags/$tag"
                params={{ tag: tag.name }}
                className="focus-ring flex min-h-11 items-center justify-between border-3 border-border bg-butter p-5 font-display text-xl shadow-brutal transition-transform duration-100 hover:-translate-x-0.5 hover:-translate-y-0.5 hover:brightness-95"
              >
                <span>#{tag.name}</span>
                <span className="font-mono text-xs">{tag.count}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}