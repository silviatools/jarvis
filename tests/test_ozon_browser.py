"""Real Chromium parsing/UI with synthetic Ozon pages and intercepted requests."""
import html
import json
import shutil
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1] / 'windows' / 'wb-extension'
ORDER = '12345678-9001'


def widget(name, value):
    return f'<div id="state-{name}" data-state="{html.escape(json.dumps(value, ensure_ascii=False), quote=True)}"></div>'


def product(sku, name, price):
    return {"title": {"name": {"text": name}, "common": {"action": {"id": sku, "link": "/product/" + sku + "/"}}},
            "price": {"price": [{"text": price}], "common": {"testInfo": {"automatizationId": "payMoney"}}},
            "image": {"productMedia": {"image": {"url": "https://ir.ozone.ru/example.jpg?token=remove"},
                       "common": {"action": {"link": "/my/orderdetails/?order=" + ORDER}}}}, "token": "DO-NOT-COPY"}


@unittest.skipUnless(sync_playwright and shutil.which('chromium'), 'requires Playwright and Chromium')
class OzonBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=shutil.which('chromium'), headless=True, args=['--no-sandbox'])
        code = (ROOT / 'background.js').read_text()
        cls.reader = code[code.index('async function readOzonPage('):code.index('\nasync function readOzon(')]

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.errors, self.requests = [], []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.p1, self.p2 = product('123456789', 'Тетрадь', '755 ₽'), product('987654321', 'Альбом', '1 017 ₽')
        def group(item, status, address):
            return {"leftBlock": {"textIcon": {"text": {"text": status, "testInfo": {"automatizationId": "tileStatus"}}},
                                  "title": {"text": address}, "subtitle": {"text": "Сегодня с 09:00 до 21:00"}},
                    "rightBlock": {"products": {"products": [item]}}}
        self.list_state = {"ordersV2": [group(self.p1, 'Можно забирать', 'Тестовая улица, 1'), group(self.p2, 'В пути', 'Другая улица, 2')]}
        self.parcels = [
            {"shipmentId": 'ready', "header": [{"text": {"text": 'Можно забирать', "testInfo": {"automatizationId": "shipment-status"}}}],
             "items": [{"sellers": [{"products": [self.p1]}]}]},
            {"shipmentId": 'transit', "header": [{"text": {"text": 'В пути', "testInfo": {"automatizationId": "shipment-status"}}}],
             "items": [{"sellers": [{"products": [self.p2]}]}]},
        ]
        self.body, self.details_status = '<h1>Мои заказы</h1>', 200

    def open(self, section='delivery'):
        def route(request):
            u = urlparse(request.request.url)
            self.requests.append(request.request.url)
            if u.netloc != 'www.ozon.ru':
                request.abort()
            elif u.path.rstrip('/') == '/my/orderlist':
                request.fulfill(body='<meta charset="utf-8">' + self.body + widget('orderList-1', self.list_state), content_type='text/html; charset=utf-8')
            elif u.path.rstrip('/') == '/my/orderdetails':
                self.assertEqual(parse_qs(u.query)['order'], [ORDER])
                request.fulfill(status=self.details_status, body=''.join(widget('shipmentWidget-' + str(i), p) for i, p in enumerate(self.parcels)), content_type='text/html; charset=utf-8')
            else:
                request.abort()
        self.page.route('**/*', route)
        self.page.add_init_script((ROOT / 'field-utils.js').read_text() + '\n' + (ROOT / 'ozon-capture.js').read_text())
        self.page.goto('https://www.ozon.ru/my/orderlist' + ('?selectedTab=archive' if section == 'orders' else ''))

    def read(self, section='delivery', details=False, selected=None):
        return self.page.evaluate('args => (' + self.reader + ')(args.section,args.selected,args.details)',
                                  {"section": section, "selected": selected or [], "details": details})

    def test_html_order_widgets_and_detail_documents(self):
        self.open()
        result = self.read()
        self.assertTrue(result['ok'], result)
        self.assertEqual([(r['name'], r['price'], r['status']) for r in result['rows']],
                         [('Тетрадь', '755 ₽', 'Можно забирать'), ('Альбом', '1 017 ₽', 'В пути')])
        self.assertEqual(result['rows'][0]['pvz'], 'Тестовая улица, 1')
        self.assertEqual(result['rows'][1]['pvz'], 'Другая улица, 2')
        self.assertEqual(len([url for url in self.requests if '/my/orderdetails/' in url]), 1)
        self.assertEqual(result['rows'][0]['extra_fields'], [])
        details = self.read(details=True, selected=['delivery.shipment_id'])
        self.assertEqual(details['rows'][0]['extra_fields'], [{'path': 'delivery.shipment_id', 'value': 'ready'}])
        self.assertNotIn('DO-NOT-COPY', json.dumps(details))
        self.assertFalse(self.errors, self.errors)

    def test_login_and_browser_challenge_are_not_empty_account(self):
        self.body = '<h1>Войдите, чтобы посмотреть заказы</h1><input type="tel">'
        self.open()
        self.assertEqual(self.read()['code'], 'login')
        self.page.locator('input').evaluate('el => el.remove()')
        self.page.locator('h1').evaluate("el => el.textContent='Доступ ограничен'")
        self.assertEqual(self.read()['code'], 'blocked')

    def test_schema_drift_is_diagnostic_not_no_orders(self):
        self.list_state = {"newUnknownList": []}
        self.open()
        result = self.read()
        self.assertFalse(result['ok'])
        self.assertEqual(result['diagnostic']['stage'], 'waiting_for_list')
        self.assertTrue(result['diagnostic']['captureLoaded'])
        self.assertIn('orderList', json.dumps(result['diagnostic']))

    def test_empty_and_recently_received_only(self):
        self.list_state = {"ordersV2": []}
        self.open()
        result = self.read()
        self.assertTrue(result['ok'])
        self.assertTrue(result['empty'])
        self.assertFalse(result['rows'])

    def test_missing_details_keeps_list_data_but_no_verified_notice_identity(self):
        self.details_status = 403
        self.open()
        result = self.read()
        self.assertTrue(result['ok'])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['diagnostic']['verifiedPositions'], 0)
        self.assertTrue(all(r['id'].startswith('ozon-list:') for r in result['rows']))

    def test_archive_and_wrong_section(self):
        self.open('orders')
        result = self.read('orders')
        self.assertTrue(result['ok'])
        self.assertIn('selectedTab=archive', result['url'])
        self.assertEqual(self.read('delivery')['diagnostic']['stage'], 'wrong_section')


@unittest.skipUnless(sync_playwright and shutil.which('chromium'), 'requires Playwright and Chromium')
class ExtensionUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=shutil.which('chromium'), headless=True, args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.route('**/*', lambda route: route.fulfill(body=(ROOT / urlparse(route.request.url).path.lstrip('/')).read_text(),
                                                         content_type='text/html' if '.html' in route.request.url else 'text/javascript'))
        self.page.add_init_script('''window.values={server:'https://jarvis.example',enabled:true,
          fieldCatalog:[{path:'office.wb',types:['string'],examples:['WB'],rows:1}],selectedFields:['office.wb'],
          ozonFieldCatalogAt:Date.now(),ozonFieldCatalog:[{path:'delivery.shipment_id',types:['string'],examples:['<img src=x>'],rows:1}],ozonSelectedFields:[],
          readLogs:[{shop:'wb',code:'WB_ONLY'}],ozonReadLogs:[{shop:'ozon',code:'OZON_ONLY'}]};window.calls=[];
          window.chrome={storage:{local:{get:async keys=>Object.fromEntries((Array.isArray(keys)?keys:[keys]).filter(k=>k in values).map(k=>[k,values[k]])),
            set:async data=>Object.assign(values,data),remove:async keys=>(Array.isArray(keys)?keys:[keys]).forEach(k=>delete values[k])}},
            runtime:{getManifest:()=>({version:'0.3.0'}),getURL:file=>'https://extension.test/'+file,
              sendMessage:async msg=>{calls.push(msg);return {ok:true,started:true}}},tabs:{create:opts=>calls.push(opts)},permissions:{request:async ()=>true}};''')

    def test_field_catalogues_and_selections_stay_separate(self):
        self.page.goto('https://extension.test/fields.html?shop=ozon')
        self.page.wait_for_selector('input[aria-label="delivery.shipment_id"]')
        self.assertEqual(self.page.locator('#shop').input_value(), 'ozon')
        self.assertEqual(self.page.locator('tbody img').count(), 0)
        self.page.locator('input[aria-label="delivery.shipment_id"]').check()
        self.page.locator('#save').click()
        self.page.wait_for_function("values.ozonSelectedFields.includes('delivery.shipment_id')")
        self.assertEqual(self.page.evaluate('values.selectedFields'), ['office.wb'])
        self.page.select_option('#shop', 'wb')
        self.page.wait_for_selector('input[aria-label="office.wb"]')
        self.assertTrue(self.page.locator('input[aria-label="office.wb"]').is_checked())
        self.assertFalse(self.errors, self.errors)

    def test_popup_selects_ozon_for_diagnostics_logs_and_open(self):
        self.page.goto('https://extension.test/popup.html')
        self.page.select_option('#shop', 'ozon')
        self.page.wait_for_function("document.getElementById('logs').value.includes('OZON_ONLY')")
        self.assertEqual(self.page.evaluate('values.popupShop'), 'ozon')
        self.assertNotIn('WB_ONLY', self.page.locator('#logs').input_value())
        self.page.locator('#diagnose').click()
        self.page.wait_for_function("calls.some(c=>c.type==='diagnose'&&c.shop==='ozon')")
        self.page.locator('#open').click()
        self.page.wait_for_function("calls.some(c=>c.url==='https://www.ozon.ru/my/orderlist')")
        self.assertFalse(self.errors, self.errors)


if __name__ == '__main__':
    unittest.main()
