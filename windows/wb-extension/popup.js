const byId = id => document.getElementById(id);
async function refresh() {
  const config = await chrome.storage.local.get(['server', 'enabled', 'lastPoll', 'lastRead', 'readOk', 'readCode', 'error']);
  if (!byId('server').value) byId('server').value = config.server || '';
  const lines = [config.enabled ? 'Подключение включено.' : 'Подключение остановлено.'];
  if (config.lastPoll) lines.push('Связь: ' + new Date(config.lastPoll).toLocaleTimeString('ru-RU'));
  if (config.lastRead) lines.push(config.readOk ? 'Последнее чтение WB прошло.' : 'WB не прочитан: ' + (config.readCode || 'откройте кабинет'));
  byId('status').textContent = lines.join('\n');
  byId('error').textContent = config.error || '';
}
byId('connect').addEventListener('click', async () => {
  try {
    const url = new URL(byId('server').value.trim());
    if (url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)
        || !(url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(url.hostname)))) {
      throw new Error('Укажите HTTPS-адрес Джарвиса без пути после домена.');
    }
    // Request before the first await, while Chrome still has the button gesture.
    const granted = await chrome.permissions.request({ origins: [url.origin + '/*'] });
    if (!granted) throw new Error('Доступ к вашему Джарвису не разрешён.');
    const existing = await chrome.storage.local.get(['token', 'server']);
    const token = byId('token').value.trim() || (existing.server === url.origin ? existing.token : '');
    if (!/^wbpc_[A-Za-z0-9_-]{40,60}$/.test(token || '')) throw new Error('Вставьте ключ ПК из настроек Джарвиса.');
    await chrome.storage.local.set({ server: url.origin, token, enabled: true, error: '' });
    await chrome.storage.local.remove('pending');
    byId('token').value = '';
    chrome.runtime.sendMessage({ type: 'poll' });
    await refresh();
  } catch (error) { byId('error').textContent = error.message; }
});
byId('disconnect').addEventListener('click', async () => {
  await chrome.storage.local.set({ enabled: false });
  await chrome.storage.local.remove(['token', 'pending']);
  await refresh();
});
byId('open').addEventListener('click', () => chrome.tabs.create({ url: 'https://www.wildberries.ru/lk/myorders/delivery' }));
byId('poll').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'poll' }, refresh));
refresh();
setInterval(refresh, 2000);
