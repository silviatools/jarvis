import base64
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import market_pc
import moodle_skill as m

SHOT = {'ok': True, 'image': base64.b64encode(b'\xff\xd8fixture').decode(), 'width': 800, 'height': 600, 'quiz': 'test-1'}


class MoodleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, DATA_DIR=self.tmp.name)
        self.env.start()
        m._run = m._pending = None
        m._live.clear()
        self.token = market_pc.pair()['token']
        self.session = 'local-session-123456'
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.env.stop)
        self.addCleanup(m.stop)

    def poll(self, **kw):
        return m.device_call('/api/moodle/pc/poll', self.token, {'session': self.session, **kw})

    def reply(self, job, result):
        return m.device_call('/api/moodle/pc/result', self.token, {'session': self.session, 'id': job['id'], 'result': result})

    def job(self):
        for _ in range(200):
            job = self.poll()[1]['job']
            if job:
                return job
            time.sleep(.005)
        self.fail('no job')

    def wait_finished(self):
        for _ in range(200):
            if m._run['status'] != 'running':
                return
            time.sleep(.005)
        self.fail('worker not finished')

    def start(self, actions, all_tests=True):
        self.poll()
        iterator = iter(actions)
        return m.execute('moodle_start', {'course': 'Облака', 'all_tests': all_tests}, {'moodle': True},
                         lambda *_: next(iterator), lambda: True)

    def test_full_course_report_and_no_replay(self):
        result = self.start([{'action': 'record', 'grade': '8/10', 'title': 'Первый'},
                             {'action': 'record', 'grade': None, 'note': 'Ожидает проверки'}])
        self.assertTrue(result['started'])
        first = self.job()
        self.assertEqual(first['action']['action'], 'snapshot')
        self.assertIsNone(self.poll()[1]['job'])
        self.assertEqual(self.reply(first, SHOT)[0], 200)
        next_job = self.job()
        self.assertEqual(next_job['action']['action'], 'next')
        self.assertEqual(self.reply(first, SHOT)[0], 409)
        self.reply(next_job, {**SHOT, 'quiz': 'test-2'})
        self.reply(self.job(), {'ok': True, 'done': True})
        self.wait_finished()
        report = m.status()['reports'][0]
        self.assertEqual(report['status'], 'completed')
        self.assertEqual([r['grade'] for r in report['results']], ['8/10', None])
        saved = m._path().read_text()
        self.assertNotIn(SHOT['image'], saved)
        self.assertNotIn(self.token, saved)

    def test_single_test_does_not_queue_next(self):
        self.start([{'action': 'record', 'grade': '5'}], all_tests=False)
        self.reply(self.job(), SHOT)
        self.wait_finished()
        self.assertEqual(m._run['status'], 'completed')
        self.assertIsNone(self.poll()[1]['job'])

    def test_cancel_during_model_call_cannot_act(self):
        entered, release = threading.Event(), threading.Event()
        def decide(*_):
            entered.set()
            release.wait(2)
            return {'action': 'click', 'x': 10, 'y': 10}
        self.poll()
        m.execute('moodle_start', {'course': 'Облака'}, {'moodle': True}, decide, lambda: True)
        self.reply(self.job(), SHOT)
        self.assertTrue(entered.wait(1))
        m.stop()
        release.set()
        self.assertEqual(m._run['status'], 'stopped')
        self.assertIsNone(self.poll()[1]['job'])

    def test_auth_bad_screenshot_and_session_change(self):
        self.assertEqual(m.device_call('/api/moodle/pc/poll', 'bad', {'session': self.session})[0], 401)
        self.start([{'action': 'wait'}])
        job = self.job()
        self.assertEqual(self.reply(job, {**SHOT, 'image': 'not-base64'})[0], 400)
        self.session += '-new'
        self.poll()
        self.assertEqual(m._run['status'], 'stopped')
        self.assertEqual(self.reply(job, SHOT)[0], 409)

    def test_disabled_and_invalid_actions(self):
        self.assertFalse(m.execute('moodle_start', {'course': 'x'}, {}, None, None)['ok'])
        for raw in ({'action': 'eval', 'text': 'evil'}, {'action': 'click', 'x': -1, 'y': 2}, {'action': 'scroll', 'delta': 99999}):
            with self.assertRaises(ValueError):
                m.validate_action(raw, SHOT)

    def test_restart_reports_interruption(self):
        m._path().write_text(json.dumps([{'id': 'old', 'status': 'running'}]))
        self.assertEqual(m.status()['reports'][0]['status'], 'interrupted')


class MoodleIntegrationTests(unittest.TestCase):
    def test_catalog_toggle_group_and_executor(self):
        import jarvis
        import tool_groups
        off, on = {'telegramSkills': {'moodle': False}}, {'telegramSkills': {'moodle': True}}
        self.assertIn('moodle', {s['id'] for s in jarvis.assistant_skills_catalog()})
        self.assertFalse(m.TOOL_NAMES & {t['name'] for t in jarvis._assistant_tool_list(off, False)})
        self.assertTrue(m.TOOL_NAMES <= {t['name'] for t in jarvis._assistant_tool_list(on, False)})
        self.assertEqual(tool_groups.group_of('moodle_start'), 'study')
        self.assertEqual(m.prompt_section(off['telegramSkills']), '')
        self.assertFalse(jarvis.execute_assistant_tool('moodle_start', {'course': 'x'}, off, '')['ok'])

    def test_vision_adapter_passes_image_to_both_providers(self):
        import jarvis
        shot = SHOT.copy()
        with patch.object(jarvis, 'load_app_data', return_value={}), patch.object(jarvis, '_ai_conf', return_value=('openai', 'key', 'gpt-test', '', '', 'main')), patch.object(jarvis, '_openai_step', return_value={'content': '{"action":"wait"}', 'usage': {}}) as api, patch.object(jarvis, '_log_call'):
            self.assertEqual(jarvis._moodle_decide('system', {}, shot), {'action': 'wait'})
            self.assertIn(shot['image'], api.call_args.args[3][1]['content'][1]['image_url']['url'])
        from unittest.mock import Mock
        response = Mock(ok=True)
        response.json.return_value = {'content': [{'type': 'text', 'text': '{"action":"wait"}'}], 'usage': {}}
        with patch.object(jarvis, 'load_app_data', return_value={}), patch.object(jarvis, '_ai_conf', return_value=('claude', 'key', 'claude-test', '', '', 'main')), patch.object(jarvis.requests, 'post', return_value=response) as api, patch.object(jarvis, '_log_call'):
            self.assertEqual(jarvis._moodle_decide('system', {}, shot), {'action': 'wait'})
            self.assertEqual(api.call_args.kwargs['json']['messages'][0]['content'][1]['source']['data'], shot['image'])

if __name__ == '__main__':
    unittest.main()
