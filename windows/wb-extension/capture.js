// Runs only on WB. Observe responses the page itself requests; issue no API calls.
// Retain only order fields, never request headers, tokens, cookies or raw JSON.
(() => {
  if (window.__jarvisWbRead) return;
  const state = window.__jarvisWbRead = { orders: null, delivery: null };
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
    if (!key || status !== 200 || !data || data.error || data.errors || data.success === false) return;
    const rows = [];
    parse(data, rows);
    const unique = [...new Map(rows.map(row => [row.id + '\n' + row.name, row])).values()];
    if (!unique.length && !emptyList(data)) return;
    const old = state[key];
    const combined = [...(old?.rows || []), ...unique];
    const merged = [...new Map(combined.map(row => [row.id + '\n' + row.name, row])).values()];
    state[key] = { rows: merged.slice(0, 40), empty: !merged.length, at: Date.now(), truncated: merged.length >= 40 };
  }
  const originalFetch = window.fetch;
  window.fetch = function (...args) {
    const pending = originalFetch.apply(this, args);
    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url;
    if (section(url)) pending.then(response => {
      const length = Number(response.headers.get('content-length') || 0);
      if (length > 2000000) return;
      response.clone().text().then(text => {
        if (text.length <= 2000000) capture(url, response.status, JSON.parse(text));
      }).catch(() => {});
    }).catch(() => {});
    return pending;
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  const urls = new WeakMap();
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    urls.set(this, String(url));
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    const url = urls.get(this);
    if (section(url)) this.addEventListener('load', () => {
      try {
        if (this.responseType === 'json') capture(url, this.status, this.response);
        else if ((!this.responseType || this.responseType === 'text') && this.responseText.length <= 2000000) {
          capture(url, this.status, JSON.parse(this.responseText));
        }
      } catch (_) { /* no readable order response */ }
    }, { once: true });
    return send.apply(this, args);
  };
})();
