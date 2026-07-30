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

const VERSION = "football-news-v4";
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
