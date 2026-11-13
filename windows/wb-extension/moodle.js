/* Moodle is opt-in and pinned locally. Never accepts URLs or executable code from the server. */
let moodleBusy = false;
let moodleBinding = false;
let moodleBindEpoch = 0;
const moodlePause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function moodlePost(config, route, body) {
  const r = await fetch(config.server + '/api/moodle/pc/' + route, {
    method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store',
    headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + config.token},
    body: JSON.stringify(body), signal: AbortSignal.timeout(12000)
  });
  if (r.status === 404) throw new Error('На сервере нет обновления Moodle (HTTP 404). Дождитесь завершения деплоя Джарвиса.');
  if (r.status === 401) throw new Error('Сервер отклонил ключ ПК. Проверьте подключение расширения к Джарвису.');
  if (!r.ok) throw new Error('Moodle: ошибка связи HTTP ' + r.status);
  return r.json();
}
async function moodlePage(tabId) {
  const [r] = await chrome.scripting.executeScript({target: {tabId}, func: () => ({
    cmid: document.body.className.match(/(?:^|\s)cmid-(\d+)(?:\s|$)/)?.[1],
    course: document.body.className.match(/(?:^|\s)course-(\d+)(?:\s|$)/)?.[1],
    width: innerWidth, height: innerHeight,
    courseTitle: (document.querySelector('.page-header-headings h1, #page-header h1, h1')?.innerText || '').slice(0,300),
    tests: [...new Map([...document.querySelectorAll('a[href]')].filter(a => {
      try { const u = new URL(a.href); return u.origin === location.origin && u.pathname.endsWith('/mod/quiz/view.php') && /^\d+$/.test(u.searchParams.get('id') || ''); } catch (_) { return false; }
    }).map(a => [new URL(a.href).searchParams.get('id'), {title:(a.innerText || a.getAttribute('aria-label') || 'Тест').trim().slice(0,300)}])).values()].slice(0,200),
    links: [...document.querySelectorAll('a[href]')].map(a => a.href).filter(h => {
      try { const u = new URL(h); return u.origin === location.origin && u.pathname.endsWith('/mod/quiz/view.php') && /^\d+$/.test(u.searchParams.get('id') || ''); } catch (_) { return false; }
    })
  })});
  return r.result;
}
async function moodleBind(tab) {
  const epoch = moodleBindEpoch;
  const config = await chrome.storage.local.get(['server', 'token', 'enabled']);
  if (!config.enabled || !config.server || !config.token) throw new Error('Сначала подключите расширение к Джарвису: укажите адрес сервера и ключ ПК вверху окна и нажмите «Подключить».');
  if (!tab?.id || !tab.url) throw new Error('Не удалось прочитать текущую вкладку. Откройте Moodle и нажмите значок расширения в панели Chrome.');
  const u = new URL(tab.url);
  if (u.protocol !== 'https:' || !u.pathname.endsWith('/course/view.php')) throw new Error('Откройте страницу курса Moodle со списком тестов. Если открыт отдельный тест, нажмите название курса вверху страницы, затем подключите его снова.');
  await chrome.storage.local.set({moodleStatus: 'Читаю страницу курса…'});
  const page = await moodlePage(tab.id);
  if (!page.course) throw new Error('Не удалось определить курс Moodle. Дождитесь загрузки страницы курса и повторите подключение.');
  const links = [...new Set(page.links.map(h => {
    const u = new URL(h);
    return u.origin + u.pathname + '?id=' + u.searchParams.get('id');
  }))];
  if (!links.length) throw new Error('На странице не найдены тесты. Раскройте разделы курса, дождитесь их загрузки и повторите подключение.');
  await chrome.storage.local.set({moodleStatus: 'Подключаю управление вкладкой…'});
  try { await chrome.debugger.attach({tabId: tab.id}, '1.3'); }
  catch (_) { throw new Error('Chrome не разрешил управление вкладкой. Закройте DevTools этой вкладки, проверьте разрешения расширения и обновите его в chrome://extensions.'); }
  const session = crypto.randomUUID();
  try {
    if (epoch !== moodleBindEpoch) throw new Error('Подключение отменено.');
    await chrome.storage.session.set({moodle: {tabId: tab.id, origin: u.origin, course: page.course,
      courseUrl: tab.url, links, index: -1, session, run: null, lastJob: null}});
    await chrome.storage.local.set({moodleStatus: 'Проверяю связь с Джарвисом…'});
    const response = await moodlePost(config, 'poll', {session, capabilities: ['inspect']});
    if (response.ok !== true) throw new Error('Сервер не подтвердил подключение Moodle.');
    const current = await chrome.storage.local.get(['server', 'token', 'enabled']);
    if (!current.enabled || current.server !== config.server || current.token !== config.token ||
        (await chrome.storage.session.get('moodle')).moodle?.session !== session) throw new Error('Подключение отменено. Повторите его после проверки настроек.');
    await chrome.storage.local.set({moodleStatus: 'Курс подключён. Найдено тестов: ' + links.length + '. Теперь запросите выполнение в Джарвисе.'});
    moodleTick();
  } catch (e) {
    await moodleStop('Не удалось подключить Moodle: ' + (e.message || 'нет связи с сервером'));
    try { await chrome.debugger.detach({tabId: tab.id}); } catch (_) {}
    throw e;
  }
}

async function moodleStop(message = 'Moodle остановлен на ПК. Уже отправленные попытки сохранены.') {
  moodleBindEpoch++;
  const {moodle: s} = await chrome.storage.session.get('moodle');
  await chrome.storage.session.remove('moodle');
  if (s) { try { await chrome.debugger.detach({tabId: s.tabId}); } catch (_) {} }
  await chrome.storage.local.set({moodleStatus: message});
}
async function moodleGuard(s) {
  const {moodle: current} = await chrome.storage.session.get('moodle');
  const config = await chrome.storage.local.get(['enabled', 'token']);
  if (!config.enabled || !config.token || current?.session !== s.session) throw new Error('Подключение остановлено.');
  const tab = await chrome.tabs.get(s.tabId), u = new URL(tab.url);
  const base = new URL(s.courseUrl).pathname.replace(/course\/view\.php$/, '');
  const quizPath = ['view.php', 'attempt.php', 'summary.php', 'review.php'].some(name => u.pathname === base + 'mod/quiz/' + name);
  if (u.origin !== s.origin || !(quizPath || u.pathname === base + 'course/view.php')) throw new Error('Вкладка вышла за пределы Moodle.');
  const page = await moodlePage(s.tabId);
  if (page.course !== s.course) throw new Error('Вкладка перешла в другой курс или требует входа.');
  if (quizPath && s.index >= 0) {
    const expected = new URL(s.links[s.index]).searchParams.get('id');
    if ((u.pathname.endsWith('/view.php') && u.searchParams.get('id') !== expected) || (page.cmid && page.cmid !== expected)) throw new Error('Вкладка перешла к другому тесту.');
  }
  return page;
}
async function moodleShot(s, inspect = false) {
  const page = await moodleGuard(s);
  const result = await chrome.debugger.sendCommand({tabId: s.tabId}, 'Page.captureScreenshot', {
    format: 'jpeg', quality: 75, captureBeyondViewport: false
  });
  // Normalize physical pixels to CSS pixels so click coordinates are unambiguous on HiDPI.
  const bitmap = await createImageBitmap(await (await fetch('data:image/jpeg;base64,' + result.data)).blob());
  const canvas = new OffscreenCanvas(page.width, page.height);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, page.width, page.height);
  bitmap.close();
  const bytes = new Uint8Array(await (await canvas.convertToBlob({type: 'image/jpeg', quality: 0.8})).arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return {ok: true, image: btoa(binary), width: page.width, height: page.height,
    quiz: s.index >= 0 ? 'test-' + (s.index + 1) : null, empty_course: s.links.length === 0,
    ...(inspect ? {course: page.courseTitle || '', tests: page.tests || []} : {})};
}
// Runs in the page: Moodle themes often scroll a central pane, not window.
function moodleScroll(delta) {
  let element = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
  while (element && element !== document.body && element !== document.documentElement) {
    const style = getComputedStyle(element);
    if (/(auto|scroll)/.test(style.overflowY) && element.scrollHeight > element.clientHeight) {
      element.scrollBy({top: delta, behavior: 'instant'});
      return;
    }
    element = element.parentElement;
  }
  (document.scrollingElement || document.documentElement).scrollBy({top: delta, behavior: 'instant'});
}
async function moodleAction(s, job) {
  if (Date.now() / 1000 >= job.expires) throw new Error('Команда устарела.');
  await moodleGuard(s);
  if (job.action?.action === 'inspect') return moodleShot(s, true);
  if (s.lastJob === job.id) throw new Error('Повтор команды остановлен.');
  s.lastJob = job.id;
  if (s.run && s.run !== job.run) throw new Error('Для нового запуска подключите курс заново.');
  s.run = job.run;
  await chrome.storage.session.set({moodle: s}); // record before any side effect
  const a = job.action, target = {tabId: s.tabId};
  if (a.action === 'next' || a.action === 'open') {
    if (a.action === 'open' && s.index !== -1) throw new Error('Тест уже открыт.');
    s.index++;
    if (s.index >= s.links.length) return {ok: true, done: true};
    await chrome.storage.session.set({moodle: s});
    await chrome.tabs.update(s.tabId, {url: s.links[s.index]});
  } else if (a.action === 'snapshot') {
    // First screenshot is the course page; the model verifies the requested course before opening a quiz.
  } else if (a.action === 'click') {
    const page = await moodleGuard(s);
    if (![a.x, a.y].every(Number.isFinite) || a.x < 0 || a.y < 0 || a.x >= page.width || a.y >= page.height) throw new Error('Неверные координаты.');
    const [hit] = await chrome.scripting.executeScript({target, func: (x, y) => {
      const el = document.elementFromPoint(x, y), link = el?.closest('a[href]');
      return {href: link?.href || '', password: !!el?.closest('input[type=password]')};
    }, args: [a.x, a.y]});
    if (hit.result.password) throw new Error('Ввод пароля запрещён.');
    if (hit.result.href) {
      const u = new URL(hit.result.href);
      const index = s.links.findIndex(h => new URL(h).href === u.href);
      if (s.index < 0 && index >= 0) {
        // Start at the first quiz; remaining tests retain course order.
        if (index !== 0) throw new Error('Начните с первого теста курса.');
        s.index = index;
        await chrome.storage.session.set({moodle: s});
      } else {
        const tab = await chrome.tabs.get(s.tabId), current = new URL(tab.url);
        if (u.origin !== s.origin || !u.pathname.startsWith(new URL(s.courseUrl).pathname.replace(/course\/view\.php$/, 'mod/quiz/')) || (u.pathname.endsWith('/view.php') && u.href !== s.links[s.index])) throw new Error('Переход вне текущего теста запрещён; используйте результат теста для перехода дальше.');
      }
    }
    await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent', {type: 'mousePressed', x: a.x, y: a.y, button: 'left', clickCount: 1});
    await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent', {type: 'mouseReleased', x: a.x, y: a.y, button: 'left', clickCount: 1});
  } else if (a.action === 'type') {
    const [focus] = await chrome.scripting.executeScript({target, func: () => {
      const e = document.activeElement;
      return e?.matches('textarea, input[type=text], input[type=number], input:not([type])') && !e.closest('form[action*="login"]');
    }});
    if (!focus.result || typeof a.text !== 'string' || a.text.length > 4000) throw new Error('Выберите текстовое поле ответа.');
    await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {type: 'keyDown', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65});
    await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent', {type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 2, windowsVirtualKeyCode: 65});
    await chrome.debugger.sendCommand(target, 'Input.insertText', {text: a.text});
  } else if (a.action === 'scroll') {
    if (typeof a.delta !== 'number' || !Number.isFinite(a.delta)) throw new Error('Для прокрутки требуется конечное число.');
    const page = await moodleGuard(s);
    const limit = Math.max(1, Math.min(1500, Math.floor(page.height * 0.8)));
    await chrome.scripting.executeScript({target, func: moodleScroll, args: [Math.max(-limit, Math.min(limit, a.delta))]});
  } else if (a.action !== 'wait') throw new Error('Неизвестная команда.');
  await moodlePause(1200);
  for (let attempt = 0; attempt < 20; attempt++) {
    if ((await chrome.tabs.get(s.tabId)).status === 'complete') break;
    await moodlePause(500);
  }
  return moodleShot(s);
}
async function moodleTick() {
  if (moodleBusy) return;
  moodleBusy = true;
  try {
    const {moodle: s} = await chrome.storage.session.get('moodle');
    const config = await chrome.storage.local.get(['server', 'token', 'enabled']);
    if (!s || !config.enabled || !config.token) return;
    const response = await moodlePost(config, 'poll', {session: s.session, capabilities: ['inspect']});
    if (['completed', 'interrupted', 'stopped'].includes(response.status) && s.run && response.job?.action?.action !== 'inspect') {
      const label = {completed:'Выполнение завершено.', interrupted:'Выполнение прервано.', stopped:'Выполнение остановлено.'}[response.status];
      await moodleStop(label + (response.message ? ' ' + response.message : ' Отчёт запросите у Джарвиса.'));
      return;
    }
    if (response.job) {
      let result;
      try { result = await moodleAction(s, response.job); }
      catch (e) { result = {ok: false}; await moodleStop(e.message); }
      const current = await chrome.storage.local.get(['server', 'token', 'enabled']);
      if (!current.enabled || current.server !== config.server || current.token !== config.token) return;
      if (result.ok && (await chrome.storage.session.get('moodle')).moodle?.session !== s.session) return;
      await moodlePost(config, 'result', {session: s.session, id: response.job.id, result});
    }
  } catch (_) {
    // Do not retry a claimed action: delivery may have failed after submission.
    await moodleStop('Связь прервана. Проверьте текущую попытку перед переподключением.');
  } finally {
    moodleBusy = false;
    if ((await chrome.storage.session.get('moodle')).moodle) setTimeout(moodleTick, 1500);
  }
}
chrome.alarms.onAlarm.addListener(a => { if (a.name === 'jarvis-wb') moodleTick(); });
chrome.debugger.onDetach.addListener(async source => {
  const {moodle: s} = await chrome.storage.session.get('moodle');
  if (s?.tabId === source.tabId) await moodleStop('Управление вкладкой отключено. Проверьте попытку Moodle.');
});
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || sender.tab) return;
  if (message?.type === 'moodleBind') {
    if (moodleBinding) { reply({ok: false, error: 'Подключение Moodle уже выполняется. Дождитесь результата.'}); return; }
    moodleBinding = true;
    chrome.storage.local.set({moodleStatus: 'Проверяю подключение Moodle…'}).then(async () => {
      const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
      if ((await chrome.storage.session.get('moodle')).moodle) throw new Error('Курс уже подключён. Для смены курса сначала нажмите «Остановить Moodle».');
      await moodleBind(tab);
    }).then(() => reply({ok: true})).catch(async e => {
      const error = e.message || 'Не удалось подключить Moodle.';
      await chrome.storage.local.set({moodleStatus: error});
      reply({ok: false, error});
    }).finally(() => { moodleBinding = false; });
    return true;
  }
  if (message?.type === 'moodleStop') {
    moodleStop().then(() => reply({ok: true})).catch(async e => {
      const error = e.message || 'Не удалось остановить Moodle.';
      await chrome.storage.local.set({moodleStatus: error});
      reply({ok: false, error});
    });
    return true;
  }
});
