import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider, createRouter } from '@tanstack/react-router';
import { routeTree } from './routeTree.gen';
import { subscribeDataChanged } from './lib/data-source';
import './styles/layout.css';

const queryClient = new QueryClient();
const router = createRouter({ routeTree });

// A successful sync means the whole local DB changed, so every route
// query is stale. Subscribed at MODULE scope — not inside a React
// effect: effects run child-first, so the root layout's mount sync
// would complete before an App-level subscription effect ever ran,
// re-creating the cold-start race (the sync populates the WASM DB
// but the cached empty home query is never invalidated). Module
// scope guarantees the listener exists before any sync runs.
subscribeDataChanged(() => {
  void queryClient.invalidateQueries();
});

export function App() { return <QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>; }
