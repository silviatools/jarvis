"""Модели по ролям, уровни рассуждений, эскалация, группы инструментов, кэш-порядок промпта и журнал расхода.
ИИ подменён (requests.post), Telegram не нужен."""

import json
import os
import sys
import tempfile
import types
import unittest

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name
if "pyaes" not in sys.modules:
    try:
        import pyaes  # noqa: F401
    except ImportError:
        sys.modules["pyaes"] = types.ModuleType("pyaes")

import ai_usage      # noqa: E402
import jarvis        # noqa: E402
import tool_groups   # noqa: E402


class R:
    def __init__(self, j, ok=True, status=200):
        self.ok, self._j, self.status_code = ok, j, status

    def json(self):
        return self._j


def oai_text(text, usage=None):
    return R({"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
              "usage": usage or {"prompt_tokens": 2000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 1000}}})


def oai_tools(*calls):
    return R({"choices": [{"message": {"content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]},
        "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 3000, "completion_tokens": 20}})


def make_app(models=None, preset_model="gpt-5.4", provider="openai", skills=None):
    settings = {"aiPresets": [{"id": "a", "name": "n", "provider": provider, "apiKey": "k", "model": preset_model}],
                "aiAssignments": {"voiceAssistant": "a"}}
    if models is not None:
        settings["aiModels"] = models
    return {"telegramSkills": skills or {}, "settings": settings}


class Base(unittest.TestCase):
    def setUp(self):
        for p in (ai_usage.usage_path(), ai_usage.turns_path()):
            if p.exists():
                p.unlink()
        self._post = jarvis.requests.post
        self.sent = []

    def tearDown(self):
        jarvis.requests.post = self._post

    def script(self, *responses):
        queue = list(responses)

        def post(url, headers=None, json=None, timeout=None):
            self.sent.append(json)
            return queue.pop(0) if len(queue) > 1 else queue[0]
        jarvis.requests.post = post


class CatalogTest(unittest.TestCase):
    def test_efforts_follow_model(self):
        self.assertIn("none", ai_usage.efforts_for("gpt-6.1-terra"))
        self.assertNotIn("none", ai_usage.efforts_for("gpt-6.1-sol"))
        self.assertEqual(ai_usage.efforts_for("gpt-4o"), [])
        self.assertEqual(ai_usage.efforts_for("gpt-5.4-2026-03-05"), ai_usage.efforts_for("gpt-5.4"))
        self.assertTrue(ai_usage.efforts_for("gpt-7-unknown"))      # неизвестная новая модель: общий набор
        self.assertEqual(ai_usage.efforts_for("claude-sonnet-5"), [])

    def test_fit_effort(self):
        self.assertEqual(ai_usage.fit_effort("gpt-6.1-sol", "none"), "low")
        self.assertEqual(ai_usage.fit_effort("gpt-6-luna", "none"), "none")
        self.assertEqual(ai_usage.fit_effort("gpt-4o", "low"), "")
        self.assertEqual(ai_usage.fit_effort("gpt-6-luna", ""), "")

    def test_defaults_prefilled_by_plan(self):
        d = ai_usage.default_ai_models()
        self.assertEqual((d["main"]["model"], d["main"]["effort"]), ("gpt-6.1-terra", "low"))
        self.assertEqual(d["complex"]["model"], "gpt-6.1-sol")
        self.assertEqual(d["background"]["model"], "gpt-6-luna")
        self.assertFalse(d["toolGroups"])        # группы по умолчанию выключены: безопасный полный набор

    def test_roles_inherit(self):
        s = {"aiModels": {"background": {"model": "gpt-6-luna", "effort": "low"}, "triage": {"model": "", "effort": "medium"}}}
        r = ai_usage.resolve_role(s, "triage", "gpt-5.4")
        self.assertEqual((r["model"], r["effort"]), ("gpt-6-luna", "medium"))     # модель от фоновой, свой уровень
        r = ai_usage.resolve_role(s, "title", "gpt-5.4")
        self.assertEqual((r["model"], r["effort"]), ("gpt-6-luna", "low"))
        off = ai_usage.resolve_role({"aiModels": {"enabled": False}}, "main", "gpt-5.4")
        self.assertEqual((off["model"], off["effort"], off["managed"]), ("gpt-5.4", "", False))

    def test_cost_matches_real_invoice_sample(self):
        # gpt-5.4 за 30.09–30.10 по CSV OpenAI: 12.12M вход, 8.79M кэш, 0.14M выход ≈ $12.64
        c = ai_usage.cost("gpt-5.4-2026-03-05", 12123433, 8792576, 140700)
        self.assertAlmostEqual(c, 12.64, delta=0.05)
        self.assertEqual(ai_usage.cost("unknown-model", 1000, 0, 10), 0.0)


class RolesTest(Base):
    def test_disabled_keeps_old_behaviour(self):
        app = make_app({"enabled": False})
        conf = jarvis._ai_conf(app)
        self.assertEqual((conf.model, conf.effort), ("gpt-5.4", ""))
        self.script(oai_text("ок"))
        jarvis._assistant_loop_openai([{"role": "user", "content": "привет"}], conf.key, conf.model, app, "", False)
        body = self.sent[0]
        self.assertEqual(body["model"], "gpt-5.4")
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("prompt_cache_key", body)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(len([m for m in body["messages"] if m["role"] == "system"]), 1)

    def test_roles_models_and_effort(self):
        app = make_app()           # aiModels не задан → значения по плану
        main, bg = jarvis._ai_conf(app, "main"), jarvis._ai_conf(app, "background")
        self.assertEqual((main.model, main.effort), ("gpt-6.1-terra", "low"))
        self.assertEqual((bg.model, bg.effort), ("gpt-6-luna", "none"))
        self.assertEqual(jarvis._ai_conf(app, "title").model, "gpt-6-luna")
        self.assertEqual((jarvis._ai_conf(app, "triage").model, jarvis._ai_conf(app, "triage").effort), ("gpt-6-luna", "low"))
        sb = jarvis._ai_conf(app, "studyBrief")
        self.assertEqual((sb.model, sb.effort), ("gpt-6.1-terra", "medium"))

    def test_effort_comes_only_from_settings(self):
        app = make_app({"main": {"model": "gpt-6.1-sol", "effort": "none"}})     # none у sol не поддерживается → low
        conf = jarvis._ai_conf(app)
        self.assertEqual((conf.model, conf.effort), ("gpt-6.1-sol", "low"))
        app = make_app({"main": {"model": "gpt-6.1-terra", "effort": "xhigh"}})
        self.assertEqual(jarvis._ai_conf(app).effort, "xhigh")
        app = make_app({"main": {"model": "gpt-4o", "effort": "high"}})
        self.assertEqual(jarvis._ai_conf(app).effort, "")                      # модель без рассуждений

    def test_request_carries_effort_and_cache_key(self):
        app = make_app()
        conf = jarvis._ai_conf(app)
        self.script(oai_text("ок"))
        jarvis._assistant_loop_openai([{"role": "user", "content": "привет"}], conf.key, conf.model, app, "", False,
                                      effort=conf.effort, role="main", func="chat", cache=True, compact=True)
        body = self.sent[0]
        self.assertEqual((body["model"], body["reasoning_effort"]), ("gpt-6.1-terra", "low"))
        self.assertTrue(body["prompt_cache_key"])
        sysmsgs = [m for m in body["messages"] if m["role"] == "system"]
        self.assertEqual(len(sysmsgs), 2)
        self.assertIn("СЕГОДНЯ", sysmsgs[1]["content"])        # дата — во втором, меняющемся сообщении
        self.assertNotIn("СЕГОДНЯ:", sysmsgs[0]["content"])

    def test_unsupported_params_retry_without(self):
        app = make_app()
        conf = jarvis._ai_conf(app)
        bad = R({"error": {"message": "Unrecognized request argument supplied: reasoning_effort"}}, ok=False, status=400)
        self.script(bad, oai_text("ок"))
        text, _ = jarvis._assistant_loop_openai([{"role": "user", "content": "привет"}], conf.key, conf.model, app, "", False,
                                                effort="low", cache=True)
        self.assertEqual(text, "ок")
        self.assertIn("reasoning_effort", self.sent[0])
        self.assertNotIn("reasoning_effort", self.sent[1])
        self.assertNotIn("prompt_cache_key", self.sent[1])

    def test_background_calls_use_luna(self):
        app = make_app()
        self.script(oai_text("Заголовок диалога"))
        title_conf = jarvis._ai_conf(app, "title")
        jarvis.generate_dialog_title(title_conf[0], title_conf[1], title_conf[2], "привет", effort=title_conf[4])
        self.assertEqual((self.sent[0]["model"], self.sent[0]["reasoning_effort"]), ("gpt-6-luna", "none"))
        self.script(oai_text("{}"))
        jarvis._llm_text(jarvis._ai_conf(app, "triage"), "sys", "user", func="triage")
        self.assertEqual((self.sent[1]["model"], self.sent[1]["reasoning_effort"]), ("gpt-6-luna", "low"))

    def test_claude_preset_ignores_roles(self):
        app = make_app(provider="claude", preset_model="claude-sonnet-5")
        conf = jarvis._ai_conf(app)
        self.assertEqual((conf.provider, conf.model, conf.effort), ("claude", "claude-sonnet-5", ""))
        self.assertIsNone(jarvis._esc_conf(app, conf))


class UsageJournalTest(Base):
    def test_every_step_is_logged_and_summed(self):
        app = make_app()
        conf = jarvis._ai_conf(app)
        self.script(oai_tools(("get_data", {"domain": "about"})), oai_text("готово"))
        stats = {}
        jarvis._assistant_loop_openai([{"role": "user", "content": "что я рассказал о себе"}], conf.key, conf.model, app, "", False,
                                      effort=conf.effort, role="main", func="chat", turn=stats)
        jarvis._record_turn(stats, ai_usage.normalize(app["settings"]), "chat", "что я рассказал о себе", 1)
        s = ai_usage.summary(7, "model")
        self.assertEqual(s["total"]["calls"], 2)
        self.assertEqual(s["lines"][0]["key"], "gpt-6.1-terra")
        self.assertGreater(s["total"]["usd"], 0)
        self.assertEqual(s["quality"]["turns"], 1)
        self.assertEqual(s["quality"]["notFinished"], 0)
        self.assertEqual(ai_usage.summary(7, "day")["lines"][0]["calls"], 2)
        self.assertEqual(ai_usage.turns_log()[0]["tools"], ["get_data"])

    def test_turn_text_log_can_be_off(self):
        ai_usage.record_turn(func="chat", role="main", model="m", text="секрет", log_text=False)
        self.assertFalse(ai_usage.turns_path().exists())

    def test_claude_loop_logs_and_caches(self):
        app = make_app(provider="claude", preset_model="claude-sonnet-5")
        seen = []

        def claude(url, headers=None, json=None, timeout=None):
            seen.append(json)
            return R({"content": [{"type": "text", "text": "ок"}], "stop_reason": "end_turn",
                      "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 5}})
        jarvis.requests.post = claude
        jarvis._assistant_loop_claude([{"role": "user", "content": "привет"}], "k", "claude-sonnet-5", app, "", False, cache=True, compact=True)
        self.assertIsInstance(seen[0]["system"], list)
        self.assertEqual(seen[0]["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(seen[0]["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(ai_usage.summary(1)["total"]["cached"], 900)


class PromptOrderTest(unittest.TestCase):
    def test_static_part_ignores_date_and_knowledge(self):
        a = {"telegramSkills": {}, "knowledgeNotes": [{"title": "Имя", "text": "Алекс"}]}
        b = {"telegramSkills": {}, "knowledgeNotes": [{"title": "Кот", "text": "Барсик"}]}
        sa, da = jarvis.build_assistant_system_prompt(a, False, compact=True, split=True)
        sb, db = jarvis.build_assistant_system_prompt(b, False, compact=True, split=True)
        self.assertEqual(sa, sb)
        self.assertNotEqual(da, db)
        self.assertIn("Алекс", da)
        self.assertNotIn("Алекс", sa)
        self.assertIn("СЕГОДНЯ", da)

    def test_default_call_is_unchanged_single_string(self):
        s = jarvis.build_assistant_system_prompt({"telegramSkills": {}}, False)
        self.assertIsInstance(s, str)
        self.assertLess(s.index("СЕГОДНЯ"), s.index("get_data(domain)"))     # как раньше: дата в начале

    def test_compact_is_shorter_and_keeps_rules(self):
        app = {"telegramSkills": {}}
        full = jarvis.build_assistant_system_prompt(app, False)
        comp = jarvis.build_assistant_system_prompt(app, False, compact=True)
        self.assertLess(len(comp), len(full) * 0.95)
        for must in ("confirmed:true", "ПОДТВЕРЖДЕНИЕ", "reminders_create", "plan_create", "СЕГОДНЯ", "get_data", "calendar_create_event"):
            self.assertIn(must, comp)

    def test_skill_off_stays_out_of_prompt_and_tools(self):
        on = {"telegramSkills": {"mail": True}}
        off = {"telegramSkills": {"mail": False}}
        self.assertIn("ПОЧТА MAIL.RU", jarvis.build_assistant_system_prompt(on, False, compact=True))
        self.assertNotIn("ПОЧТА MAIL.RU", jarvis.build_assistant_system_prompt(off, False, compact=True))
        names = {t["name"] for t in jarvis._assistant_tool_list(off, False)}
        self.assertFalse({n for n in names if n.startswith("mail_")})


class EscalationTest(Base):
    def app(self, **over):
        m = {"main": {"model": "gpt-6.1-terra", "effort": "low"}, "complex": {"model": "gpt-6.1-sol", "effort": "medium"}}
        m.update(over)
        return make_app(m)

    def run_loop(self, app, text, first_conf=None, max_steps=None):
        conf = jarvis._ai_conf(app)
        conf, esc, reason = jarvis._start_conf(app, conf, text)
        stats = {"esc": reason}
        out = jarvis._assistant_loop_openai([{"role": "user", "content": text}], conf.key, conf.model, app, "", False,
                                            effort=conf.effort, role=conf.role, esc=esc, turn=stats, max_steps=max_steps)
        return out, stats

    def test_two_tool_errors_switch_to_complex(self):
        self.script(oai_tools(("get_data", {"domain": "нет1"}), ("get_data", {"domain": "нет2"})), oai_text("разобрался"))
        (text, _), stats = self.run_loop(self.app(), "покажи данные")
        self.assertEqual(text, "разобрался")
        self.assertEqual(self.sent[0]["model"], "gpt-6.1-terra")
        self.assertEqual((self.sent[1]["model"], self.sent[1]["reasoning_effort"]), ("gpt-6.1-sol", "medium"))
        self.assertEqual(stats["esc"], "errors")
        self.assertEqual(stats["tool_errors"], 2)

    def test_out_of_steps_continues_on_complex(self):
        loop = oai_tools(("get_data", {"domain": "about"}))
        self.script(loop)
        (text, _), stats = self.run_loop(self.app(), "покажи", max_steps=1)    # лимит 1+4 шага → потом сложная
        self.assertIsNone(text)
        models = [b["model"] for b in self.sent]
        self.assertEqual(models[:5], ["gpt-6.1-terra"] * 5)
        self.assertEqual(set(models[5:]), {"gpt-6.1-sol"})
        self.assertEqual(stats["esc"], "steps")
        self.assertFalse(stats["ok"])

    def test_pre_escalation_by_text(self):
        self.script(oai_text("вот код"))
        (text, _), stats = self.run_loop(self.app(), "напиши python скрипт для разбора csv")
        self.assertEqual(self.sent[0]["model"], "gpt-6.1-sol")
        self.assertEqual(stats["esc"], "pre:text")
        self.sent.clear()
        self.script(oai_text("привет"))
        self.run_loop(self.app(), "привет, как дела")
        self.assertEqual(self.sent[0]["model"], "gpt-6.1-terra")

    def test_pre_escalation_by_size(self):
        self.script(oai_text("ок"))
        self.run_loop(self.app(), "слово " * 1500)
        self.assertEqual(self.sent[0]["model"], "gpt-6.1-sol")

    def test_switch_off_and_daily_limit(self):
        app = self.app(escalation=False)
        self.script(oai_text("ок"))
        self.run_loop(app, "напиши python скрипт")
        self.assertEqual(self.sent[0]["model"], "gpt-6.1-terra")
        self.sent.clear()
        app = self.app(escalationDailyLimit=1)
        ai_usage.record_turn(func="chat", role="complex", model="gpt-6.1-sol", escalated="pre:text", text="", log_text=False)
        self.script(oai_text("ок"))
        self.run_loop(app, "напиши python скрипт")
        self.assertEqual(self.sent[0]["model"], "gpt-6.1-terra")        # лимит на сутки выбран
        self.assertEqual(ai_usage.escalations_today(), 1)

    def test_disabled_roles_never_escalate(self):
        app = make_app({"enabled": False})
        self.assertIsNone(jarvis._esc_conf(app, jarvis._ai_conf(app)))

    def test_background_is_not_escalated(self):
        conf = jarvis._ai_conf(self.app(), "triage")
        c, esc, reason = jarvis._start_conf(self.app(), conf, "напиши python")
        self.assertEqual((c.role, reason), ("triage", ""))


class ToolGroupsTest(Base):
    def app(self, groups_on=True, skills=None):
        return make_app({"toolGroups": groups_on}, skills=skills)

    def test_every_tool_belongs_to_group(self):
        app = {"telegramSkills": {"organize": True, "danger": True}}
        tools = jarvis._assistant_tool_list(app, True)
        self.assertEqual(tool_groups.unassigned(tools), [])

    def test_pick(self):
        self.assertEqual(tool_groups.pick("какая погода завтра") & {"weather", "mail", "deck"}, {"weather"})
        self.assertIn("mail", tool_groups.pick("что нового в почте"))
        self.assertIn("tg_read", tool_groups.pick("что мне написали в чате"))
        self.assertIn("deck", tool_groups.pick("сделай презентацию"))
        self.assertTrue({"core", "plan"} <= tool_groups.pick("???"))
        self.assertIn("market", tool_groups.pick("где мой заказ на озоне"))

    def test_sticky_and_short_confirmation(self):
        tool_groups.remember(777, {"apple"})
        self.assertIn("apple", tool_groups.pick("да", chat_id=777))
        tool_groups.forget(777)
        self.assertNotIn("apple", tool_groups.pick("да", chat_id=777))

    def test_off_means_full_set(self):
        app = self.app(groups_on=False)
        self.script(oai_text("ок"))
        jarvis._assistant_loop_openai([{"role": "user", "content": "погода"}], "k", "m", app, "", False)
        full = {t["function"]["name"] for t in self.sent[0]["tools"]}
        self.assertEqual(full, {t["name"] for t in jarvis._assistant_tool_list(app, False)})

    def test_groups_cut_tools_and_prompt(self):
        app = self.app()
        groups = tool_groups.pick("какая погода завтра")
        self.script(oai_text("ок"))
        jarvis._assistant_loop_openai([{"role": "user", "content": "погода"}], "k", "m", app, "", False, groups=groups, compact=True, cache=True)
        names = {t["function"]["name"] for t in self.sent[0]["tools"]}
        self.assertIn("get_weather", names)
        self.assertIn("tools_more", names)
        self.assertIn("get_data", names)
        self.assertNotIn("presentation_build", names)
        self.assertNotIn("mail_search", names)
        full = len(json.dumps(self.sent[0]["tools"]))
        self.sent.clear()
        self.script(oai_text("ок"))
        jarvis._assistant_loop_openai([{"role": "user", "content": "погода"}], "k", "m", app, "", False)
        self.assertLess(full, len(json.dumps(self.sent[0]["tools"])) * 0.6)

    def test_tools_more_adds_group_within_turn(self):
        app = self.app(skills={"mail": True})
        self.script(oai_tools(("tools_more", {"groups": ["mail"]})), oai_text("нашёл"))
        stats = {}
        jarvis._assistant_loop_openai([{"role": "user", "content": "привет"}], "k", "m", app, "", False,
                                      groups=tool_groups.pick("привет"), turn=stats)
        first = {t["function"]["name"] for t in self.sent[0]["tools"]}
        second = {t["function"]["name"] for t in self.sent[1]["tools"]}
        self.assertNotIn("mail_search", first)
        self.assertIn("mail_search", second)
        self.assertEqual(stats["tools_more"], 1)
        self.assertIn("mail", stats["groups"])

    def test_disabled_skill_cannot_come_back_via_tools_more(self):
        app = self.app(skills={"mail": False})
        self.script(oai_tools(("tools_more", {"groups": ["mail"]})), oai_text("не вышло"))
        jarvis._assistant_loop_openai([{"role": "user", "content": "привет"}], "k", "m", app, "", False, groups=tool_groups.pick("привет"))
        self.assertNotIn("mail_search", {t["function"]["name"] for t in self.sent[1]["tools"]})
        tool_msg = [m for m in self.sent[1]["messages"] if m["role"] == "tool"][0]
        self.assertIn("error", json.loads(tool_msg["content"]))

    def test_out_of_set_tool_executes_and_group_is_added(self):
        app = self.app()
        self.script(oai_tools(("get_weather", {"city": "Москва"})), oai_text("тепло"))
        stats = {}
        jarvis._assistant_loop_openai([{"role": "user", "content": "привет"}], "k", "m", app, "", False,
                                      groups=tool_groups.pick("привет"), turn=stats)
        self.assertIn("get_weather", {t["function"]["name"] for t in self.sent[1]["tools"]})
        tool_msg = [m for m in self.sent[1]["messages"] if m["role"] == "tool"][0]
        self.assertNotEqual(json.loads(tool_msg["content"]).get("error"), "unknown_tool")

    def test_background_agents_ignore_groups(self):
        app = self.app()
        self.script(oai_text("ок"))
        allowed = {"get_data"}
        jarvis._assistant_loop_openai([{"role": "user", "content": "x"}], "k", "m", app, "", False, allowed=allowed,
                                      groups=tool_groups.pick("x"))
        self.assertEqual({t["function"]["name"] for t in self.sent[0]["tools"]}, allowed)


if __name__ == "__main__":
    unittest.main()
