/* Web Push Джарвиса. Запросы страницы не перехватывает — однофайловый SPA не кэшируется. */
self.addEventListener('push', function (event) {
  var title = 'Джарвис';
  var body = '';
  var tag = '';
  try {
    var data = event.data ? event.data.json() : {};
    if (data && data.title) title = String(data.title);
    if (data && data.body) body = String(data.body);
    if (data && data.tag) tag = String(data.tag);
  } catch (e) {
    try { body = event.data ? event.data.text() : ''; } catch (e2) {}
  }
  var opts = { body: body, icon: '/apple-touch-icon.png' };
  if (tag) opts.tag = tag;
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
    for (var i = 0; i < list.length; i++) {
      if (list[i].focus) return list[i].focus();
    }
    if (self.clients.openWindow) return self.clients.openWindow('/');
  }));
});
