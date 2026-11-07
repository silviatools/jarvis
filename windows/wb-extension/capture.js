// Runs only on WB. Observe responses the page itself requests; issue no API calls.
// Retain only order fields, never request headers, tokens, cookies or raw JSON.
(() => {
  if (window.__jarvisWbRead) return;
  const state = window.__jarvisWbRead = { orders: null, delivery: null,
    debug: { version: '0.1.1', fetchSeen: 0, xhrSeen: 0, events: [] } };
  const installedFetch = () => window.fetch === wrappedFetch;
  state.hooksActive = () => ({ fetch: installedFetch(), xhr: XMLHttpRequest.prototype.send === wrappedSend });
  function endpoint(raw) {
    try {
      const u = new URL(raw, location.href);
      if (u.protocol !== 'https:' || !(u.hostname === 'wildberries.ru' || u.hostname.endsWith('.wildberries.ru')
          || u.hostname === 'wb.ru' || u.hostname.endsWith('.wb.ru'))) return null;
      if (!/\/api\/|\/webapi\/|\/lk\/|delivery|purchases|myorders/.test(u.pathname)) return null;
      // No query/fragment, opaque ids, personal path segments or account values.
      const path = u.pathname.split('/').map(part =>
        /^[a-z][a-z-]{0,36}$/.test(part) || /^v\d{1,2}$/.test(part) ? part : part ? ':id' : '').join('/');
      return { host: u.hostname, path };
    } catch (_) { return null; }
  }
  function shape(node, depth = 0, budget = { left: 40 }) {
    if (--budget.left < 0) return 'not inspected';
    if (node === null) return 'null';
    if (Array.isArray(node)) return { arrayLength: node.length, item: depth < 4 && node.length ? shape(node[0], depth + 1, budget) : 'not inspected' };
    if (typeof node !== 'object') return typeof node; // types, NEVER scalar values
    if (depth >= 4) return 'object';
    return Object.fromEntries(Object.keys(node).filter(key => /^[A-Za-z_]{1,40}$/.test(key)).slice(0, 16)
      .map(key => [key, /token|authorization|cookie|secret|password/i.test(key) ? 'redacted' : shape(node[key], depth + 1, budget)]));
  }
  function note(raw, transport, status, reason, data) {
    const target = endpoint(raw);
    if (!target) return;
    const entry = { ...target, transport, status: Number(status) || 0, section: section(raw) || 'unmatched', reason };
    if (data !== undefined) entry.shape = shape(data);
    state.debug.events.push(entry);
    state.debug.events = state.debug.events.slice(-24);
  }
  function section(url) {
    try {
      const u = new URL(url, location.href);
      if (u.protocol !== 'https:' || !(u.hostname === 'wildberries.ru' || u.hostname.endsWith('.wildberries.ru'))) return '';
      if (/\/delivery\/offices|\/srids\/?$/.test(u.pathname)) return '';
      if (/\/myorders\/delivery\/(active|pending)|\/delivery\/get/.test(u.pathname)) return 'delivery';
      if (/\/myorders\/archive\/get|\/archive\/getbyrids|\/shopper\/api\/v2\/purchases\/list/.test(u.pathname)) return 'orders';
    } catch (_) { /* unrelated response */ }
    return '';
  }
  const scalar = value => ['string', 'number'].includes(typeof value) && String(value).trim();
  const clean = (value, max) => String(value).replace(/\b(?:\+?7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b/g, '[телефон скрыт]').slice(0, max);
  function pick(flat, keys, max) {
    for (const key of keys) if (scalar(flat[key])) return clean(flat[key], max);
    return '';
  }
  function parse(node, rows, inherited = '', depth = 0) {
    if (depth > 7 || rows.length >= 40) return;
    if (Array.isArray(node)) {
      for (const item of node.slice(0, 100)) parse(item, rows, inherited, depth + 1);
      return;
    }
    if (!node || typeof node !== 'object') return;
    const flat = Object.fromEntries(Object.entries(node).map(([k, v]) => [k.toLowerCase().replace(/[^a-z0-9]/g, ''), v]));
    const id = pick(flat, ['orderid', 'rid', 'srid'], 80) || inherited;
    const name = pick(flat, ['goodsname', 'imtname', 'name', 'title'], 160);
    const status = pick(flat, ['statusname', 'trackingstatus'], 100) || (typeof flat.status === 'string' ? clean(flat.status, 100) : '');
    // Plain numeric "price" is ambiguous across WB endpoints (roubles/kopecks).
    // Show only an explicit currency string or a known kopeck field.
    let price = '';
    if (typeof flat.price === 'string' && /₽|руб/i.test(flat.price)) price = clean(flat.price, 40);
    if (typeof flat.priceu === 'number' && Number.isFinite(flat.priceu)) price = (flat.priceu / 100).toLocaleString('ru-RU') + ' ₽';
    const date = pick(flat, ['orderdate', 'deliverydate', 'orderdatestring', 'createdat', 'date'], 50);
    let pvz = pick(flat, ['pooaddress', 'deliveryaddress', 'officeaddress', 'pvzaddress'], 180);
    if (!pvz && flat.office && typeof flat.office === 'object') pvz = pick(flat.office, ['address'], 180);
    if (id && (name || status)) rows.push({ id, name, price, status, date, pvz });
    for (const [key, item] of Object.entries(node)) {
      if (/office|address|customer|recipient|payment|supplier|seller|brand|user/i.test(key)) continue;
      if (item && typeof item === 'object') parse(item, rows, id, depth + 1);
    }
  }
  function emptyList(node, depth = 0) {
    if (depth > 3) return false;
    if (Array.isArray(node)) return node.length === 0;
    if (!node || typeof node !== 'object' || node.error || node.errors || node.success === false) return false;
    return ['orders', 'purchases', 'items', 'data', 'result', 'goods', 'products'].some(key =>
      Object.prototype.hasOwnProperty.call(node, key) && emptyList(node[key], depth + 1));
  }
  function capture(url, status, data) {
    const key = section(url);
    if (!key) return 'endpoint_unmatched';
    if (status !== 200) return 'http_error';
    if (!data || data.error || data.errors || data.success === false) return 'api_error';
    const rows = [];
    parse(data, rows);
    const unique = [...new Map(rows.map(row => [row.id + '\n' + row.name, row])).values()];
    if (!unique.length && !emptyList(data)) return 'schema_unrecognized';
    const old = state[key];
    const combined = [...(old?.rows || []), ...unique];
    const merged = [...new Map(combined.map(row => [row.id + '\n' + row.name, row])).values()];
    state[key] = { rows: merged.slice(0, 40), empty: !merged.length, at: Date.now(), truncated: merged.length >= 40 };
    return merged.length ? 'rows_parsed' : 'empty_list';
  }
  const originalFetch = window.fetch;
  function wrappedFetch(...args) {
    state.debug.fetchSeen++;
    const pending = originalFetch.apply(this, args);
    const url = args[0] instanceof URL ? args[0].href : typeof args[0] === 'string' ? args[0] : args[0]?.url;
    if (endpoint(url)) pending.then(response => {
      const length = Number(response.headers.get('content-length') || 0);
      if (length > 2000000) { note(url, 'fetch', response.status, 'body_too_large'); return; }
      response.clone().text().then(text => {
        if (text.length > 2000000) { note(url, 'fetch', response.status, 'body_too_large'); return; }
        try {
          const data = JSON.parse(text);
          note(url, 'fetch', response.status, capture(url, response.status, data), data);
        } catch (_) { note(url, 'fetch', response.status, 'not_json'); }
      }).catch(() => note(url, 'fetch', response.status, 'body_unreadable'));
    }).catch(() => note(url, 'fetch', 0, 'network_error'));
    return pending;
  }
  window.fetch = wrappedFetch;
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  const urls = new WeakMap();
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    urls.set(this, String(url));
    return open.call(this, method, url, ...rest);
  };
  function wrappedSend(...args) {
    state.debug.xhrSeen++;
    const url = urls.get(this);
    if (endpoint(url)) this.addEventListener('load', () => {
      try {
        if (this.responseType === 'json') note(url, 'xhr', this.status, capture(url, this.status, this.response), this.response);
        else if ((!this.responseType || this.responseType === 'text') && this.responseText.length <= 2000000) {
          const data = JSON.parse(this.responseText);
          note(url, 'xhr', this.status, capture(url, this.status, data), data);
        }
        else note(url, 'xhr', this.status, 'body_unreadable');
      } catch (_) { note(url, 'xhr', this.status, 'not_json'); }
    }, { once: true });
    return send.apply(this, args);
  }
  XMLHttpRequest.prototype.send = wrappedSend;
})();
