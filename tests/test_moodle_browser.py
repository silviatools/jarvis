"""Actual Chromium extension against synthetic Moodle; no account or AI calls."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class MoodleBrowser(unittest.TestCase):
    def test_screenshot_answer_submit_next_and_course_guard(self):
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
            ext = Path(tmp) / 'extension'
            shutil.copytree(ROOT / 'windows/wb-extension', ext)
            manifest = json.loads((ext / 'manifest.json').read_text())
            # Test-only substitute for the toolbar click's activeTab grant.
            manifest['host_permissions'].append('https://moodle.test/*')
            (ext / 'manifest.json').write_text(json.dumps(manifest))
            browser = p.chromium.launch_persistent_context(str(Path(tmp) / 'profile'), executable_path='/usr/bin/chromium',
                headless=True, ignore_default_args=['--disable-extensions'],
                args=[f'--disable-extensions-except={ext}', f'--load-extension={ext}', '--no-sandbox'])
            def fixture(route):
                path = route.request.url
                if '/course/' in path:
                    body = '<h1>Облака</h1><a href="/mod/quiz/view.php?id=1">Тест 1</a><a href="/mod/quiz/view.php?id=2">Тест 2</a>'
                else:
                    body = '<h1>Тест</h1><input id="answer"><button id="submit" onclick="document.body.innerHTML=\'Оценка: 8/10\'">Отправить всё и завершить</button>'
                route.fulfill(content_type='text/html', body='<html><body class="course-42">' + body + '</body></html>')
            browser.route('https://moodle.test/**', fixture)
            page = browser.new_page()
            page.goto('https://moodle.test/course/view.php?id=42')
            debug_page = browser.new_page()
            debug_page.goto('chrome://extensions/')
            extensions = debug_page.evaluate("""async () => (await new Promise(r => chrome.developerPrivate.getExtensionsInfo({includeDisabled:true,includeTerminated:true},r))).map(e => ({name:e.name,errors:e.runtimeErrors,manifest:e.manifestErrors}))""")
            debug_page.close()
            loaded = [e for e in extensions if e['name'].startswith('Джарвис')]
            if not loaded:
                browser.close()
                self.skipTest('This browser runtime ignores unpacked-extension launch flags; run on desktop Chromium.')
            self.assertEqual(loaded[0]['manifest'], [])
            self.assertEqual(loaded[0]['errors'], [])
            worker = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker')
            worker.evaluate('''async () => {
              moodleTick = async () => {};
              moodlePost = async () => ({ok:true});
              await chrome.storage.local.set({enabled:true, token:'synthetic', server:'https://jarvis.test'});
              const [tab] = await chrome.tabs.query({url:'https://moodle.test/*'});
              await moodleBind(tab);
            }''')
            count = 0
            def action(a):
                nonlocal count
                count += 1
                return worker.evaluate('''async ({a,id}) => {
                    const {moodle:s} = await chrome.storage.session.get('moodle');
                    return moodleAction(s, {id, run:'fixture', action:a, expires: Date.now()/1000+30});
                }''', {'a': a, 'id': str(count)})
            shot = action({'action': 'snapshot'})
            self.assertTrue(shot['image'])
            self.assertNotIn('url', shot)
            self.assertIsNone(shot['quiz'])
            shot = action({'action': 'open'})
            self.assertEqual(shot['quiz'], 'test-1')
            box = page.locator('#answer').bounding_box()
            action({'action': 'click', 'x': box['x']+5, 'y': box['y']+5})
            action({'action': 'type', 'text': 'Облачные вычисления'})
            self.assertEqual(page.locator('#answer').input_value(), 'Облачные вычисления')
            box = page.locator('#submit').bounding_box()
            action({'action': 'click', 'x': box['x']+5, 'y': box['y']+5})
            self.assertIn('8/10', page.inner_text('body'))
            self.assertEqual(action({'action': 'next'})['quiz'], 'test-2')
            page.evaluate("document.body.className = 'course-99'")
            with self.assertRaisesRegex(Exception, 'другой курс'):
                action({'action': 'click', 'x': 20, 'y': 20})
            browser.close()


if __name__ == '__main__':
    unittest.main()
