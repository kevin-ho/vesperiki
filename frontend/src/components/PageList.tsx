import { Link } from '@tanstack/react-router';
import type { Page } from '../lib/types';
import { relativeTime } from '../lib/format';
import { TagChip } from './TagChip';

export function PageList({ pages }: { pages: Page[] }) {
  if (!pages.length) return <EmptyState message="No pages yet. Your knowledge garden is ready." />;
  return (
    <div className="flex flex-col gap-8">
      {pages.map((page) => (
        <article
          key={page.slug}
          className="border-3 border-border bg-surface p-6 shadow-brutal transition-transform duration-100 hover:-translate-x-0.5 hover:-translate-y-0.5"
        >
          <div className="mb-3 flex items-center gap-3 font-mono text-xs uppercase text-muted">
            <span className="bg-peach px-2 py-1 font-bold text-text">{page.type}</span>
            <span>{relativeTime(page.updated_at)}</span>
          </div>
          <h2 className="font-display text-2xl leading-tight">
            <Link className="focus-ring" to="/p/$slug" params={{ slug: page.slug }}>
              {page.title}
            </Link>
          </h2>
          <p className="mt-1 font-mono text-xs uppercase text-muted">/{page.slug}</p>
          {page.body && (
            <p className="mt-4 line-clamp-4 text-base text-muted">
              {page.body.slice(0, 200)}
              {page.body.length > 200 ? '…' : ''}
            </p>
          )}
          {page.tags.length > 0 && (
            <div className="mt-5 flex flex-wrap gap-2">
              {page.tags.map((tag) => (
                <TagChip key={tag} tag={tag} />
              ))}
            </div>
          )}
        </article>
      ))}
    </div>
  );
}
export function EmptyState({ message }: { message: string }) { return <div className="border-3 border-border bg-butter p-8 text-center text-lg shadow-brutal">{message}</div>; }
export function LoadingState() { return <p role="status" className="border-3 border-border bg-sage p-5 font-mono">Loading…</p>; }
export function ErrorState({ message }: { message: string }) { return <p role="alert" className="border-3 border-border bg-rose p-5 font-mono">Could not load this garden: {message}</p>; }
