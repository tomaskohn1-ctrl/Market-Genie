// Market Genie Service Worker v2.0
// Strategy: Cache-first for static shell, network-first for API calls

const CACHE_NAME = 'market-genie-v2';
const SHELL_URLS = [
  '/',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600&display=swap',
  'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js',
  'https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js'
];

// ── Install: pre-cache the app shell ─────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      // Cache what we can; don't fail install if CDN is unreachable
      return Promise.allSettled(
        SHELL_URLS.map(url =>
          cache.add(url).catch(err => console.warn('[SW] Could not cache:', url, err))
        )
      );
    }).then(() => self.skipWaiting())
  );
});

// ── Activate: clean up old caches ────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(k => k !== CACHE_NAME)
          .map(k => {
            console.log('[SW] Deleting old cache:', k);
            return caches.delete(k);
          })
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch: network-first for /api/, cache-first for shell ────────────────────
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Always go to network for API calls — live data must be fresh
  if (url.pathname.startsWith('/api/') || url.hostname === 'ws.finnhub.io') {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(JSON.stringify({ error: 'offline', offline: true }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' }
        })
      )
    );
    return;
  }

  // For cross-origin requests (CDN fonts/scripts): cache-first
  if (url.origin !== self.location.origin) {
    event.respondWith(
      caches.match(event.request).then(cached => {
        if (cached) return cached;
        return fetch(event.request).then(response => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // For the app shell (same-origin HTML/assets): network-first with cache fallback
  event.respondWith(
    fetch(event.request)
      .then(response => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
        }
        return response;
      })
      .catch(() => caches.match(event.request).then(cached => {
        if (cached) return cached;
        // Offline fallback page
        return new Response(
          `<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
          <title>Market Genie — Offline</title>
          <style>body{background:#080810;color:#e2e8f0;font-family:Inter,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:16px}
          h1{color:#5b7fff;font-size:1.5rem}p{color:#718096;text-align:center;max-width:300px}</style></head>
          <body><h1>📡 Market Genie</h1><p>You're offline. Connect to the internet to get live market data.</p>
          <button onclick="location.reload()" style="background:#5b7fff;color:#fff;border:none;padding:10px 24px;border-radius:8px;cursor:pointer;font-size:1rem">Retry</button>
          </body></html>`,
          { headers: { 'Content-Type': 'text/html' } }
        );
      }))
  );
});

// ── Push notifications (future) ───────────────────────────────────────────────
self.addEventListener('push', event => {
  if (!event.data) return;
  const data = event.data.json();
  event.waitUntil(
    self.registration.showNotification(data.title || 'Market Genie Alert', {
      body: data.body || '',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      tag: data.tag || 'market-genie',
      data: { url: data.url || '/' }
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window' }).then(windowClients => {
      const url = event.notification.data.url;
      for (const client of windowClients) {
        if (client.url === url && 'focus' in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
