"""Медицинские документы из фото → «Здоровье»: подмена Telegram и ИИ, только выдуманные данные."""

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from unittest.mock import patch

_TMP = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _TMP.name
if "pyaes" not in sys.modules:
    try:
        import pyaes  # noqa: F401
    except ImportError:
        sys.modules["pyaes"] = types.ModuleType("pyaes")

import jarvis        # noqa: E402
import health_skill  # noqa: E402
import plan_skill    # noqa: E402
import tool_groups   # noqa: E402

CHAT = 4242
TODAY = health_skill.now_msk().date()
D0 = TODAY.isoformat()
VISIT_DUE = (TODAY + timedelta(days=5)).isoformat()
SECRET_DX = "Синдром вымышленной диспепсии"

TRANSCRIPT = f"""=== Фото 1 ===
Клиника «Северный ветер». Дата: {TODAY.strftime('%d.%m.%Y')} 09:30
Прием врача-гастроэнтеролога первичный T05.001
ДИАГНОЗ: {SECRET_DX} (K30)
Обследования: T01.001 Общий анализ крови; T02.002 УЗИ органов брюшной полости
[рукописно: + дыхательный тест]
Повторная явка {(TODAY + timedelta(days=5)).strftime('%d.%m.%Y')}
Тестомицин 100 мг по 1 таб 2 раза в день 2 недели, затем по 1/2 таб 1 месяц
Врач-гастроэнтеролог Иванова Анна Петровна
=== Фото 2 ===
Протокол УЗИ. Услуга T02.002. Дата {TODAY.strftime('%d.%m.%Y')} 10:05
Печень: косой вертикальный 140 мм (норма до 150 мм)
Желчный пузырь: стенка 4 мм (норма до 3 мм)
Заключение: признаки вымышленного уплотнения стенки.
Врач УЗД Петров Пётр Петрович"""


def visit_doc(**kw):
    d = {"photos": [1], "kind": "visit", "title": "Приём гастроэнтеролога", "date": D0, "time": "09:30",
         "clinic": "Клиника «Северный ветер»", "doctor": "Иванова Анна Петровна", "specialty": "гастроэнтеролог",
         "complaints": "тяжесть после еды", "diagnosis": SECRET_DX, "icd": ["K30"],
         "orders": [{"title": "Общий анализ крови", "kind": "lab", "code": "T01.001", "group": "Клинический анализ крови"},
                    {"title": "УЗИ органов брюшной полости", "kind": "study", "code": "T02.002"},
                    {"title": "Дыхательный тест", "kind": "lab", "handwritten": True},
                    {"title": "Повторный приём гастроэнтеролога", "kind": "visit", "due_date": VISIT_DUE}],
         "medications": [{"name": "Тестомицин", "dosage": "100 мг", "route": "внутрь",
                          "stages": [{"dosage": "1 таб", "schedule": "2 раза в день", "duration": "2 недели"},
                                     {"dosage": "1/2 таб", "schedule": "1 раз в день", "duration": "1 месяц"}]}],
         "uncertain": [{"field": "orders", "text": "+ дыхательный тест", "reason": "рукописно"}]}
    d.update(kw)
    return d


def study_doc():
    return {"photos": [2], "kind": "study", "title": "УЗИ брюшной полости", "date": D0, "time": "10:05",
            "clinic": "Клиника «Северный ветер»", "doctor": "Петров Пётр Петрович", "study_kind": "УЗИ органов брюшной полости",
            "service_code": "T02.002",
            "metrics": [{"group": "Печень", "name": "Косой вертикальный размер", "value": "140", "unit": "мм", "ref": "до 150 мм"},
                        {"group": "Желчный пузырь", "name": "Толщина стенки", "value": "4", "unit": "мм", "ref": "до 3 мм"}],
            "conclusion": "Признаки вымышленного уплотнения стенки."}


def structure(docs, questions=None, episode_title="Обследование ЖКТ"):
    return {"documents": docs, "episode": {"title": episode_title}, "questions": questions or [], "missing": []}


class R:
    def __init__(self, j):
        self.ok, self._j, self.status_code = True, j, 200

    def json(self):
        return self._j


def img(tag: bytes):
    return {"media_type": "image/jpeg", "b64": base64.b64encode(b"\xff\xd8fake-" + tag).decode()}


class HealthFlowTest(unittest.TestCase):
    def setUp(self):
        for p in (jarvis.APP_DATA_FILE, health_skill.store_path(), plan_skill.store_path()):
            if p.exists():
                p.unlink()
        self.app = {"telegramSkills": {}, "settings": {
            "aiPresets": [{"id": "a", "name": "n", "provider": "claude", "apiKey": "k", "model": ""}],
            "aiAssignments": {"voiceAssistant": "a"}}}
        jarvis.save_app_data(self.app)
        self.tg, self.ai, self.main = [], [], []
        self.struct = structure([visit_doc(), study_doc()])
        self.mid = 100
        jarvis.tg_bot.reset(CHAT)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(jarvis.tg_bot, "api", side_effect=self.fake_tg))
        stack.enter_context(patch.object(jarvis.requests, "post", side_effect=self.fake_ai))
        stack.enter_context(patch.object(jarvis, "get_token", return_value="TOKEN"))
        stack.enter_context(patch.object(jarvis.tg_user, "is_owner_chat", return_value=False))
        self.out = io.StringIO()
        stack.enter_context(contextlib.redirect_stdout(self.out))

    # ── подмены ──
    def fake_tg(self, token, method, payload=None, files=None, timeout=30):
        self.tg.append((method, payload or {}))
        if method == "sendMessage":
            self.mid += 1
            return {"message_id": self.mid}
        return True

    def fake_ai(self, url, headers=None, json=None, timeout=None, **kw):
        body = json or {}
        sysp = body.get("system") or ""
        if isinstance(sysp, list):
            sysp = "\n".join(b.get("text", "") for b in sysp)
        if body.get("tools") is not None:
            self.ai.append(("main", body, sysp))
            step = self.main.pop(0) if self.main else "Готово."
            step = step() if callable(step) else step
            if isinstance(step, dict):
                return R({"content": [{"type": "tool_use", "id": f"t{len(self.ai)}", **step}], "stop_reason": "tool_use"})
            return R({"content": [{"type": "text", "text": step}], "stop_reason": "end_turn"})
        if sysp.startswith("Ты расшифровываешь"):
            self.ai.append(("transcribe", body, sysp))
            return R({"content": [{"type": "text", "text": TRANSCRIPT}]})
        if sysp.startswith("Ты раскладываешь"):
            self.ai.append(("structure", body, sysp))
            return R({"content": [{"type": "text", "text": "```json\n" + jarvis.json.dumps(self.struct, ensure_ascii=False) + "\n```"}]})
        return R({"content": [{"type": "text", "text": "Здоровье"}]})

    # ── помощники ──
    def send_album(self, images, caption="внеси в здоровье"):
        msgs = [{"message_id": 10 + i, "chat": {"id": CHAT}, "date": 100, "media_group_id": "alb",
                 "caption": caption if i == 0 else ""} for i in range(len(images))]
        outcomes = [((caption if i == 0 else ""), [im], None) for i, im in enumerate(images)]
        with patch.object(jarvis.tg_bot, "extract_attachments", side_effect=outcomes):
            jarvis._dispatch_user_message("TOKEN", CHAT, msgs)

    def say(self, text):
        jarvis.handle_assistant_message("TOKEN", CHAT, text, [], 50)

    def last_markup(self):
        for method, p in reversed(self.tg):
            if p.get("reply_markup") and p["reply_markup"].get("inline_keyboard"):
                return p
        self.fail("нет клавиатуры")

    def press(self, label_part):
        p = self.last_markup()
        btn = next((b for row in p["reply_markup"]["inline_keyboard"] for b in row if label_part in b["text"]), None)
        self.assertIsNotNone(btn, f"нет кнопки «{label_part}» в {p['reply_markup']}")
        health_skill.handle_callback("TOKEN", {"id": "cb", "data": btn["callback_data"],
                                               "message": {"message_id": p.get("message_id", self.mid), "chat": {"id": CHAT}}})

    def texts(self):
        return [p.get("text", "") for m, p in self.tg if m in ("sendMessage", "editMessageText")]

    def card(self):
        return next(t for t in reversed(self.texts()) if t.startswith(("🩺", "✅", "🔔")))

    def data(self):
        return jarvis.load_app_data()

    # ── сценарии ──
    def test_album_one_analysis_episode_confirm_reminders_and_no_duplicates(self):
        self.main = [{"name": "health_import", "input": {}}, "Разобрал, проверьте сводку."]
        self.send_album([img(b"visit"), img(b"uzi")])
        self.assertEqual([k for k, *_ in self.ai].count("transcribe"), 1)          # два фото — один разбор
        tr = next(b for k, b, _ in self.ai if k == "transcribe")
        self.assertEqual(sum(1 for c in tr["messages"][0]["content"] if c["type"] == "image"), 2)
        self.assertIn("health_import", {t["name"] for t in self.ai[0][1]["tools"]})
        self.assertTrue(any("Связать документы в эпизод" in t for t in self.texts()))
        self.assertTrue(any("Распознаю документы (2 фото)" in t for t in self.texts()))
        self.assertEqual(self.data().get("healthEvents") or [], [])                # без подтверждения — ничего
        self.press("Новый")
        summary = self.card()
        for s in ("Приём гастроэнтеролога", "УЗИ брюшной полости", "вне нормы: 1", "закроет назначение",
                  "создам «Обследование ЖКТ»", "рукописно", "Результатов назначенного пока нет"):
            self.assertIn(s, summary)
        self.press("Записать")
        app = self.data()
        visit = next(e for e in app["healthEvents"] if e["type"] == "visit")
        study = next(e for e in app["healthEvents"] if e["type"] == "study")
        cond = app["healthConditions"][0]
        self.assertEqual((cond["name"], cond["kind"], cond["status"], cond["diagnosedDate"]), ("Обследование ЖКТ", "episode", "active", D0))
        self.assertEqual({visit["conditionId"], study["conditionId"]}, {cond["id"]})
        self.assertEqual((visit["icd"], visit["time"], visit["specialty"]), (["K30"], "09:30", "гастроэнтеролог"))
        uzi = next(o for o in visit["orders"] if o["code"] == "T02.002")
        self.assertEqual((uzi["status"], uzi["resultEventId"]), ("done", study["id"]))
        self.assertEqual(next(o for o in visit["orders"] if o["kind"] == "visit")["dueDate"], VISIT_DUE)
        self.assertTrue(next(o for o in visit["orders"] if o["title"] == "Дыхательный тест")["handwritten"])
        med = visit["medications"][0]
        self.assertEqual((len(med["stages"]), med["route"], med["startDate"]), (2, "внутрь", D0))
        flags = {m["name"]: m["flag"] for m in study["metrics"]}
        self.assertEqual(flags, {"Косой вертикальный размер": "", "Толщина стенки": "high"})
        self.assertEqual(study["metrics"][0]["ref"], "до 150 мм")
        self.assertEqual(len(visit["photos"]), 1)
        self.assertTrue((jarvis.DATA_DIR / "photos" / visit["photos"][0]["filename"]).exists())
        self.assertIn("Повторная явка", visit["source"]["text"])
        self.assertTrue(any("Записал в «Здоровье»" in t for t in self.texts()))
        # напоминания — только предложение
        self.assertEqual(plan_skill.list_tasks(), [])
        self.assertIn("Поставить напоминания", self.card())
        self.press("Явка")
        tasks = plan_skill.list_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertTrue(tasks[0]["run_at"].startswith((TODAY + timedelta(days=4)).isoformat()))
        # повторное нажатие «Записать» не дублирует
        did = health_skill.pending_for(CHAT)
        self.assertIsNone(did)
        draft_id = next(iter(health_skill._load()))
        self.assertTrue(health_skill.commit(draft_id).get("already"))
        # те же фото ещё раз — уже внесено
        self.main = [{"name": "health_import", "input": {}}, "Это уже есть."]
        self.send_album([img(b"visit"), img(b"uzi")])
        self.assertIn("уже внесено", self.card())
        self.assertFalse(any("Записать" in b["text"] for b in sum(self.last_markup()["reply_markup"]["inline_keyboard"], [])))
        # новое фото того же визита и протокола — тоже без дублей
        self.main = [{"name": "health_import", "input": {}}, "Это уже есть."]
        self.send_album([img(b"visit-again"), img(b"uzi-again")])
        self.assertIn("уже внесено", self.card())
        self.assertEqual(len(self.data()["healthEvents"]), 2)
        self.assertEqual(len(self.data()["healthConditions"]), 1)
        # медицинский текст не попал в журналы и общий лог
        for name in ("jarvis_ai_usage.jsonl", "jarvis_ai_turns.jsonl"):
            p = jarvis.DATA_DIR / name
            if p.exists():
                self.assertNotIn("диспепс", p.read_text(encoding="utf-8").lower())
                self.assertNotIn("внеси в здоровье", p.read_text(encoding="utf-8"))
        self.assertNotIn("диспепс", self.out.getvalue().lower())

    def test_clarify_then_text_answer_then_episode_name_then_write(self):
        self.struct = structure([visit_doc(medications=[{"name": "Тестомицин", "dosage": "", "schedule": "2 раза в день"}])],
                                questions=[{"doc": 1, "field": "medications.1.dosage", "text": "Доза Тестомицина неразборчива: 10 или 100 мг?",
                                            "options": ["10 мг", "100 мг"]}])
        self.main = [{"name": "health_import", "input": {}}, "Есть вопрос — ответьте в карточке."]
        self.send_album([img(b"one")])
        self.assertIn("вопрос 1 из 2", self.card())
        self.assertIn("10 или 100 мг", self.card())
        draft = health_skill.pending_for(CHAT)
        self.main = [lambda: {"name": "health_import_answer", "input": {"draft_id": draft["id"], "question_id": "q1", "answer": "100 мг"}},
                     "Принял."]
        self.say("там 100 мг")
        main_sys = [s for k, b, s in self.ai if k == "main"][-1]
        self.assertIn(f"draft_id={draft['id']}", main_sys)                       # ассистент знает, что ждёт ответа
        self.assertIn("вопрос 2 из 2", self.card())
        self.main = [lambda: {"name": "health_import_answer", "input": {"draft_id": draft["id"], "episode_title": "Пищевое отравление"}},
                     "Готово."]
        self.say("назови эпизод «Пищевое отравление»")
        self.assertIn("создам «Пищевое отравление»", self.card())
        self.press("Записать")
        app = self.data()
        self.assertEqual(app["healthEvents"][0]["medications"][0]["dosage"], "100 мг")
        self.assertEqual(app["healthConditions"][0]["name"], "Пищевое отравление")
        self.assertNotIn("там 100 мг", (jarvis.DATA_DIR / "jarvis_ai_turns.jsonl").read_text(encoding="utf-8")
                         if (jarvis.DATA_DIR / "jarvis_ai_turns.jsonl").exists() else "")

    def test_open_episode_is_suggested_and_old_order_closed(self):
        cond = {"id": "ep1", "name": "Пищевое отравление", "kind": "episode", "isChronic": False, "status": "active",
                "diagnosedDate": (TODAY - timedelta(days=3)).isoformat()}
        old = {"id": "v1", "type": "visit", "date": (TODAY - timedelta(days=3)).isoformat(), "title": "Приём терапевта",
               "conditionId": "ep1", "orders": [{"id": "o1", "title": "Общий анализ крови", "kind": "lab", "code": "T01.001",
                                                 "status": "assigned"}]}
        self.app.update(healthConditions=[cond], healthEvents=[old])
        jarvis.save_app_data(self.app)
        self.struct = structure([{"photos": [1], "kind": "labs", "title": "Общий анализ крови", "date": D0, "service_code": "T01.001",
                                  "metrics": [{"group": "ОАК", "name": "Гемоглобин", "value": "150", "unit": "г/л", "ref": "130-170"}]}])
        self.main = [{"name": "health_import", "input": {}}, "Ок."]
        self.send_album([img(b"labs")])
        self.assertIn("Похоже на продолжение эпизода «Пищевое отравление»", self.card())
        self.assertEqual(health_skill._load()[health_skill.pending_for(CHAT)["id"]]["episode"]["mode"], None)
        self.press("К «Пищевое")
        self.assertIn("привяжу к «Пищевое отравление»", self.card())
        self.press("Записать")
        app = self.data()
        labs = next(e for e in app["healthEvents"] if e["type"] == "labs")
        self.assertEqual(labs["conditionId"], "ep1")
        self.assertEqual(len(app["healthConditions"]), 1)
        o1 = app["healthEvents"][0]["orders"][0]
        self.assertEqual((o1["status"], o1["resultEventId"]), ("done", labs["id"]))

    def test_no_episode_created_without_confirmation_and_cancel(self):
        self.main = [{"name": "health_import", "input": {}}, "Ок."]
        self.send_album([img(b"x")])
        files = [p["file"] for p in health_skill.pending_for(CHAT)["photos"]]
        self.press("Отмена")
        app = self.data()
        self.assertEqual(app.get("healthConditions") or [], [])
        self.assertEqual(app.get("healthEvents") or [], [])
        self.assertFalse(any((jarvis.DATA_DIR / "photos" / f).exists() for f in files))
        self.assertIsNone(health_skill.pending_for(CHAT))
        self.assertEqual(jarvis.execute_assistant_tool("health_import_commit", {"draft_id": "nope"}, app, "")["needs_confirmation"], True)

    def test_skill_off_hides_tools_prompt_and_execution(self):
        off = {"telegramSkills": {"health_docs": False}}
        self.assertIn("health_docs", [s["id"] for s in jarvis.assistant_skills_catalog()])
        names = {t["name"] for t in jarvis._assistant_tool_list(off, False)}
        self.assertFalse(names & health_skill.TOOL_NAMES)
        self.assertTrue(health_skill.TOOL_NAMES <= {t["name"] for t in jarvis._assistant_tool_list({}, False)})
        self.assertNotIn("ЗДОРОВЬЕ ИЗ ФОТО", jarvis.build_assistant_system_prompt(off, False))
        self.assertIn("ЗДОРОВЬЕ ИЗ ФОТО", jarvis.build_assistant_system_prompt({}, False))
        self.assertIn("выключен", jarvis.execute_assistant_tool("health_import", {}, off, "")["error"])
        self.app["telegramSkills"] = {"health_docs": False}
        jarvis.save_app_data(self.app)
        self.main = ["Не могу."]
        self.send_album([img(b"off")])
        self.assertFalse({t["name"] for t in self.ai[0][1]["tools"]} & health_skill.TOOL_NAMES)
        self.assertFalse(any(k == "transcribe" for k, *_ in self.ai))

    def test_tool_groups_and_roles(self):
        self.assertEqual(tool_groups.unassigned(health_skill.TOOLS), [])
        self.assertIn("health", tool_groups.pick("внеси в здоровье"))
        self.assertIn("health", tool_groups.pick("вот протокол УЗИ"))
        self.assertIn("healthExtract", jarvis.ai_usage.TASK_ROLES)
        r = jarvis.ai_usage.resolve_role({"aiModels": {"healthExtract": {"model": "gpt-6.1-sol", "effort": "high"}}}, "healthExtract")
        self.assertEqual((r["model"], r["effort"]), ("gpt-6.1-sol", "high"))
        self.assertEqual(jarvis.ai_usage.resolve_role({}, "healthExtract")["model"], jarvis.ai_usage.resolve_role({}, "main")["model"])


class RefFlagTest(unittest.TestCase):
    def test_flags(self):
        f = health_skill.ref_flag
        self.assertEqual(f("140", "до 150 мм"), "")
        self.assertEqual(f("160", "до 150"), "high")
        self.assertEqual(f("29", "до 30-35 мм"), "")
        self.assertEqual(f("5", "7-28"), "low")
        self.assertEqual(f("5,5", "3,9–6,1"), "")
        self.assertEqual(f("140", "до 150 мм у женщин, до 160 мм у мужчин"), "")
        self.assertEqual(f("отрицательно", "отрицательно"), "")
        self.assertEqual(f("2", "> 3"), "low")

    def test_normalize_keeps_stages_and_marks_handwritten(self):
        d = health_skill.normalize_doc(visit_doc(uncertain=[]), 1)
        self.assertEqual(len(d["medications"][0]["stages"]), 2)
        self.assertEqual(d["medications"][0]["dosage"], "100 мг")
        self.assertTrue(any(u["reason"] == "рукописно" for u in d["uncertain"]))
        self.assertEqual(health_skill._days("2 недели"), 14)
        self.assertEqual(health_skill._days("3 месяца"), 90)


if __name__ == "__main__":
    unittest.main()
