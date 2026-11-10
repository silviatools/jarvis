"""Выбор профиля кабинета РЭУ: пустая группа не означает успешную загрузку."""

import unittest
from unittest.mock import patch

import jarvis


GROUP = "15.14д-мц01/25м"
PROFILES = '''
<input data-url="/lessons/?student_id=100">
<input data-url="/lessons/?student_id=200" checked>
'''


def page(group="", link="", day="16.11.2026"):
    block = (f'''<div class="lesson__block">
        <a class="fancybox" href="#lesson1"><span>Управление рисками<br>Лекция</span></a>
        <a class="lesson__external-link-lenta" href="{link}">Войти</a>
        </div>''' if link else "")
    return f'''{PROFILES}<h1>Расписание для группы: {group}</h1>
        <table class="table_lessons"><tr><td>{day}</td></tr>
        <tr><td><span class="pairnum">6 пара<br>17:20 - 18:50</span></td>
        <td>{block}</td></tr></table>'''


class StudentProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = patch.dict(jarvis._student_profile, {"group": "", "id": ""})
        self.profile.start()
        self.addCleanup(self.profile.stop)

    def test_blank_or_wrong_group_selects_matching_profile_for_all_weeks(self):
        for initial_group in ("", "Другая-группа"):
            with self.subTest(group=initial_group):
                jarvis._student_profile.update(group="", id="")

                def fetch(wn, sid=""):
                    if sid != "100":
                        return page(initial_group)
                    return page(GROUP, f"https://example.org/meeting/{wn}",
                                "16.11.2026" if wn == 0 else "23.11.2026")

                with patch.object(jarvis, "_student_week_page", side_effect=fetch) as request:
                    result = jarvis.student_fetch_links(GROUP)
                self.assertEqual(len(result["lessons"]), 2)
                self.assertEqual(jarvis._student_profile["id"], "100")
                self.assertEqual(request.call_args_list[-1].args, (1, "100"))
                lessons = [{"date": "2026-11-16", "start": "17:20", "subject": "Управление рисками"}]
                self.assertEqual(jarvis.attach_lesson_links(lessons, result["lessons"]), 1)
                self.assertEqual(lessons[0]["link"], "https://example.org/meeting/0")

    def test_no_matching_profile_reports_error_instead_of_empty_success(self):
        with patch.object(jarvis, "_student_week_page", return_value=page()):
            with self.assertRaises(jarvis.StudentError):
                jarvis.student_fetch_links(GROUP)
        self.assertEqual(jarvis._student_profile["group"], "")

    def test_correct_profile_does_not_need_selection(self):
        with patch.object(jarvis, "_student_week_page", return_value=page(GROUP)) as request:
            result = jarvis.student_fetch_links(GROUP, weeks=1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["days"], ["2026-11-16"])

    def test_failed_selection_reaches_links_error_without_trusting_empty_days(self):
        fetched = {"lessons": [], "weeks": []}
        with patch.object(jarvis, "rea_fetch_weeks", return_value=fetched), \
             patch.object(jarvis, "student_status", return_value={"configured": True}), \
             patch.object(jarvis, "_student_week_page", return_value=page()):
            result = jarvis.study_fetch(GROUP)
        self.assertTrue(result["linksError"])
        self.assertEqual(result["linksDays"], [])


if __name__ == "__main__":
    unittest.main()
