import html
import io
import zipfile
from pathlib import Path
import re
import tempfile
import unittest
from xml.etree import ElementTree as ET

from epub_merge.books import Epub, parse_donor, parse_text
from epub_merge.demo import mojimoon_demo_files
from epub_merge.export import build_epub, initial_settings
from epub_merge.locations import page_start
from epub_merge.presentation import prepare_epub, serialize_xml, OPF, DC, NCX, XHTML
from third_party.mojimoon_wenku8 import epub_maker as maker


def legacy_metadata(data):
    """Extract the conventional syntax used by older EPUB metadata scanners."""
    text = data.decode('utf-8')
    metadata = re.search(r'<metadata\b[^>]*>(.*?)</metadata>', text, re.S)[1]
    field = lambda name: html.unescape(re.search(r'<dc:' + name + r'\b[^>]*>(.*?)</dc:' + name + '>', metadata, re.S)[1])
    series = re.search(r'<meta\b(?=[^>]*name="calibre:series")(?=[^>]*content="([^"]*)")[^>]*/>', metadata)[1]
    return {'title': field('title'), 'author': field('creator'), 'description': field('description'),
            'series': html.unescape(series), 'tags': [html.unescape(t) for t in re.findall(r'<dc:subject>(.*?)</dc:subject>', metadata)]}


class ReaderCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.original, art = mojimoon_demo_files()
        self.book = parse_text(self.original, 'base.epub')
        self.donor = parse_donor(art, 'donor.epub')
        self.settings = initial_settings(self.book, self.donor)
        for image in self.settings['images']: image.update(role='ignore', confirmed=True)

    def test_standard_and_legacy_metadata_scanners_both_read_all_fields(self):
        self.settings.update(description='简介 & <内容>\n第二段', series='系列 & 名称', subjects=['校园', '青春'])
        result = Epub(build_epub(self.book, self.donor, self.settings))
        data = result.read(result.opf_path)
        self.assertIn(b'<package ', data)
        self.assertIn(b'<metadata', data)
        self.assertIn(b'xmlns:dc="http://purl.org/dc/elements/1.1/"', data)
        self.assertIn(b'<meta name="calibre:series"', data)
        md = legacy_metadata(data)
        self.assertEqual(md, {'title': self.settings['title'], 'author': self.book.author,
                             'description': self.settings['description'], 'series': self.settings['series'],
                             'tags': self.settings['subjects']})
        self.assertEqual(result.metadata['series_index'], '1')
        self.assertIn('dcterms: http://purl.org/dc/terms/', result.opf.get('prefix'))

    def test_nav_and_ncx_have_same_order_and_only_one_level(self):
        result = Epub(build_epub(self.book, self.donor, self.settings))
        nav_item = next(v for v in result.manifest.values() if 'nav' in v.get('properties', '').split())
        nav = ET.fromstring(result.read(nav_item['path']))
        ol = nav.find('.//{%s}ol' % XHTML)
        self.assertFalse(ol.findall('.//{%s}ol' % XHTML))
        nav_paths = [result.resolve(nav_item['path'], li.find('{%s}a' % XHTML).get('href')) for li in ol]
        ncx_item = next(v for v in result.manifest.values() if v.get('media-type') == 'application/x-dtbncx+xml')
        ncx = ET.fromstring(result.read(ncx_item['path']))
        points = ncx.find('{%s}navMap' % NCX)
        self.assertFalse(points.findall('./{%s}navPoint/{%s}navPoint' % (NCX, NCX)))
        ncx_paths = [result.resolve(ncx_item['path'], p.find('{%s}content' % NCX).get('src')) for p in points]
        self.assertEqual(nav_paths, ncx_paths)
        self.assertEqual(len(nav_paths), 1 + len(self.book.chapters))
        self.assertEqual([p.get('playOrder') for p in points], ['1','2','3','4'])
        self.assertTrue(all(p in result.names for p in nav_paths))

    def test_empty_title_page_skipped_but_can_still_receive_art(self):
        prepared = prepare_epub(self.original)
        source = Epub(prepared)
        page = self.book.pages[0]
        self.assertNotIn(page['path'], [p for p, _, _ in source.documents()])
        book = parse_text(prepared, 'new-base.epub')
        self.assertEqual(book.pages[0]['id'], page['id'])
        self.assertEqual([p.text for p in book.paragraphs], [p.text for p in self.book.paragraphs])
        settings = initial_settings(book, self.donor)
        for image in settings['images']: image.update(role='ignore', confirmed=True)
        settings['images'][2].update(role='body', after_id=page_start(page['id']))
        result = Epub(build_epub(book, self.donor, settings))
        paths = [p for p, _, _ in result.documents()]
        first_chapter = book.anchors[book.paragraphs[0].id]['path']
        self.assertEqual(paths.index(page['path']) + 1, paths.index(first_chapter))
        content = result.read(page['path'])
        self.assertNotIn(b'<h1', content)
        self.assertIn(b'em_i0003', content)
        nav_item = next(v for v in result.manifest.values() if 'nav' in v.get('properties', '').split())
        nav = ET.fromstring(result.read(nav_item['path']))
        refs = [result.resolve(nav_item['path'], a.get('href')) for a in nav.iter('{%s}a' % XHTML)]
        self.assertEqual(refs.count(page['path']), 1)
        self.assertEqual(refs.index(page['path']) + 1, refs.index(first_chapter))

    def test_multiple_volume_slots_return_before_their_own_chapters(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'multi.epub')
            maker.create_epub(maker.NovelMeta('多卷', '作者'), [maker.Volume('第01卷', [maker.Chapter('第一章', [('p', '卷一正文。')])]), maker.Volume('第02卷', [maker.Chapter('第二章', [('p', '卷二正文。')])])], {}, None, path)
            book = parse_text(prepare_epub(Path(path).read_bytes()), 'multi.epub')
        self.assertEqual(len(book.pages), 2)
        self.assertEqual([p['index'] for p in book.pages], [0, 1])
        settings = initial_settings(book, self.donor)
        for image in settings['images']: image.update(role='ignore', confirmed=True)
        settings['images'][2].update(role='body', after_id=page_start(book.pages[1]['id']))
        result = Epub(build_epub(book, self.donor, settings))
        paths = [p for p, _, _ in result.documents()]
        self.assertNotIn(book.pages[0]['path'], paths)
        self.assertEqual(paths.index(book.pages[1]['path'])+1, paths.index(book.anchors[book.paragraphs[1].id]['path']))

    def test_all_prose_css_intro_and_image_bytes_survive(self):
        # Use an already merged EPUB to check repair never relocates artwork.
        for image in self.settings['images']: image.update(role='front', confirmed=True)
        self.settings['images'][0]['role'] = 'cover'
        self.settings['images'][2].update(role='body', after_id=page_start(self.book.pages[0]['id']))
        merged = build_epub(self.book, self.donor, self.settings)
        first, again = Epub(merged), Epub(prepare_epub(merged))
        for path in first.names:
            self.assertEqual(first.read(path), again.read(path), path)
        self.assertEqual(again.read('OEBPS/Text/v01_Intro.xhtml'), Epub(self.original).read('OEBPS/Text/v01_Intro.xhtml'))
        for paragraph in self.book.paragraphs:
            path = self.book.anchors[paragraph.id]['path']
            self.assertEqual(again.read(path), Epub(self.original).read(path))
        for image in self.donor.illustrations:
            self.assertEqual(again.read('OEBPS/Images/em_' + image.id + '.' + image.ext), image.data)

    def test_serialization_preserves_namespaces_and_qualified_attributes(self):
        root = ET.fromstring('<package xmlns="'+OPF+'" xmlns:dc="'+DC+'" xmlns:opf="'+OPF+'"><metadata><dc:creator opf:role="aut">姓名 &amp; 作者</dc:creator><meta name="calibre:series" content="系列 &amp; 书名"/></metadata></package>')
        serialized = serialize_xml(root)
        result = ET.fromstring(serialized)
        creator = result.find('{%s}metadata/{%s}creator' % (OPF,DC))
        self.assertEqual(creator.get('{%s}role' % OPF), 'aut')
        self.assertEqual(creator.text, '姓名 & 作者')
        self.assertEqual(result.find('{%s}metadata/{%s}meta' % (OPF,OPF)).get('content'), '系列 & 书名')

    def test_only_volume_heading_removed_and_other_title_page_content_survives_repair(self):
        source = Epub(self.original)
        page = self.book.pages[0]['path']
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            for info in source.zip.infolist():
                data = source.read(info.filename)
                if info.filename == page:
                    data = data.replace(b'</body>', '<h1>保留的其他内容</h1><p>署名</p></body>'.encode())
                archive.writestr(info, data)
        repaired = prepare_epub(buffer.getvalue())
        first, second = Epub(repaired), Epub(prepare_epub(repaired))
        self.assertIn('<h1>保留的其他内容</h1>'.encode(), first.read(page))
        self.assertNotIn(b'class="volume"', first.read(page))
        self.assertEqual(first.read(page), second.read(page))
