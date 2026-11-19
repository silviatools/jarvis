const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('windows/wb-extension/browse.js', 'utf8');

function fixture() {
  const local = {}, session = {};
  const tab = {id: 7, url: 'about:blank', status: 'complete'};
  const commands = [], creates = [], updates = [];
  let fileHit = false, editable = true;
  const storage = values => ({
    get: async keys => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).filter(k => k in values).map(k => [k, structuredClone(values[k])])),
    set: async value => Object.assign(values, structuredClone(value)),
    remove: async key => { delete values[key]; }
  });
  const listener = {addListener() {}, removeListener() {}};
  const context = vm.createContext({
    URL, URLSearchParams, Date, Number, Uint8Array, String, crypto: require('node:crypto').webcrypto, btoa,
    fetch: async () => ({blob: async () => ({})}),
    setTimeout: fn => queueMicrotask(fn),
    createImageBitmap: async () => ({close() {}}),
    OffscreenCanvas: class { getContext() { return {drawImage() {}}; } async convertToBlob() { return {arrayBuffer: async () => new Uint8Array([255, 216, 1]).buffer}; } },
    chrome: {
      storage: {local: storage(local), session: storage(session)},
      tabs: {
        create: async opts => { creates.push(opts); tab.url = opts.url; return tab; },
        update: async (_id, opts) => { updates.push(opts); if (opts.url) tab.url = opts.url; return tab; },
        get: async id => { if (id !== tab.id) throw new Error('no tab'); return tab; },
        remove: async () => {},
        onCreated: listener
      },
      debugger: {
        attach: async () => {}, detach: async () => {}, onDetach: listener,
        sendCommand: async (_target, name, args) => {
          commands.push({name, args});
          if (name !== 'Runtime.evaluate') return name === 'Page.captureScreenshot' ? {data: '/9gB'} : {};
          const expr = args.expression || '';
          if (expr.includes('input[type="file"]')) return {result: {value: fileHit}};
          if (expr.includes('activeElement')) return {result: {value: editable}};
          if (expr.includes('innerWidth')) return {result: {value: {href: tab.url, title: 'Контур', width: 800, height: 600}}};
          return {result: {value: tab.url}};
        }
      },
      alarms: {onAlarm: listener}
    }
  });
  vm.runInContext(code, context);
  const act = (action, id = '1') => vm.runInContext(
    'browseAction(' + JSON.stringify({id, expires: Date.now() / 1000 + 30, action}) + ')', context);
  return {commands, creates, updates, tab, session, act,
    file: value => { fileHit = value; }, edit: value => { editable = value; },
    url: value => vm.runInContext('browsePublicUrl(' + JSON.stringify(value) + ')', context)};
}

test('one tab is reused and a later address does not open another', async () => {
  const f = fixture();
  const first = await f.act({action: 'open', url: 'https://kontur.ru/'});
  assert.equal(first.ok, true);
  assert.equal(first.width, 800);
  assert.equal(f.creates.length, 1);
  await f.act({action: 'open', url: 'https://kontur.ru/price'});
  assert.equal(f.creates.length, 1);
  assert.equal(f.updates[f.updates.length - 1].url, 'https://kontur.ru/price');
  assert.equal(f.session.browse.tabId, 7);
});

test('screenshot drops secrets from the page address', async () => {
  const f = fixture();
  f.tab.url = 'https://kontur.ru/price?token=SECRET&q=1';
  const shot = await f.act({action: 'open', url: 'https://kontur.ru/price?token=SECRET&q=1'});
  assert.equal(shot.url, 'https://kontur.ru/price?q=1');
  assert.equal(await f.url('https://user:secret@kontur.ru/a?token=SECRET'), 'https://kontur.ru/a');
});

test('javascript and password-in-url are rejected before navigation', async () => {
  const f = fixture();
  await assert.rejects(f.act({action: 'open', url: 'javascript:alert(1)'}), /http/);
  await assert.rejects(f.act({action: 'open', url: 'https://user:pw@kontur.ru/'}), /парол/);
  assert.equal(f.creates.length, 0);
});

test('click, type into a password field and scroll stay in the same tab', async () => {
  const f = fixture();
  await f.act({action: 'open', url: 'https://kontur.ru/'});
  await f.act({action: 'click', x: 12, y: 20});
  assert.ok(f.commands.some(command => command.name === 'Input.dispatchMouseEvent'));
  f.edit(true);
  await f.act({action: 'type', text: 'p@ss-secret'});
  assert.equal(f.commands.find(command => command.name === 'Input.insertText').args.text, 'p@ss-secret');
  await f.act({action: 'key', key: 'Enter'});
  assert.ok(f.commands.some(command => command.name === 'Input.dispatchKeyEvent' && command.args.key === 'Enter'));
  await f.act({action: 'scroll', delta: 400});
  assert.equal(f.creates.length, 1);
});

test('file input and a missing field do not type or click', async () => {
  const f = fixture();
  await f.act({action: 'open', url: 'https://kontur.ru/'});
  const before = f.commands.length;
  f.file(true);
  const blocked = await f.act({action: 'click', x: 10, y: 10});
  assert.equal(blocked.retry, true);
  assert.equal(f.commands.length, before + 2); // page metrics and the file-target check, no mouse
  assert.ok(!f.commands.slice(before).some(command => command.name === 'Input.dispatchMouseEvent'));
  f.file(false);
  f.edit(false);
  const typed = await f.act({action: 'type', text: 'secret'});
  assert.equal(typed.retry, true);
  assert.ok(!f.commands.some(command => command.name === 'Input.insertText'));
});

test('closed tab asks to open the address again', async () => {
  const f = fixture();
  await f.act({action: 'open', url: 'https://kontur.ru/'});
  f.tab.id = 99;
  const result = await f.act({action: 'click', x: 1, y: 1});
  assert.equal(result.retry, true);
  assert.match(result.error, /Вкладка закрыта/);
});
