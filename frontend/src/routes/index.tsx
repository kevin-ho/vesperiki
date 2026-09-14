import { createFileRoute } from '@tanstack/react-router';
import { lazyRouteComponent } from '@tanstack/react-router';

export const Route = createFileRoute('/')({
  component: lazyRouteComponent(() => import('./index.lazy'), 'HomePage'),
});