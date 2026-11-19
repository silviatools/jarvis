import base64
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import market_pc
import browse_skill as b

SHOT = {'ok': True, 'image': base64.b64encode(b'\xff\xd8fixture').decode(), 'width': 800, 'height': 600,
        'url': 'https://kontur.ru/', 'title': 'Контур'}


class BrowseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, DATA_DIR=self.tmp.name)
        self.env.start()
        b._run = b._pending = None
        b._live.clear()
        self.token = market_pc.pair()['token']
        self.session = 'browse-session-123456'
        self.notes = []
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.env.stop)
        self.addCleanup(b.stop)

    def poll(self, **kw):
        payload = {'session': self.session, 'capabilities': ['browse']}
        payload.update(kw)
        return b.device_call('/api/browse/pc/poll', self.token, payload)

    def reply(self, job, result):
        return b.device_call('/api/browse/pc/result', self.token, {'session': self.session, 'id': job['id'], 'result': result})

    def job(self):
        for _ in range(400):
            found = self.poll()[1]['job']
            if found:
                return found
            time.sleep(.005)
        self.fail('no job')

    def wait_finished(self):
        for _ in range(400):
            if b._run['status'] not in ('running', 'waiting'):
                return
            time.sleep(.005)
        self.fail('worker not finished')

    def wait_question(self):
        for _ in range(400):
            question = (b._run or {}).get('question') or {}
            if question.get('message_id'):
                return question
            time.sleep(.005)
        self.fail('no question')

    def start(self, actions, task='Зайди на https://kontur.ru/ и найди цены', url='https://kontur.ru/', notify=None, chat_id=7):
        self.poll()
        decide = actions if callable(actions) else (lambda *_a, it=iter(actions): next(it))

        def default_notify(chat, text):
            self.notes.append((chat, text))
            return 77

        return b.execute('browse_start', {'task': task, 'url': url} if url is not None else {'task': task},
                         {'web_browse': True}, decide, lambda: True, notify or default_notify, chat_id)

    def test_done_report_omits_screenshot_typed_secret_and_query_token(self):
        seen = {}

        def decide(_system, context, _shot):
            seen.setdefault('url', context['page']['url'])
            if not seen.get('typed'):
                seen['typed'] = True
                return {'action': 'type', 'text': 'p@ss-secret', 'seen': 'форма входа'}
            return {'action': 'done', 'answer': 'Цены на странице от 1000 ₽.'}

        result = self.start(decide)
        self.assertTrue(result['started'])
        self.assertTrue(result['background'])
        self.assertIn('фон', b.background_note(7).lower())
        self.assertEqual(b.background_note(8), '')
        opened = self.job()
        self.assertEqual(opened['action'], {'action': 'open', 'url': 'https://kontur.ru/'})
        self.assertIsNone(self.poll()[1]['job'])
        self.reply(opened, {**SHOT, 'url': 'https://kontur.ru/price?token=SECRET&q=1'})
        typed = self.job()
        self.assertEqual(typed['action']['text'], 'p@ss-secret')
        self.assertEqual(self.reply(typed, SHOT)[0], 200)
        self.assertEqual(self.reply(typed, SHOT)[0], 409)
        self.wait_finished()
        self.assertEqual(b._run['status'], 'completed')
        self.assertEqual(seen['url'], 'https://kontur.ru/price?q=1')
        saved = b._path().read_text()
        self.assertIn('1000', saved)
        self.assertNotIn('p@ss-secret', saved)
        self.assertNotIn('SECRET', saved)
        self.assertNotIn(SHOT['image'], saved)
        self.assertNotIn(self.token, saved)
        self.assertTrue(any('1000' in text for _chat, text in self.notes))

    def test_telegram_reply_continues_and_is_not_stored(self):
        result = self.start([{'action': 'ask', 'question': 'Какой тариф смотреть?'},
                             {'action': 'done', 'answer': 'Стартовый тариф 1000 ₽.'}])
        self.assertTrue(result['started'])
        self.reply(self.job(), SHOT)
        question = self.wait_question()
        self.assertIn('Какой тариф', self.notes[0][1])
        self.assertIn('как обычно', self.notes[0][1])
        waiting = b.background_note(7)
        self.assertIn('browse_status', waiting)
        self.assertIn('явно отвечает', waiting)
        self.assertIn('Какой тариф', waiting)
        self.assertIsNone(b.claim_reply(7, 'Смотри стартовый', None))
        self.assertIsNone(b.claim_reply(8, 'Смотри стартовый', question['message_id']))
        self.assertIn('Принял', b.claim_reply(7, 'Смотри стартовый', question['message_id']))
        self.reply(self.job(), SHOT)
        self.wait_finished()
        self.assertEqual(b._run['status'], 'completed')
        self.assertNotIn('Смотри стартовый', b._path().read_text())
        self.assertTrue(b.pending_note(7) == '')
        self.assertIn('уже закончилось', b.background_note(7))

    def test_reply_tool_continues(self):
        self.start([{'action': 'ask', 'question': 'Продолжать?'},
                    {'action': 'done', 'answer': 'Стартовый тариф 1000 ₽.'}])
        self.reply(self.job(), SHOT)
        self.wait_question()
        accepted = b.execute('browse_reply', {'text': 'да, стартовый'}, {'web_browse': True}, None, lambda: True)
        self.assertTrue(accepted['accepted'])
        self.reply(self.job(), SHOT)
        self.wait_finished()
        self.assertEqual(b._run['status'], 'completed')
        self.assertNotIn('да, стартовый', b._path().read_text())

    def test_stop_word_on_the_question_stops_without_another_action(self):
        self.start([{'action': 'ask', 'question': 'Какой пароль?'}])
        self.reply(self.job(), SHOT)
        question = self.wait_question()
        self.assertIn('Остановил', b.claim_reply('7', 'Стоп', question['message_id']))
        self.wait_finished()
        self.assertEqual(b._run['status'], 'stopped')
        self.assertNotIn('пароль', b._path().read_text().lower())
        self.assertTrue(all('По сайту' not in text for _chat, text in self.notes[1:]))

    def test_step_limit_reports_what_was_seen(self):
        self.start(lambda *_a: {'action': 'click', 'x': 10, 'y': 10, 'seen': 'блок цен'})
        self.reply(self.job(), SHOT)
        for _ in range(b.MAX_STEPS):
            self.reply(self.job(), SHOT)
        self.wait_finished()
        self.assertEqual(b._run['status'], 'interrupted')
        self.assertIn('40', b._run['message'])
        self.assertIn('блок цен', b._run['message'])

    def test_retryable_tab_error_is_returned_to_the_model(self):
        self.start([{'action': 'click', 'x': 4, 'y': 5}, {'action': 'done', 'answer': 'Цены от 500 ₽.'}])
        self.reply(self.job(), SHOT)
        failed = self.job()
        self.assertEqual(failed['action']['action'], 'click')
        self.reply(failed, {'ok': False, 'error': 'Сначала кликните в поле.', 'retry': True})
        self.wait_finished()
        self.assertEqual(b._run['status'], 'completed')
        self.assertEqual(b._run['steps'], 0)

    def test_three_retries_stop(self):
        self.start(lambda *_a: {'action': 'click', 'x': 4, 'y': 5})
        self.reply(self.job(), SHOT)
        for _ in range(3):
            self.reply(self.job(), {'ok': False, 'error': 'мимо', 'retry': True})
        self.wait_finished()
        self.assertEqual(b._run['status'], 'interrupted')
        self.assertIn('мимо', b._run['message'])

    def test_bad_screenshot_auth_and_session_change(self):
        self.assertEqual(b.device_call('/api/browse/pc/poll', 'bad', {'session': self.session})[0], 401)
        self.start([{'action': 'wait'}])
        job = self.job()
        self.assertEqual(self.reply(job, {**SHOT, 'image': 'not-base64'})[0], 400)
        self.session = 'other-session-654321'
        self.poll()
        self.wait_finished()
        self.assertEqual(b._run['status'], 'interrupted')
        self.assertEqual(self.reply(job, SHOT)[0], 409)

    def test_validation_and_address_rules(self):
        self.assertFalse(b.execute('browse_start', {'task': 'x', 'url': 'https://kontur.ru/'}, {}, None, None, None, 7)['ok'])
        offline = b.execute('browse_start', {'task': 'https://kontur.ru/'}, {'web_browse': True}, None, lambda: True, lambda *_a: 1, 7)
        self.assertIn('0.5.0', offline['error'])
        self.poll()
        for task, url in (('найди цены', None), ('x', 'javascript:alert(1)'), ('x', 'https://user:secret@kontur.ru/')):
            result = b.execute('browse_start', {'task': task} if url is None else {'task': task, 'url': url},
                               {'web_browse': True}, None, lambda: True, lambda *_a: 1, 7)
            self.assertFalse(result['ok'], result)
            self.assertNotIn('started', result)
        for raw in ({'action': 'eval'}, {'action': 'click', 'x': -1, 'y': 2}, {'action': 'type', 'text': ''},
                    {'action': 'key', 'key': 'F5'}, {'action': 'done'}):
            with self.assertRaises(ValueError):
                b.validate_action(raw, SHOT)
        self.assertEqual(b.validate_action({'action': 'scroll', 'delta': '5000'}, SHOT)['delta'], 480)
        self.assertEqual(b.public_url('https://kontur.ru/a?token=SECRET&q=1'), 'https://kontur.ru/a?q=1')
        self.assertEqual(b.clean_url('https://kontur.ru/prices'), 'https://kontur.ru/prices')
        self.assertEqual(b.clean_url('https://kontur.ru/app#/prices'), 'https://kontur.ru/app#/prices')

    def test_invalid_model_action_does_not_reach_the_browser(self):
        self.start([{'action': 'fly'}, {'action': 'fly'}, {'action': 'fly'}])
        self.reply(self.job(), SHOT)
        self.wait_finished()
        self.assertIn('трижды', b._run['message'])
        self.assertIsNone(self.poll()[1]['job'])

    def test_restart_marks_running_report_interrupted(self):
        b._path().write_text(json.dumps([{'id': 'old', 'status': 'running', 'task': 'x'}]))
        b._run = None
        self.assertEqual(b.status()['reports'][0]['status'], 'interrupted')


class BrowseIntegrationTests(unittest.TestCase):
    def test_catalog_toggle_group_voice_and_executor(self):
        import jarvis
        import tool_groups
        off, on = {'telegramSkills': {'web_browse': False}}, {'telegramSkills': {'web_browse': True}}
        self.assertIn('web_browse', {s['id'] for s in jarvis.assistant_skills_catalog()})
        self.assertFalse(b.TOOL_NAMES & {t['name'] for t in jarvis._assistant_tool_list(off, False)})
        self.assertTrue(b.TOOL_NAMES <= {t['name'] for t in jarvis._assistant_tool_list(on, False)})
        self.assertEqual(tool_groups.unassigned(jarvis._assistant_tool_list(on, False)), [])
        self.assertEqual(tool_groups.group_of('browse_start'), 'browse')
        self.assertIn('browse', tool_groups.pick('зайди на https://kontur.ru/ и найди цены'))
        self.assertNotIn('browse', tool_groups.pick('какая погода завтра'))
        self.assertEqual(b.prompt_section(off['telegramSkills']), '')
        self.assertIn('browse_reply', b.prompt_section(on['telegramSkills']))
        self.assertFalse(jarvis.execute_assistant_tool('browse_start', {'task': 'https://kontur.ru/'}, off, '')['ok'])
        with open('index (9).html', encoding='utf-8') as handle:
            page = handle.read()
        for marker in ('loadBrowseTools', '/api/browse/tool', 'BROWSE_AGENT_TOOLS', "startsWith('browse_')",
                       'output.background', 'download-pc-extension', 'Скачайте расширение 0.5.0'):
            self.assertIn(marker, page)

    def test_vision_adapter_passes_image(self):
        import jarvis
        shot = SHOT.copy()
        with patch.object(jarvis, 'load_app_data', return_value={}), \
             patch.object(jarvis, '_ai_conf', return_value=('openai', 'key', 'gpt-test', '', '', 'main')), \
             patch.object(jarvis, '_openai_step', return_value={'content': '{"action":"wait"}', 'usage': {}}) as api, \
             patch.object(jarvis, '_log_call'):
            self.assertEqual(jarvis._browse_decide('system', {}, shot), {'action': 'wait'})
            self.assertIn(shot['image'], api.call_args.args[3][1]['content'][1]['image_url']['url'])
            self.assertEqual(jarvis._log_call.call_args.args[3], 'browse')
        response = Mock(ok=True)
        response.json.return_value = {'content': [{'type': 'text', 'text': '{"action":"done","answer":"1000"}'}], 'usage': {}}
        with patch.object(jarvis, 'load_app_data', return_value={}), \
             patch.object(jarvis, '_ai_conf', return_value=('claude', 'key', 'claude-test', '', '', 'main')), \
             patch.object(jarvis.requests, 'post', return_value=response), \
             patch.object(jarvis, '_log_call'):
            self.assertEqual(jarvis._browse_decide('system', {}, shot)['answer'], '1000')

    def test_browse_start_releases_the_chat_turn(self):
        import jarvis
        calls = []

        def post(url, **kwargs):
            body = kwargs.get('json') or {}
            calls.append(body.get('tools'))
            resp = Mock(ok=True)
            if len(calls) == 1:
                resp.json.return_value = {
                    'content': [{'type': 'tool_use', 'id': 't1', 'name': 'browse_start',
                                 'input': {'task': 'цены', 'url': 'https://kontur.ru/'}}],
                    'usage': {}, 'stop_reason': 'tool_use'}
            else:
                self.assertNotIn('tools', body)
                resp.json.return_value = {'content': [{'type': 'text', 'text': 'Смотрю сайт в фоне.'}],
                                          'usage': {}, 'stop_reason': 'end_turn'}
            return resp

        app = {'telegramSkills': {'web_browse': True}, 'settings': {}}
        with patch.object(jarvis.requests, 'post', side_effect=post), \
             patch.object(jarvis, 'execute_assistant_tool', return_value={'ok': True, 'started': True, 'background': True}), \
             patch.object(jarvis, '_log_call'):
            answer, _links = jarvis._assistant_loop_claude(
                [{'role': 'user', 'content': 'Зайди на https://kontur.ru/'}], 'key', 'claude-test', app, '', False)
        self.assertEqual(answer, 'Смотрю сайт в фоне.')
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
