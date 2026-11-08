const byId = id => document.getElementById(id);
byId('shop').value = new URL(location.href).searchParams.get('shop') === 'ozon' ? 'ozon' : 'wb';
const shop = () => byId('shop').value;
const keys = () => shop() === 'ozon' ? ['ozonFieldCatalog', 'ozonFieldCatalogAt', 'ozonSelectedFields'] : ['fieldCatalog', 'fieldCatalogAt', 'selectedFields'];
byId('shop').addEventListener('change', load);
let catalog = [], selected = new Set(), receivedAt = 0;
function shown() { const query = byId('search').value.trim().toLowerCase(); return catalog.filter(field => field.path.toLowerCase().includes(query)); }
const selectable = field => !field.types.some(type => type === 'object' || type === 'array');
function render() {
  byId('summary').textContent = catalog.length
    ? `Найдено полей: ${catalog.length}. Выбрано: ${selected.size}. Проверка: ${new Date(receivedAt).toLocaleString('ru-RU')}.`
    : 'Пока нет каталога. В расширении выберите этот магазин, нажмите «Проверить», дождитесь окончания и обновите список.';
  const fragment = document.createDocumentFragment();
  for (const field of shown()) {
    const tr = document.createElement('tr'), checkCell = document.createElement('td');
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.has(field.path);
    checkbox.disabled = !selectable(field);
    if (checkbox.disabled) checkbox.title = 'Выберите отдельные поля этой структуры ниже.';
    checkbox.setAttribute('aria-label', field.path);
    checkbox.addEventListener('change', () => { if (checkbox.checked) selected.add(field.path); else selected.delete(field.path); render(); });
    checkCell.append(checkbox); tr.append(checkCell);
    for (const text of [field.path, field.types.join(', ') + ' / ' + field.rows, field.examples.join('\n') || '(структура или пустое значение)']) {
      const td = document.createElement('td'); td.textContent = text; tr.append(td);
    }
    fragment.append(tr);
  }
  byId('rows').replaceChildren(fragment);
}
async function load() {
  const [catalogKey, atKey, selectedKey] = keys();
  const data = await chrome.storage.local.get(keys());
  catalog = Array.isArray(data[catalogKey]) ? data[catalogKey].filter(field =>
    !window.JarvisWbFields || window.JarvisWbFields.allowedPath(field.path)) : [];
  receivedAt = data[atKey] || 0;
  selected = new Set((Array.isArray(data[selectedKey]) ? data[selectedKey] : []).filter(path =>
    !window.JarvisWbFields || window.JarvisWbFields.allowedPath(path)));
  render();
}
byId('search').addEventListener('input', render);
byId('refresh').addEventListener('click', load);
byId('all').addEventListener('click', () => { shown().filter(selectable).forEach(field => selected.add(field.path)); render(); });
byId('none').addEventListener('click', () => { shown().forEach(field => selected.delete(field.path)); render(); });
byId('save').addEventListener('click', async () => {
  await chrome.storage.local.set({ [keys()[2]]: [...selected].slice(0, 600) });
  byId('status').textContent = 'Выбор сохранён для подробных ответов по вашему запросу. В обычных ответах остаются название, цена и статус. Большие подробные ответы сокращаются.';
});
byId('copy').addEventListener('click', async () => {
  const text = JSON.stringify({ version: chrome.runtime.getManifest().version, shop: shop(), at: receivedAt, fields: catalog, selected: [...selected] }, null, 2);
  try { await navigator.clipboard.writeText(text); byId('status').textContent = 'Каталог скопирован. В нём есть примеры ваших данных — отправляйте его осознанно.'; }
  catch (_) { byId('status').textContent = 'Chrome не разрешил копирование. Выделите нужные строки таблицы и нажмите Ctrl+C.'; }
});
load();
