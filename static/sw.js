const CACHE = "networkmap-shell-v10";
const DEVICE_IMAGES = [
  "router", "switch", "server", "workstation", "laptop", "mobile", "access-point",
  "firewall", "printer", "camera", "storage", "nas", "iot", "cloud", "other"
].map(kind => `/static/devices/${kind}.svg`);
const SHELL = ["/", "/static/styles.css", "/static/areas.js", "/static/app.js", "/static/favicon.svg", "/static/manifest.webmanifest", ...DEVICE_IMAGES];
const SHELL_PATHS = new Set([...SHELL, "/index.html"]);

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
