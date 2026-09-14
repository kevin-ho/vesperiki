import { createFileRoute } from '@tanstack/react-router';
import { lazyRouteComponent } from '@tanstack/react-router';

export const Route = createFileRoute('/tags')({
  component: lazyRouteComponent(() => import('./tags.lazy'), 'TagsPage'),
});