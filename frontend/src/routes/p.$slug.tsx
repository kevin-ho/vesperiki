import { createFileRoute } from '@tanstack/react-router';
import { lazyRouteComponent } from '@tanstack/react-router';

export const Route = createFileRoute('/p/$slug')({
  component: lazyRouteComponent(() => import('./p.$slug.lazy'), 'PageReader'),
});