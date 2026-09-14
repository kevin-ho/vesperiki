import { createFileRoute } from '@tanstack/react-router';
import { lazyRouteComponent } from '@tanstack/react-router';

export const Route = createFileRoute('/tags/$tag')({
  component: lazyRouteComponent(() => import('./tags.$tag.lazy'), 'TagPage'),
});