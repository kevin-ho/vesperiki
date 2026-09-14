/**
 * `useOfflineStatus` — single source of truth for the footer.
 *
 * Subscribes to the WASM data-source's sync state and exposes a
 * `SyncStatus` snapshot. Also wires the browser-level `online` /
 * `offline` events so the indicator flips correctly when the
 * device's connectivity changes.
 *
 * On mount the hook:
 *
 *   1. Probes the environment (`readSelectionInput`).
 *   2. If WASM is the right data source, calls `getDataSource()`
 *      which lazily boots the SQLite-WASM DB (OPFS when available,
 *      in-memory otherwise).
 *   3. Runs the first sync (fetches `/api/sync` and applies the
 *      delta locally).
 *   4. Requests `navigator.storage.persist()` so the offline copy
 *      is durable, and surfaces the grant in the indicator.
 *
 * Designed to be called exactly once per page (in the root layout).
 */

import { useEffect, useState } from 'react';
import {
  getDataSource,
  makeSyncDriver,
  readSelectionInput,
  selectDataSource,
} from './data-source';
import type { SyncStatus } from './types';
import type { SearchStrategy } from './wasm-data-source';

const initialStatus = (): SyncStatus => ({
  ready: false,
  lastSyncAt: null,
  cursor: 0,
  isOnline: typeof navigator === 'undefined' ? true : navigator.onLine !== false,
  syncState: 'idle',
  lastError: null,
  persisted: null,
  searchStrategy: 'fts5',
});

/**
 * Holder for the active sync driver so the hook can call it without
 * re-creating it on every state update. Lives outside React so the
 * module can be imported by code that doesn't render (e.g. tests).
 */
let driverSingleton: ReturnType<typeof makeSyncDriver> | null = null;
function getDriver() {
  if (!driverSingleton) driverSingleton = makeSyncDriver();
  return driverSingleton;
}

export function useOfflineStatus(): {
  status: SyncStatus;
  syncNow: () => Promise<void>;
  /** Underlying FTS strategy — surfaced separately so the UI can show it. */
  strategy: SearchStrategy;
} {
  const [status, setStatus] = useState<SyncStatus>(initialStatus);
  const [strategy, setStrategy] = useState<SearchStrategy>('fts5');

  // Boot + initial sync. Runs on mount and whenever connectivity
  // returns (the `online` listener below re-invokes it so the local
  // copy is refreshed as soon as the network is back).
  useEffect(() => {
    let cancelled = false;

    const boot = async () => {
      try {
        const input = await readSelectionInput();
        if (cancelled) return;
        const choice = selectDataSource(input);
        setStatus((prev) => ({
          ...prev,
          ready: choice === 'wasm',
          isOnline: input.isOnline,
        }));
        if (choice === 'wasm') {
          // Boot the WASM DB (module-level cache; no-op if already
          // up) then sync.
          await getDataSource();
          if (cancelled) return;
          setStatus((prev) => ({ ...prev, ready: true, syncState: 'syncing' }));
          await getDriver().sync();
          if (cancelled) return;
          const snap = await getDriver().status();
          setStatus((prev) => ({
            ...prev,
            syncState: 'ok',
            cursor: snap?.cursor ?? prev.cursor,
            lastSyncAt: snap?.lastSyncAt ?? prev.lastSyncAt,
            searchStrategy: snap?.searchStrategy ?? prev.searchStrategy,
          }));
          if (snap?.searchStrategy) setStrategy(snap.searchStrategy);
        }
      } catch (error) {
        if (cancelled) return;
        const message = error instanceof Error ? error.message : String(error);
        setStatus((prev) => ({
          ...prev,
          syncState: 'error',
          lastError: message,
          ready: false,
        }));
      }
    };

    void boot();

    const onlineHandler = () => {
      // Connectivity is back — refresh the indicator and pull any
      // deltas that accumulated while offline.
      void boot();
    };
    const offlineHandler = () => {
      setStatus((prev) => ({ ...prev, isOnline: false }));
    };
    window.addEventListener('online', onlineHandler);
    window.addEventListener('offline', offlineHandler);
    return () => {
      cancelled = true;
      window.removeEventListener('online', onlineHandler);
      window.removeEventListener('offline', offlineHandler);
    };
  }, []);

  // Ask the browser for durable storage on every cold start. The
  // browser may decline (storage pressure); we surface the answer
  // either way.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (typeof navigator === 'undefined' || !navigator.storage?.persist) {
        if (!cancelled) setStatus((prev) => ({ ...prev, persisted: null }));
        return;
      }
      try {
        const already = await navigator.storage.persisted();
        let granted = already;
        if (!already) granted = await navigator.storage.persist();
        if (!cancelled) setStatus((prev) => ({ ...prev, persisted: granted }));
      } catch {
        if (!cancelled) setStatus((prev) => ({ ...prev, persisted: false }));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const syncNow = async () => {
    setStatus((prev) => ({ ...prev, syncState: 'syncing', lastError: null }));
    try {
      // The source may not be booted if the footer rendered before
      // the effect ran (StrictMode double-render, slow OPFS open).
      // Boot it here so the button always works.
      const input = await readSelectionInput();
      if (selectDataSource(input) === 'wasm') {
        await getDataSource();
      }
      await getDriver().sync();
      const snap = await getDriver().status();
      setStatus((prev) => ({
        ...prev,
        syncState: 'ok',
        cursor: snap?.cursor ?? prev.cursor,
        lastSyncAt: snap?.lastSyncAt ?? prev.lastSyncAt,
      }));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setStatus((prev) => ({
        ...prev,
        syncState: 'error',
        lastError: message,
      }));
    }
  };

  return { status, syncNow, strategy };
}
