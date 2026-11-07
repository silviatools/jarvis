// Run with node --test tests/wb_extension.test.cjs; no real WB/network/browser.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const captureCode = fs.readFileSync('windows/wb-extension/capture.js', 'utf8');
const backgroundCode = fs.readFileSync('windows/wb-extension/background.js', 'utf8');

function captureFixture(body, url = 'https://www.wildberries.ru/webapi/lk/myorders/archive/get') {
  class Xhr { open() {} send() {} addEventListener() {} }
  const context = vm.createContext({ URL, Map, WeakMap, Date, console,
    location: { href: 'https://www.wildberries.ru/lk/myorders/archive' },
    XMLHttpRequest: Xhr, window: { fetch: async () => new Response(JSON.stringify(body)) } });
  vm.runInContext(captureCode, context);
  return context.window.fetch(url).then(async response => {
    assert.deepEqual(await response.json(), body, 'observer must not consume site response');
    await new Promise(resolve => setTimeout(resolve, 20));
    return JSON.parse(JSON.stringify(context.window.__jarvisWbRead));
  });
}

test('capture only normalized personal order fields, never authorization', async () => {
  const state = await captureFixture({ orders: [{ rid: 'r-123', name: 'Книга', statusName: 'Получен',
    price: 15000, token: 'secret', phone: '+79991234567', office: { address: 'ПВЗ' } }] });
  assert.equal(state.orders.rows[0].name, 'Книга');
  assert.equal(state.orders.rows[0].pvz, 'ПВЗ');
  assert.equal(state.orders.rows[0].price, '', 'ambiguous price units are not guessed');
  assert.equal(JSON.stringify(state).includes('secret'), false);
  assert.equal(JSON.stringify(state).includes('79991234567'), false);
  assert.equal(state.delivery, null);
});

test('empty list, errors and unrelated responses are distinguished', async () => {
  assert.equal((await captureFixture({ data: [] })).orders.empty, true);
  for (const body of [{}, { error: 'expired', data: [] }, { success: false, data: [] }, { offices: [] }]) {
    assert.equal((await captureFixture(body)).orders, null);
  }
  for (const url of ['https://evilwildberries.ru/webapi/lk/myorders/archive/get',
                     'https://www.wildberries.ru/webapi/lk/myorders/delivery/offices',
                     'https://www.wildberries.ru/catalog/1/detail.aspx']) {
    assert.equal((await captureFixture({ data: [] }, url)).orders, null);
  }
  const delivery = await captureFixture({ items: [{ srid: 's-1', goodsName: 'Посылка' }] },
    'https://www.wildberries.ru/webapi/lk/myorders/delivery/active');
  assert.equal(delivery.orders, null);
  assert.equal(delivery.delivery.rows.length, 1);
});

function readerFixture(text, found) {
  const noop = () => {};
  const context = vm.createContext({ URL, Date, AbortSignal, setTimeout,
    location: new URL('https://www.wildberries.ru/lk/myorders/delivery'),
    document: { body: { innerText: text }, querySelectorAll: () => [] },
    window: { __jarvisWbRead: { delivery: found } },
    chrome: { runtime: { onInstalled: { addListener: noop }, onStartup: { addListener: noop }, onMessage: { addListener: noop } },
      alarms: { onAlarm: { addListener: noop } } } });
  vm.runInContext(backgroundCode, context);
  return JSON.parse(JSON.stringify(vm.runInContext('readPage("delivery")', context)));
}

test('cabinet shell never becomes no orders; login and browser check remain errors', () => {
  assert.equal(readerFixture('Профиль Доставки Покупки', null).code, 'unreadable');
  assert.equal(readerFixture('Введите номер телефона', null).code, 'login');
  const empty = { at: Date.now(), empty: true, rows: [] };
  assert.equal(readerFixture('Проверяем браузер', empty).code, 'blocked');
  assert.equal(readerFixture('Профиль Доставки', empty).ok, true);
  assert.equal(readerFixture('Профиль Доставки', { ...empty, at: Date.now() - 70000 }).code, 'unreadable');
});

test('worker polls, reads only its WB tab, uploads result and retries failed upload', async () => {
  const noop = () => {};
  const values = { server: 'https://jarvis.example', token: 'wbpc_test', enabled: true };
  const requests = [], navigations = [];
  let jobs = [{ id: 'j1', section: 'delivery' }], failUpload = true;
  const local = {
    get: async keys => Object.fromEntries(keys.filter(k => k in values).map(k => [k, values[k]])),
    set: async data => Object.assign(values, data),
    remove: async keys => { for (const k of Array.isArray(keys) ? keys : [keys]) delete values[k]; }
  };
  const result = { ok: true, cabinet: true, url: 'https://www.wildberries.ru/lk/myorders/delivery',
    rows: [{ id: 'r1', name: 'Книга', status: 'В пути' }] };
  const context = vm.createContext({ URL, Date, AbortSignal, setTimeout: callback => queueMicrotask(callback),
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url.endsWith('/result') && failUpload) { failUpload = false; throw new Error('network unavailable'); }
      return new Response(JSON.stringify(url.endsWith('/poll') ? { ok: true, job: jobs.shift() || null } : { ok: true }));
    },
    chrome: {
      storage: { local, session: { get: async () => ({}), set: async () => {} } },
      tabs: { create: async options => { navigations.push(options); return { id: 7 }; },
        reload: async () => {}, get: async () => ({ id: 7, url: result.url, status: 'complete' }) },
      scripting: { executeScript: async () => [{ result }] },
      runtime: { onInstalled: { addListener: noop }, onStartup: { addListener: noop }, onMessage: { addListener: noop } },
      alarms: { onAlarm: { addListener: noop } }
    } });
  vm.runInContext(backgroundCode, context);
  await vm.runInContext('tick()', context);
  assert.equal(values.pending.id, 'j1');
  assert.equal(values.error, 'network unavailable');
  await vm.runInContext('tick()', context);
  assert.equal(values.pending, undefined);
  assert.equal(requests.filter(r => r.url.endsWith('/result')).length, 2);
  assert.equal(navigations.length, 1, 'retry must not read a different order snapshot');
  assert.equal(navigations[0].url, result.url);
  assert.equal(navigations[0].active, false);
  for (const { options } of requests) {
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
  }
  const rejected = await vm.runInContext('readWb("https://evil.test/")', context);
  assert.equal(rejected.ok, false);
  assert.equal(navigations.length, 1);
});
