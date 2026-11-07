"""Real DOM regression tests; no WB/network/account. Requires Playwright + Chromium."""
import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1] / 'windows' / 'wb-extension'


@unittest.skipUnless(sync_playwright and shutil.which('chromium'), 'requires Playwright and Chromium')
class PageReadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=shutil.which('chromium'), headless=True,
                                                       args=['--no-sandbox'])
        source = (ROOT / 'background.js').read_text()
        cls.reader = source[source.index('function readPage('):source.index('\nasync function readWb(')]

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def read(self, html, rows, selected_fields=None, include_details=False):
        self.page.set_content('<base href="https://www.wildberries.ru/">' + html)
        for script in ('field-utils.js', 'page-data.js'):
            self.page.add_script_tag(content=(ROOT / script).read_text())
        self.page.evaluate('rows => window.__jarvisWbRead={delivery:{at:Date.now(),rows}}', rows)
        return self.page.evaluate('''args => new Function('location','selected','details', 'return ('+args.source+')("delivery",selected,details)')(
            new URL('https://www.wildberries.ru/lk/myorders/delivery'), args.selected, args.details)''',
            {'source': self.reader, 'selected': selected_fields or [], 'details': include_details})

    def test_extract_without_api_article_but_never_pair_by_order(self):
        result = self.read('''<section class="delivery-list">
            <article data-rid="rid-1">
              <a href="/catalog/123456/detail.aspx"><img src="data:image/gif;base64,AA==" data-src="https://basket-01.wbbasket.ru/vol1/image.webp?secret=strip" alt="Фото товара"></a>
              <a href="/catalog/123456/detail.aspx"><span class="product-name">Первый товар</span></a>
            </article>
            <article><a href="/catalog/654321/detail.aspx" title="Второй товар">Подробнее</a></article>
            <aside class="recommendations"><a href="/catalog/999999/detail.aspx" title="Рекомендация">Не заказ</a></aside>
            </section><p>Сумма к оплате: 1\u2009299 ₽</p>''',
            [{'id': 'rid-1', 'status': 'В пути'}, {'id': 'rid-2', 'status': 'Готов'}])
        first, second = result['rows']
        self.assertEqual(first['article'], '123456')
        self.assertEqual(first['name'], 'Первый товар')
        self.assertEqual(first['image_url'], 'https://basket-01.wbbasket.ru/vol1/image.webp')
        self.assertNotIn('article', second)
        self.assertEqual([p['article'] for p in result['page_products']], ['654321'])
        self.assertEqual(result['page_amount_due_labels'], ['1 299 ₽'])
        fields = {field['path']: field for field in result['fieldCatalog']}
        self.assertIn('Первый товар', fields['page.name']['examples'])
        self.assertIn('Второй товар', fields['page.name']['examples'])
        self.assertEqual(result['diagnostic']['page']['excludedRecommendations'], 1)

    def test_unbound_cards_still_populate_catalogue(self):
        result = self.read('''<div class="delivery-card"><a href="/catalog/123456/detail.aspx" title="Товар">Ссылка</a></div>
            <button>Оплатить заказ 1500</button>''', [{'id': 'unknown-rid', 'status': 'В пути'}])
        self.assertNotIn('name', result['rows'][0])
        self.assertEqual(result['page_products'][0]['name'], 'Товар')
        self.assertEqual(result['page_amount_due_labels'], ['Оплатить заказ 1500'])
        self.assertNotIn('payable', result['rows'][0])
        paths = {field['path']: field for field in result['fieldCatalog']}
        self.assertIn('123456', paths['page.article']['examples'])
        self.assertIn('unmatched_page_product', paths['page.binding']['examples'])

    def test_payment_number_does_not_absorb_next_item_count(self):
        result = self.read('<p>К оплате 1500</p><span>2 товара</span>', [{'id': 'rid', 'status': 'В пути'}])
        self.assertEqual(result['page_amount_due_labels'], ['К оплате 1500'])

    def test_product_image_outside_link_binds_by_article_in_loaded_path(self):
        result = self.read('''<div class="delivery-card">
            <img src="data:image/gif;base64,AA==" data-src="https://basket-01.wbbasket.ru/vol12/part123/123456/images/c246x328/1.webp?token=strip">
            <a href="/catalog/123456/detail.aspx" title="Товар">Товар</a>
            <img src="data:image/gif;base64,AA==" data-src="https://basket-01.wbbasket.ru/vol12/part123/654321/images/1.webp">
            </div>''', [{'id': 'rid', 'article': '123456', 'name': 'Товар', 'status': 'В пути'}])
        self.assertEqual(result['rows'][0]['image_url'], 'https://basket-01.wbbasket.ru/vol12/part123/123456/images/c246x328/1.webp')
        field = next(field for field in result['fieldCatalog'] if field['path'] == 'page.image_url')
        self.assertNotIn('token', field['examples'][0])

    def test_payment_captions_do_not_fabricate_an_amount_due(self):
        result = self.read('<button>Оплата при получении</button>', [{'id': 'rid', 'status': 'В пути'}])
        self.assertEqual(result['page_amount_due_labels'], [])
        self.assertNotIn('payable', result['rows'][0])
        field = next(field for field in result['fieldCatalog'] if field['path'] == 'page.payment_captions.*')
        self.assertEqual(field['examples'], ['Оплата при получении'])

    def test_extra_fields_are_used_only_when_details_are_requested(self):
        rows = [{'id': 'rid', 'name': 'Товар', 'status': 'В пути', 'price': '755 ₽',
                 'details': {'office': {'workTime': '09–21'}, 'item': {'token': 'SECRET'}}}]
        compact = self.read('<h1>Доставки</h1>', rows, selected_fields=['office.workTime'])
        self.assertEqual(compact['rows'][0]['extra_fields'], [])
        self.assertTrue(any(field['path'] == 'office.workTime' for field in compact['fieldCatalog']))
        detailed = self.read('<h1>Доставки</h1>', rows, selected_fields=['office.workTime'], include_details=True)
        self.assertEqual(detailed['rows'][0]['extra_fields'], [{'path': 'office.workTime', 'value': '09–21'}])
        automatic = self.read('<h1>Доставки</h1>', rows, include_details=True)
        self.assertTrue(any(field['path'] == 'office.workTime' for field in automatic['rows'][0]['extra_fields']))
        self.assertNotIn('SECRET', str(automatic))

    def test_existing_article_joins_page_without_rid_and_rejects_foreign_links(self):
        result = self.read('''<a href="https://evil.example/catalog/123456/detail.aspx" title="Чужой">Чужой</a>
            <div class="delivery-card"><a href="/catalog/123456/detail.aspx" title="Правильный товар">Товар</a></div>''',
            [{'id': 'rid', 'article': '123456', 'status': 'В пути'}])
        self.assertEqual(result['rows'][0]['name'], 'Правильный товар')
        self.assertEqual(result['page_products'], [])


if __name__ == '__main__':
    unittest.main()
