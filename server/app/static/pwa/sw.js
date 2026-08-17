/* deepseek-harness-cloud workspace service worker: minimal, network-first.
   The workspace is a live agent UI — never serve it stale. The SW exists for
   PWA installability plus icon caching only. */
const ICON_CACHE = "dshc-icons-v1";
self.addEventListener("install", (e) => { self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/pwa/icon-")) {
    e.respondWith(
      caches.open(ICON_CACHE).then(async (c) => {
        const hit = await c.match(e.request);
        if (hit) return hit;
        const res = await fetch(e.request);
        if (res.ok) c.put(e.request, res.clone());
        return res;
      })
    );
  }
  // everything else: straight to network (default), streams/WS untouched
});
