import base64
import copy
import importlib.util
import http.client
import threading
import io
import json
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET
import zipfile

from epub_merge.align import START, align
from epub_merge.books import BookError, Epub, FlowParser, parse_donor, parse_text
from epub_merge.demo import demo_files, mojimoon_demo_files
from epub_merge.export import build_epub, initial_settings
from epub_merge import wenku
from epub_merge.server import make_server
from third_party.mojimoon_wenku8 import epub_maker as maker


def reviewed():
    data, donor_data = mojimoon_demo_files()
    book = parse_text(data, 'mojimoon.epub')
    donor = parse_donor(donor_data, 'auto-novel.epub')
    align(book, donor)
    settings = initial_settings(book, donor)
    for image in settings['images']:
        image['confirmed'] = True
    return data, book, donor, settings


class PreserveTests(unittest.TestCase):
    def test_mojimoon_detection_and_prose_only_anchors(self):
        data, book, donor, settings = reviewed()
        self.assertEqual(book.public()['layout'], 'mojimoon')
        self.assertEqual(len(book.paragraphs), 16)
        self.assertEqual(book.paragraphs[0].text, '夜里十点，雨水沿着旧书店的玻璃窗滑落。')
        self.assertEqual(len(book.anchors), len(book.paragraphs))
        self.assertEqual([i.after_id for i in donor.illustrations if i.role == 'body'], ['p00003', 'p00009'])
        self.assertTrue(all('Intro' not in anchor['path'] for anchor in book.anchors.values()))

    def test_css_intro_and_paragraph_markup_unchanged(self):
        data, book, donor, settings = reviewed()
        merged = build_epub(book, donor, settings)
        with zipfile.ZipFile(io.BytesIO(data)) as before, zipfile.ZipFile(io.BytesIO(merged)) as after:
            self.assertEqual(after.read('OEBPS/Styles/style.css'), before.read('OEBPS/Styles/style.css'))
            for name in before.namelist():
                if name.endswith('.xhtml') and 'Intro' in name:
                    self.assertEqual(after.read(name), before.read(name), name)
                elif re.search(r'c\d+\.xhtml$', name):
                    original_p = re.findall(br'<p\b[^>]*>.*?</p>', before.read(name), re.S)
                    merged_p = re.findall(br'<p\b[^>]*>.*?</p>', after.read(name), re.S)
                    self.assertEqual(merged_p, original_p)
                if name.endswith(('.xhtml', '.xml', '.opf', '.ncx')):
                    ET.fromstring(after.read(name))
            for image in donor.illustrations:
                path = 'OEBPS/Images/em_' + image.id + '.' + image.ext
                self.assertEqual(after.read(path), image.data)
            self.assertEqual(after.infolist()[0].compress_type, zipfile.ZIP_STORED)

    def test_flat_nav_and_collection_metadata_survive(self):
        data, book, donor, settings = reviewed()
        result = Epub(build_epub(book, donor, settings))
        nav_item = next(x for x in result.manifest.values() if 'nav' in x.get('properties', '').split())
        nav = ET.fromstring(result.read(nav_item['path']))
        self.assertFalse(nav.findall('.//{*}ol/{*}li/{*}ol/{*}li'))
        self.assertEqual(len(nav.findall('.//{*}ol/{*}li')), 6)
        values = [x.text for x in result.opf.iter() if x.get('property') == 'belongs-to-collection']
        self.assertEqual(values, ['雨夜来信'])
        self.assertEqual(len(result.spine), len(Epub(data).spine) + 1)
        for idref in result.spine:
            self.assertIn(result.manifest[idref]['path'], result.names)
        for item in result.manifest.values():
            self.assertIn(item['path'], result.names)

    def test_inserted_images_have_correct_context(self):
        data, book, donor, settings = reviewed()
        merged_donor = parse_donor(build_epub(book, donor, settings), 'result.epub')
        original_images = [i for i in donor.illustrations if i.role == 'body']
        actual_images = [i for i in merged_donor.illustrations if i.role == 'body']
        self.assertEqual(len(actual_images), 2)
        for before, after in zip(original_images, actual_images):
            self.assertEqual(before.before, after.before)
            self.assertEqual(before.after, after.after)

    def test_start_anchor_is_in_first_body_chapter(self):
        data, book, donor, settings = reviewed()
        settings['images'][2]['after_id'] = START
        output = Epub(build_epub(book, donor, settings))
        anchor = book.anchors[book.paragraphs[0].id]
        content = output.read(anchor['path'])
        self.assertLess(content.index(b'id="em_i0003"'), content.index(book.paragraphs[0].text.encode()))
        self.assertIn('简介'.encode(), output.read('OEBPS/Text/v01_Intro.xhtml'))

    def test_replacing_upstream_fallback_cover(self):
        text, donor_data = mojimoon_demo_files()
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jvRkAAAAASUVORK5CYII=')
        with tempfile.TemporaryDirectory() as folder:
            output = str(Path(folder) / 'base.epub')
            maker.create_epub(maker.NovelMeta('封面测试', '作者'), [maker.Volume('第一卷', [maker.Chapter('第一章', [('p', '正文。')])])], {}, png, output, prefix='v01_')
            data = Path(output).read_bytes()
        book = parse_text(data, 'base.epub')
        donor = parse_donor(donor_data, 'donor.epub')
        settings = initial_settings(book, donor)
        for item in settings['images']:
            if item['role'] == 'body':
                item['role'] = 'ignore'
        original = Epub(data)
        result = Epub(build_epub(book, donor, settings))
        self.assertNotIn(original.cover_path, result.names)
        covers = [x for x in result.manifest.values() if 'cover-image' in x.get('properties', '').split()]
        self.assertEqual(len(covers), 1)
        self.assertEqual(result.cover_path, 'OEBPS/Images/em_i0001.svg')
        self.assertFalse(any('Cover.xhtml' in p for p, _, _ in result.documents()))

    def test_ignoring_all_images_keeps_intro_and_original_prose(self):
        data, book, donor, settings = reviewed()
        for item in settings['images']:
            item['role'] = 'ignore'
        with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(io.BytesIO(build_epub(book, donor, settings))) as result:
            for path in source.namelist():
                if path.startswith('OEBPS/Text/') and not re.search(r'v\d+\.xhtml$', path):
                    self.assertEqual(source.read(path), result.read(path))


class GenerationTests(unittest.TestCase):
    def test_id_and_volume_validation(self):
        self.assertEqual(wenku.book_id('https://www.wenku8.net/book/129.htm'), 129)
        self.assertEqual(wenku.book_id('129'), 129)
        for invalid in ['https://evil.example/book/129.htm', '1; echo hi', -1, True]:
            with self.assertRaises(BookError):
                wenku.book_id(invalid)
        for invalid in ['0', '-1', '1,2', '2.5', '2001']:
            with self.assertRaises(BookError):
                wenku.volume_number(invalid)

    @unittest.skipUnless(importlib.util.find_spec('requests') and importlib.util.find_spec('bs4'), '需要可选的文库抓取依赖')
    def test_actual_upstream_pipeline_is_truly_image_free(self):
        class FixtureFetcher:
            def __init__(self):
                self.urls = []
            def get(self, url, validate=None, encoding='utf-8'):
                self.urls.append(url)
                if '/book/' in url:
                    return '<title>测试小说 - 轻小说文库</title><div id="content">小说作者：测试作者<br/>最后更新：2026-10-19<br/>内容简介：<br/>测试简介<br/>阅读<br/>小说目录</div>'
                if url.endswith('index.htm'):
                    return '<table><tr><td class="vcss">第一卷</td><td class="ccss"><a href="1.htm">第一章</a></td><td class="ccss"><a href="2.htm">插图</a></td><td class="vcss">第二卷</td><td class="ccss"><a href="3.htm">第二章</a></td></tr></table>'
                if url.endswith('/1.htm'):
                    return '<div id="content">正文第一段。<br/>正文第二段。</div>'
                if url.endswith('/2.htm'):
                    return '<div id="content"><div><img src="https://example.invalid/illustration.jpg"/></div></div>'
                raise AssertionError('不应获取其他卷：' + url)
            def get_bytes(self, url):
                self.urls.append(url)
                return ' ★☆轻小说文库(Www.WenKu8.com)☆★ \n\n  第一章  \n    正文第一段。\n    正文第二段。'.encode('utf-8')
        fetcher = FixtureFetcher()
        upstream = wenku.load_upstream()
        with tempfile.TemporaryDirectory() as folder, patch.object(upstream, 'fetch_image', side_effect=AssertionError('不应请求图片或封面')) as images:
            result = wenku.generate_no_images(129, 1, fetcher=fetcher, output_root=Path(folder) / 'text')
            epub = Epub(Path(result['file']).read_bytes())
            self.assertFalse(any(x.get('media-type', '').startswith('image/') for x in epub.manifest.values()))
            self.assertIsNone(epub.cover_path)
            self.assertEqual(len(fetcher.urls), 3)
            images.assert_not_called()
            book = parse_text(Path(result['file']).read_bytes(), 'v01.epub')
            self.assertEqual([p.text for p in book.paragraphs], ['正文第一段。', '正文第二段。'])
            self.assertIn('第01卷', result['title'])
            self.assertIn('第01卷', result['filename'])
            self.assertEqual(epub.author, '测试作者')
            self.assertEqual(epub.metadata['description'], '测试简介')
            self.assertEqual(epub.metadata['description_source'], '全书简介')

    def test_background_job_hides_file_path_and_serves_result(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(wenku, 'OUTPUT', Path(folder)):
            path = Path(folder) / '129' / 'v01.epub'
            path.parent.mkdir()
            path.write_bytes(b'demo file')
            result = {'file': str(path), 'filename': '测试.epub', 'title': '测试', 'volume': 1}
            process = Mock(stdout=io.StringIO('正在生成\nRESULT:' + json.dumps(result) + '\n'))
            process.wait.return_value = 0
            jobs = wenku.GeneratorJobs()
            with patch.object(wenku.subprocess, 'Popen', return_value=process):
                token = jobs.start('129', '1', 'requests')
                until = time.monotonic() + 2
                while jobs.get(token)['status'] == 'running' and time.monotonic() < until:
                    time.sleep(.01)
                status = jobs.get(token)
                self.assertEqual(status['status'], 'done')
                self.assertNotIn('file', status['result'])
                self.assertEqual(jobs.download(token), (b'demo file', '测试.epub'))


class GenerationHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, value=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request(method, path, json.dumps(value) if value is not None else None,
                           {'Content-Type': 'application/json'} if value is not None else {})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.headers)
        connection.close()
        return result

    def test_start_progress_and_download_routes(self):
        generator = self.server.state.generator
        with patch.object(generator, 'start', return_value='fixture') as start:
            status, data, _ = self.request('POST', '/api/wenku/build', {'aid': '129', 'volume': '1'})
            self.assertEqual(status, 202)
            self.assertEqual(json.loads(data), {'job': 'fixture'})
            start.assert_called_once_with('129', '1', 'requests')
        with patch.object(generator, 'get', return_value={'status': 'done'}) as get:
            status, data, _ = self.request('GET', '/api/wenku/jobs/fixture')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(data), {'status': 'done'})
            get.assert_called_once_with('fixture')
        with patch.object(generator, 'download', return_value=(b'fixture', '无图.epub')):
            status, data, headers = self.request('GET', '/api/wenku/jobs/fixture/epub')
            self.assertEqual((status, data), (200, b'fixture'))
            self.assertEqual(headers['Content-Type'], 'application/epub+zip')

    def test_explicit_vid_reaches_generator_and_invalid_vid_is_rejected(self):
        generator=self.server.state.generator
        with patch.object(generator,'start',return_value='fixture') as start:
            status,data,_=self.request('POST','/api/wenku/build',{'aid':'1163','volume':'1','vid':'35538'})
            self.assertEqual(status,202)
            start.assert_called_once_with('1163','1','requests',vid='35538')
        for vid in ['-1','../2',True]:
            status,_,_=self.request('POST','/api/wenku/build',{'aid':'1163','vid':vid})
            self.assertEqual(status,400)

    def test_invalid_generation_request_does_not_start_worker(self):
        for payload in [{'aid': '../129'}, {'aid': '129', 'volume': 0}, {'aid': '129', 'scraper': 'auto'}]:
            status, data, _ = self.request('POST', '/api/wenku/build', payload)
            self.assertEqual(status, 400, data)
        self.assertIsNone(self.server.state.generator.active)
        self.assertEqual(self.request('GET', '/api/wenku/jobs/missing')[0], 400)


if __name__ == '__main__':
    unittest.main()
