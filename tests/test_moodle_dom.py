"""Verify theme scrolling and read-only discovery against actual Chromium DOM."""
from pathlib import Path
import unittest
from playwright.sync_api import sync_playwright

SOURCE = (Path(__file__).resolve().parents[1] / 'windows/wb-extension/moodle.js').read_text()
SCROLL = SOURCE[SOURCE.index('function moodleScroll('):SOURCE.index('async function moodleAction(')]
PAGE = SOURCE[SOURCE.index('async function moodlePage('):SOURCE.index('async function moodleBind(')]


class MoodleDOMTests(unittest.TestCase):
    def test_pane_and_document_scroll_and_readonly_catalog(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True)
            page = browser.new_page(viewport={'width': 800, 'height': 600})
            page.route('https://moodle.test/**', lambda r: r.fulfill(content_type='text/html; charset=utf-8', body='''
                <body class="course-42"><div class="page-header-headings"><h1>Облака</h1></div>
                <div id="pane" style="position:absolute;inset:0;overflow-y:auto"><div style="height:3000px">
                <a href="/mod/quiz/view.php?id=1">Тест 1</a>
                <a href="/mod/quiz/view.php?id=1&forceview=1">Тест 1</a>
                <a href="https://other.test/mod/quiz/view.php?id=2">Чужой</a>
                <input id="answer" value="unchanged"></div></div></body>'''))
            page.goto('https://moodle.test/course/view.php?id=42')
            page.evaluate('() => {' + SCROLL + '\nmoodleScroll(480);}')
            self.assertEqual(page.locator('#pane').evaluate('(e)=>e.scrollTop'), 480)
            self.assertEqual(page.evaluate('scrollY'), 0)
            result = page.evaluate('''async () => {
                const chrome = {scripting:{executeScript:async ({func})=>[{result:func()}]}};
            ''' + PAGE + '\nreturn moodlePage(1);}')
            self.assertEqual(result['courseTitle'], 'Облака')
            self.assertEqual(result['tests'], [{'title': 'Тест 1'}])
            self.assertEqual(page.locator('#answer').input_value(), 'unchanged')
            page.set_content('<body style="margin:0"><div style="height:3000px">Questions</div></body>')
            page.evaluate('() => {' + SCROLL + '\nmoodleScroll(480);}')
            self.assertEqual(page.evaluate('scrollY'), 480)
            browser.close()


if __name__ == '__main__':
    unittest.main()
