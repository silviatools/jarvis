// Runs only on WB. Observe responses the page itself requests; issue no API calls.
// Retain sanitized order/product/PVZ fields, never headers or credentials.
// Diagnostic logs contain only schemas; the local field catalogue has examples.
(() => {
  if (window.__jarvisWbRead) return;
  const state = window.__jarvisWbRead = { orders: null, delivery: null,
    debug: { version: '0.2.0', fetchSeen: 0, xhrSeen: 0, events: [] } };
  const fieldsHelper = window.JarvisWbFields;
  // Current WB deliveries arrive from three sources. Retain normalized fields
  // only and join status histories to the explicitly active rids.
  const delivery = { primarySeen: false, primaryEmpty: false, supplementalSeen: false, supplementalEmpty: false,
    active: new Set(), rows: new Map(), statuses: new Map(), items: [], products: new Map(), offices: new Map() };
  const installedFetch = () => window.fetch === wrappedFetch;
  state.hooksActive = () => ({ fetch: installedFetch(), xhr: XMLHttpRequest.prototype.send === wrappedSend });
  function endpoint(raw) {
    try {
      const u = new URL(raw, location.href);
      if (u.protocol !== 'https:' || !(u.hostname === 'wildberries.ru' || u.hostname.endsWith('.wildberries.ru')
          || u.hostname === 'wb.ru' || u.hostname.endsWith('.wb.ru'))) return null;
      if (!/\/api\/|\/webapi\/|\/lk\/|delivery|purchases|myorders|\/cards\//.test(u.pathname)) return null;
      // No query/fragment, opaque ids, personal path segments or account values.
      const path = u.pathname.split('/').map(part =>
        /^[a-z][a-z-]{0,36}$/.test(part) || /^v\d{1,2}$/.test(part) ? part : part ? ':id' : '').join('/');
      return { host: u.hostname, path };
    } catch (_) { return null; }
  }
  function shape(node, depth = 0, budget = { left: 40 }, maxDepth = 4) {
    if (--budget.left < 0) return 'not inspected';
    if (node === null) return 'null';
    if (Array.isArray(node)) return { arrayLength: node.length, item: depth < maxDepth && node.length ? shape(node[0], depth + 1, budget, maxDepth) : 'not inspected' };
    if (typeof node !== 'object') return typeof node; // types, NEVER scalar values
    if (depth >= maxDepth) return 'object';
    return Object.fromEntries(Object.keys(node).filter(key => /^[A-Za-z_]{1,40}$/.test(key)).slice(0, maxDepth > 4 ? 40 : 16)
      .map(key => [key, /token|authorization|cookie|secret|password|trackerkey|session|shard/i.test(key) ? 'redacted' : shape(node[key], depth + 1, budget, maxDepth)]));
  }
  function note(raw, transport, status, reason, data) {
    const target = endpoint(raw);
    if (!target) return;
    const entry = { ...target, transport, status: Number(status) || 0, section: section(raw) || 'unmatched', reason };
    if (data !== undefined) entry.shape = shape(data, 0, { left: section(raw) ? 100 : 40 }, section(raw) ? 6 : 4);
    state.debug.events.push(entry);
    state.debug.events = state.debug.events.slice(-24);
  }
  function section(url) {
    try {
      const u = new URL(url, location.href);
      if (u.protocol !== 'https:') return '';
      if (u.hostname === 'status-tracker.wb.ru' && /^\/api\/v\d+\/statuses\/?$/.test(u.pathname)) return 'delivery';
      if (u.hostname === 'wbxoofex.wildberries.ru' && /^\/api\/v\d+\/orders\/?$/.test(u.pathname)) return 'delivery';
      if (!(u.hostname === 'wildberries.ru' || u.hostname.endsWith('.wildberries.ru'))) return '';
      if (/\/delivery\/offices|\/srids\/?$/.test(u.pathname)) return '';
      if (/\/myorders\/delivery\/(active|pending)|\/delivery\/get/.test(u.pathname)) return 'delivery';
      if (/\/myorders\/archive\/get|\/archive\/getbyrids|\/shopper\/api\/v2\/purchases\/list/.test(u.pathname)) return 'orders';
    } catch (_) { /* unrelated response */ }
    return '';
  }
  const scalar = value => ['string', 'number', 'bigint'].includes(typeof value) && String(value).trim();
  const clean = (value, max) => String(value).replace(/\b(?:\+?7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b/g, '[телефон скрыт]').slice(0, max);
  function pick(flat, keys, max) {
    for (const key of keys) if (scalar(flat[key])) return clean(flat[key], max);
    return '';
  }
  function parse(node, rows, inherited = '', depth = 0, keepIdentity = false) {
    if (depth > 7 || rows.length >= 40) return;
    if (Array.isArray(node)) {
      for (const item of node.slice(0, 100)) parse(item, rows, inherited, depth + 1, keepIdentity);
      return;
    }
    if (!node || typeof node !== 'object') return;
    const flat = Object.fromEntries(Object.entries(node).map(([k, v]) => [k.toLowerCase().replace(/[^a-z0-9]/g, ''), v]));
    const id = pick(flat, ['rid', 'srid', 'orderid'], 80) || inherited;
    const name = pick(flat, ['goodsname', 'imtname', 'name', 'title'], 160);
    const status = pick(flat, ['statusname', 'trackingstatus'], 100) || (typeof flat.status === 'string' ? clean(flat.status, 100) : '');
    // Plain numeric "price" is ambiguous across WB endpoints (roubles/kopecks).
    // Show only an explicit currency string or a known kopeck field.
    let price = '';
    if (typeof flat.price === 'string' && /₽|руб/i.test(flat.price)) price = clean(flat.price, 40);
    if (typeof flat.priceu === 'number' && Number.isFinite(flat.priceu)) price = (flat.priceu / 100).toLocaleString('ru-RU') + ' ₽';
    const date = pick(flat, ['orderdate', 'deliverydate', 'orderdatestring', 'orderdt', 'createdat', 'date'], 50);
    let pvz = pick(flat, ['pooaddress', 'deliveryaddress', 'officeaddress', 'pvzaddress'], 180);
    if (!pvz && flat.office && typeof flat.office === 'object') pvz = pick(flat.office, ['address'], 180);
    if (id && (name || status || keepIdentity)) rows.push({ id, name, price, status, date, pvz });
    for (const [key, item] of Object.entries(node)) {
      if (/office|address|customer|recipient|payment|supplier|seller|brand|user/i.test(key)) continue;
      if (item && typeof item === 'object') parse(item, rows, id, depth + 1, keepIdentity);
    }
  }
  function addDeliveryRow(row, active = true) {
    if (!row.id) return;
    if (active) delivery.active.add(row.id);
    const previous = delivery.rows.get(row.id) || {};
    delivery.rows.set(row.id, { ...previous, ...Object.fromEntries(Object.entries(row).filter(([, value]) => value !== '')) });
  }
  const flatten = node => Object.fromEntries(Object.entries(node || {}).map(([k, v]) => [k.toLowerCase().replace(/[^a-z0-9]/g, ''), v]));
  function displayDate(value) {
    const raw = String(value || '');
    if (!/^\d{10}(?:\d{3})?$/.test(raw)) return ''; // unknown timestamp units are not guessed
    const date = new Date(Number(raw) * (raw.length === 10 ? 1000 : 1));
    if (date.getUTCFullYear() < 2000 || date.getUTCFullYear() > 2100) return '';
    return date.toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' });
  }
  function describeItem(order, item) {
    const flat = flatten(item);
    const rows = [];
    parse(item, rows, '__descriptor__', 0, true);
    const fields = {};
    for (const row of rows) for (const field of ['name', 'price', 'status', 'pvz']) if (row[field]) fields[field] = row[field];
    const article = pick(flat, ['nmid', 'nomenclatureid', 'article'], 15);
    if (/^\d{4,15}$/.test(article)) fields.article = article;
    fields.brand = pick(flat, ['brand', 'brandname'], 100);
    fields.size = pick(flat, ['size', 'techsize', 'origsize', 'originalsize'], 60);
    fields.quantity = pick(flat, ['quantity', 'qty', 'count'], 30);
    const payable = pick(flat, ['amountdue', 'amountdueatpickup', 'topay', 'paymentamount'], 60);
    if (/₽|руб/i.test(payable)) fields.payable = payable;
    fields.date = displayDate(order.order_dt);
    fields.delivery_date = displayDate(item.estimated_delivery_time || item.delivery_time);
    const destinations = Array.isArray(order.deliveries) ? order.deliveries : [];
    const destination = destinations.find(d => String(d.id) === String(item.delivery_id));
    if (destination && typeof destination.full_address === 'string') fields.pvz = clean(destination.full_address, 180);
    fields.details = fieldsHelper.sanitize({ order: Object.fromEntries(Object.entries(order).filter(([key]) => !['rids', 'deliveries'].includes(key))),
      item, delivery: destination || {} });
    fields.office_id = String(order.dst_office_id || destination?.office_id || '');
    return fields;
  }
  function bindDeliveryItems() {
    for (const item of delivery.items) {
      const ids = item.id ? [item.id] : [...delivery.active].filter(id => {
        // Match an EXISTING active EB rid to this exact order and item index.
        // Never create a guessed rid or match goods by list position alone.
        const match = /^eb\d+\.i([a-f0-9]{32})\.(\d+)\.(\d+)$/i.exec(id);
        return match && match[1].toLowerCase() === item.orderKey && Number(match[2]) === item.index;
      });
      for (const id of ids) addDeliveryRow({ id, ...item.fields }, false);
    }
  }
  function productResponse(raw) {
    try {
      const u = new URL(raw, location.href);
      return u.protocol === 'https:' && (u.hostname === 'wildberries.ru' || u.hostname.endsWith('.wildberries.ru')
        || u.hostname === 'wb.ru' || u.hostname.endsWith('.wb.ru')) && /\/cards\/(?:v\d+\/)?detail\/?$/.test(u.pathname);
    } catch (_) { return false; }
  }
  function collectProducts(data) {
    const products = data?.products || data?.data?.products;
    if (!Array.isArray(products)) return 'product_schema_unrecognized';
    for (const product of products.slice(0, 100)) {
      if (!product || typeof product !== 'object') continue;
      const flat = flatten(product);
      const article = pick(flat, ['id', 'nmid'], 15);
      const name = pick(flat, ['name', 'goodsname', 'imtname'], 160);
      if (/^\d{4,15}$/.test(article) && name) delivery.products.set(article,
        { name, brand: pick(flat, ['brand', 'brandname'], 100), details: fieldsHelper.sanitize(product) });
    }
    publishDelivery();
    return 'products_observed';
  }
  function statusRank(value) {
    const raw = String(value || '');
    if (/^\d+$/.test(raw)) return BigInt(raw);
    const millis = Date.parse(raw);
    return Number.isFinite(millis) ? BigInt(millis) : 0n;
  }
  function collectStatuses(node, depth = 0) {
    if (depth > 4 || !node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      for (const item of node.slice(0, 200)) collectStatuses(item, depth + 1);
      return;
    }
    const flat = Object.fromEntries(Object.entries(node).map(([k, v]) => [k.toLowerCase().replace(/[^a-z0-9]/g, ''), v]));
    const id = pick(flat, ['rid', 'srid'], 80);
    const status = pick(flat, ['statusname', 'trackingstatus'], 100);
    if (id && status) {
      const rank = statusRank(flat.date);
      const previous = delivery.statuses.get(id);
      if (!previous || rank >= previous.rank) delivery.statuses.set(id, { status, rank, details: fieldsHelper.sanitize(node) });
    }
    for (const key of ['data', 'result', 'statuses', 'payload']) if (node[key]) collectStatuses(node[key], depth + 1);
  }
  function publishDelivery() {
    bindDeliveryItems();
    // Tracker histories may include old received orders. They can enrich an
    // active id, but must NEVER create a delivery by themselves.
    const rows = [...delivery.active].slice(0, 40).map(id => {
      const row = { id, ...(delivery.rows.get(id) || {}) };
      row.details = { ...(row.details || {}) };
      const latest = delivery.statuses.get(id);
      if (latest) { row.status = latest.status; row.details.status = latest.details; }
      const product = delivery.products.get(row.article);
      if (product) {
        if (!row.name) row.name = product.name;
        if (!row.brand) row.brand = product.brand;
        row.details.product = product.details;
        const chrt = row.details.item?.chrt_id ?? row.details.item?.chrtId;
        const variants = Array.isArray(product.details.sizes) ? product.details.sizes : [];
        const variant = chrt === undefined ? null : variants.find(size => String(size.optionId ?? size.chrt_id ?? size.chrtId) === String(chrt));
        if (!row.size && variant) row.size = pick(flatten(variant), ['origname', 'name', 'techsize'], 60);
      }
      const office = delivery.offices.get(row.office_id);
      if (office) {
        row.details.office = office;
        if (!row.pvz) row.pvz = pick(flatten(office), ['address', 'fulladdress'], 180);
        const flatOffice = flatten(office);
        row.pvz_hours = pick(flatOffice, ['worktime', 'workhours', 'workingtime', 'workinghours', 'schedule'], 200);
        if (!row.pvz_hours) {
          const schedule = flatOffice.worktime || flatOffice.workinghours || flatOffice.schedule;
          if (schedule && typeof schedule === 'object') row.pvz_hours = JSON.stringify(schedule).slice(0, 200);
        }
      }
      return row;
    });
    const missing = rows.filter(row => !row.name && !row.status).length;
    state.debug.deliveryJoin = { primarySeen: delivery.primarySeen, supplementalSeen: delivery.supplementalSeen,
      activeIds: delivery.active.size, supplementalIds: delivery.rows.size,
      statusIds: delivery.statuses.size, unresolvedIds: missing,
      rowsWithArticle: rows.filter(row => row.article).length, rowsWithName: rows.filter(row => row.name).length,
      productsObserved: delivery.products.size };
    state.delivery = null;
    if (rows.length && !missing && delivery.primarySeen && delivery.supplementalSeen) {
      state.delivery = { rows, empty: false, at: Date.now(), truncated: delivery.active.size > 40 };
      return 'rows_joined';
    }
    if (!rows.length && delivery.primaryEmpty && delivery.supplementalEmpty) {
      state.delivery = { rows: [], empty: true, at: Date.now(), truncated: false };
      return 'empty_list';
    }
    return 'waiting_delivery_sources';
  }
  function collectOffices(data) {
    function walk(node, id = '', depth = 0) {
      if (!node || typeof node !== 'object' || depth > 5) return;
      if (Array.isArray(node)) { for (const item of node.slice(0, 2000)) walk(item, '', depth + 1); return; }
      const flat = flatten(node);
      const key = pick(flat, ['officeid', 'id'], 30) || id;
      if (key && (flat.address || flat.fulladdress)) delivery.offices.set(key, fieldsHelper.sanitize(node));
      for (const [name, item] of Object.entries(node).slice(0, 2000)) if (item && typeof item === 'object') walk(item, /^\d+$/.test(name) ? name : '', depth + 1);
    }
    walk(data.value || data);
    publishDelivery();
    return 'offices_observed';
  }
  function captureCurrentDelivery(url, data) {
    const u = new URL(url, location.href);
    if (u.hostname === 'status-tracker.wb.ru') {
      collectStatuses(data);
      return publishDelivery();
    }
    if (u.hostname === 'wbxoofex.wildberries.ru') {
      if (!Array.isArray(data.data)) return 'schema_unrecognized';
      delivery.supplementalSeen = true;
      delivery.supplementalEmpty = !data.data.length;
      delivery.items = [];
      for (const order of data.data.slice(0, 100)) {
        if (!order || !Array.isArray(order.rids)) continue;
        for (const [index, item] of order.rids.slice(0, 100).entries()) {
          if (!item || typeof item !== 'object') continue;
          const flat = flatten(item);
          const id = pick(flat, ['rid', 'srid', 'id'], 80);
          const orderKey = String(order.id || '').replace(/-/g, '').toLowerCase();
          delivery.items.push({ id, orderKey, index, fields: describeItem(order, item) });
        }
      }
      return publishDelivery();
    }
    const value = data.value;
    if (!value || !Array.isArray(value.positions) || !Array.isArray(value.notFound)) return '';
    if (value.broken === true) return 'api_error';
    delivery.primarySeen = true;
    delivery.primaryEmpty = !value.positions.length && !value.notFound.length;
    const rows = [];
    parse(value.positions, rows, '', 0, true);
    for (const row of rows) addDeliveryRow(row);
    for (const id of value.notFound.slice(0, 100)) if (scalar(id)) delivery.active.add(clean(id, 80));
    return publishDelivery();
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
    const offices = /\/myorders\/delivery\/offices\/?$/.test(new URL(url, location.href).pathname);
    if (!key && !productResponse(url) && !offices) return 'endpoint_unmatched';
    if (status !== 200) return 'http_error';
    if (!data || data.error || data.errors || data.success === false) return 'api_error';
    if (productResponse(url)) return collectProducts(data);
    if (offices) return collectOffices(data);
    if (key === 'delivery') {
      const joined = captureCurrentDelivery(url, data);
      if (joined) return joined;
    }
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
