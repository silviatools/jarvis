// No incoming network listener, arbitrary URLs, clicks or remotely supplied JS.
const URLS = {
  orders: 'https://www.wildberries.ru/lk/myorders/archive',
  delivery: 'https://www.wildberries.ru/lk/myorders/delivery',
  status: 'https://www.wildberries.ru/lk/myorders/delivery'
};
let running = false;
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function post(config, route, body) {
  const response = await fetch(config.server + '/api/market/pc/' + route, {
    method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + config.token },
    body: JSON.stringify(body), signal: AbortSignal.timeout(12000)
  });
  if (response.status === 401) throw new Error('Ключ отключён. Создайте новый ключ в настройках Джарвиса.');
  if (response.status === 409 && route === 'result') return { ok: true };
  if (!response.ok) throw new Error('Джарвис ответил HTTP ' + response.status);
  return response.json();
}

// Serialized into the page by Chrome. Read only; no cookies or localStorage.
function readPage(key, selectedFields = [], includeDetails = false) {
  const url = new URL(location.href);
  const text = (document.body?.innerText || '').slice(0, 16000).toLowerCase();
  const state = window.__jarvisWbRead;
  const diagnostic = { path: url.pathname.replace(/\d{6,}|[a-f0-9]{16,}/gi, ':id'),
    readyState: document.readyState, captureLoaded: !!state,
    hooks: typeof state?.hooksActive === 'function' ? state.hooksActive() : null,
    orders: state?.orders?.rows?.length ?? null, delivery: state?.delivery?.rows?.length ?? null,
    network: state?.debug || null };
  // Resource Timing also sees completed requests that bypassed our fetch/XHR
  // wrappers. It exposes no request headers or response bodies.
  diagnostic.resources = typeof performance !== 'undefined' ? performance.getEntriesByType('resource')
    .filter(entry => ['fetch', 'xmlhttprequest'].includes(entry.initiatorType))
    .map(entry => {
      try {
        const u = new URL(entry.name);
        if (!(u.hostname === 'wildberries.ru' || u.hostname.endsWith('.wildberries.ru')
              || u.hostname === 'wb.ru' || u.hostname.endsWith('.wb.ru'))
            || !/\/api\/|\/webapi\/|\/lk\/|delivery|purchases|myorders/.test(u.pathname)) return null;
        return { host: u.hostname, path: u.pathname.split('/').map(part =>
          /^[a-z][a-z-]{0,36}$/.test(part) || /^v\d{1,2}$/.test(part) ? part : part ? ':id' : '').join('/'),
          transport: entry.initiatorType, status: Number(entry.responseStatus) || 0 };
      } catch (_) { return null; }
    }).filter(Boolean).slice(-24) : [];
  if (url.hostname !== 'www.wildberries.ru' || /\/security\/login|\/signin/.test(url.pathname)
      || /введите номер телефона|войти по номеру телефона|введите код из|введите пароль/.test(text)
      || [...document.querySelectorAll('input[type="tel"], input[autocomplete="one-time-code"]')]
        .some(input => input.getClientRects().length > 0)) {
    return { ok: false, code: 'login', diagnostic: { ...diagnostic, stage: 'login_page' } };
  }
  if (/проверяем браузер|доступ ограничен|подтвердите, что вы|нет соединения|captcha/.test(text)) {
    return { ok: false, code: 'blocked', diagnostic: { ...diagnostic, stage: 'browser_check' } };
  }
  if (url.pathname.replace(/\/$/, '') !== (key === 'orders' ? '/lk/myorders/archive' : '/lk/myorders/delivery')) {
    return { ok: false, code: 'unreadable', diagnostic: { ...diagnostic, stage: 'wrong_section' } };
  }
  const found = state?.[key];
  // A heading alone is not proof that the account's list was loaded.
  if (found && Date.now() - found.at < 60000) {
    const helper = window.JarvisWbFields;
    let extraBudget = 12000;
    let detailsTruncated = false;
    const detailsList = [];
    const pageData = window.JarvisWbPage?.read(found.rows.map(row => row.id), found.rows.map(row => row.article).filter(Boolean))
      || { products: [], amount_due_labels: [], diagnostic: { readerMissing: true } };
    diagnostic.page = pageData.diagnostic;
    const rows = found.rows.map(source => {
      const { details: originalDetails, office_id, ...row } = source;
      const details = { ...(originalDetails || {}) };
      let imageUrl = '';
      if (!row.article) {
        const linked = pageData.products.filter(product => product.rids.includes(row.id));
        if (linked.length === 1) row.article = linked[0].article;
      }
      const pageProduct = pageData.products.find(product => product.article === row.article);
      if (pageProduct) {
        if (!row.name) row.name = pageProduct.name;
        imageUrl = pageProduct.image_url;
      }
      if (row.article && !window.JarvisWbPage) {
        // Only a link to this position's known article may supply its title.
        for (const link of document.querySelectorAll('a[href*="/catalog/"]')) {
          let article;
          try { article = /^\/catalog\/(\d+)\/detail\.aspx/.exec(new URL(link.href, location.href).pathname)?.[1]; } catch (_) { continue; }
          if (article !== row.article) continue;
          const label = link.getAttribute('title') || link.getAttribute('aria-label')
            || link.querySelector('[class*="good-name"], [class*="product-name"], [class*="goods-name"], [class*="title"]')?.textContent
            || link.querySelector('img[alt]')?.getAttribute('alt') || link.innerText;
          const name = (label || '').replace(/\s+/g, ' ').trim();
          if (!row.name && name.length > 2 && !/^(подробнее|открыть|перейти|фото|изображение|\d+[\s₽руб.]*)$/i.test(name)) row.name = name.slice(0, 160);
          const image = link.querySelector('img');
          imageUrl = image?.currentSrc || image?.src || imageUrl;
          if (row.name) break;
        }
      }
      const paymentLabels = pageData.amount_due_labels;
      details.page = { article: row.article || null, name: row.name || null,
        image_url: imageUrl || null, binding: row.article ? 'matched_article_or_rid' : 'status_only',
        amount_due_labels: [...new Set(paymentLabels)], payment_captions: pageData.payment_captions || [] }; // page labels, not per-item or a computed total
      if (imageUrl) row.image_url = imageUrl;
      const safeDetails = helper ? helper.sanitize(details) : {};
      detailsList.push(safeDetails);
      const requestedFields = includeDetails && !selectedFields.length && helper
        ? helper.inventory([safeDetails]).filter(field => !field.types.some(type => type === 'object' || type === 'array')).map(field => field.path)
        : selectedFields;
      const extras = includeDetails && helper ? helper.select(safeDetails, requestedFields) : [];
      row.extra_fields = [];
      for (const field of extras) {
        if (row.extra_fields.length >= 100 || extraBudget < field.path.length + field.value.length) { detailsTruncated = true; break; }
        row.extra_fields.push(field);
        extraBudget -= field.path.length + field.value.length;
      }
      return row;
    });
    const boundArticles = new Set(rows.map(row => row.article).filter(Boolean));
    const unmatchedProducts = pageData.products.filter(product => !boundArticles.has(product.article));
    for (const product of unmatchedProducts) detailsList.push({ page: { article: product.article,
      name: product.name || null, image_url: product.image_url || null, binding: 'unmatched_page_product',
      scope: product.scope, amount_due_labels: pageData.amount_due_labels,
      payment_captions: pageData.payment_captions || [] } });
    if (typeof state.unmatchedItemDetails === 'function') detailsList.push(...state.unmatchedItemDetails());
    diagnostic.rowsWithName = rows.filter(row => row.name).length;
    diagnostic.rowsWithArticle = rows.filter(row => row.article).length;
    return { ok: true, cabinet: true, url: location.origin + location.pathname,
      rows, empty: found.empty, truncated: found.truncated, details_truncated: detailsTruncated,
      fieldCatalog: helper ? helper.inventory(detailsList) : [],
      page_products: unmatchedProducts.filter(product => product.scope === 'delivery_section').slice(0, 40)
        .map(({ article, name, image_url }) => ({ article, name, image_url })),
      page_amount_due_labels: pageData.amount_due_labels,
      diagnostic: { ...diagnostic, stage: 'list_read' } };
  }
  return { ok: false, code: 'unreadable', diagnostic: { ...diagnostic, stage: state ? 'waiting_for_list' : 'capture_missing' } };
}

async function readWb(section, foreground = false, includeDetails = false) {
  if (!Object.prototype.hasOwnProperty.call(URLS, section)) return { ok: false, code: 'browser' };
  let tabId;
  let stage = 'open_tab';
  let tabStatus = 'unknown';
  let attempt = 0;
  const started = Date.now();
  const finish = result => ({ ...result, diagnostic: { ...(result.diagnostic || {}),
    version: chrome.runtime.getManifest().version, section, tabStatus,
    attempts: attempt, elapsedMs: Date.now() - started,
    stage: result.diagnostic?.stage || stage } });
  try {
    // Use an extension-owned background tab; never navigate a user's shopping tab.
    const previous = await chrome.storage.session.get('wbTab');
    if (Number.isInteger(previous.wbTab)) {
      try {
        const old = await chrome.tabs.get(previous.wbTab);
        if (old.url?.startsWith('https://www.wildberries.ru/lk/myorders/')) tabId = old.id;
      } catch (_) { /* closed by the user */ }
    }
    if (tabId !== undefined) await chrome.tabs.update(tabId, { url: URLS[section], ...(foreground ? { active: true } : {}) });
    else {
      const tab = await chrome.tabs.create({ url: URLS[section], active: foreground });
      tabId = tab.id;
      await chrome.storage.session.set({ wbTab: tabId });
    }
    // Force a full reload on a reused tab so the observed responses belong to
    // this request, not to the previous section or an old signed-in session.
    stage = 'reload_tab';
    await chrome.tabs.reload(tabId);
    const key = section === 'orders' ? 'orders' : 'delivery';
    const settings = await chrome.storage.local.get(['selectedFields']);
    const selectedFields = Array.isArray(settings.selectedFields) ? settings.selectedFields.filter(value => typeof value === 'string').slice(0, 600) : [];
    let result = { ok: false, code: 'unreadable' };
    let firstRead = 0;
    for (attempt = 1; attempt <= 30; attempt++) {
      await pause(1000);
      stage = 'read_tab';
      const tab = await chrome.tabs.get(tabId);
      tabStatus = tab.status || 'unknown';
      if (!tab.url?.startsWith('https://www.wildberries.ru/')) {
        if (tab.status !== 'complete') continue;
        return finish({ ok: false, code: 'login' });
      }
      stage = 'snapshot';
      let snapshots;
      try {
        snapshots = await chrome.scripting.executeScript({
          target: { tabId }, world: 'MAIN', func: readPage, args: [key, selectedFields, includeDetails]
        });
      } catch (_) {
        // The document can change during navigation. Retry without losing the
        // last readable snapshot or silently replacing it with "browser".
        result = { ...result, diagnostic: { ...(result.diagnostic || {}), stage: 'snapshot_unavailable' } };
        continue;
      }
      result = snapshots[0]?.result || result;
      if (result.code === 'login') return finish(result);
      if (result.ok) {
        if (!firstRead) firstRead = attempt;
        // Metadata/card responses and visible titles can arrive after statuses.
        if (section === 'status' || result.empty || (result.rows?.every(row => row.name) && attempt - firstRead >= 2) || attempt - firstRead >= 10) return finish(result);
      }
      // Keep waiting through the site's browser check instead of calling it expired.
    }
    return finish(result);
  } catch (error) {
    return finish({ ok: false, code: 'browser', diagnostic: { stage,
      errorType: ['Error', 'TypeError', 'SyntaxError'].includes(error.name) ? error.name : 'BrowserError' } });
  }
}

async function recordRead(result, trigger) {
  if (Array.isArray(result.fieldCatalog)) await chrome.storage.local.set({ fieldCatalog: result.fieldCatalog, fieldCatalogAt: Date.now() });
  // Deliberately omit rows, text, credentials, server URL and arbitrary errors.
  const report = { at: new Date().toISOString(), trigger, ok: result.ok === true,
    code: result.code || '', ...(result.diagnostic || {}) };
  const { readLogs = [] } = await chrome.storage.local.get(['readLogs']);
  await chrome.storage.local.set({ readLogs: [...readLogs.slice(-5), report],
    lastRead: Date.now(), readOk: result.ok === true, readCode: result.code || '' });
  console.info('Jarvis WB diagnostic', report);
}

async function diagnose() {
  if (running) return { ok: false, error: 'Чтение WB уже идёт. Дождитесь результата.' };
  running = true;
  try {
    const result = await readWb('delivery', true);
    await recordRead(result, 'manual');
    return { ok: true };
  } finally { running = false; }
}

async function tick() {
  if (running) return;
  running = true;
  try {
    const config = await chrome.storage.local.get(['server', 'token', 'enabled', 'pending']);
    if (!config.enabled || !config.server || !config.token) return;
    // A worker may be stopped after reading but before upload: retry that one
    // result, never persist it beyond the next successful delivery.
    if (config.pending) {
      await post(config, 'result', config.pending);
      await chrome.storage.local.remove('pending');
    }
    const response = await post(config, 'poll', {});
    await chrome.storage.local.set({ lastPoll: Date.now(), error: '' });
    if (response.job) {
      const current = await chrome.storage.local.get(['server', 'token', 'enabled']);
      if (!current.enabled || current.server !== config.server || current.token !== config.token) return;
      const result = await readWb(response.job.section, false, response.job.details === true);
      await recordRead(result, 'jarvis');
      const after = await chrome.storage.local.get(['server', 'token', 'enabled']);
      if (!after.enabled || after.server !== config.server || after.token !== config.token) return;
      const { diagnostic, fieldCatalog, ...upload } = result;
      const pending = { id: response.job.id, result: upload };
      await chrome.storage.local.set({ pending });
      await post(config, 'result', pending);
      await chrome.storage.local.remove('pending');
    }
  } catch (error) {
    await chrome.storage.local.set({ error: error.message || 'Нет связи с Джарвисом.' });
  } finally {
    running = false;
  }
}

async function start() {
  await chrome.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' });
  await chrome.alarms.create('jarvis-wb', { periodInMinutes: 0.5 });
  await tick();
}
chrome.runtime.onInstalled.addListener(start);
chrome.runtime.onStartup.addListener(start);
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'jarvis-wb') tick(); });
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id || sender.tab) return;
  if (message?.type === 'poll') {
    tick().then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message?.type === 'diagnose') {
    if (running) {
      sendResponse({ ok: false, error: 'Чтение WB уже идёт. Дождитесь результата.' });
      return false;
    }
    // Activating the WB tab closes the toolbar popup. Acknowledge now; the
    // check writes its result to storage without depending on that popup port.
    diagnose().catch(() => chrome.storage.local.set({ error: 'Не удалось записать диагностику.' }));
    sendResponse({ ok: true, started: true });
    return false;
  }
});
