/* One persistent tab for site tasks. The server sends actions, never page scripts. */
let browseBusy = false;
const browsePause = ms => new Promise(resolve => setTimeout(resolve, ms));
const BROWSE_KEYS = {
  Enter: {key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13, text: '\r'},
  Tab: {key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9},
  Escape: {key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27},
  Backspace: {key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8},
  ArrowUp: {key: 'ArrowUp', code: 'ArrowUp', windowsVirtualKeyCode: 38},
  ArrowDown: {key: 'ArrowDown', code: 'ArrowDown', windowsVirtualKeyCode: 40},
  ArrowLeft: {key: 'ArrowLeft', code: 'ArrowLeft', windowsVirtualKeyCode: 37},
  ArrowRight: {key: 'ArrowRight', code: 'ArrowRight', windowsVirtualKeyCode: 39},
  PageDown: {key: 'PageDown', code: 'PageDown', windowsVirtualKeyCode: 34},
  PageUp: {key: 'PageUp', code: 'PageUp', windowsVirtualKeyCode: 33},
  Home: {key: 'Home', code: 'Home', windowsVirtualKeyCode: 36},
  End: {key: 'End', code: 'End', windowsVirtualKeyCode: 35}
};

function browseUrl(value) {
  let url;
  try { url = new URL(value); }
  catch (_) { throw new Error('Некорректный адрес.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('Можно открыть только http или https без пароля в адресе.');
  }
  return url.href;
}

function browsePublicUrl(href) {
  let url;
  try { url = new URL(href); }
  catch (_) { return ''; }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
  const hash = url.hash.length > 81 || /token|session|password|passwd|code|auth|secret/i.test(url.hash) ? '' : url.hash;
  const params = [...url.searchParams.entries()]
    .filter(([name, val]) => val.length <= 80 && !/token|session|password|passwd|code|auth|key|secret/i.test(name))
    .slice(0, 12);
  url.hash = '';
  url.username = '';
  url.password = '';
  url.search = new URLSearchParams(params).toString();
  return (url.href + hash).slice(0, 500);
}

async function browseLoad() {
  const stored = (await chrome.storage.session.get('browse')).browse || {};
  if (stored.session) return stored;
  const next = {session: crypto.randomUUID()};
  await chrome.storage.session.set({browse: next});
  return next;
}

async function browseExisting(state) {
  if (!state.tabId) return null;
  try { return await chrome.tabs.get(state.tabId); }
  catch (_) { return null; }
}

async function browseAttach(tabId) {
  try { await chrome.debugger.attach({tabId}, '1.3'); }
  catch (error) {
    if (!/already/i.test(String(error && error.message || error || ''))) {
      throw new Error('Chrome не разрешил управление вкладкой. Закройте инструменты разработчика на ней.');
    }
  }
}

async function browseDetach() {
  const state = (await chrome.storage.session.get('browse')).browse;
  if (!state || !state.tabId) return;
  try { await chrome.debugger.detach({tabId: state.tabId}); } catch (_) {}
}

async function browseSettle(tabId) {
  await browsePause(700);
  for (let attempt = 0; attempt < 20; attempt++) {
    let tab;
    try { tab = await chrome.tabs.get(tabId); }
    catch (_) { throw new Error('Вкладка закрыта.'); }
    if (tab.status === 'complete') return;
    await browsePause(400);
  }
}

async function browsePage(tabId) {
  const evaluated = await chrome.debugger.sendCommand({tabId}, 'Runtime.evaluate', {
    expression: '({href:location.href,title:document.title,width:innerWidth,height:innerHeight})',
    returnByValue: true
  });
  const value = evaluated && evaluated.result && evaluated.result.value || {};
  if (![value.width, value.height].every(size => Number.isInteger(size) && size > 0 && size <= 6000)) {
    throw new Error('Не удалось прочитать размер страницы.');
  }
  return {url: browsePublicUrl(value.href || ''), title: String(value.title || '').slice(0, 300), width: value.width, height: value.height};
}

async function browseShot(tabId) {
  const page = await browsePage(tabId);
  const shot = await chrome.debugger.sendCommand({tabId}, 'Page.captureScreenshot', {
    format: 'jpeg', quality: 70, captureBeyondViewport: false
  });
  const bitmap = await createImageBitmap(await (await fetch('data:image/jpeg;base64,' + shot.data)).blob());
  const canvas = new OffscreenCanvas(page.width, page.height);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, page.width, page.height);
  bitmap.close();
  const bytes = new Uint8Array(await (await canvas.convertToBlob({type: 'image/jpeg', quality: 0.8})).arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return {ok: true, image: btoa(binary), width: page.width, height: page.height, url: page.url, title: page.title};
}

async function browseOpen(url) {
  const state = await browseLoad();
  const moodle = (await chrome.storage.session.get('moodle')).moodle;
  let tab = await browseExisting(state);
  if (tab && moodle && tab.id === moodle.tabId) tab = null;
  if (!tab) {
    tab = await chrome.tabs.create({url, active: true});
    state.tabId = tab.id;
  } else {
    await chrome.tabs.update(tab.id, {url, active: true});
  }
  await chrome.storage.session.set({browse: state});
  await browseAttach(tab.id);
  await browseSettle(tab.id);
  return tab.id;
}

async function browseRequire() {
  const state = await browseLoad();
  const tab = await browseExisting(state);
  if (!tab) {
    const error = new Error('Вкладка закрыта. Откройте адрес снова.');
    error.retry = true;
    throw error;
  }
  await chrome.tabs.update(tab.id, {active: true});
  await browseAttach(tab.id);
  return tab.id;
}

async function browseFoldPopups(tabId) {
  const spawned = [];
  const onCreated = tab => { if (tab.id !== tabId) spawned.push(tab.id); };
  chrome.tabs.onCreated.addListener(onCreated);
  await browsePause(600);
  chrome.tabs.onCreated.removeListener(onCreated);
  for (const id of spawned) {
    let href = '';
    try {
      await chrome.debugger.attach({tabId: id}, '1.3');
      const info = await chrome.debugger.sendCommand({tabId: id}, 'Runtime.evaluate', {expression: 'location.href', returnByValue: true});
      href = info && info.result ? info.result.value : '';
      try { await chrome.debugger.detach({tabId: id}); } catch (_) {}
    } catch (_) {}
    try { await chrome.tabs.remove(id); } catch (_) {}
    if (typeof href === 'string' && /^https?:/i.test(href)) {
      try {
        browseUrl(href);
        await chrome.tabs.update(tabId, {url: href, active: true});
        await browseSettle(tabId);
      } catch (_) {}
    }
  }
}

async function browseAction(job) {
  if (!job || Date.now() / 1000 >= job.expires) throw new Error('Команда устарела.');
  const action = job.action || {};
  if (action.action === 'open') return browseShot(await browseOpen(browseUrl(action.url)));
  let tabId;
  try { tabId = await browseRequire(); }
  catch (error) { return {ok: false, error: error.message, retry: true}; }
  if (action.action === 'snapshot') return browseShot(tabId);
  if (action.action === 'wait') {
    await browsePause(1000);
    return browseShot(tabId);
  }
  if (action.action === 'click') {
    const page = await browsePage(tabId);
    const x = action.x, y = action.y;
    if (![x, y].every(Number.isFinite) || x < 0 || y < 0 || x >= page.width || y >= page.height) {
      return {ok: false, error: 'Координаты вне страницы.', retry: true};
    }
    const file = await chrome.debugger.sendCommand({tabId}, 'Runtime.evaluate', {
      expression: '(() => { const el = document.elementFromPoint(' + x + ',' + y + '); return !!(el && el.closest && el.closest(\'input[type="file"]\')); })()',
      returnByValue: true
    });
    if (file && file.result && file.result.value) {
      return {ok: false, error: 'Выбор файла с компьютера отсюда недоступен. Попросите пользователя выбрать файл или найдите другой путь.', retry: true};
    }
    await chrome.debugger.sendCommand({tabId}, 'Input.dispatchMouseEvent', {type: 'mousePressed', x, y, button: 'left', clickCount: 1});
    await chrome.debugger.sendCommand({tabId}, 'Input.dispatchMouseEvent', {type: 'mouseReleased', x, y, button: 'left', clickCount: 1});
    await browseFoldPopups(tabId);
    await browseSettle(tabId);
    return browseShot(tabId);
  }
  if (action.action === 'type') {
    if (typeof action.text !== 'string' || !action.text || action.text.length > 4000) return {ok: false, error: 'Неверный текст.', retry: true};
    const focus = await chrome.debugger.sendCommand({tabId}, 'Runtime.evaluate', {
      expression: '(() => { const e = document.activeElement; if (!e) return false; if (e.isContentEditable) return true; return !!(e.matches && e.matches(\'textarea, input, [contenteditable="true"]\')); })()',
      returnByValue: true
    });
    if (!focus || !focus.result || !focus.result.value) return {ok: false, error: 'Сначала кликните в поле.', retry: true};
    await chrome.debugger.sendCommand({tabId}, 'Input.insertText', {text: action.text});
    await browsePause(300);
    return browseShot(tabId);
  }
  if (action.action === 'key') {
    const key = BROWSE_KEYS[action.key];
    if (!key) return {ok: false, error: 'Неизвестная клавиша.', retry: true};
    await chrome.debugger.sendCommand({tabId}, 'Input.dispatchKeyEvent', {type: 'keyDown', ...key});
    await chrome.debugger.sendCommand({tabId}, 'Input.dispatchKeyEvent', {type: 'keyUp', key: key.key, code: key.code, windowsVirtualKeyCode: key.windowsVirtualKeyCode});
    await browsePause(300);
    return browseShot(tabId);
  }
  if (action.action === 'scroll') {
    const page = await browsePage(tabId);
    const limit = Math.max(1, Math.min(1500, Math.floor(page.height * 0.8)));
    const distance = Math.max(-limit, Math.min(limit, Number(action.delta)));
    if (!Number.isFinite(distance)) return {ok: false, error: 'Некорректная прокрутка.', retry: true};
    await chrome.debugger.sendCommand({tabId}, 'Runtime.evaluate', {
      expression: '(() => { const delta = ' + distance + '; let element = document.elementFromPoint(innerWidth / 2, innerHeight / 2); while (element && element !== document.body && element !== document.documentElement) { const style = getComputedStyle(element); if (/(auto|scroll)/.test(style.overflowY) && element.scrollHeight > element.clientHeight) { element.scrollBy({top: delta, behavior: "instant"}); return; } element = element.parentElement; } (document.scrollingElement || document.documentElement).scrollBy({top: delta, behavior: "instant"}); })()',
      returnByValue: true
    });
    await browsePause(300);
    return browseShot(tabId);
  }
  return {ok: false, error: 'Неизвестная команда.'};
}

async function browsePost(config, route, body) {
  const response = await fetch(config.server + '/api/browse/pc/' + route, {
    method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store',
    headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + config.token},
    body: JSON.stringify(body), signal: AbortSignal.timeout(20000)
  });
  if (response.status === 401) throw new Error('Ключ отключён. Создайте новый ключ в настройках Джарвиса.');
  if (response.status === 409 && route === 'result') return {ok: true};
  if (!response.ok) throw new Error('Джарвис ответил HTTP ' + response.status);
  return response.json();
}

async function browseTick() {
  if (browseBusy) return;
  browseBusy = true;
  let schedule = true;
  try {
    const config = await chrome.storage.local.get(['server', 'token', 'enabled']);
    if (!config.enabled || !config.server || !config.token) { schedule = false; return; }
    const state = await browseLoad();
    const response = await browsePost(config, 'poll', {session: state.session, capabilities: ['browse']});
    if (!response.job) {
      if (response.status !== 'running') await browseDetach();
      const label = {running: 'Выполняю задание на сайте…', waiting: 'Жду ответ в Telegram.',
        completed: 'Задание по сайту выполнено.', stopped: 'Просмотр сайта остановлен.',
        interrupted: 'Просмотр сайта прерван.'}[response.status] || 'Вкладка сайтов свободна.';
      await chrome.storage.local.set({browseStatus: label});
      return;
    }
    await chrome.storage.local.set({browseStatus: 'Выполняю задание на сайте…'});
    let result;
    try { result = await browseAction(response.job); }
    catch (error) { result = {ok: false, error: error.message || 'Не удалось выполнить действие.'}; }
    const current = await chrome.storage.local.get(['server', 'token', 'enabled']);
    if (!current.enabled || current.server !== config.server || current.token !== config.token) return;
    await browsePost(config, 'result', {session: state.session, id: response.job.id, result});
  } catch (error) {
    try { await chrome.storage.local.set({browseStatus: error.message || 'Нет связи с Джарвисом.'}); } catch (_) {}
  } finally {
    browseBusy = false;
    if (schedule) setTimeout(browseTick, 1500);
  }
}

function browseKick() { setTimeout(browseTick, 200); }

chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'jarvis-wb') browseTick(); });
chrome.debugger.onDetach.addListener(async source => {
  const state = (await chrome.storage.session.get('browse')).browse;
  if (state && state.tabId === source.tabId) {
    try { await chrome.storage.local.set({browseStatus: 'Управление вкладкой отключено. Проверка сайта остановлена на этом шаге.'}); } catch (_) {}
  }
});
