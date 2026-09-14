import { Link } from '@tanstack/react-router';

const colors = ['bg-sage', 'bg-butter', 'bg-peach', 'bg-rose', 'bg-lavender'];
export function TagChip({ tag }: { tag: string }) {
  const hash = [...tag].reduce((total, char) => total + char.charCodeAt(0), 0);
  return (
    <Link
      to="/tags/$tag"
      params={{ tag }}
      className={`focus-ring inline-flex min-h-11 items-center border-2 border-border px-4 py-2 font-mono text-xs font-medium uppercase transition-transform duration-100 hover:-translate-x-0.5 hover:-translate-y-0.5 hover:brightness-95 hover:shadow-brutal ${colors[hash % colors.length]}`}
    >
      #{tag}
    </Link>
  );
}
