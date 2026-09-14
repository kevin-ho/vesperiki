/* vesperiki service worker
 *
 * Strategy:
 *   - Static shell (`/`, `/index.html`, `/manifest.webmanifest`, the four
 *     `/icon*.svg` icons referenced by the manifest, plus any hashed
 *     JS/CSS asset) is precached at install time.
 *   - Navigation requests (`mode: 'navigate'`) are served cache-first with a
 *     `/index.html` fallback so deep links work offline.
 *   - Same-origin asset GETs use stale-while-revalidate so updated assets
 *     roll in on the next request.
 *   - API GETs use network-first and only fall back to cache for the small
 *     set of read-only endpoints the offline reader needs (`/api/sync`,
 *     `/api/pages/:slug`, `/api/pages?…`, `/api/tags`, `/api/graph`).
 *   - POST/PUT/PATCH/DELETE and any cross-origin request bypass the SW.
 */

const VERSION = 'vesperiki-v1.3';
const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/icon.svg',
  '/icon-192.svg',
  '/icon-512.svg',
  '/icon-maskable.svg',
];

// Build-time manifest of every hashed JS/CSS/WASM chunk plus the
// unhashed sqlite-wasm worker files. Emitted by vite.config.ts's
// emitSwPrecacheManifest() plugin to dist/assets/sw-assets.js so the
// existing /assets StaticFiles mount delivers it as JS. Fetched via
// importScripts() in the install handler so the manifest stays out of
// this source file (hashes change every build).
const PRECACHE_MANIFEST_URL = '/assets/sw-assets.js';

const CACHEABLE_API = (path) =>
  path === '/api/sync' ||
  path === '/api/tags' ||
  path === '/api/graph' ||
  path === '/api/orphans' ||
  path === '/api/pages' ||
  /^\/api\/pages\/[^/]+$/.test(path) ||
  /^\/api\/pages\/[^/]+\/revisions$/.test(path) ||
  /^\/api\/search$/.test(path);

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(VERSION);
      await cache.addAll(SHELL);
      const assets = await loadPrecacheAssets();
      if (assets && assets.length > 0) await cache.addAll(assets);
      await self.skipWaiting();
    })(),
  );
});

// Load the build-time manifest of hashed assets to precache. Returns
// null if the manifest is missing (dev server without a build, older
// deployment) so the install handler can skip the extra cache.addAll
// and fall back to SHELL-only precaching — same behavior as v1.2.
async function loadPrecacheAssets() {
  try {
    importScripts(PRECACHE_MANIFEST_URL);
    const assets = self.VESPERIKI_PRECACHE_ASSETS;
    return Array.isArray(assets) && assets.length > 0 ? assets : null;
  } catch (error) {
    return null;
  }
}

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== VERSION).map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // API: network-first with cache fallback for read-only endpoints.
  if (url.pathname.startsWith('/api/')) {
    if (!CACHEABLE_API(url.pathname)) return;
    event.respondWith(networkFirst(request));
    return;
  }

  // SPA navigations: cache-first with index.html fallback.
  if (request.mode === 'navigate') {
    event.respondWith(navigationFallback(request));
    return;
  }

  // Hashed assets: stale-while-revalidate.
  event.respondWith(staleWhileRevalidate(request));
});

async function networkFirst(request) {
  const cache = await caches.open(VERSION);
  try {
    const response = await fetch(request);
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    return new Response(
      JSON.stringify({ code: 'offline', message: 'Offline and no cached response.' }),
      { status: 503, headers: { 'content-type': 'application/json' } },
    );
  }
}

async function navigationFallback(request) {
  const cache = await caches.open(VERSION);
  const cached = await cache.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response && response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    const index = await cache.match('/index.html');
    if (index) return index;
    return new Response('<h1>Offline</h1>', {
      status: 503,
      headers: { 'content-type': 'text/html' },
    });
  }
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(VERSION);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((response) => {
      if (response && response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => null);
  return cached || (await network) || new Response('', { status: 504 });
}