self.addEventListener('push', event => {
  const payload = event.data?.json() || {};
  event.waitUntil(self.registration.showNotification(payload.title || 'Silent Guardian', {
    body: payload.body || 'A movement event needs a check.',
    tag: payload.event_id || 'guardian', icon: '/guardian.svg',
    data: { url: payload.url || '/' }, requireInteraction: true
  }));
});
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL(event.notification.data.url, self.location.origin);
  if (url.origin === self.location.origin) event.waitUntil(clients.openWindow(url.href));
});
