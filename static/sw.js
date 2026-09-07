const CACHE = "networkmap-shell-v6";
const SHELL = ["/", "/static/styles.css", "/static/app.js", "/static/favicon.svg", "/static/manifest.webmanifest"];
const SHELL_PATHS = new Set(["/", "/index.html", "/static/styles.css", "/static/app.js", "/static/favicon.svg", "/static/manifest.webmanifest"]);

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || !SHELL_PATHS.has(url.pathname) || url.search || url.searchParams.has("token")) return;
  event.respondWith(fetch(request).then(response => {
    if (response.ok && !response.redirected) {
      const copy = response.clone();
      caches.open(CACHE).then(cache => cache.put(request, copy));
    }
    return response;
  }).catch(() => caches.match(request).then(hit => hit || (url.pathname === "/" || url.pathname === "/index.html" ? caches.match("/") : undefined))));
});
