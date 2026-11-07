// Read visible product links independently of API-to-rid binding. Never pair
// products and statuses by their positions in two lists.
(() => {
  const normalize = value => String(value || '').replace(/[\u00a0\u202f\u2009]/g, ' ').replace(/\s+/g, ' ').trim();
  const currency = '(?:₽|руб\\.?|RUB)';
  const amount = '(?:\\d{1,3}(?: \\d{3})+|\\d+)(?:[.,]\\d{1,2})?';
  const label = '(?:сумма к оплате|всего к оплате|осталось оплатить|к оплате|оплатить(?: заказ)?)';
  function moneyLabels(text) {
    text = normalize(text);
    const matches = [...text.matchAll(new RegExp(label + '\\s*[:—-]?\\s*(' + amount + '\\s*' + currency + ')', 'gi'))];
    // Preserve an explicit payment caption without inventing a currency when
    // WB draws the rouble sign with CSS instead of real text.
    const plain = [...text.matchAll(new RegExp(label + '\\s*[:—-]?\\s*(' + amount + ')(?=\\s|$)', 'gi'))];
    const result = matches.map(match => normalize(match[1]));
    for (const match of plain) {
      const number = normalize(match[1]);
      if (!result.some(value => value.startsWith(number))) result.push(normalize(match[0]));
    }
    return [...new Set(result)].slice(0, 12);
  }
  function safeImageUrl(candidate) {
    if (!candidate) return '';
    try {
      const u = new URL(candidate, location.href);
      if (u.protocol === 'https:' && ['wildberries.ru', 'wb.ru', 'wbbasket.ru', 'wbstatic.net', 'wbcontent.net']
        .some(domain => u.hostname === domain || u.hostname.endsWith('.' + domain))) return u.origin + u.pathname;
    } catch (_) { /* not a product image URL */ }
    return '';
  }
  function safeImage(image) {
    if (!image) return '';
    const candidates = [image.currentSrc, image.getAttribute('data-src'), image.getAttribute('src'),
      (image.getAttribute('data-srcset') || image.getAttribute('srcset') || '').split(',')[0]?.trim().split(/\s+/)[0]];
    for (const candidate of candidates) {
      if (!candidate) continue;
      const url = safeImageUrl(candidate);
      if (url) return url;
    }
    return '';
  }
  function read(activeIds = [], knownArticles = []) {
    const active = new Set(activeIds);
    const products = new Map();
    const images = [...document.querySelectorAll('img')].slice(0, 2000);
    const imageResources = typeof performance !== 'undefined' ? performance.getEntriesByType('resource')
      .filter(entry => entry.initiatorType === 'img').slice(-2000) : [];
    const multipleArticles = new WeakMap();
    let candidates = 0, excluded = 0;
    for (const link of document.querySelectorAll('a[href*="/catalog/"]')) {
      candidates++;
      let target, article;
      try {
        target = new URL(link.href, location.href);
        if (!['www.wildberries.ru', 'wildberries.ru'].includes(target.hostname)) continue;
        article = /^\/catalog\/(\d{4,15})(?:\/|$)/.exec(target.pathname)?.[1];
      } catch (_) { continue; }
      if (!article) continue;
      const recommendation = link.closest('aside,[class*="recommend"],[class*="suggest"],[class*="similar"],[data-widget*="recommend"]');
      const heading = normalize(link.closest('section')?.querySelector('h2,h3')?.textContent);
      if (recommendation || /^(рекомендуем|вам может понравиться|похожие товары|с этим товаром)/i.test(heading)) { excluded++; continue; }
      const container = link.closest('[data-rid],[data-srid],[class*="delivery"],[class*="order"]');
      const main = link.closest('main,[role="main"]');
      const inDeliveryMain = /доставк|заказ/i.test(normalize(main?.querySelector('h1')?.textContent));
      const rids = new Set();
      for (const key of ['rid', 'srid']) if (active.has(target.searchParams.get(key))) rids.add(target.searchParams.get(key));
      let node = link;
      for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
        if (!multipleArticles.has(node)) {
          const articles = new Set();
          for (const anchor of node.querySelectorAll('a[href*="/catalog/"]')) {
            try {
              const id = /^\/catalog\/(\d{4,15})(?:\/|$)/.exec(new URL(anchor.href, location.href).pathname)?.[1];
              if (id) articles.add(id);
            } catch (_) { /* unrelated URL */ }
            if (articles.size > 1) break;
          }
          multipleArticles.set(node, articles.size > 1);
        }
        if (multipleArticles.get(node)) continue; // group-level data is not a per-product link
        for (const key of node.getAttributeNames()) {
          if (key.startsWith('data-') && active.has(node.getAttribute(key))) rids.add(node.getAttribute(key));
        }
      }
      const nameNode = link.querySelector('[class*="good-name"],[class*="goods-name"],[class*="product-name"],[class*="title"]');
      const image = link.querySelector('img');
      const labels = [[nameNode?.textContent, 5], [link.getAttribute('title'), 4], [link.getAttribute('aria-label'), 4],
        [link.innerText, 3], [image?.getAttribute('alt'), 2]];
      const candidate = labels.map(([value, quality]) => [normalize(value), quality]).find(([value]) => value.length > 2 && value.length < 250
        && !/^(подробнее|открыть|перейти|фото(?: товара)?|изображение(?: товара)?|\d+[\s₽руб.]*)$/i.test(value));
      const [name, quality] = candidate || ['', 0];
      const previous = products.get(article) || { article, name: '', image_url: '', rids: [], scope: 'page_unverified' };
      if (name && quality > (previous.name_quality || 0)) { previous.name = name.slice(0, 160); previous.name_quality = quality; }
      if (!previous.image_url) previous.image_url = safeImage(image);
      previous.rids = [...new Set([...previous.rids, ...rids])];
      if (container || inDeliveryMain || rids.size) previous.scope = 'delivery_section';
      products.set(article, previous);
      if (products.size >= 80) break;
    }
    // WB can render an image beside (rather than inside) its product link.
    // Only a loaded URL containing the exact article/images path may bind it.
    for (const article of new Set([...products.keys(), ...knownArticles.filter(value => /^\d{4,15}$/.test(value))])) {
      let product = products.get(article);
      if (product?.image_url) continue;
      const matches = url => url && new URL(url).pathname.includes('/' + article + '/images/');
      let imageUrl = images.map(safeImage).find(matches) || '';
      if (!imageUrl) imageUrl = imageResources.map(entry => safeImageUrl(entry.name)).find(matches) || '';
      if (!imageUrl) continue;
      if (!product) product = { article, name: '', image_url: '', rids: [], scope: 'delivery_section' };
      product.image_url = imageUrl;
      products.set(article, product);
    }
    const labels = moneyLabels(document.body?.innerText || '');
    const paymentCaptions = new Set();
    for (const node of document.querySelectorAll('button,[role="button"],[class*="pay"],[class*="payment"],[data-testid*="pay"]')) {
      const caption = normalize(node.innerText);
      if (caption.length > 2 && caption.length <= 180 && /оплат|при получении/i.test(caption)) paymentCaptions.add(caption);
      if (paymentCaptions.size >= 12) break;
    }
    return { products: [...products.values()].map(({ name_quality, ...product }) => product), amount_due_labels: labels,
      payment_captions: [...paymentCaptions],
      diagnostic: { catalogLinks: candidates, excludedRecommendations: excluded, pageProducts: products.size,
        deliverySectionProducts: [...products.values()].filter(product => product.scope === 'delivery_section').length,
        productsBoundByRid: [...products.values()].filter(product => product.rids.length).length,
        paymentLabels: labels.length, paymentCaptions: paymentCaptions.size,
        imageElements: images.length, loadedImageResources: imageResources.length,
        productsWithImage: [...products.values()].filter(product => product.image_url).length } };
  }
  window.JarvisWbPage = { read, moneyLabels };
})();
