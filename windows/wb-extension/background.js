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
function readPage(key) {
  const url = new URL(location.href);
  const text = (document.body?.innerText || '').slice(0, 16000).toLowerCase();
  if (url.hostname !== 'www.wildberries.ru' || /\/security\/login|\/signin/.test(url.pathname)
      || /введите номер телефона|войти по номеру телефона|введите код из|введите пароль/.test(text)
      || [...document.querySelectorAll('input[type="tel"], input[autocomplete="one-time-code"]')]
        .some(input => input.getClientRects().length > 0)) {
    return { ok: false, code: 'login' };
  }
  if (/проверяем браузер|доступ ограничен|подтвердите, что вы|нет соединения|captcha/.test(text)) return { ok: false, code: 'blocked' };
  const found = window.__jarvisWbRead?.[key];
  // A heading alone is not proof that the account's list was loaded.
  if (found && Date.now() - found.at < 60000) {
    return { ok: true, cabinet: true, url: location.origin + location.pathname,
      rows: found.rows, empty: found.empty, truncated: found.truncated };
  }
  return { ok: false, code: 'unreadable' };
}

async function readWb(section) {
  if (!Object.prototype.hasOwnProperty.call(URLS, section)) return { ok: false, code: 'browser' };
  let tabId;
  try {
    // Use an extension-owned background tab; never navigate a user's shopping tab.
    const previous = await chrome.storage.session.get('wbTab');
    if (Number.isInteger(previous.wbTab)) {
      try {
        const old = await chrome.tabs.get(previous.wbTab);
        if (old.url?.startsWith('https://www.wildberries.ru/lk/myorders/')) tabId = old.id;
      } catch (_) { /* closed by the user */ }
    }
    if (tabId !== undefined) await chrome.tabs.update(tabId, { url: URLS[section] });
    else {
      const tab = await chrome.tabs.create({ url: URLS[section], active: false });
      tabId = tab.id;
      await chrome.storage.session.set({ wbTab: tabId });
    }
    // Force a full reload on a reused tab so the observed responses belong to
    // this request, not to the previous section or an old signed-in session.
    await chrome.tabs.reload(tabId);
    const key = section === 'orders' ? 'orders' : 'delivery';
    let result = { ok: false, code: 'unreadable' };
    for (let attempt = 0; attempt < 30; attempt++) {
      await pause(1000);
      const tab = await chrome.tabs.get(tabId);
      if (tab.status !== 'complete') continue;
      if (!tab.url?.startsWith('https://www.wildberries.ru/')) return { ok: false, code: 'login' };
      const snapshots = await chrome.scripting.executeScript({
        target: { tabId }, world: 'MAIN', func: readPage, args: [key]
      });
      result = snapshots[0]?.result || result;
      if (result.ok || result.code === 'login') return result;
      // Keep waiting through the site's browser check instead of calling it expired.
    }
    return result;
  } catch (_) {
    return { ok: false, code: 'browser' };
  }
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
      const result = await readWb(response.job.section);
      const after = await chrome.storage.local.get(['server', 'token', 'enabled']);
      if (!after.enabled || after.server !== config.server || after.token !== config.token) return;
      const pending = { id: response.job.id, result };
      await chrome.storage.local.set({ pending });
      await post(config, 'result', pending);
      await chrome.storage.local.remove('pending');
      await chrome.storage.local.set({ lastRead: Date.now(), readOk: result.ok === true, readCode: result.code || '' });
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
});
