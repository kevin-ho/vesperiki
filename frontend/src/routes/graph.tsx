import { createFileRoute } from '@tanstack/react-router';
import { lazyRouteComponent } from '@tanstack/react-router';

export const Route = createFileRoute('/graph')({
  component: lazyRouteComponent(() => import('./graph.lazy'), 'GraphPage'),
});
