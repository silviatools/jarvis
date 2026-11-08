// Buyer account only. Observe responses and embedded order widgets; read a
// bounded number of same-origin order-detail pages. No login/payment actions.
(() => {
  if (window.__jarvisOzonRead) return;
  const fields = window.JarvisWbFields;
  const state = window.__jarvisOzonRead = { list: null, details: new Map(), attempted: new Set(),
    debug: { version: '0.3.0', fetchSeen: 0, xhrSeen: 0, events: [] } };
  const orderRE = /^\d{6,12}-\d{3,8}$/;
  const scalar = value => typeof value === 'string' ? value.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim().slice(0, 400)
    : value && typeof value === 'object' ? scalar(value.text ?? value.name ?? '') : '';
  const money = value => {
    if (typeof value === 'string') return /\d[\d\s.,]*\s*(?:₽|руб\.?|RUB)/i.test(value) ? scalar(value) : '';
    if (Array.isArray(value)) return value.map(money).find(Boolean) || '';
    return value && typeof value === 'object' ? money(value.price ?? value.text) : '';
  };
  function nodes(root, limit = 1800) {
    const out = [], queue = [root];
    while (queue.length && out.length < limit) {
      const item = queue.shift();
      if (!item || typeof item !== 'object') continue;
      out.push(item);
      for (const value of Object.values(item)) if (value && typeof value === 'object') queue.push(value);
    }
    return out;
  }
  const tagged = (root, tag) => nodes(root).find(node =>
    node.testInfo?.automatizationId === tag || node.common?.testInfo?.automatizationId === tag);
  const tagText = (root, tag) => scalar(tagged(root, tag));
  function safeImage(value) {
    try {
      const url = new URL(value, location.origin);
      if (url.protocol === 'https:' && !url.username && !url.password
          && ['ozone.ru', 'ozon.ru'].some(host => url.hostname === host || url.hostname.endsWith('.' + host))) return url.origin + url.pathname;
    } catch (_) { /* not an image URL */ }
    return '';
  }
  function orderOf(product) {
    const found = new Set();
    for (const node of nodes(product, 250)) {
      if (typeof node.link !== 'string') continue;
      try {
        const url = new URL(node.link, 'https://www.ozon.ru');
        const order = url.searchParams.get('order');
        if (url.origin === 'https://www.ozon.ru' && url.pathname.replace(/\/$/, '') === '/my/orderdetails' && orderRE.test(order || '')) found.add(order);
      } catch (_) { /* ignore unproven link */ }
    }
    return found.size === 1 ? [...found][0] : '';
  }
  function skuOf(product) {
    const action = product.title?.common?.action || {};
    if (/^\d{4,15}$/.test(String(action.id || ''))) return String(action.id);
    for (const node of nodes(product, 250)) {
      if (typeof node.link !== 'string') continue;
      try {
        const u = new URL(node.link, 'https://www.ozon.ru');
        const match = /^\/product\/(?:[^/?]*-)?(\d{4,15})(?:\/|$)/.exec(u.pathname);
        if (u.origin === 'https://www.ozon.ru' && match) return match[1];
      } catch (_) { /* ignore */ }
    }
    return '';
  }
  function schema(node, depth = 0) {
    if (node === null) return 'null';
    if (typeof node !== 'object') return typeof node;
    if (depth > 3) return Array.isArray(node) ? 'array' : 'object';
    if (Array.isArray(node)) return { arrayLength: node.length, item: node.length ? schema(node[0], depth + 1) : 'not inspected' };
    return Object.fromEntries(Object.entries(node).slice(0, 22).map(([key, value]) =>
      [/^[A-Za-z_][A-Za-z0-9_-]{0,60}$/.test(key) ? key : ':key', /token|cookie|secret|password|sign|authorization/i.test(key) ? 'redacted' : schema(value, depth + 1)]));
  }
  function log(url, status, reason, data, transport = 'fetch') {
    let path = '';
    try { path = new URL(url, location.origin).pathname.split('/').map(part => /^[a-z][a-z.-]{0,36}$/.test(part) || /^v\d{1,2}$/.test(part) ? part : part ? ':id' : '').join('/'); } catch (_) { /* no raw URL */ }
    state.debug.events.push({ host: 'www.ozon.ru', path, status, reason, transport, ...(data ? { shape: schema(data) } : {}) });
    state.debug.events = state.debug.events.slice(-16);
  }
  function listWidget(data) {
    if (!data || !Array.isArray(data.ordersV2) || data.error || data.loading || data.isLoading) return false;
    const rows = [], orderIds = new Set();
    data.ordersV2.slice(0, 40).forEach((group, groupIndex) => {
      const left = group.leftBlock || {}, products = group.rightBlock?.products?.products;
      if (!Array.isArray(products)) return;
      const status = tagText(left.textIcon, 'tileStatus') || scalar(left.textIcon);
      const payable = money(tagged(left, 'postpay_sum_cell')?.rightBlock);
      products.slice(0, 40).forEach((product, index) => {
        const order = orderOf(product);
        if (!order) return; // a bundle's cache link is NOT an order-to-item binding
        orderIds.add(order);
        const article = skuOf(product), media = product.image?.productMedia || {};
        rows.push({ id: `ozon-list:${order}:${groupIndex}:${index}`, order_number: order, article,
          name: scalar(product.title?.name) || scalar(product.name) || scalar(media.title) || scalar(media.image?.alt),
          price: money(tagged(product, 'payMoney')), status, quantity: tagText(product, 'quantity'),
          image_url: safeImage(media.image?.url), pvz: scalar(left.title), delivery_date: scalar(left.subtitle),
          payable, details: { order: { number: order }, item: fields.sanitize(product), delivery: fields.sanitize(left) } });
      });
    });
    if (data.ordersV2.length && !rows.length) return false;
    state.list = { at: Date.now(), rows, orders: [...orderIds], empty: data.ordersV2.length === 0,
      truncated: data.ordersV2.length > 40 || !!(data.hasMore || data.nextPage || data.paging?.nextPage || data.paging?.next) };
    return true;
  }
  function shipmentWidget(data, order) {
    if (!orderRE.test(order || '') || !Array.isArray(data?.items) || data.error) return [];
    const shipment = String(data.shipmentId || '');
    if (!/^[A-Za-z0-9_-]{1,60}$/.test(shipment)) return [];
    const status = tagText(data.header, 'shipment-status');
    const { items, ...shipmentFields } = data;
    const rows = [];
    for (const entry of data.items) for (const seller of entry.sellers || []) for (const product of seller.products || []) {
      const article = skuOf(product);
      if (!article) continue;
      const media = product.image?.productMedia || {};
      rows.push({ id: `ozon:${order}:${shipment}:${article}`, order_number: order, article,
        name: scalar(product.title?.name), price: money(product.price), status,
        size: (product.attributes || []).map(scalar).filter(Boolean).join('; '),
        quantity: scalar(product.quantity) || tagText(product, 'quantity'),
        image_url: safeImage(media.image?.url || product.image?.url),
        details: { order: { number: order }, delivery: { ...fields.sanitize(shipmentFields), shipment_id: shipment, status },
          item: fields.sanitize(product), product: { seller: scalar(seller.name) } } });
    }
    return rows;
  }
  function widgets(body) {
    if (!body || typeof body !== 'object' || body.error || body.success === false) return [];
    return Object.entries(body.widgetStates || {}).filter(([key]) => /^(?:orderList|shipmentWidget)(?:-|$)/.test(key))
      .flatMap(([key, value]) => { try { return [[key, typeof value === 'string' ? JSON.parse(value) : value]]; } catch (_) { return []; } });
  }
  function consume(body, order = '') {
    let matched = false;
    const detailRows = [];
    for (const [key, data] of widgets(body)) {
      state.debug.widgetSchemas = [...(state.debug.widgetSchemas || []).filter(item => item.widget !== key.split('-')[0]),
        { widget: key.split('-')[0], shape: schema(data) }].slice(-4);
      if (key.startsWith('orderList')) matched = listWidget(data) || matched;
      else if (order) detailRows.push(...shipmentWidget(data, order));
    }
    if (detailRows.length) {
      state.details.set(order, [...new Map(detailRows.map(row => [row.id, row])).values()]);
      matched = true;
    }
    return matched;
  }
  function embedded(doc, order = '') {
    const body = { widgetStates: {} };
    for (const node of [...doc.querySelectorAll('[id*="state-orderList"], [id*="state-shipmentWidget"]')].slice(0, 60)) {
      const key = node.id.replace(/^state-/, '');
      const raw = node.getAttribute('data-state') || node.textContent || '';
      if (raw.length <= 2000000) body.widgetStates[key] = raw;
    }
    return consume(body, order);
  }
  state.scan = () => embedded(document);
  state.enrich = async (offset = 0) => {
    const orders = state.list?.orders || [];
    const start = Number.isInteger(offset) && offset >= 0 && orders.length ? offset % orders.length : 0;
    const todo = [...orders.slice(start), ...orders.slice(0, start)].filter(order => !state.attempted.has(order)).slice(0, 40);
    const deadline = Date.now() + 18000;
    let cursor = 0;
    async function worker() {
      while (cursor < todo.length && Date.now() < deadline) {
        const order = todo[cursor++]; state.attempted.add(order);
        try {
          // Fixed read-only document route, validated number, no redirects.
          const response = await originalFetch(`https://www.ozon.ru/my/orderdetails/?order=${encodeURIComponent(order)}`,
            { credentials: 'same-origin', redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(Math.max(1, Math.min(5500, deadline - Date.now()))) });
          if (!response.ok) { log('/my/orderdetails/', response.status, 'details_http_error'); continue; }
          const html = await response.text();
          if (html.length > 5000000) { log('/my/orderdetails/', response.status, 'details_too_large'); continue; }
          const doc = new DOMParser().parseFromString(html, 'text/html');
          log('/my/orderdetails/', response.status, embedded(doc, order) ? 'details_parsed' : 'details_schema_unrecognized');
        } catch (_) { log('/my/orderdetails/', 0, 'details_unavailable'); }
      }
    }
    await Promise.all([worker(), worker(), worker()]);
    state.debug.nextDetailOffset = orders.length ? (start + cursor) % orders.length : 0;
  };
  state.read = (section, selectedFields = [], includeDetails = false) => {
    state.scan();
    const list = state.list;
    if (!list || Date.now() - list.at > 60000) return null;
    let rows = [], truncated = list.truncated, extraBudget = 12000, detailsTruncated = false;
    const catalog = [];
    for (const order of list.orders) {
      const coarse = list.rows.filter(row => row.order_number === order), detailed = state.details.get(order);
      if (detailed?.length) {
        for (const detail of detailed) {
          const matches = coarse.filter(row => row.article && row.article === detail.article);
          const shared = matches.length === 1 ? matches[0] : coarse.length === 1 && detailed.length === 1 ? coarse[0] : null;
          rows.push({ ...detail, ...(shared ? { pvz: shared.pvz, delivery_date: shared.delivery_date } : {}),
            image_url: detail.image_url || shared?.image_url || '',
            details: { ...detail.details, ...(shared ? { delivery: { ...detail.details.delivery, group: shared.details.delivery }, office: { address: shared.pvz },
              page: { delivery_caption: shared.delivery_date } } : {}) } });
        }
      } else { rows.push(...coarse.map(row => ({ ...row }))); truncated = true; }
    }
    if (section !== 'orders') rows = rows.filter(row => !/^(?:получен[аоы]?|выдан[аоы]?|отмен[её]н[аоы]?|возвращ[её]н)/i.test(row.status));
    for (const row of rows) {
      const details = fields.sanitize(row.details || {}); catalog.push(details);
      const requested = includeDetails && !selectedFields.length ? fields.inventory([details])
        .filter(field => !field.types.some(type => type === 'object' || type === 'array')).map(field => field.path) : selectedFields;
      row.extra_fields = [];
      for (const extra of includeDetails ? fields.select(details, requested) : []) {
        if (row.extra_fields.length >= 100 || extraBudget < extra.path.length + extra.value.length) { detailsTruncated = true; break; }
        row.extra_fields.push(extra); extraBudget -= extra.path.length + extra.value.length;
      }
      delete row.details;
      delete row.order_number;
    }
    return { rows: rows.slice(0, 40), empty: list.empty || rows.length === 0 && list.rows.length > 0,
      truncated: section === 'orders' || truncated || rows.length > 40, details_truncated: detailsTruncated, fieldCatalog: fields.inventory(catalog) };
  };
  const originalFetch = window.fetch.bind(window);
  async function observe(url, response, transport) {
    try {
      const u = new URL(typeof url === 'string' ? url : url.url || String(url), location.href);
      if (u.origin !== 'https://www.ozon.ru' || !/^\/api\/(?:composer-api|entrypoint-api)\.bx\//.test(u.pathname)) return;
      const route = new URL(u.searchParams.get('url') || '/', u.origin);
      const order = route.pathname.replace(/\/$/, '') === '/my/orderdetails' ? route.searchParams.get('order') || '' : '';
      const copy = response.clone();
      if (!response.ok) { log(u.href, response.status, 'http_error', null, transport); return; }
      const raw = await copy.text();
      if (raw.length > 3000000) { log(u.href, response.status, 'response_too_large', null, transport); return; }
      const body = JSON.parse(raw);
      log(u.href, response.status, consume(body, order) ? 'orders_parsed' : 'schema_unrecognized', body, transport);
    } catch (_) { log(url, response.status, 'not_json', null, transport); }
  }
  const fetchHook = async function (...args) {
    state.debug.fetchSeen++;
    const response = await originalFetch(...args);
    observe(args[0], response, 'fetch').catch(() => {});
    return response;
  };
  window.fetch = fetchHook;
  const originalOpen = XMLHttpRequest.prototype.open, originalSend = XMLHttpRequest.prototype.send;
  const urls = new WeakMap();
  const openHook = function (method, url, ...args) { urls.set(this, url); return originalOpen.call(this, method, url, ...args); };
  const sendHook = function (...args) {
    state.debug.xhrSeen++;
    this.addEventListener('load', () => {
      try {
        const value = this.responseType === 'json' ? JSON.stringify(this.response) : this.responseType === '' || this.responseType === 'text' ? this.responseText : '';
        if (value && value.length <= 3000000) observe(urls.get(this), new Response(value, { status: this.status || 200 }), 'xhr').catch(() => {});
      } catch (_) { /* never interfere with site XHR */ }
    }, { once: true });
    return originalSend.apply(this, args);
  };
  XMLHttpRequest.prototype.open = openHook;
  XMLHttpRequest.prototype.send = sendHook;
  state.hooksActive = () => ({ fetch: window.fetch === fetchHook, xhr: XMLHttpRequest.prototype.open === openHook && XMLHttpRequest.prototype.send === sendHook });
})();
