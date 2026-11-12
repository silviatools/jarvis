// Published buyer widget shapes, synthetic values; no real Ozon account/network.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const root = 'windows/wb-extension/';
const fieldsCode = fs.readFileSync(root + 'field-utils.js', 'utf8');
const captureCode = fs.readFileSync(root + 'ozon-capture.js', 'utf8');
const backgroundCode = fs.readFileSync(root + 'background.js', 'utf8');
const ORDER = '12345678-9001';

function product(sku, price = '755 ₽', name = 'Тетрадь') {
  return { title: { name: { text: name }, common: { action: { id: sku, link: '/product/' + sku + '/' } } },
    price: { price: [{ text: price }] }, attributes: [{ text: 'Синий' }],
    image: { productMedia: { common: { action: { link: '/my/orderdetails/?order=' + ORDER } },
      image: { url: 'https://ir.ozone.ru/item.jpg?private=strip' } } },
    token: 'SECRET-TOKEN', phone: '+79991234567', url: '/api/test?data=ENCODED-PRIVATE' };
}
function group(sku, status, address) {
  const item = product(sku);
  item.price.common = { testInfo: { automatizationId: 'payMoney' } };
  return { leftBlock: { title: { text: address }, subtitle: { text: 'Сегодня с 09:00 до 21:00' },
    textIcon: { text: { text: status, testInfo: { automatizationId: 'tileStatus' } } } },
    rightBlock: { products: { products: [item] } } };
}
function list(groups = [group('123456789', 'Можно забирать', 'ПВЗ Тестовая, 1'), group('987654321', 'В пути', 'ПВЗ Другая, 2')]) {
  return { ordersV2: groups };
}
function parcel(id, sku, status, price = '755 ₽') {
  return { shipmentId: id, header: [{ text: { text: status, testInfo: { automatizationId: 'shipment-status' } } }],
    items: [{ sellers: [{ name: { text: 'Тестовый продавец' }, products: [product(sku, price)] }] }] };
}
function doc(entries) {
  return { querySelectorAll: () => Object.entries(entries).map(([key, value]) => ({
    id: 'state-' + key, getAttribute: name => name === 'data-state' ? JSON.stringify(value) : null, textContent: '' })) };
}
function fixture({ groups = list(), parcels = [parcel('parcel-1', '123456789', 'Можно забирать'), parcel('parcel-2', '987654321', 'В пути', '1 017 ₽')], embedded = true, dateType = Date, onDetail = () => {} } = {}) {
  const requests = [];
  class Xhr { open() {} send() {} addEventListener() {} }
  const context = vm.createContext({ URL, Map, WeakMap, Date: dateType, console, Response, AbortSignal,
    location: new URL('https://www.ozon.ru/my/orderlist'),
    document: doc(embedded ? { 'orderList-1': groups } : {}), XMLHttpRequest: Xhr,
    DOMParser: class { parseFromString(html) { return doc(JSON.parse(html)); } },
    window: { fetch: async (url, options) => {
      requests.push({ url, options });
      if (String(url).includes('/my/orderdetails/')) { onDetail(); return new Response(JSON.stringify(Object.fromEntries(parcels.map((p, i) => ['shipmentWidget-' + i, p])))); }
      return new Response(JSON.stringify({ widgetStates: { 'orderList-1': JSON.stringify(groups) } }));
    } } });
  vm.runInContext(fieldsCode, context);
  vm.runInContext(captureCode, context);
  return { context, requests, state: context.window.__jarvisOzonRead };
}
const plain = value => JSON.parse(JSON.stringify(value));

test('embedded buyer widgets enrich individual parcels with names and actual item prices', async () => {
  const { state, requests } = fixture();
  state.scan(); await state.enrich();
  const result = plain(state.read('delivery'));
  assert.deepEqual(result.rows.map(row => [row.article, row.price, row.status]), [
    ['123456789', '755 ₽', 'Можно забирать'], ['987654321', '1 017 ₽', 'В пути']]);
  assert.equal(result.rows[0].name, 'Тетрадь');
  assert.equal(result.rows[0].pvz, 'ПВЗ Тестовая, 1');
  assert.equal(result.rows[1].pvz, 'ПВЗ Другая, 2');
  assert.equal(result.rows[0].image_url, 'https://ir.ozone.ru/item.jpg');
  assert.equal(result.rows[0].id, 'ozon:' + ORDER + ':parcel-1:123456789');
  assert.deepEqual(result.rows[0].extra_fields, []);
  assert.equal(requests.length, 1);
  assert.equal(result.truncated, false);
  assert.ok(result.fieldCatalog.some(field => field.path === 'delivery.shipment_id'));
  const detailed = plain(state.read('delivery', ['delivery.shipment_id'], true));
  assert.deepEqual(detailed.rows[0].extra_fields, [{ path: 'delivery.shipment_id', value: 'parcel-1' }]);
  assert.equal(JSON.stringify(detailed).includes('SECRET-TOKEN'), false);
  assert.equal(JSON.stringify(detailed).includes('79991234567'), false);
  assert.equal(JSON.stringify(detailed).includes('ENCODED-PRIVATE'), false);
});

test('same sku in different parcels has distinct stable ids and independent outcomes', async () => {
  const { state } = fixture({ parcels: [parcel('ready', '123456789', 'Можно забирать'), parcel('done', '123456789', 'Получен')] });
  state.scan(); await state.enrich();
  const active = plain(state.read('delivery'));
  assert.equal(active.rows.length, 1);
  assert.ok(active.rows[0].id.includes(':ready:'));
  const all = plain(state.read('orders'));
  assert.equal(all.rows.length, 2);
  assert.notEqual(all.rows[0].id, all.rows[1].id);
});

test('ambiguous item links never attach a pickup point by list order', async () => {
  const { state } = fixture({ groups: list([group('123456789', 'Можно забирать', 'Первый ПВЗ'), group('123456789', 'В пути', 'Второй ПВЗ')]) });
  state.scan(); await state.enrich();
  const result = plain(state.read('delivery'));
  assert.equal(result.rows[0].pvz, undefined);
});

test('unrecognized widgets, loading/error states and empty account are distinct', () => {
  for (const groups of [{}, { ordersV2: [], error: 'expired' }, { ordersV2: [], loading: true }, { ordersV2: [{}] }]) {
    const { state } = fixture({ groups }); state.scan(); assert.equal(state.read('delivery'), null);
  }
  const { state } = fixture({ groups: list([]) }); state.scan();
  assert.equal(state.read('delivery').empty, true);
});

test('response observation preserves the response and logs shapes without personal values', async () => {
  const { context, state } = fixture({ embedded: false });
  const url = 'https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/my/orderlist&token=PRIVATE';
  const response = await context.window.fetch(url);
  assert.ok((await response.json()).widgetStates);
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.equal(state.read('delivery').rows.length, 2);
  assert.ok(state.debug.events.some(event => event.reason === 'orders_parsed'));
  for (const value of ['PRIVATE', 'SECRET-TOKEN', 'Тетрадь', '755', '12345678', 'Тестовая', '79991234567']) {
    assert.equal(JSON.stringify(state.debug).includes(value), false, value);
  }
  const isolated = fixture({ embedded: false });
  await isolated.context.window.fetch('https://www.ozon.ru.evil.test/api/composer-api.bx/page/json/v2');
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(isolated.state.read('delivery'), null);
});

test('read-only enrichment is bounded and each order is fetched once', async () => {
  const groups = list(Array.from({ length: 50 }, (_, i) => {
    const row = group('123456789', 'Можно забирать', 'ПВЗ');
    row.rightBlock.products.products[0].image.productMedia.common.action.link = '/my/orderdetails/?order=12345678-' + (9000 + i);
    return row;
  }));
  const { state, requests } = fixture({ groups });
  state.scan(); await state.enrich();
  assert.equal(requests.length, 40);
  await state.enrich();
  assert.equal(requests.length, 40);
  assert.equal(new Set(requests.map(r => r.url)).size, 40);
  for (const request of requests) {
    assert.match(request.url, /^https:\/\/www\.ozon\.ru\/my\/orderdetails\/\?order=\d+-\d+$/);
    assert.equal(request.options.credentials, 'same-origin');
    assert.equal(request.options.redirect, 'error');
    assert.equal(request.options.method, undefined, 'GET documents only');
  }
});

test('slow detail reads obey the overall deadline and resume from the remaining orders', async () => {
  let now = 100000;
  class Clock extends Date { static now() { return now; } }
  const groups = list(Array.from({ length: 20 }, (_, i) => {
    const row = group('123456789', 'Можно забирать', 'ПВЗ');
    row.rightBlock.products.products[0].image.productMedia.common.action.link = '/my/orderdetails/?order=12345678-' + (9000 + i);
    return row;
  }));
  const first = fixture({ groups, dateType: Clock, onDetail: () => { now += 7000; } });
  first.state.scan(); await first.state.enrich();
  assert.ok(first.requests.length < 20);
  assert.equal(first.state.debug.nextDetailOffset, first.requests.length);
  assert.equal(first.state.read('delivery').truncated, true);
  const next = fixture({ groups, dateType: Clock });
  next.state.scan(); await next.state.enrich(first.state.debug.nextDetailOffset);
  assert.ok(next.requests[0].url.endsWith('order=12345678-' + (9000 + first.requests.length)));
});

test('XHR HTTP failures cannot turn into a successful empty account', async () => {
  const { context, state } = fixture({ embedded: false });
  const xhr = new context.XMLHttpRequest();
  xhr.addEventListener = (_, callback) => { xhr.loaded = callback; };
  xhr.status = 401; xhr.responseType = 'json';
  xhr.response = { widgetStates: { 'orderList-1': JSON.stringify(list([])) } };
  xhr.open('GET', 'https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/my/orderlist');
  xhr.send(); xhr.loaded();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(state.read('delivery'), null);
  assert.equal(state.debug.events[0].status, 401);
  assert.equal(state.debug.events[0].reason, 'http_error');
});

test('unknown numeric currency is not guessed; coarse lists stay usable across reads', () => {
  const data = list();
  data.ordersV2[0].rightBlock.products.products[0].price.price[0].text = 75500;
  // Feed via the actual observer, so there is no DOM rescan to hide mutation.
  return (async () => {
    const item = fixture({ groups: data, embedded: false });
    await item.context.window.fetch('https://www.ozon.ru/api/composer-api.bx/page/json/v2');
    await new Promise(resolve => setTimeout(resolve, 10));
    assert.equal(item.state.read('delivery').rows[0].price, '');
    assert.equal(item.state.read('delivery').rows.length, 2);
  })();
});

test('worker sends Ozon jobs to a separate owned tab and keeps per-shop fields/logs local', async () => {
  const values = { server: 'https://jarvis.example', token: 'wbpc_test', enabled: true,
    ozonSelectedFields: ['delivery.shipment_id'], selectedFields: ['office.wb_only'] };
  const get = async keys => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).filter(k => k in values).map(k => [k, values[k]]));
  const writes = [], calls = [], requests = [];
  let job = { id: 'ozon-job', shop: 'ozon', section: 'delivery', details: true };
  const noop = () => {};
  const context = vm.createContext({ importScripts: () => {}, URL, Date, AbortSignal, console, setTimeout: callback => queueMicrotask(callback),
    fetch: async (url, options) => { requests.push({ url, body: JSON.parse(options.body) }); const next = job; if (url.endsWith('/poll')) job = null;
      return new Response(JSON.stringify(url.endsWith('/poll') ? { ok: true, job: next } : { ok: true })); },
    chrome: { storage: { local: { get, set: async v => Object.assign(values, v), remove: async key => { delete values[key]; } },
      session: { get: async () => ({ wbTab: 1 }), set: async v => writes.push(v) } },
      tabs: { create: async options => { calls.push(options); return { id: 2 }; }, reload: async () => {},
        get: async () => ({ id: 2, url: 'https://www.ozon.ru/my/orderlist', status: 'complete' }) },
      scripting: { executeScript: async options => { assert.equal(options.func.name, 'readOzonPage'); assert.deepEqual(plain(options.args[1]), ['delivery.shipment_id']);
        return [{ result: { ok: true, cabinet: true, url: 'https://www.ozon.ru/my/orderlist', rows: [], empty: true,
          diagnostic: { stage: 'list_read' }, fieldCatalog: [{ path: 'item.name', types: ['string'], examples: ['LOCAL-ONLY'], rows: 1 }] } }]; } },
      runtime: { id: 'ext', getManifest: () => ({ version: '0.3.0' }), onInstalled: { addListener: noop }, onStartup: { addListener: noop }, onMessage: { addListener: noop } },
      alarms: { onAlarm: { addListener: noop } } } });
  vm.runInContext(backgroundCode, context);
  await vm.runInContext('tick()', context);
  assert.deepEqual(plain(requests[0].body.capabilities), ['wb', 'ozon']);
  assert.equal(calls[0].url, 'https://www.ozon.ru/my/orderlist');
  assert.equal(calls[0].active, false);
  assert.equal(writes[0].ozonTab, 2);
  assert.equal(values.ozonReadLogs[0].shop, 'ozon');
  assert.equal(values.readLogs, undefined);
  assert.equal(values.ozonFieldCatalog[0].examples[0], 'LOCAL-ONLY');
  const upload = requests.find(r => r.url.endsWith('/result')).body;
  assert.equal(upload.id, 'ozon-job');
  assert.equal('fieldCatalog' in upload.result, false);
  assert.equal('diagnostic' in upload.result, false);
});
