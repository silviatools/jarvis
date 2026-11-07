const byId = id => document.getElementById(id);
async function refresh() {
  const config = await chrome.storage.local.get(['server', 'enabled', 'lastPoll', 'lastRead', 'readOk', 'readCode', 'error', 'readLogs']);
  if (!byId('server').value) byId('server').value = config.server || '';
  const lines = [config.enabled ? 'Подключение включено.' : 'Подключение остановлено.'];
  if (config.lastPoll) lines.push('Связь: ' + new Date(config.lastPoll).toLocaleTimeString('ru-RU'));
  if (config.lastRead) lines.push(config.readOk ? 'Последнее чтение WB прошло.' : 'WB не прочитан: ' + (config.readCode || 'откройте кабинет'));
  byId('status').textContent = lines.join('\n');
  byId('error').textContent = config.error || '';
  const logs = config.readLogs?.length ? JSON.stringify({ extensionVersion: chrome.runtime.getManifest().version, checks: config.readLogs }, null, 2) : '';
  if (byId('logs').value !== logs) byId('logs').value = logs;
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
byId('diagnose').addEventListener('click', async () => {
  byId('diagnose').disabled = true;
  byId('diagnosticStatus').textContent = 'Читаю WB и записываю логи, до 30 секунд…';
  try {
    const reply = await chrome.runtime.sendMessage({ type: 'diagnose' });
    byId('diagnosticStatus').textContent = reply?.ok ? 'Проверка записана. Нажмите «Скопировать логи».' : reply?.error || 'Не удалось запустить проверку.';
    await refresh();
  } catch (_) { byId('diagnosticStatus').textContent = 'Не удалось связаться с расширением. Перезагрузите его в chrome://extensions.'; }
  finally { byId('diagnose').disabled = false; }
});
byId('copyLogs').addEventListener('click', async () => {
  if (!byId('logs').value) { byId('diagnosticStatus').textContent = 'Сначала нажмите «Проверить WB».'; return; }
  try {
    await navigator.clipboard.writeText(byId('logs').value);
    byId('diagnosticStatus').textContent = 'Логи скопированы. Их можно вставить в чат.';
  } catch (_) {
    byId('logs').focus(); byId('logs').select();
    byId('diagnosticStatus').textContent = 'Отчёт выделен. Нажмите Ctrl+C.';
  }
});
byId('clearLogs').addEventListener('click', async () => {
  await chrome.storage.local.remove('readLogs');
  byId('diagnosticStatus').textContent = 'Логи удалены.';
  await refresh();
});
refresh();
setInterval(refresh, 2000);
