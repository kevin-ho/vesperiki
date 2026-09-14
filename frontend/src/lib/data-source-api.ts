import { getDataSource } from './data-source';
import type { DataSource } from './data-source';

/**
 * Resolve the active {@link DataSource} (WASM when a secure context
 * + OPFS is available, HTTP otherwise).
 *
 * This is an async ACCESSOR, not a React hook: TanStack Query
 * queryFn closures can't call hooks, so routes use it as
 * `const ds = await useDataSource(); return ds.getPage(slug);`.
 *
 * Deliberately a pure pass-through to {@link getDataSource}: the
 * factory already pins its in-flight boot promise
 * (`activeSourcePromise`), so concurrent callers share one sqlite
 * instance and the WASM DB is never initialized twice. Wrapping
 * that pin with another cache layer here would add state for no
 * benefit — pass-through preserves the factory's single-boot
 * guarantee by construction.
 */
export function useDataSource(): Promise<DataSource> {
  return getDataSource();
}
