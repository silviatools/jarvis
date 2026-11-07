"""Read-only Windows bridge: scoped authentication and full queue round trips."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest.mock import patch

import jarvis
import market_pc as pc
import market_skill


class PcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DATA_DIR": self.tmp.name})
        self.env.start()
        self.token = pc.pair()["token"]
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.env.stop)
        self.addCleanup(pc.disconnect)

    def poll(self, token=None):
        return pc.device_call('/api/market/pc/poll', token or self.token, {})

    def start_read(self, section='delivery', timeout=2):
        self.poll()
        results = []
        thread = threading.Thread(target=lambda: results.append(pc.request(section, timeout=timeout)))
        thread.start()
        for _ in range(100):
            status, response = self.poll()
            if response.get('job'):
                return thread, results, response['job']
            time.sleep(.005)
        self.fail('job not queued')

    def result(self, job, result):
        return pc.device_call('/api/market/pc/result', self.token, {'id': job['id'], 'result': result})

    def test_queue_and_key_scope(self):
        self.assertNotIn(self.token, pc._path().read_text())
        self.assertFalse(pc.public_status()['online'])
        self.assertEqual(self.poll('bad')[0], 401)
        thread, results, job = self.start_read()
        self.assertEqual(job['section'], 'delivery')
        self.assertEqual(set(job), {'id', 'section'})
        self.assertIsNone(self.poll()[1]['job'])
        self.assertEqual(pc.request('orders')['code'], 'busy')
        self.assertEqual(self.result(job, {
            'ok': True, 'cabinet': True, 'url': 'https://www.wildberries.ru/lk/myorders/delivery?secret=hidden',
            'rows': [{'id': 'r-1', 'name': 'Книга', 'status': 'В пути', 'cookie': 'do-not-copy'}],
            'cookies': 'do-not-copy'
        })[0], 200)
        thread.join(2)
        self.assertTrue(results[0]['ok'])
        self.assertIn('Книга', results[0]['text'])
        self.assertNotIn('secret', results[0]['url'])
        self.assertNotIn('do-not-copy', json.dumps(results))
        self.assertEqual(pc.public_status()['session'], 'active')
        self.assertEqual(self.result(job, {})[0], 409)

    def test_empty_requires_explicit_proof_and_correct_section(self):
        base = {'ok': True, 'cabinet': True, 'url': 'https://www.wildberries.ru/lk/myorders/archive'}
        self.assertFalse(pc._clean_result('orders', base)['ok'])
        self.assertEqual(pc._clean_result('orders', {**base, 'empty': True})['text'], 'Покупок нет.')
        self.assertFalse(pc._clean_result('delivery', {**base, 'empty': True})['ok'])
        self.assertFalse(pc._clean_result('orders', {**base, 'empty': 'true'})['ok'])
        for url in ('https://www.wildberries.ru.evil.test/lk/myorders/archive',
                    'https://www.wildberries.ru/catalog/1/detail.aspx', 'not a URL'):
            self.assertFalse(pc._clean_result('orders', {**base, 'url': url, 'empty': True})['ok'])

    def test_expired_revoked_and_stale_jobs(self):
        self.assertEqual(pc.request('delivery')['code'], 'offline')
        thread, results, job = self.start_read(timeout=.06)
        thread.join(1)
        self.assertEqual(results[0]['code'], 'timeout')
        self.assertEqual(self.result(job, {})[0], 409)
        thread, results, job = self.start_read()
        old = self.token
        self.token = pc.pair()['token']
        thread.join(1)
        self.assertEqual(results[0]['code'], 'revoked')
        self.assertEqual(self.poll(old)[0], 401)
        self.assertFalse(pc.public_status()['online'])

    def test_errors_are_not_success_and_no_secrets_echoed(self):
        thread, results, job = self.start_read()
        self.result(job, {'ok': False, 'code': 'login', 'error': 'secret-cookie'})
        thread.join(1)
        self.assertEqual(results[0]['code'], 'login')
        self.assertNotIn('secret-cookie', json.dumps(results))
        self.assertEqual(pc.public_status()['session'], 'expired')
        self.assertFalse(pc._clean_result('orders', ['malformed'])['ok'])
        self.assertFalse(pc._clean_result('orders', {'ok': True, 'url': []})['ok'])

    def test_existing_assistant_tools_use_pc_without_proxy(self):
        with patch.object(pc, 'request', return_value={'ok': True, 'source': 'pc'}) as request, \
                patch.object(market_skill, 'get_driver', side_effect=AssertionError('server browser used')):
            self.assertEqual(market_skill.execute('market_delivery', {'shop': 'wb'})['source'], 'pc')
            request.assert_called_once_with('delivery')
            request.reset_mock()
            self.assertIn('error', market_skill.execute('market_delivery', {'shop': 'wb'}, {'marketplaces': False}))
            request.assert_not_called()
            self.assertFalse(market_skill.execute('market_cancel_order', {'shop': 'wb', 'order_id': '1001-0001', 'confirmed': True})['ok'])
            self.assertIn('error', market_skill.execute('market_order', {'shop': 'wb', 'order_id': '1001-0001'}))
        with patch.object(market_skill, 'get_driver', side_effect=AssertionError('server fallback used')):
            for tool in ('market_favorites', 'market_profile', 'market_returns'):
                self.assertFalse(market_skill.execute(tool, {'shop': 'wb'})['ok'])
            self.assertEqual(market_skill.execute('market_delivery', {'shop': 'wb'})['code'], 'offline')
        self.assertFalse(pc.request('profile')['ok'])

    def test_download_archive(self):
        with zipfile.ZipFile(io.BytesIO(pc.extension_zip())) as archive:
            self.assertEqual(len(archive.namelist()), 6)
            manifest = json.loads(archive.read('manifest.json'))
            self.assertNotIn('cookies', manifest['permissions'])
            self.assertNotIn(self.token.encode(), archive.read('background.js'))


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DATA_DIR': self.tmp.name})
        self.env.start()
        self.server = jarvis.JarvisServer(('127.0.0.1', 0), jarvis.JarvisHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(2)
        self.server.server_close()
        pc.disconnect()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, path, body=None, owner=False, token=None):
        headers = {'Content-Type': 'application/json'}
        if owner:
            headers['Cookie'] = 'jarvis_auth=' + jarvis.AUTH_COOKIE_VALUE
        if token:
            headers['Authorization'] = 'Bearer ' + token
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=4) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_device_key_only_authenticates_two_endpoints(self):
        self.assertEqual(self.call('/api/market/pc/pair', {})[0], 401)
        token = json.loads(self.call('/api/market/pc/pair', {}, owner=True)[1])['token']
        self.assertEqual(self.call('/api/market/pc/poll', {}, token=token)[0], 200)
        self.assertEqual(self.call('/api/market/pc/poll', {}, owner=True)[0], 401)
        for path, body in [('/api/market/status', None), ('/api/market/pc/download', None),
                           ('/api/market/pc/pair', {}), ('/api/market/pc/disconnect', {}),
                           ('/api/market/pc/check', {}), ('/api/market/tool', {}), ('/api/config', {})]:
            self.assertEqual(self.call(path, body, token=token)[0], 401, path)
        self.assertEqual(self.call('/api/market/pc/result', {'id': 'invented'}, token=token)[0], 409)
        self.assertEqual(self.call('/api/market/pc/poll', [], token=token)[0], 400)
        code, blob = self.call('/api/market/pc/download', owner=True)
        self.assertEqual(code, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(blob)))
        self.call('/api/market/pc/disconnect', {}, owner=True)
        self.assertEqual(self.call('/api/market/pc/poll', {}, token=token)[0], 401)


if __name__ == '__main__':
    unittest.main()
