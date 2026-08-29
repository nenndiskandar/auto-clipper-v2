const CACHE_NAME = 'yt-clipper-v3';
const ASSETS = [
  '/',
  '/index.html',
  '/create.html',
  '/session.html',
  '/tasks.html',
  '/settings.html',
  '/templates.js',
  '/pwa.js',
  '/manifest.json'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.map(k => k !== CACHE_NAME && caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.url.includes('/api/')) return;
  // media (video/thumb/download) jangan di-cache: selalu ambil dari network terbaru
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('/video/') || u.pathname.startsWith('/download/') || u.pathname.startsWith('/thumb/')) {
    e.respondWith(fetch(e.request));
    return;
  }
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).catch(() => caches.match('/')))
  );
});