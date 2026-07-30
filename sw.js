/* Service worker — makes the home-screen app open instantly and work offline.
 *
 * Two strategies, because the two kinds of file want opposite things:
 *   app shell (html, icons, manifest) → cache first, refresh in the background
 *   posts.json                        → network first, fall back to cache
 *
 * So the app opens from cache in a fraction of a second even on a bad train
 * connection, but the posts you see are always the newest that could be reached.
 *
 * Note: browsers only allow service workers on https:// or localhost. Over plain
 * http on your home wifi it simply won't register — the app still works, just
 * without offline support.
 */

const VERSION = "romano-tracker-v2";
const SHELL = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./apple-touch-icon.png",
  "./icon-192.png",
  "./icon-512.png",
  "./favicon-32.png",
  "./favicon-64.png",
  "./favicon-180.png",
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(VERSION)
      // addAll is all-or-nothing; a single 404 would abort the install, so add
      // them one at a time and tolerate misses.
      .then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // relays, t.me — leave alone

  // posts.json: always try the network, keep a copy for offline
  if (url.pathname.endsWith("posts.json")) {
    event.respondWith(
      fetch(req)
        .then(res => {
          const copy = res.clone();
          caches.open(VERSION).then(c => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then(hit => hit || Response.json([])))
    );
    return;
  }

  // everything else: cache first, then quietly refresh it for next time
  event.respondWith(
    caches.match(req).then(hit => {
      const live = fetch(req)
        .then(res => {
          if (res && res.status === 200) {
            const copy = res.clone();
            caches.open(VERSION).then(c => c.put(req, copy));
          }
          return res;
        })
        .catch(() => hit);
      return hit || live;
    })
  );
});

/* The page posts this after an update so a new build can take over at once. */
self.addEventListener("message", e => {
  if (e.data === "skipWaiting") self.skipWaiting();
});
