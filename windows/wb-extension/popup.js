const byId = id => document.getElementById(id);
const shop = () => byId('shop').value === 'ozon' ? 'ozon' : 'wb';
const title = () => shop() === 'ozon' ? 'Ozon' : 'WB';
const logKey = () => shop() === 'ozon' ? 'ozonReadLogs' : 'readLogs';
let selectionLoaded = false;
byId('shop').addEventListener('change', async () => {
  selectionLoaded = true;
  await chrome.storage.local.set({ popupShop: shop() });
  await refresh();
});
byId('version').textContent = 'Версия ' + chrome.runtime.getManifest().version;
async function refresh() {
  const config = await chrome.storage.local.get(['server', 'enabled', 'lastPoll', 'lastRead', 'readOk', 'readCode', 'error', 'readLogs', 'ozonLastRead', 'ozonReadOk', 'ozonReadCode', 'ozonReadLogs', 'popupShop']);
  if (!selectionLoaded) { byId('shop').value = config.popupShop === 'ozon' ? 'ozon' : 'wb'; selectionLoaded = true; }
  const name = title(), ozon = shop() === 'ozon';
  byId('open').textContent = 'Открыть ' + name;
  byId('diagnosticTitle').textContent = 'Диагностика ' + name;
  byId('diagnose').textContent = 'Проверить ' + name + ' (открыть вкладку)';
  if (!byId('server').value) byId('server').value = config.server || '';
  const lines = [config.enabled ? 'Подключение включено.' : 'Подключение остановлено.'];
  if (config.lastPoll) lines.push('Связь: ' + new Date(config.lastPoll).toLocaleTimeString('ru-RU'));
  const lastRead = ozon ? config.ozonLastRead : config.lastRead;
  if (lastRead) lines.push((ozon ? config.ozonReadOk : config.readOk) ? 'Последнее чтение ' + name + ' прошло.' : name + ' не прочитан: ' + ((ozon ? config.ozonReadCode : config.readCode) || 'откройте кабинет'));
  byId('status').textContent = lines.join('\n');
  byId('error').textContent = config.error || '';
  const checks = config[logKey()];
  const logs = checks?.length ? JSON.stringify({ extensionVersion: chrome.runtime.getManifest().version, shop: shop(), checks }, null, 2) : '';
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
byId('open').addEventListener('click', () => chrome.tabs.create({ url: shop() === 'ozon' ? 'https://www.ozon.ru/my/orderlist' : 'https://www.wildberries.ru/lk/myorders/delivery' }));
byId('poll').addEventListener('click', () => chrome.runtime.sendMessage({ type: 'poll' }, refresh));
byId('diagnose').addEventListener('click', async () => {
  byId('diagnose').disabled = true;
  byId('diagnosticStatus').textContent = 'Читаю ' + title() + ' и записываю логи, до минуты…';
  try {
    const reply = await chrome.runtime.sendMessage({ type: 'diagnose', shop: shop() });
    byId('diagnosticStatus').textContent = reply?.ok
      ? (reply.started ? 'Проверка запущена. Подождите до 30 секунд и откройте расширение снова — результат сохранится.' : 'Проверка записана. Нажмите «Скопировать логи».')
      : reply?.error || 'Фоновый обработчик не ответил. Закройте окно и обновите расширение круговой стрелкой в chrome://extensions.';
    await refresh();
  } catch (_) { byId('diagnosticStatus').textContent = 'Не удалось связаться с расширением. Перезагрузите его в chrome://extensions.'; }
  finally { byId('diagnose').disabled = false; }
});
byId('copyLogs').addEventListener('click', async () => {
  if (!byId('logs').value) { byId('diagnosticStatus').textContent = 'Сначала нажмите «Проверить ' + title() + '».'; return; }
  try {
    await navigator.clipboard.writeText(byId('logs').value);
    byId('diagnosticStatus').textContent = 'Логи скопированы. Их можно вставить в чат.';
  } catch (_) {
    byId('logs').focus(); byId('logs').select();
    byId('diagnosticStatus').textContent = 'Отчёт выделен. Нажмите Ctrl+C.';
  }
});
byId('clearLogs').addEventListener('click', async () => {
  await chrome.storage.local.remove(logKey());
  byId('diagnosticStatus').textContent = 'Логи удалены.';
  await refresh();
});
byId('fields').addEventListener('click', () => chrome.tabs.create({ url: chrome.runtime.getURL('fields.html') + '?shop=' + shop() }));
refresh();
setInterval(refresh, 2000);
