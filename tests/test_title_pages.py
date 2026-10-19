import copy
import io
import unittest
import zipfile
from xml.etree import ElementTree as ET

from epub_merge.books import BookError, Epub, parse_donor, parse_text
from epub_merge.demo import demo_files, mojimoon_demo_files
from epub_merge.export import build_epub, build_report, initial_settings
from epub_merge.locations import boundary_index, page_start, page_end, valid_locations


class TitlePageTests(unittest.TestCase):
    def setUp(self):
        self.original, art = mojimoon_demo_files()
        self.book = parse_text(self.original, 'base.epub')
        self.donor = parse_donor(art, 'donor.epub')
        self.settings = initial_settings(self.book, self.donor)
        for image in self.settings['images']:
            image.update(role='ignore', confirmed=True)

    def test_title_page_available_without_changing_prose_or_chapter_ids(self):
        self.assertEqual(len(self.book.pages), 1)
        page = self.book.pages[0]
        self.assertIn('扉页', page['title'])
        self.assertIn(b'<h1 class="volume">', Epub(self.original).read(page['path']))
        self.assertEqual(self.book.paragraphs[0].id, 'p00001')
        self.assertEqual(self.book.chapters[0].id, 'c0001')
        self.assertEqual(len(self.book.paragraphs), 16)
        self.assertEqual(len(self.book.anchors), 16)
        self.assertEqual(boundary_index(self.book, page_start(page['id'])), 0)
        self.assertIn(page_end(page['id']), valid_locations(self.book))
        public = self.book.public()['pages'][0]
        self.assertNotIn('path', public)
        self.assertEqual(public['preview'], page['preview'])
        self.assertEqual(parse_text(self.original, 'base.epub').pages[0]['id'], page['id'])

    def test_title_image_is_inside_original_page_instead_of_front_or_first_chapter(self):
        page = self.book.pages[0]
        self.settings['images'][2].update(role='body', after_id=page_start(page['id']))
        self.settings['images'][3].update(role='body', after_id=page_end(page['id']))
        # Metadata editing must not overwrite the page insertions.
        self.settings['description'] = '修改后的简介'
        result = Epub(build_epub(self.book, self.donor, self.settings))
        original = Epub(self.original)
        content = result.read(page['path'])
        self.assertNotIn(b'<h1', content)
        self.assertLess(content.index(b'<body>'), content.index(b'em_i0003'))
        self.assertLess(content.index(b'em_i0003'), content.index(b'em_i0004'))
        self.assertLess(content.index(b'em_i0004'), content.index(b'</body>'))
        self.assertEqual(result.spine, original.spine)
        for path, _, _ in original.documents():
            if path == page['path'] or 'Intro' in path:
                continue
            self.assertEqual(result.read(path), original.read(path))
        for item in result.manifest.values():
            if item.get('media-type') == 'application/xhtml+xml':
                ET.fromstring(result.read(item['path']))
        for image in self.donor.illustrations[2:]:
            self.assertEqual(result.read('OEBPS/Images/em_' + image.id + '.' + image.ext), image.data)
        report = build_report(self.book, self.donor, self.settings)
        self.assertIn('扉页 · 扉页开头', report)
        self.assertIn('扉页 · 页面末尾', report)

    def test_start_and_end_surround_existing_title_page_content(self):
        source = Epub(self.original)
        path = self.book.pages[0]['path']
        modified = io.BytesIO()
        with zipfile.ZipFile(modified, 'w') as archive:
            for info in source.zip.infolist():
                data = source.read(info.filename)
                if info.filename == path:
                    data = data.replace(b'</body>', '<p>扉页署名</p></body>'.encode())
                archive.writestr(info, data)
        book = parse_text(modified.getvalue(), 'extra.epub')
        settings = copy.deepcopy(self.settings)
        settings['text_fingerprint'] = book.fingerprint
        page = book.pages[0]
        settings['images'][2].update(role='body', after_id=page_start(page['id']))
        settings['images'][3].update(role='body', after_id=page_end(page['id']))
        result = Epub(build_epub(book, self.donor, settings))
        content = result.read(path)
        self.assertLess(content.index(b'em_i0003'), content.index('扉页署名'.encode()))
        self.assertLess(content.index('扉页署名'.encode()), content.index(b'em_i0004'))

    def test_page_location_must_exist_and_be_confirmed(self):
        image = self.settings['images'][2]
        image.update(role='body', after_id=page_start('missing'))
        with self.assertRaises(BookError): build_epub(self.book, self.donor, self.settings)
        image.update(after_id=page_start(self.book.pages[0]['id']), confirmed=False)
        with self.assertRaises(BookError): build_epub(self.book, self.donor, self.settings)
        text, _ = demo_files()
        book = parse_text(text, 'plain.txt')
        settings = copy.deepcopy(self.settings)
        settings['text_fingerprint'] = book.fingerprint
        settings['layout'] = 'rebuild'
        settings['images'][2].update(after_id=page_start(self.book.pages[0]['id']), confirmed=True)
        with self.assertRaises(BookError): build_epub(book, self.donor, settings)
