/**
 * ESLint flat config — the ONLY rule enforced here is the project
 * convention that the SPA must never call `fetch()` outside the data
 * layer.
 *
 * `fetch` is a restricted global everywhere except:
 *   - `src/lib/api.ts`        — the HTTP API module (canonical network
 *                               calls; the data-source adapter and the
 *                               sync driver route everything through it)
 *   - `src/lib/pin.ts`        — media prefetch for pages pinned for
 *                               offline (spec: "the client eagerly
 *                               fetches and caches its media ... via
 *                               the Cache API")
 *
 * Everything else — route components, hooks, utilities — must import
 * from `src/lib/api.ts` or use the data-source layer instead.
 *
 * This file is deliberately self-contained (no plugins, no external
 * config) so it runs standalone and never blocks a build even if
 * eslint isn't installed in CI yet.
 */
export default [
  {
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-globals': [
        'error',
        {
          name: 'fetch',
          message:
            'Call api.ts or the data-source layer instead of fetch() directly. ' +
            'The SPA must never touch the network outside the data layer.',
        },
      ],
    },
  },
  {
    files: ['src/lib/api.ts', 'src/lib/pin.ts'],
    rules: {
      'no-restricted-globals': 'off',
    },
  },
];
