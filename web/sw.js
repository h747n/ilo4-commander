/* Service worker: makes the panel installable and opens instantly.
   Only the app shell is cached. API calls and the live socket always go to
   the network, so nothing you see about a server can ever be stale. */
const CACHE = "ilo4c-{{V}}";
const SHELL = ["/", "/style.css?v={{V}}", "/app.js?v={{V}}", "/history.js?v={{V}}",
               "/manifest.webmanifest", "/icons/icon.svg", "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws")) return;
  // Network first, so an upgrade shows up immediately; cache when offline.
  e.respondWith(
    fetch(e.request)
      .then(r => {
        if (r.ok && SHELL.includes(url.pathname + url.search)) {
          const copy = r.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return r;
      })
      .catch(() => caches.match(e.request).then(r => r || caches.match("/")))
  );
});
