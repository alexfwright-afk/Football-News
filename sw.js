/* Service worker — makes the home-screen app work offline and start fast.
 *
 * Strategy, and the reason for it:
 *
 *   code and data (html, js, json, manifest) → NETWORK FIRST, cache as backup
 *   images (icons)                           → cache first, they never change
 *
 * The app used to serve the HTML from cache first and refresh in the
 * background. That is the textbook advice, and it was wrong here: a
 * home-screen app on iOS gets killed the moment it returns a response, so the
 * background refresh often never finished. The result was an app that showed
 * an old version until you deleted it and added it back. Fetching the code
 * from the network first costs a fraction of a second on a normal connection
 * and means what you see is always what was deployed. Offline still works —
 * the cache is right there as the fallback.
 *
 * Note: browsers only allow service workers on https:// or localhost. Over
 * plain http on your home wifi it won't register — the app still works, just
 * without offline support.
 */

const VERSION = "football-news-v5";
const SHELL = [
  "./",
  "./index.html",
  "./config.js",
  "./manifest.webmanifest",
  "./apple-touch-icon.png",
  "./icon-192.png",
  "./icon-512.png",
  "./favicon-32.png",
  "./favicon-64.png",
  "./favicon-180.png",
];

/* Anything that can change between deploys. Images are excluded on purpose:
   they're the big files, they don't change, and a VERSION bump clears them. */
const LIVE = /\.(?:html|js|json|webmanifest)$/i;

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
      .then(async () => {
        // an app already open should pick the new version up without being
        // force-quit; the page decides whether to act on this
        const tabs = await self.clients.matchAll({ type: "window" });
        tabs.forEach(c => c.postMessage({ swUpdated: VERSION }));
      })
  );
});

self.addEventListener("fetch", event => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // the push server etc.

  const isDoc  = req.mode === "navigate";
  const isLive = isDoc || LIVE.test(url.pathname) || url.pathname.endsWith("/");

  if (isLive) {
    event.respondWith((async () => {
      try {
        // no-store so GitHub's CDN headers can't hand back yesterday's copy
        const res = await fetch(req, { cache: "no-store" });
        if (res && res.status === 200) {
          // awaited, not fired-and-forgotten: respondWith keeps this worker
          // alive until the promise settles, so the write always completes
          const copy = res.clone();
          const cache = await caches.open(VERSION);
          await cache.put(req, copy);
        }
        return res;
      } catch (e) {
        const hit = await caches.match(req);
        if (hit) return hit;
        if (isDoc) {
          const shell = await caches.match("./index.html") || await caches.match("./");
          if (shell) return shell;
        }
        if (url.pathname.endsWith("posts.json")) {
          return new Response("[]", { headers: { "Content-Type": "application/json" } });
        }
        throw e;
      }
    })());
    return;
  }

  // images: straight from cache, fetched and stored the first time
  event.respondWith((async () => {
    const hit = await caches.match(req);
    if (hit) return hit;
    const res = await fetch(req);
    if (res && res.status === 200) {
      const copy = res.clone();
      const cache = await caches.open(VERSION);
      await cache.put(req, copy);
    }
    return res;
  })());
});

/* The page posts this after an update so a new build can take over at once. */
self.addEventListener("message", e => {
  if (e.data === "skipWaiting") self.skipWaiting();
});

/* ══════════════════════════════════════════════════════════════════════════
   NOTIFICATIONS
   The server sends an empty push — a nudge, nothing more. No headline
   travels through Apple's or Google's servers. We then ask our own Worker
   what the news actually was, so the text stays between you and your site.
   ══════════════════════════════════════════════════════════════════════════ */

try { importScripts("config.js"); } catch (e) { /* not configured yet */ }

const b64url = buf =>
  btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

/* Same derivation the Worker uses, so it recognises which queue is ours. */
async function deviceId() {
  const sub = await self.registration.pushManager.getSubscription();
  if (!sub) return null;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(sub.endpoint));
  return b64url(digest).slice(0, 22);
}

self.addEventListener("push", event => {
  event.waitUntil((async () => {
    const cfg = self.FN_CONFIG || {};
    let items = [];

    const server = (function(u){
      u = (u || "").trim().replace(/\/+$/, "");
      return !u ? "" : (/^https?:\/\//.test(u) ? u : "https://" + u);
    })(cfg.pushServer);

    if (server) {
      try {
        const id = await deviceId();
        if (id) {
          const r = await fetch(server + "/pending?id=" + encodeURIComponent(id), { cache: "no-store" });
          if (r.ok) items = (await r.json()).items || [];
        }
      } catch (e) { /* fall through to the generic one below */ }
    }

    // A push must always produce a visible notification — browsers revoke the
    // permission of apps that swallow them.
    if (!items.length) {
      items = [{ title: "RD Football News", body: "Something new about someone you follow.", url: "" }];
    }

    for (const it of items.slice(0, 3)) {
      await self.registration.showNotification(it.title || "RD Football News", {
        body: it.body || "",
        icon: "icon-192.png",
        badge: "favicon-64.png",
        data: { url: it.url || "./" },
        tag: it.url || undefined,        // same post twice replaces, never stacks
        timestamp: it.ts ? Date.parse(it.ts) : Date.now(),
      });
    }
  })());
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "./";
  event.waitUntil((async () => {
    // If the app is already open, bring it forward rather than opening a copy.
    const tabs = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of tabs) {
      if (c.url.includes(self.registration.scope) && "focus" in c) {
        await c.focus();
        if (target && target.startsWith("http")) c.postMessage({ open: target });
        return;
      }
    }
    await self.clients.openWindow(target && target.startsWith("http") ? target : "./");
  })());
});
