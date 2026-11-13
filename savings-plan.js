/* Равный план накоплений.
 *
 * Остаток цели делится на месяцы, где взнос ещё впереди:
 *   - прошлые месяцы в делитель не входят;
 *   - текущий входит, только если за него ещё нет взноса (income);
 *     если взнос уже есть, остаток им уже уменьшен — месяц второй раз не делит;
 *   - будущие месяцы до «накопить к» включительно входят.
 * Свой план (customPlan + monthlyPlan) сюда не попадает: суммы ручные.
 *
 * Рубли целые. Лишний рубль получают ранние месяцы окна, последний забирает
 * остаток от деления — сумма долей равна остатку. Пример: 10 000 ₽ на 3 месяца
 * → 3334 + 3333 + 3333. Если платить показанную долю текущего месяца, следующий
 * пересчёт сходится к той же раскладке.
 *
 * Пустое окно (цель просрочена, или текущий месяц — последний и взнос уже есть)
 * не делит остаток: к доплате вся уже уменьшенная сумма.
 */
(function (root) {
  'use strict';

  function currentMonth(date) {
    var d = date || new Date();
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  }

  function addMonths(ym, n) {
    var p = String(ym || '').split('-');
    var y = parseInt(p[0], 10);
    var m = parseInt(p[1], 10);
    if (!y || !m) return '';
    var d = new Date(y, m - 1 + n, 1);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  }

  function roundRub(n) {
    n = Number(n);
    return isFinite(n) ? Math.round(n) : 0;
  }

  /** Пополнение цели в месяце. Списание взносом не считается. */
  function paidInMonth(expenseId, month, transactions) {
    if (!expenseId || !month || !transactions || !transactions.length) return false;
    for (var i = 0; i < transactions.length; i++) {
      var t = transactions[i];
      if (!t || t.expenseId !== expenseId || t.type !== 'income') continue;
      if (String(t.date || '').slice(0, 7) === month) return true;
    }
    return false;
  }

  /**
   * Месяцы равного плана, на которые ещё делится остаток.
   * today — 'YYYY-MM'. paidCurrent — в today уже есть взнос.
   */
  function planMonths(exp, today, paidCurrent) {
    if (!exp || !exp.targetDate || !today) return [];
    var start = paidCurrent ? addMonths(today, 1) : today;
    if (!start) return [];
    if (exp.startDate && exp.startDate > start) start = exp.startDate;
    if (exp.targetDate < start) return [];
    var months = [];
    var guard = 0;
    var m = start;
    while (m && m <= exp.targetDate && guard < 600) {
      months.push(m);
      m = addMonths(m, 1);
      guard++;
    }
    return months;
  }

  /** Целые рубли: лишние — ранним месяцам. Сумма = remaining. */
  function splitRubles(remaining, n) {
    var rem = Math.max(0, roundRub(remaining));
    var shares = [];
    if (n <= 0) return shares;
    var base = Math.floor(rem / n);
    var extra = rem - base * n;
    for (var i = 0; i < n; i++) shares.push(base + (i < extra ? 1 : 0));
    return shares;
  }

  function monthsLeft(exp, opts) {
    opts = opts || {};
    return planMonths(exp, opts.today || currentMonth(), !!opts.paidCurrent).length;
  }

  function inSchedule(exp, month) {
    return !!(month && exp.startDate && exp.targetDate && month >= exp.startDate && month <= exp.targetDate);
  }

  /**
   * Сколько показать в месяце month.
   * month пустой — доля первого месяца окна (цифра списка и модалки).
   * month внутри окна — доля этого месяца.
   * month в графике, но уже вне окна (прошлый или текущий с взносом) —
   * та же ставка, что в списке, чтобы колонки «По месяцу» не разъезжались.
   * Свой план: ручная сумма month (или today), без пересчёта остатка.
   */
  function monthly(exp, balance, month, opts) {
    opts = opts || {};
    if (!exp || !(Number(exp.amount) > 0) || !exp.startDate || !exp.targetDate) return 0;
    if (exp.startDate > exp.targetDate) return 0;
    var today = opts.today || currentMonth();
    if (exp.customPlan) {
      var slot = month || today;
      if (!inSchedule(exp, slot)) return 0;
      var raw = Number((exp.monthlyPlan || {})[slot]);
      return isFinite(raw) && raw > 0 ? raw : 0;
    }
    var bal = Number(balance);
    if (!isFinite(bal)) bal = 0;
    var remaining = Math.max(roundRub(Number(exp.amount) - bal), 0);
    if (remaining <= 0) return 0;
    var months = planMonths(exp, today, !!opts.paidCurrent);
    if (!months.length) {
      if (!month || month === today || inSchedule(exp, month)) return remaining;
      return 0;
    }
    var shares = splitRubles(remaining, months.length);
    if (!month) return shares[0];
    var idx = months.indexOf(month);
    if (idx >= 0) return shares[idx];
    if (inSchedule(exp, month)) return shares[0];
    return 0;
  }

  var api = {
    currentMonth: currentMonth,
    addMonths: addMonths,
    roundRub: roundRub,
    paidInMonth: paidInMonth,
    planMonths: planMonths,
    splitRubles: splitRubles,
    monthsLeft: monthsLeft,
    monthly: monthly,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.SavingsPlan = api;
})(typeof window !== 'undefined' ? window : globalThis);
