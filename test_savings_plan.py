"""Равный план накоплений: остаток делится на месяцы, где взнос ещё впереди.

Пример на 2 ноября 2026. Цель 26 000 ₽, старт октябрь, накопить к декабрю,
уже 11 500 ₽, в ноябре взноса нет → 7 250 ₽ и 2 месяца (ноябрь + декабрь).
"""

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JS = ROOT / "savings-plan.js"
HTML = ROOT / "index (9).html"

NY = {
    "id": "ny",
    "name": "Новый год (общие подарки)",
    "amount": 26000,
    "startDate": "2026-10",
    "targetDate": "2026-12",
}


def _run(code):
    script = "const SP = require(%s);\n%s" % (json.dumps(str(JS)), code)
    out = subprocess.check_output(["node", "-e", script], cwd=str(ROOT), text=True)
    return json.loads(out)


def plan(exp, balance, opts, columns=None):
    """Один вызов node: ставка, число месяцев, окно и доли колонок."""
    payload = {
        "exp": exp,
        "balance": balance,
        "opts": opts,
        "columns": columns or [],
    }
    code = """
const c = %s;
const months = SP.planMonths(c.exp, c.opts.today, !!c.opts.paidCurrent);
const cols = {};
c.columns.forEach(m => { cols[m] = SP.monthly(c.exp, c.balance, m, c.opts); });
process.stdout.write(JSON.stringify({
  monthly: SP.monthly(c.exp, c.balance, null, c.opts),
  months: SP.monthsLeft(c.exp, c.opts),
  plan: months,
  cols: cols,
  sum: months.reduce((s, m) => s + SP.monthly(c.exp, c.balance, m, c.opts), 0),
}));
""" % json.dumps(payload)
    return _run(code)


class SavingsPlanTest(unittest.TestCase):
    def test_nov2_empty_month_splits_across_two(self):
        # 2 ноября, взноса в ноябре нет. Прошлый октябрь в делитель не входит.
        r = plan(NY, 11500, {"today": "2026-11", "paidCurrent": False},
                 ["2026-10", "2026-11", "2026-12"])
        self.assertEqual(r["plan"], ["2026-11", "2026-12"])
        self.assertEqual(r["months"], 2)
        self.assertEqual(r["monthly"], 7250)
        self.assertEqual(r["cols"], {"2026-10": 7250, "2026-11": 7250, "2026-12": 7250})
        self.assertEqual(r["sum"], 14500)

    def test_november_payment_leaves_only_december(self):
        # Взнос уже в остатке — ноябрь второй раз остаток не делит.
        r = plan(NY, 11500, {"today": "2026-11", "paidCurrent": True},
                 ["2026-11", "2026-12"])
        self.assertEqual(r["plan"], ["2026-12"])
        self.assertEqual(r["months"], 1)
        self.assertEqual(r["monthly"], 14500)
        self.assertEqual(r["cols"]["2026-12"], 14500)
        self.assertEqual(r["sum"], 14500)

    def test_dec1_without_payment_is_one_month(self):
        r = plan(NY, 11500, {"today": "2026-12", "paidCurrent": False}, ["2026-12"])
        self.assertEqual(r["plan"], ["2026-12"])
        self.assertEqual(r["months"], 1)
        self.assertEqual(r["monthly"], 14500)
        self.assertEqual(r["sum"], 14500)

    def test_dec1_with_payment_is_lump_not_a_second_slot(self):
        # Последний месяц срока, взнос уже есть: делитель пуст, к доплате весь остаток.
        r = plan(NY, 11500, {"today": "2026-12", "paidCurrent": True}, ["2026-12"])
        self.assertEqual(r["plan"], [])
        self.assertEqual(r["months"], 0)
        self.assertEqual(r["monthly"], 14500)
        self.assertEqual(r["cols"]["2026-12"], 14500)

    def test_withdrawal_and_foreign_income_do_not_count(self):
        flags = _run("""
const tx = [
  {expenseId:'ny', type:'expense', date:'2026-11-02', amount:500},
  {expenseId:'other', type:'income', date:'2026-11-02', amount:5000},
];
process.stdout.write(JSON.stringify({
  withdraw: SP.paidInMonth('ny', '2026-11', tx),
  foreign: SP.paidInMonth('ny', '2026-11', tx),
  own: SP.paidInMonth('ny', '2026-11', [{expenseId:'ny', type:'income', date:'2026-11-02', amount:5000}]),
  empty: SP.paidInMonth('ny', '2026-11', []),
  october: SP.paidInMonth('ny', '2026-10', [{expenseId:'ny', type:'income', date:'2026-10-15', amount:11500}]),
}));
""")
        self.assertFalse(flags["withdraw"])
        self.assertFalse(flags["foreign"])
        self.assertTrue(flags["own"])
        self.assertFalse(flags["empty"])
        self.assertTrue(flags["october"])
        # Пустой ноябрь при уже внесённом октябре — всё равно 2 месяца.
        r = plan(NY, 11500, {"today": "2026-11", "paidCurrent": False})
        self.assertEqual(r["plan"], ["2026-11", "2026-12"])

    def test_closed_goal_is_zero(self):
        # Собрано ровно и с запасом: плана нет, даже если месяцы впереди.
        exact = plan(NY, 26000, {"today": "2026-11", "paidCurrent": False})
        over = plan(NY, 30000, {"today": "2026-11", "paidCurrent": False})
        overdue = plan(NY, 26000, {"today": "2027-01", "paidCurrent": False})
        self.assertEqual(exact["monthly"], 0)
        self.assertEqual(exact["months"], 2)  # срок ещё идёт, платить нечего
        self.assertEqual(over["monthly"], 0)
        self.assertEqual(overdue["monthly"], 0)
        self.assertEqual(overdue["months"], 0)

    def test_overdue_unpaid_remainder_is_lump(self):
        r = plan(NY, 11500, {"today": "2027-01", "paidCurrent": False}, ["2026-12"])
        self.assertEqual(r["plan"], [])
        self.assertEqual(r["months"], 0)
        self.assertEqual(r["monthly"], 14500)
        self.assertEqual(r["cols"]["2026-12"], 14500)

    def test_rubles_sum_to_remainder(self):
        # 10 000 ₽ на ноябрь–январь. Ранние месяцы забирают лишний рубль.
        exp = dict(NY, amount=10000, startDate="2026-11", targetDate="2027-01")
        r = plan(exp, 0, {"today": "2026-11", "paidCurrent": False},
                 ["2026-11", "2026-12", "2027-01"])
        self.assertEqual(r["plan"], ["2026-11", "2026-12", "2027-01"])
        self.assertEqual(r["months"], 3)
        self.assertEqual(r["cols"], {"2026-11": 3334, "2026-12": 3333, "2027-01": 3333})
        self.assertEqual(r["monthly"], 3334)
        self.assertEqual(r["sum"], 10000)

    def test_paying_shown_share_stays_consistent(self):
        # Заплатил показанную долю — следующий месяц сходится с прежней раскладкой.
        exp = dict(NY, amount=10000, startDate="2026-11", targetDate="2027-01")
        paid = _run("""
const exp = %s;
let balance = 0;
const months = ['2026-11','2026-12','2027-01'];
const got = [];
months.forEach(m => {
  const share = SP.monthly(exp, balance, null, {today: m, paidCurrent: false});
  got.push(share);
  balance += share;
});
process.stdout.write(JSON.stringify({got: got, balance: balance, after: SP.monthly(exp, balance, null, {today:'2027-01', paidCurrent: true})}));
""" % json.dumps(exp))
        self.assertEqual(paid["got"], [3334, 3333, 3333])
        self.assertEqual(paid["balance"], 10000)
        self.assertEqual(paid["after"], 0)

    def test_custom_plan_keeps_manual_amounts(self):
        exp = dict(NY, customPlan=True, monthlyPlan={"2026-11": 1000, "2026-12": 9000, "2026-10": 4000})
        r = _run("""
const exp = %s;
const opts = {today:'2026-11', paidCurrent:true};
process.stdout.write(JSON.stringify({
  now: SP.monthly(exp, 11500, null, opts),
  nov: SP.monthly(exp, 11500, '2026-11', opts),
  dec: SP.monthly(exp, 11500, '2026-12', {today:'2026-11', paidCurrent:false}),
  oct: SP.monthly(exp, 11500, '2026-10', opts),
}));
""" % json.dumps(exp))
        self.assertEqual(r, {"now": 1000, "nov": 1000, "dec": 9000, "oct": 4000})

    def test_before_start_uses_schedule(self):
        # Сентябрь, копим с октября: текущий месяц до старта окно не сужает.
        r = plan(NY, 0, {"today": "2026-09", "paidCurrent": True},
                 ["2026-10", "2026-12"])
        self.assertEqual(r["plan"], ["2026-10", "2026-11", "2026-12"])
        self.assertEqual(r["months"], 3)
        self.assertEqual(r["sum"], 26000)

    def test_html_uses_the_same_module(self):
        html = HTML.read_text(encoding="utf-8")
        self.assertIn('src="/savings-plan.js"', html)
        self.assertIn("window.SavingsPlan.monthly", html)
        self.assertIn("window.SavingsPlan.planMonths", html)
        self.assertIn("paidCurrent={paidThisMonth(editing.id)}", html)
        self.assertNotIn("Текущий месяц в расчёт остатка Накоплений не включаем", html)
        self.assertNotIn("return Math.ceil(remaining / monthsLeft)", html)


if __name__ == "__main__":
    unittest.main()
