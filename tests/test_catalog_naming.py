import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from epub_merge import wenku
from epub_merge.books import Epub, parse_text
from epub_merge.local_txt import convert_txt
from epub_merge.metadata import DC, OPF


class CatalogFixture:
    titles = ['第一卷 春', '短篇集 春夏秋冬', '第二卷 夏', '外传 雨夜', '第六点五卷 秋']
    raw_txt = '  内容简介\n    这是正文中的简介章节。\n  第一章\n    正文内容。'.encode('utf-16')

    def get(self, url, validate=None, encoding=None):
        if '/book/' in url:
            return '<title>测试系列 - 文库</title><div id="content">小说作者：测试作者<br/>作品Tags：校园 青春<br/>内容简介：<br/>全书简介。<br/>阅读<br/>小说目录</div>'
        if url.endswith('index.htm'):
            return ''.join(
                '<td class="vcss" vid="%s">%s</td><td class="ccss"><a href="%s.htm">内容简介</a></td><td class="ccss"><a href="%s.htm">第一章</a></td>'
                % (index * 100, title, index * 100 + 1, index * 100 + 2)
                for index, title in enumerate(self.titles, 1)
            )
        raise AssertionError('不应获取逐章网页：' + url)

    def get_bytes(self, url):
        self.vid = int(parse_qs(urlparse(url).query)['vid'][0])
        return self.raw_txt


class CatalogNamingTests(unittest.TestCase):
    def test_epub_and_txt_use_actual_titles_and_only_first_entry_has_description(self):
        expected_titles = ['第01卷 春', '短篇集 春夏秋冬', '第02卷 夏', '外传 雨夜', '第06.5卷 秋']
        expected_indices = ['1', '2', '3', '4', '5']
        with tempfile.TemporaryDirectory() as folder:
            output_root = Path(folder) / 'text'
            for ordinal, (title, series_index) in enumerate(zip(expected_titles, expected_indices), 1):
                for format_ in ['epub', 'txt']:
                    with self.subTest(ordinal=ordinal, format=format_):
                        fetcher = CatalogFixture()
                        result = wenku.generate_no_images(129, ordinal, fetcher=fetcher, output_root=output_root, output_format=format_)
                        self.assertEqual(result['volume'], ordinal)
                        self.assertEqual(result['vid'], ordinal * 100)
                        self.assertEqual(result['title'], '测试系列 ' + title)
                        self.assertEqual(result['filename'], '测试系列 ' + title + '.' + format_)
                        self.assertEqual(result['series_index'], series_index)
                        description = '全书简介。' if ordinal == 1 else ''
                        self.assertEqual(result['description'], description)
                        self.assertEqual(result['description_source'], '全书简介' if ordinal == 1 else '')
                        data = Path(result['file']).read_bytes()
                        if format_ == 'txt':
                            self.assertEqual(data, fetcher.raw_txt)
                            continue
                        epub = Epub(data)
                        self.assertEqual(epub.title, result['title'])
                        self.assertEqual(epub.metadata['description'], description)
                        self.assertEqual(epub.metadata['series'], '测试系列')
                        self.assertEqual(epub.metadata['series_index'], series_index)
                        self.assertEqual(epub.metadata['subjects'], ['校园', '青春'])
                        intro = epub.read('OEBPS/Text/v%02d_Intro.xhtml' % ordinal).decode('utf-8')
                        self.assertIn(result['title'], intro)
                        self.assertEqual('全书简介。' in intro, ordinal == 1)
                        self.assertNotIn('这是正文中的简介章节。', intro)
                        self.assertEqual([p.text for p in parse_text(data,'base.epub').paragraphs], ['这是正文中的简介章节。', '正文内容。'])
                        md = epub.opf.find('{%s}metadata' % OPF)
                        self.assertEqual(''.join(md.find('{%s}description' % DC).itertext()), description)
                        self.assertEqual(next(n.text for n in md if n.get('property') == 'group-position'), str(ordinal))
                        self.assertEqual(next(n.get('content') for n in md if n.get('name') == 'calibre:series_index'), str(ordinal))
            index = json.loads((output_root / '129' / 'index.json').read_text())
            self.assertEqual([v['title'] for v in index['volumes']], expected_titles)

    def test_vid_and_download_link_use_catalog_position_for_description(self):
        with tempfile.TemporaryDirectory() as folder:
            cases = [
                ('129', 5, 100, 1, '全书简介。'),
                ('129', 1, 200, 2, ''),
                ('https://dl.wenku8.com/packtxt.php?aid=129&vid=301&charset=utf-8', 1, None, 3, ''),
            ]
            for aid, volume, vid, expected_ordinal, description in cases:
                with self.subTest(aid=aid, volume=volume, vid=vid):
                    result = wenku.generate_no_images(aid, volume, vid=vid, fetcher=CatalogFixture(), output_root=Path(folder) / 'text')
                    self.assertEqual(result['volume'], expected_ordinal)
                    self.assertEqual(result['series_index'], str(expected_ordinal))
                    self.assertEqual(result['description'], description)
                    self.assertEqual(Epub(Path(result['file']).read_bytes()).metadata['description'], description)

    def test_unnumbered_first_book_keeps_title_and_uses_global_description(self):
        fetcher = CatalogFixture()
        fetcher.titles = ['冰果']
        with tempfile.TemporaryDirectory() as folder:
            result = wenku.generate_no_images(129, fetcher=fetcher, output_root=Path(folder) / 'text')
            self.assertEqual(result['title'], '测试系列 冰果')
            self.assertEqual(result['filename'], '测试系列 冰果.epub')
            self.assertEqual(result['series_index'], '1')
            self.assertEqual(result['description'], '全书简介。')

    def test_local_txt_does_not_reintroduce_a_volume_number(self):
        for filename, settings, expected_title, expected_index in [
            ('测试系列 外传.txt', {}, '测试系列 外传', ''),
            ('测试系列 短篇集.txt', {}, '测试系列 短篇集', ''),
            ('测试系列 第二卷.txt', {}, '测试系列 第02卷', ''),
            ('测试系列 第二卷.txt', {'series_index':'3'}, '测试系列 第02卷', '3'),
            ('测试系列.txt', {'volume_title':'外传'}, '测试系列 外传', ''),
        ]:
            with self.subTest(filename=filename, settings=settings):
                data, name, _ = convert_txt('第一章\n正文内容。'.encode('utf-8'), filename, settings)
                self.assertEqual(name, expected_title + '.epub')
                self.assertEqual(Epub(data).title, expected_title)
                self.assertEqual(Epub(data).metadata['series_index'], expected_index)

    def test_downloaded_txt_keeps_catalog_reading_order_when_converted(self):
        with tempfile.TemporaryDirectory() as folder:
            result = wenku.generate_no_images(129, 3, fetcher=CatalogFixture(), output_root=Path(folder) / 'text', output_format='txt')
            data, filename, _ = convert_txt(Path(result['file']).read_bytes(), result['filename'], {
                'title':result['title'], 'author':result['author'], 'series':result['series'],
                'series_index':result['series_index'], 'description':result['description'],
            })
            epub = Epub(data)
            self.assertEqual(filename, '测试系列 第02卷 夏.epub')
            self.assertEqual(epub.metadata['series_index'], '3')
            self.assertEqual(epub.metadata['description'], '')
