/* Планировщик дел — расчёт расходов мероприятия.
 *
 * Один и тот же файл подключают ОБЕ страницы: приложение (index (9).html)
 * и гостевая страница события (event.html). Так суммы у организатора и у
 * участника считаются буквально одним кодом — разъехаться они не могут.
 *
 * Деньги внутри — целые копейки. Рубли с плавающей точкой на делении «на
 * троих» дают 333.33333…, и на разных устройствах итог округлялся бы
 * по-разному; в копейках остаток раздаётся явно и одинаково везде.
 */
(function (root) {
  'use strict';

  var MAX_CENTS = 100000000000; // 1 млрд ₽ — верхняя граница вменяемой суммы

  function toCents(value) {
    if (typeof value === 'number') {
      return isFinite(value) ? Math.round(value * 100) : 0;
    }
    var raw = String(value == null ? '' : value).replace(/\s/g, '').replace(',', '.');
    var n = parseFloat(raw);
    return isFinite(n) ? Math.round(n * 100) : 0;
  }

  function fromCents(cents) {
    return Math.round(cents) / 100;
  }

  /** «1 234,50» — без валюты, знак минуса типографский. */
  function formatCents(cents) {
    var neg = cents < 0;
    var abs = Math.abs(Math.round(cents));
    var rub = Math.floor(abs / 100);
    var kop = abs % 100;
    var s = String(rub).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    if (kop) s += ',' + (kop < 10 ? '0' + kop : String(kop));
    return (neg ? '−' : '') + s;
  }

  function formatMoney(cents) {
    return formatCents(cents) + ' ₽';
  }

  /**
   * Поровну, но без потерянных копеек: базовая доля всем, остаток — по одной
   * копейке в порядке сортировки id. Порядок стабильный, поэтому два
   * устройства получат ровно одинаковую раскладку.
   */
  function splitEqually(totalCents, ids) {
    var out = {};
    var uniq = [];
    (ids || []).forEach(function (id) {
      if (id && uniq.indexOf(id) < 0) uniq.push(id);
    });
    var n = uniq.length;
    if (!n || !totalCents) return out;
    var sign = totalCents < 0 ? -1 : 1;
    var abs = Math.abs(totalCents);
    var base = Math.floor(abs / n);
    var rest = abs - base * n;
    uniq.slice().sort().forEach(function (id, i) {
      out[id] = sign * (base + (i < rest ? 1 : 0));
    });
    return out;
  }

  /** Кто сколько должен по одной трате: { participantId: копейки }. */
  function expenseShares(exp) {
    if (!exp) return {};
    if (exp.splitMode === 'custom') {
      var out = {};
      (exp.shares || []).forEach(function (s) {
        if (!s || !s.participantId) return;
        var c = toCents(s.amount);
        if (c > 0) out[s.participantId] = (out[s.participantId] || 0) + c;
      });
      return out;
    }
    return splitEqually(toCents(exp.amount), exp.participantIds || []);
  }

  /** Сумма долей — для проверки «ручное деление сходится с общей суммой». */
  function sharesTotal(exp) {
    var shares = expenseShares(exp);
    var sum = 0;
    Object.keys(shares).forEach(function (k) { sum += shares[k]; });
    return sum;
  }

  /**
   * Куда переводить каждому: реквизиты из его САМОЙ СВЕЖЕЙ траты, где они
   * заполнены. Человек мог поменять карту — актуальной считается последняя.
   */
  function requisitesOf(expenses) {
    var best = {};
    (expenses || []).forEach(function (e) {
      if (!e || !e.payerId) return;
      var payTo = (e.payTo || '').trim();
      if (!payTo) return;
      var ts = e.updatedAt || e.createdAt || 0;
      if (!best[e.payerId] || ts >= best[e.payerId].ts) {
        best[e.payerId] = { ts: ts, payTo: payTo };
      }
    });
    var out = {};
    Object.keys(best).forEach(function (k) { out[k] = best[k].payTo; });
    return out;
  }

  function paymentId(eventId, fromId, toId) {
    return eventId + ':' + fromId + '>' + toId;
  }

  /**
   * Полный расчёт по событию.
   *
   * Встречные долги схлопываются: если Аня должна Боре 500, а Боря Ане 300,
   * в таблице будет одна строка «Аня → Боря 200», а не два перевода
   * навстречу друг другу. В расшифровке видно обе стороны: траты Бори идут
   * плюсом, свои траты Ани — минусом.
   */
  function computeSettlement(opts) {
    var eventId = opts.eventId;
    var expenses = (opts.expenses || []).filter(function (e) {
      return e && e.eventId === eventId;
    }).slice().sort(function (a, b) {
      return (a.createdAt || 0) - (b.createdAt || 0);
    });
    var payments = (opts.payments || []).filter(function (p) {
      return p && p.eventId === eventId;
    });
    var names = {};
    (opts.participants || []).forEach(function (p) { if (p && p.id) names[p.id] = p.name; });

    var gross = {};   // должник → кредитор → копейки
    var detail = {};  // должник → кредитор → [{ expenseId, title, cents }]
    var spent = {};   // кто сколько выложил
    var share = {};   // чья доля сколько всего
    var people = [];
    var addPerson = function (id) { if (id && people.indexOf(id) < 0) people.push(id); };
    Object.keys(names).forEach(addPerson);

    expenses.forEach(function (e) {
      var payer = e.payerId;
      if (!payer) return;
      addPerson(payer);
      var total = toCents(e.amount);
      spent[payer] = (spent[payer] || 0) + total;
      var shares = expenseShares(e);
      Object.keys(shares).forEach(function (pid) {
        var cents = shares[pid];
        if (cents <= 0) return;
        addPerson(pid);
        share[pid] = (share[pid] || 0) + cents;
        if (pid === payer) return;   // свою долю сам себе не переводишь
        gross[pid] = gross[pid] || {};
        gross[pid][payer] = (gross[pid][payer] || 0) + cents;
        detail[pid] = detail[pid] || {};
        detail[pid][payer] = detail[pid][payer] || [];
        detail[pid][payer].push({
          expenseId: e.id,
          title: (e.title || '').trim() || 'Без описания',
          cents: cents,
        });
      });
    });

    var requisites = requisitesOf(expenses);
    var paymentByKey = {};
    payments.forEach(function (p) {
      paymentByKey[paymentId(eventId, p.fromId, p.toId)] = p;
    });

    var byParticipant = {};
    people.forEach(function (id) {
      byParticipant[id] = {
        id: id,
        name: names[id] || 'Бывший участник',
        spent: spent[id] || 0,
        share: share[id] || 0,
        balance: (spent[id] || 0) - (share[id] || 0),
        owes: [],      // кому этот человек переводит
        getsFrom: [],  // кто переводит ему
      };
    });

    // Пары обходим один раз, сразу схлопывая встречные суммы.
    var ordered = people.slice().sort();
    for (var i = 0; i < ordered.length; i++) {
      for (var j = i + 1; j < ordered.length; j++) {
        var a = ordered[i], b = ordered[j];
        var ab = (gross[a] && gross[a][b]) || 0;
        var ba = (gross[b] && gross[b][a]) || 0;
        var net = ab - ba;
        if (!net) continue;
        var from = net > 0 ? a : b;
        var to = net > 0 ? b : a;
        var amount = Math.abs(net);
        var items = ((detail[from] || {})[to] || []).map(function (it) {
          return { expenseId: it.expenseId, title: it.title, cents: it.cents, own: false };
        }).concat(((detail[to] || {})[from] || []).map(function (it) {
          return { expenseId: it.expenseId, title: it.title, cents: -it.cents, own: true };
        }));
        var payment = paymentByKey[paymentId(eventId, from, to)] || null;
        var row = {
          fromId: from,
          toId: to,
          fromName: (byParticipant[from] || {}).name || 'Бывший участник',
          toName: (byParticipant[to] || {}).name || 'Бывший участник',
          amount: amount,
          payTo: requisites[to] || '',
          items: items,
          paid: !!(payment && payment.paid),
          paidAt: payment ? (payment.paidAt || 0) : 0,
          paidBy: payment ? (payment.paidBy || '') : '',
          // Отметку ставили под конкретную сумму. Если после этого добавили
          // трату, сумма разъехалась — молчать об этом нельзя.
          markedAmount: payment && payment.paid ? (payment.amount || 0) : 0,
          amountChangedAfterPaid: !!(payment && payment.paid && payment.amount && payment.amount !== amount),
        };
        byParticipant[from].owes.push(row);
        byParticipant[to].getsFrom.push(row);
      }
    }

    var totalSpent = 0;
    Object.keys(spent).forEach(function (k) { totalSpent += spent[k]; });

    var sortRows = function (rows) {
      rows.sort(function (x, y) { return y.amount - x.amount; });
    };
    Object.keys(byParticipant).forEach(function (id) {
      sortRows(byParticipant[id].owes);
      sortRows(byParticipant[id].getsFrom);
    });

    return {
      byParticipant: byParticipant,
      people: people,
      expenses: expenses,
      requisites: requisites,
      totalSpent: totalSpent,
      perHead: people.length ? Math.round(totalSpent / people.length) : 0,
    };
  }

  var api = {
    toCents: toCents,
    fromCents: fromCents,
    formatCents: formatCents,
    formatMoney: formatMoney,
    splitEqually: splitEqually,
    expenseShares: expenseShares,
    sharesTotal: sharesTotal,
    requisitesOf: requisitesOf,
    paymentId: paymentId,
    computeSettlement: computeSettlement,
    MAX_CENTS: MAX_CENTS,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.PlannerSplit = api;
})(typeof window !== 'undefined' ? window : this);
