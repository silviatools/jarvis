// Shared, local-only catalogue helpers. No network, credentials or HTML output.
(() => {
  const blocked = /^(?:.*token.*|.*password.*|.*secret.*|authorization|cookie|cookies|sign|signature|payload|link_3_ds|tracker_key|trackerkey|session_id|sessionid|shard_key|shardkey|user_shard_id|encrypted_user_id|user_id|client_id|phone|email|contact|card_number|bank_account)$/i;
  function sanitize(node, depth = 0, budget = { left: 1200 }) {
    if (--budget.left < 0 || depth > 8) return '[ограничено]';
    if (node === null || typeof node === 'boolean') return node;
    if (typeof node === 'bigint') return String(node);
    if (typeof node === 'number') return Number.isFinite(node) ? node : null;
    if (typeof node === 'string') {
      let text = node.replace(/\b(?:\+?7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}\b/g, '[телефон скрыт]');
      if (/^https?:\/\//.test(text)) { try { const u = new URL(text); text = u.origin + u.pathname; } catch (_) { return ''; } }
      return text.slice(0, 1500);
    }
    if (Array.isArray(node)) return node.slice(0, 80).map(item => sanitize(item, depth + 1, budget));
    if (!node || typeof node !== 'object') return null;
    return Object.fromEntries(Object.entries(node).filter(([key]) => !blocked.test(key) && /^[a-zA-Z_][a-zA-Z0-9_]{0,60}$/.test(key)).slice(0, 120)
      .map(([key, value]) => [key, sanitize(value, depth + 1, budget)]));
  }
  function inventory(detailsList) {
    const fields = new Map();
    for (const details of detailsList) {
      const seen = new Set();
      function visit(value, path, depth = 0) {
        if (depth > 8 || fields.size >= 600 || !path) return;
        const type = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
        let field = fields.get(path);
        if (!field) { field = { path, types: [], examples: [], rows: 0 }; fields.set(path, field); }
        if (!field.types.includes(type)) field.types.push(type);
        if (!seen.has(path)) { field.rows++; seen.add(path); }
        if (value !== null && typeof value !== 'object') {
          const example = String(value).slice(0, 180);
          if (field.examples.length < 3 && !field.examples.includes(example)) field.examples.push(example);
        }
        if (Array.isArray(value)) value.slice(0, 80).forEach(item => visit(item, path + '.*', depth + 1));
        else if (value && typeof value === 'object') Object.entries(value).forEach(([key, item]) => visit(item, path + '.' + key, depth + 1));
      }
      Object.entries(details || {}).forEach(([key, value]) => visit(value, key));
    }
    return [...fields.values()].sort((a, b) => a.path.localeCompare(b.path));
  }
  function valuesAt(node, segments) {
    if (!segments.length) return node;
    if (node === null || typeof node !== 'object') return undefined;
    const [key, ...rest] = segments;
    if (key === '*') return Array.isArray(node) ? node.map(item => valuesAt(item, rest)).filter(value => value !== undefined) : undefined;
    if (blocked.test(key) || !Object.prototype.hasOwnProperty.call(node, key)) return undefined;
    return valuesAt(node[key], rest);
  }
  function select(details, paths) {
    const out = [];
    for (const path of paths) {
      if (typeof path !== 'string' || path.length > 180) continue;
      const value = valuesAt(details, path.split('.'));
      if (value === undefined) continue;
      const text = typeof value === 'string' ? value : JSON.stringify(value);
      out.push({ path, value: text.slice(0, 600) });
    }
    return out;
  }
  const allowedPath = path => typeof path === 'string' && path.split('.').every(key => !blocked.test(key));
  window.JarvisWbFields = { sanitize, inventory, select, allowedPath };
})();
