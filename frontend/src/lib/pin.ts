/**
 * `pin.ts` — page pinning for offline availability.
 *
 * A pinned page is a slug the user has marked as "I want this
 * available on a plane." On pin we:
 *
 *   1. Write the slug to a localStorage list (order matters; the
 *      UI shows recent pins first).
 *   2. Walk the page body for `(/api/media/{id})` references and
 *      prefetch each into the offline-friendly `vesperiki-media`
 *      cache via the Cache API. The service worker already handles
 *      same-origin GETs, so the second time the page is opened
 *      the image comes straight out of the cache.
 *
 * LocalStorage is the right tool here because we're storing small,
 * synchronous, infrequently-updated data. IndexedDB would be
 * overkill. The media bytes themselves go to the Cache API, which
 * is the only storage the service worker can also serve from.
 */

import { relativeTime } from './format';

export const PIN_STORAGE_KEY = 'vesperiki-pinned-slugs';
/** Cache name used for the pinned media blobs. */
export const MEDIA_CACHE_NAME = 'vesperiki-media-v1';

export interface PinState {
  /** Slugs in pin order (newest last). */
  slugs: string[];
}

const MEDIA_SRC_PATTERN = /\(\/api\/media\/(\d+)\)/g;

/**
 * Read the current pin list from localStorage. Returns `[]` when
 * the value is missing or corrupt — pinning should never crash the
 * page because of bad storage state.
 */
export function readPinnedSlugs(): string[] {
  if (typeof localStorage === 'undefined') return [];
  const raw = localStorage.getItem(PIN_STORAGE_KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((s): s is string => typeof s === 'string');
  } catch {
    return [];
  }
}

/**
 * Persistent write of the pinned-slug list.
 */
export function writePinnedSlugs(slugs: string[]): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(slugs));
  } catch {
    /* storage quota / private mode — non-fatal */
  }
}

/** True if `slug` is in the pin list. */
export function isPinned(slug: string): boolean {
  return readPinnedSlugs().includes(slug);
}

/**
 * Toggle a slug's pin state. Returns the new pinned set so callers
 * can render the resulting state without re-reading storage.
 */
export function togglePin(slug: string): { slugs: string[]; pinned: boolean } {
  const current = readPinnedSlugs();
  const idx = current.indexOf(slug);
  let pinned: boolean;
  let next: string[];
  if (idx >= 0) {
    next = [...current.slice(0, idx), ...current.slice(idx + 1)];
    pinned = false;
  } else {
    next = [...current, slug];
    pinned = true;
  }
  writePinnedSlugs(next);
  return { slugs: next, pinned };
}

/**
 * Pull all `(id)` media references out of a page body so we know
 * which URLs to prefetch.
 */
export function mediaReferencesIn(body: string): number[] {
  const ids = new Set<number>();
  for (const match of body.matchAll(MEDIA_SRC_PATTERN)) {
    const id = Number.parseInt(match[1] ?? '', 10);
    if (Number.isFinite(id)) ids.add(id);
  }
  return [...ids];
}

/**
 * Fetch each media URL once and put it in the offline cache.
 * Returns the number of URLs successfully cached. Failures are
 * swallowed silently — pinning should never block the UI, and a
 * later sync or page-load will re-attempt via the cache fetch
 * handler in the service worker.
 */
export async function prefetchMedia(ids: number[]): Promise<number> {
  if (typeof caches === 'undefined') return 0;
  if (ids.length === 0) return 0;
  let cache: Cache;
  try {
    cache = await caches.open(MEDIA_CACHE_NAME);
  } catch {
    return 0;
  }
  let ok = 0;
  await Promise.all(
    ids.map(async (id) => {
      const req = `/api/media/${id}`;
      try {
        const res = await fetch(req);
        if (res.ok) {
          await cache.put(req, res.clone());
          ok += 1;
        }
      } catch {
        /* offline / 5xx — will retry on next open */
      }
    }),
  );
  return ok;
}

/**
 * Format a human-readable last-pin-time. Re-exported here so the
 * UI module doesn't pull in the whole `format.ts` if it needs
 * something specific to pins later.
 */
export { relativeTime };
