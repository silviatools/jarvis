// Run with node --test tests/wb_extension.test.cjs; no real WB/network/browser.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const captureCode = fs.readFileSync('windows/wb-extension/capture.js', 'utf8');
const fieldCode = fs.readFileSync('windows/wb-extension/field-utils.js', 'utf8');
const backgroundCode = fs.readFileSync('windows/wb-extension/background.js', 'utf8');

function captureFixture(body, url = 'https://www.wildberries.ru/webapi/lk/myorders/archive/get') {
  class Xhr { open() {} send() {} addEventListener() {} }
  const context = vm.createContext({ URL, Map, WeakMap, Date, console,
    location: { href: 'https://www.wildberries.ru/lk/myorders/archive' },
    XMLHttpRequest: Xhr, window: { fetch: async () => new Response(JSON.stringify(body)) } });
  vm.runInContext(fieldCode, context);
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

test('diagnostics report unmatched endpoints and schema without secrets or customer values', async () => {
  const state = await captureFixture({ data: { products: [{ orderId: 'PRIVATE-ORDER',
    name: 'PRIVATE-PRODUCT', phone: '+79991234567', address: 'PRIVATE-ADDRESS' }] },
    token: 'TOKEN-SECRET', password: 'PASSWORD-SECRET' },
    'https://www.wildberries.ru/webapi/lk/myorders/delivery/new-format/123456789?token=URL-SECRET');
  assert.equal(state.delivery, null);
  const log = state.debug.events[0];
  assert.equal(log.status, 200);
  assert.equal(log.reason, 'endpoint_unmatched');
  assert.equal(log.path, '/webapi/lk/myorders/delivery/new-format/:id');
  assert.equal(log.shape.token, 'redacted');
  assert.equal(log.shape.data.products.item.name, 'string');
  const text = JSON.stringify(state.debug);
  for (const value of ['PRIVATE-ORDER', 'PRIVATE-PRODUCT', '79991234567', 'PRIVATE-ADDRESS', 'TOKEN-SECRET', 'PASSWORD-SECRET', 'URL-SECRET', '123456789']) {
    assert.equal(text.includes(value), false, value);
  }
  const failed = await captureFixture({ data: [{ unknownValue: 'not for logs' }] });
  assert.equal(failed.debug.events[0].reason, 'schema_unrecognized');
});

test('fetch accepts URL objects and records API errors rather than empty account', async () => {
  const state = await captureFixture({ orders: [{ orderId: 'r1', name: 'Книга' }] },
    new URL('https://www.wildberries.ru/webapi/lk/myorders/archive/get'));
  assert.equal(state.orders.rows.length, 1);
  assert.equal(state.debug.events[0].reason, 'rows_parsed');
  const failed = await captureFixture({ error: 'private error value', data: [] });
  assert.equal(failed.orders, null);
  assert.equal(failed.debug.events[0].reason, 'api_error');
  assert.equal(JSON.stringify(failed.debug).includes('private error value'), false);
});

async function deliverySequence(responses) {
  class Xhr { open() {} send() {} addEventListener() {} }
  const context = vm.createContext({ URL, Map, Set, WeakMap, Date,
    location: { href: 'https://www.wildberries.ru/lk/myorders/delivery' },
    XMLHttpRequest: Xhr,
    window: { fetch: async url => new Response(JSON.stringify(responses.get(String(url)))) } });
  vm.runInContext(fieldCode, context);
  vm.runInContext(captureCode, context);
  const snapshots = [];
  for (const url of responses.keys()) {
    await context.window.fetch(url);
    await new Promise(resolve => setTimeout(resolve, 20));
    snapshots.push(JSON.parse(JSON.stringify(context.window.__jarvisWbRead)));
  }
  return snapshots;
}

const primaryUrl = 'https://www.wildberries.ru/webapi/v2/lk/myorders/delivery/active';
const supplementalUrl = 'https://wbxoofex.wildberries.ru/api/v2/orders';
const trackerUrl = 'https://status-tracker.wb.ru/api/v6/statuses';

test('current WB joins active notFound/rids with latest tracker status; old history stays out', async () => {
  const [primary, supplemental, complete] = await deliverySequence(new Map([
    [primaryUrl, { resultState: 0, value: { positions: [], notFound: ['live-rid', 'legacy-rid'], archiveRids: [], broken: false } }],
    [supplementalUrl, { data: [{ id: 'parent-order', rids: [{ rid: 'live-rid', name: 'Товар' },
      { rid: 'historic-only', name: 'Уже получен' }] }] }],
    [trackerUrl, [
      [{ rid: 'live-rid', status_name: 'В пути', date: '1731000000000000000' },
       { rid: 'live-rid', status_name: 'Оформлен', date: '1730000000000000000' }],
      [{ rid: 'legacy-rid', status_name: 'Готов к выдаче', date: '1732000000000000000' }],
      [{ rid: 'historic-only', status_name: 'Получен', date: '1733000000000000000' }]
    ]]
  ]));
  assert.equal(primary.delivery, null, 'notFound is not an empty account');
  assert.equal(supplemental.delivery, null, 'wait for the still unresolved active rid');
  assert.equal(complete.delivery.rows.length, 2);
  const current = complete.delivery.rows.find(row => row.id === 'live-rid');
  assert.equal(current.name, 'Товар');
  assert.equal(current.status, 'В пути', 'numeric dates compare as bigint, not arrival order');
  assert.equal(complete.delivery.rows.some(row => row.id === 'historic-only'), false);
  assert.equal(complete.debug.deliveryJoin.activeIds, 2);
  assert.equal(complete.debug.events.at(-1).reason, 'rows_joined');
});

test('join supports different response order and id fields inside rids', async () => {
  const snapshots = await deliverySequence(new Map([
    [trackerUrl, [[{ rid: 'r-1', status_name: 'В пути', date: '1731000000000' }]]],
    [supplementalUrl, { data: [{ id: 'parent', rids: [{ id: 'r-1' }] }] }],
    [primaryUrl, { value: { positions: [], notFound: ['r-1'], broken: false } }]
  ]));
  assert.equal(snapshots[0].delivery, null);
  assert.equal(snapshots[1].delivery, null, 'do not publish before primary delivery response');
  assert.equal(snapshots[2].delivery.rows[0].status, 'В пути');
});

test('an unrelated item id does not block verified EB order/index matching', async () => {
  const order = 'a'.repeat(32), rid = `eb6.i${order}.0.0`;
  const snapshots = await deliverySequence(new Map([
    [primaryUrl, { value: { positions: [], notFound: [rid], broken: false } }],
    [supplementalUrl, { data: [{ id: 'order:' + order, rids: [{ id: 999, nm_id: 123456, name: 'Товар' }] }] }],
    [trackerUrl, [[{ rid, status_name: 'В пути', date: '1731000000000' }]]]
  ]));
  const row = snapshots.at(-1).delivery.rows[0];
  assert.equal(row.id, rid);
  assert.equal(row.article, '123456');
  assert.equal(row.name, 'Товар');
});

test('current empty lists require both sources and never use tracker history as active orders', async () => {
  const snapshots = await deliverySequence(new Map([
    [trackerUrl, [[{ rid: 'old', status_name: 'Получен', date: '1731000000000' }]]],
    [primaryUrl, { value: { positions: [], notFound: [], broken: false } }],
    [supplementalUrl, { data: [] }]
  ]));
  assert.equal(snapshots[1].delivery, null);
  assert.equal(snapshots[2].delivery.empty, true);
  assert.equal(snapshots[2].delivery.rows.length, 0);
  const broken = await deliverySequence(new Map([
    [primaryUrl, { value: { positions: [], notFound: [], broken: true } }],
    [supplementalUrl, { data: [] }]
  ]));
  assert.equal(broken[1].delivery, null);
});

test('EB order/item mapping enriches the right article, price, variant and pickup point', async () => {
  const order = 'a'.repeat(32), other = 'b'.repeat(32);
  const one = `eb6.i${order}.0.0`, two = `eb6.i${order}.1.0`, unknown = `eb6.i${other}.0.0`;
  const snapshots = await deliverySequence(new Map([
    [supplementalUrl, { data: [{ id: order, order_dt: 1794081600, dst_office_id: 12,
      deliveries: [{ id: 'dest', full_address: 'Адрес доставки' }],
      rids: [{ nm_id: 111111, chrt_id: 55, delivery_id: 'dest', quantity: 1, priceU: 199900, token: 'PRIVATE-TOKEN' },
        { nm_id: 222222, delivery_id: 'dest', quantity: 2 }] }] }],
    [primaryUrl, { value: { positions: [], notFound: [one, two, unknown], broken: false } }],
    [trackerUrl, [[{ rid: one, status_name: 'В пути', date: '1731000000000' }],
      [{ rid: two, status_name: 'Готов', date: '1731000000000' }], [{ rid: unknown, status_name: 'Готов', date: '1731000000000' }]]],
    ['https://www.wildberries.ru/webapi/lk/myorders/delivery/offices', { value: { 12: { address: 'Адрес ПВЗ', workTime: '09:00–21:00', sign: 'PRIVATE-SIGN' } } }],
    ['https://card.wb.ru/cards/v4/detail?nm=111111;222222', { products: [
      { id: 111111, name: 'Первый товар', brand: 'Бренд', priceU: 5000, sizes: [{ optionId: 55, origName: 'M' }, { optionId: 56, origName: 'L' }] },
      { id: 222222, name: 'Второй товар' }, { id: 333333, name: 'Чужой товар' }] }]
  ]));
  const complete = snapshots.at(-1), rows = complete.delivery.rows;
  const first = rows.find(row => row.id === one), second = rows.find(row => row.id === two);
  assert.equal(first.article, '111111');
  assert.equal(first.name, 'Первый товар');
  assert.equal(first.brand, 'Бренд');
  assert.equal(first.size, 'M');
  assert.equal(first.price.replace(/\s/g, ''), '1999₽', 'catalogue price must not replace order price');
  assert.equal(first.pvz, 'Адрес доставки');
  assert.equal(first.pvz_hours, '09:00–21:00');
  assert.equal(second.name, 'Второй товар');
  assert.equal(second.quantity, '2');
  assert.equal(rows.find(row => row.id === unknown).article, undefined, 'do not guess another order by index');
  const data = JSON.stringify(first.details);
  assert.equal(data.includes('PRIVATE-TOKEN'), false);
  assert.equal(data.includes('PRIVATE-SIGN'), false);
  assert.equal(first.details.office.workTime, '09:00–21:00');
});

test('field catalogue covers observed scalar fields and safely selects requested paths', () => {
  const context = vm.createContext({ window: {}, URL });
  vm.runInContext(fieldCode, context);
  const helper = context.window.JarvisWbFields;
  const details = helper.sanitize({ item: { nm_id: 111111, token: 'SECRET', pay_state: 7 },
    office: { workTime: '09:00–21:00', address: 'Адрес', cookie: 'COOKIE' },
    product: { name: 'Товар', sizes: [{ name: 'M', price: 1234 }, { name: 'L', price: 1500 }] } });
  const fields = helper.inventory([details]);
  assert.ok(fields.some(field => field.path === 'product.sizes.*.price' && field.types.includes('number')));
  assert.ok(fields.some(field => field.path === 'office.workTime' && field.examples[0] === '09:00–21:00'));
  assert.equal(JSON.stringify(fields).includes('SECRET'), false);
  assert.equal(fields.some(field => /token|cookie/.test(field.path)), false);
  const chosen = helper.select(details, ['office.workTime', 'product.sizes.*.name', 'item.token']);
  assert.equal(chosen.length, 2);
  assert.equal(chosen[0].value, '09:00–21:00');
  assert.equal(chosen[1].value, '["M","L"]');
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
  let messageListener;
  const values = { server: 'https://jarvis.example', token: 'wbpc_test', enabled: true };
  const requests = [], navigations = [];
  let jobs = [{ id: 'j1', section: 'delivery' }], failUpload = true;
  const local = {
    get: async keys => Object.fromEntries(keys.filter(k => k in values).map(k => [k, values[k]])),
    set: async data => Object.assign(values, data),
    remove: async keys => { for (const k of Array.isArray(keys) ? keys : [keys]) delete values[k]; }
  };
  const result = { ok: true, cabinet: true, url: 'https://www.wildberries.ru/lk/myorders/delivery',
    rows: [{ id: 'r1', name: 'Книга', status: 'В пути' }],
    fieldCatalog: [{ path: 'product.name', types: ['string'], examples: ['LOCAL-ONLY-PRODUCT'], rows: 1 }] };
  const context = vm.createContext({ URL, Date, AbortSignal, setTimeout: callback => queueMicrotask(callback),
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url.endsWith('/result') && failUpload) { failUpload = false; throw new Error('network unavailable'); }
      return new Response(JSON.stringify(url.endsWith('/poll') ? { ok: true, job: jobs.shift() || null } : { ok: true }));
    },
    chrome: {
      storage: { local, session: { get: async () => ({}), set: async () => {} } },
      tabs: { create: async options => { navigations.push(options); return { id: 7 }; },
        reload: async () => {}, get: async () => ({ id: 7, url: result.url, status: 'loading' }) },
      scripting: { executeScript: async () => [{ result }] },
      runtime: { id: 'test-extension', getManifest: () => ({ version: '0.2.1' }), onInstalled: { addListener: noop }, onStartup: { addListener: noop }, onMessage: { addListener: listener => { messageListener = listener; } } },
      alarms: { onAlarm: { addListener: noop } }
    } });
  vm.runInContext(backgroundCode, context);
  await vm.runInContext('tick()', context);
  assert.equal(values.pending.id, 'j1');
  assert.equal(values.error, 'network unavailable');
  await vm.runInContext('tick()', context);
  assert.equal(values.pending, undefined);
  assert.equal(requests.filter(r => r.url.endsWith('/result')).length, 2);
  assert.equal(values.readLogs.length, 1);
  assert.equal(values.readLogs[0].trigger, 'jarvis');
  assert.equal(JSON.stringify(values.readLogs).includes('Книга'), false);
  assert.equal(navigations.length, 1, 'retry must not read a different order snapshot');
  assert.equal(navigations[0].url, result.url);
  assert.equal(navigations[0].active, false);
  for (const { options } of requests) {
    assert.equal(options.credentials, 'omit');
    assert.equal(options.redirect, 'error');
  }
  const sentResult = JSON.parse(requests.find(r => r.url.endsWith('/result')).options.body).result;
  assert.equal('diagnostic' in sentResult, false, 'diagnostics stay on the PC');
  assert.equal('fieldCatalog' in sentResult, false, 'entire catalogue/examples stay on PC');
  assert.equal(values.fieldCatalog[0].examples[0], 'LOCAL-ONLY-PRODUCT');
  assert.equal(JSON.stringify(values.readLogs).includes('LOCAL-ONLY-PRODUCT'), false, 'examples are not diagnostic logs');
  assert.equal(values.readLogs[0].tabStatus, 'loading', 'read a loaded list while images are still loading');
  for (let i = 0; i < 10; i++) await vm.runInContext('recordRead({ok:false,code:"unreadable"}, "manual")', context);
  assert.equal(values.readLogs.length, 6);
  const rejected = await vm.runInContext('readWb("https://evil.test/")', context);
  assert.equal(rejected.ok, false);
  assert.equal(navigations.length, 1);
  let acknowledgement;
  const keepPort = messageListener({ type: 'diagnose' }, { id: 'test-extension' }, reply => { acknowledgement = reply; });
  assert.equal(keepPort, false);
  assert.equal(acknowledgement.started, true, 'acknowledge before opening a tab closes the popup');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(navigations.at(-1).active, true, 'manual page check can load lazy content in foreground');
  assert.equal(values.readLogs.at(-1).trigger, 'manual', 'result saved independently of popup lifetime');
});
