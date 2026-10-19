import copy
import hashlib
import http.client
import io
import json
from pathlib import PurePosixPath
import threading
import unittest
import zipfile
from xml.etree import ElementTree as ET

from epub_merge.align import START, align
from epub_merge.books import BookError, Epub, FlowParser, is_japanese, parse_donor, parse_text
from epub_merge.demo import TARGET, demo_files
from epub_merge.export import build_epub, build_report, initial_settings
from epub_merge.server import make_server


def replace_entry(data, path, content):
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(buf, 'w') as out:
        for info in src.infolist():
            out.writestr(info, content if info.filename == path else src.read(info.filename))
    return buf.getvalue()


def analyzed():
    text, donor = demo_files()
    book = parse_text(text, '雨夜来信.txt')
    pictures = parse_donor(donor, '插图.epub')
    align(book, pictures)
    settings = initial_settings(book, pictures)
    return book, pictures, settings


def confirmed(settings):
    settings = copy.deepcopy(settings)
    for image in settings['images']:
        image['confirmed'] = True
    return settings


class ReaderTests(unittest.TestCase):
    def test_spine_order_and_standalone_svg_image(self):
        book, donor, settings = analyzed()
        self.assertEqual([x.source_path for x in donor.illustrations], [
            'OEBPS/Images/cover.svg', 'OEBPS/Images/front.svg',
            'OEBPS/Images/letter.svg', 'OEBPS/Images/coast.svg'])
        self.assertEqual([x.role for x in donor.illustrations], ['cover', 'front', 'body', 'body'])
        letter = donor.illustrations[2]
        self.assertIn('没有署名的信', letter.before[-1])
        self.assertIn('展开信纸', letter.after[0])

    def test_ruby_and_inline_image_text_survive(self):
        blocks = FlowParser().read('<html><head><title>标题</title></head><body><p>走到<ruby>海<rt>うみ</rt></ruby>边<img src="a.jpg"/>以后。<br/>第二行。</p><svg><title>非正文</title><image href="b.jpg"/></svg></body></html>'.encode())
        self.assertEqual([(b.kind, b.text) for b in blocks], [
            ('text', '走到海边'), ('image', 'a.jpg'), ('text', '以后。'),
            ('text', '第二行。'), ('image', 'b.jpg')])

    def test_encoding_and_chapter_detection(self):
        book = parse_text('第一章 来信\n她打开了信。\n第二章 海边\n她看见灯塔。'.encode('gb18030'), 'test.txt')
        self.assertEqual([x.title for x in book.chapters], ['第一章 来信', '第二章 海边'])
        self.assertEqual(len(book.paragraphs), 2)
        utf16 = parse_text('序章\n测试正文。'.encode('utf-16'), 'utf16.txt')
        self.assertEqual(utf16.paragraphs[0].text, '测试正文。')

    def test_plain_text_is_not_rewritten(self):
        text = '第一章 起点\n正文中的 wenku8 字样和译者说明都应保留。\n“ & < > ”与省略号……\n＊　＊　＊'
        book = parse_text(text.encode(), 'test.txt')
        self.assertEqual([x.text for x in book.paragraphs], text.splitlines()[1:])

    def test_bilingual_kana_is_filtered(self):
        self.assertTrue(is_japanese('彼女は静かに扉を開けた。'))
        self.assertFalse(is_japanese('她安静地推开门。'))

    def test_missing_or_remote_resources_do_not_fetch(self):
        _, original = demo_files()
        broken = replace_entry(original, 'OEBPS/Text/plate.xhtml', '<html><body><img src="https://example.com/a.jpg"/></body></html>')
        donor = parse_donor(broken, 'donor.epub')
        self.assertEqual(len(donor.illustrations), 3)
        self.assertTrue(any('无法读取' in x for x in donor.warnings))

    def test_invalid_zip_and_entities_are_rejected(self):
        with self.assertRaises(BookError):
            parse_text(b'not an epub', 'bad.epub')
        _, original = demo_files()
        entity = b'<!DOCTYPE package [<!ENTITY x "abc">]><package>&x;</package>'
        with self.assertRaises(BookError):
            Epub(replace_entry(original, 'OEBPS/content.opf', entity))


class AlignmentTests(unittest.TestCase):
    def test_exact_context_and_monotonic_order(self):
        book, donor, settings = analyzed()
        images = donor.illustrations[2:]
        self.assertEqual([x.after_id for x in images], ['p00003', 'p00009'])
        self.assertTrue(all(x.score > .9 for x in images))
        self.assertTrue(all(not x.confirmed for x in images))

    def test_different_paragraph_splits(self):
        target, source = demo_files()
        target = target.replace('柜台上放着一封没有署名的信，信封边缘已经被雨水打湿。'.encode(),
                                '柜台上放着一封没有署名的信。\n信封边缘已经被雨水打湿。'.encode())
        book, donor = parse_text(target, 'split.txt'), parse_donor(source, 'source.epub')
        align(book, donor)
        anchor = next(p for p in book.paragraphs if p.text == '信封边缘已经被雨水打湿。')
        self.assertEqual(donor.illustrations[2].after_id, anchor.id)

    def test_unrelated_book_is_left_unplaced(self):
        _, source = demo_files()
        book = parse_text('第一章\n量子场论课程介绍算符的代数结构。\n计算结果需要经过严格的误差分析。'.encode(), 'unrelated.txt')
        donor = parse_donor(source, 'source.epub')
        align(book, donor)
        self.assertTrue(all(x.after_id is None for x in donor.illustrations if x.role == 'body'))


class ExportTests(unittest.TestCase):
    def test_requires_review_and_matching_project(self):
        book, donor, settings = analyzed()
        with self.assertRaisesRegex(BookError, '未确认'):
            build_epub(book, donor, settings)
        changed = confirmed(settings)
        changed['text_fingerprint'] = 'wrong'
        with self.assertRaisesRegex(BookError, '不一致'):
            build_epub(book, donor, changed)

    def test_invalid_anchors_duplicate_ids_and_covers(self):
        book, donor, settings = analyzed()
        settings = confirmed(settings)
        for modify in [lambda s: s['images'][2].update(after_id='missing'),
                       lambda s: s['images'][1].update(role='cover'),
                       lambda s: s['images'][1].update(id=s['images'][0]['id'])]:
            bad = copy.deepcopy(settings)
            modify(bad)
            with self.assertRaises(BookError):
                build_epub(book, donor, bad)

    def test_roundtrip_paragraphs_and_original_image_bytes(self):
        book, donor, settings = analyzed()
        data = build_epub(book, donor, confirmed(settings))
        roundtrip = parse_text(data, 'out.epub')
        self.assertEqual([p.text for p in roundtrip.paragraphs], [p.text for p in book.paragraphs])
        self.assertEqual([c.title for c in roundtrip.chapters], [c.title for c in book.chapters])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertEqual(z.infolist()[0].filename, 'mimetype')
            self.assertEqual(z.infolist()[0].compress_type, zipfile.ZIP_STORED)
            for image in donor.illustrations:
                self.assertEqual(z.read('OEBPS/images/' + image.id + '.' + image.ext), image.data)
            # All XML documents parse and every manifest href exists.
            for name in z.namelist():
                if name.endswith(('.xml', '.opf', '.xhtml', '.ncx', '.svg')):
                    ET.fromstring(z.read(name))
            opf = ET.fromstring(z.read('OEBPS/content.opf'))
            for item in opf.findall('{*}manifest/{*}item'):
                self.assertIn('OEBPS/' + item.get('href'), z.namelist())

    def test_exported_image_positions(self):
        book, donor, settings = analyzed()
        exported = parse_donor(build_epub(book, donor, confirmed(settings)), 'out.epub')
        for original, result in zip(donor.illustrations[2:], exported.illustrations[2:]):
            self.assertEqual(original.before, result.before)
            self.assertEqual(original.after, result.after)

    def test_manual_start_ignore_and_metadata_escape(self):
        book, donor, settings = analyzed()
        settings = confirmed(settings)
        settings['title'] = '故事 <A> & B'
        settings['images'][2]['after_id'] = START
        settings['images'][3]['role'] = 'ignore'
        data = build_epub(book, donor, settings)
        exported = Epub(data)
        self.assertEqual(exported.title, settings['title'])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertNotIn('OEBPS/images/i0004.svg', z.namelist())
            items = list(exported.documents())
            self.assertEqual(items[2][0], 'OEBPS/text/i0003.xhtml')

    def test_report_tracks_manual_reordering(self):
        book, donor, settings = analyzed()
        settings = confirmed(settings)
        settings['images'][2]['after_id'] = 'p00010'
        settings['images'][3]['after_id'] = 'p00002'
        self.assertIn('不同的先后顺序', build_report(book, donor, settings))


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, data=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        c.request(method, path, data, headers or {})
        r = c.getresponse()
        result = r.status, r.read(), dict(r.headers)
        c.close()
        return result

    def test_demo_review_export_and_report(self):
        status, data, _ = self.request('POST', '/api/demo')
        self.assertEqual(status, 200)
        result = json.loads(data)
        body = {'session': result['session'], 'settings': result['settings']}
        status, _, _ = self.request('POST', '/api/export', json.dumps(body), {'Content-Type':'application/json'})
        self.assertEqual(status, 400)
        body['settings'] = confirmed(body['settings'])
        status, data, headers = self.request('POST', '/api/export', json.dumps(body), {'Content-Type':'application/json'})
        self.assertEqual(status, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        self.assertEqual(headers['Content-Type'], 'application/epub+zip')
        status, report, _ = self.request('POST', '/api/report', json.dumps(body), {'Content-Type':'application/json'})
        self.assertEqual(status, 200)
        self.assertIn('合并记录'.encode(), report)
        image = result['donor']['illustrations'][0]['id']
        self.assertEqual(self.request('GET', '/api/image/' + result['session'] + '/' + image)[0], 200)

    def test_multipart_import_nonascii_names(self):
        target, donor = demo_files()
        boundary = 'local-merger-test-boundary'
        chunks = []
        for field, name, data in [('text','中文正文.txt',target), ('donor','高清插图.epub',donor)]:
            chunks.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + field + '"; filename="' + name + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + data + b'\r\n')
        chunks.append(('--' + boundary + '--\r\n').encode())
        status, data, _ = self.request('POST', '/api/analyze', b''.join(chunks), {'Content-Type':'multipart/form-data; boundary=' + boundary})
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)['text']['filename'], '中文正文.txt')

    def test_host_origin_and_static_routes(self):
        self.assertEqual(self.request('GET', '/api/health')[0], 200)
        self.assertEqual(self.request('GET', '/')[0], 200)
        self.assertEqual(self.request('GET', '/app.js')[0], 200)
        self.assertEqual(self.request('GET', '/api/health', headers={'Host':'evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/demo', headers={'Origin':'https://example.com'})[0], 403)
        self.assertEqual(self.request('GET', '/../README.md')[0], 404)


if __name__ == '__main__':
    unittest.main()
