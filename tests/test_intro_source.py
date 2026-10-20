import io
import re
import unittest
import zipfile

from epub_merge.books import Epub, parse_donor, parse_text
from epub_merge.demo import mojimoon_demo_files
from epub_merge.export import build_epub, initial_settings
from epub_merge.presentation import prepare_epub


INTRO = 'OEBPS/Text/v01_Intro.xhtml'
DESCRIPTION = re.compile(br'<div class="intro">.*?</div>', re.S)
SOURCE = re.compile(rb'<p class="meta">' + '来源：'.encode() + rb'.*?</p>', re.S)


def replace_intro(data, content):
    source = Epub(data)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for info in source.zip.infolist():
            archive.writestr(info, content if info.filename == INTRO else source.read(info.filename))
    return output.getvalue()


class IntroSourceTests(unittest.TestCase):
    def setUp(self):
        self.data, donor = mojimoon_demo_files()
        self.intro = Epub(self.data).read(INTRO)
        self.description = DESCRIPTION.search(self.intro)[0]
        self.source = SOURCE.search(self.intro)[0]
        self.donor = parse_donor(donor, 'donor.epub')

    def export(self, data, **updates):
        book = parse_text(data, 'base.epub')
        settings = initial_settings(book, self.donor)
        for image in settings['images']:
            image['role'] = 'ignore'
        settings.update(updates)
        return build_epub(book, self.donor, settings)

    def assert_source_below_description(self, data):
        intro = Epub(data).read(INTRO)
        self.assertEqual(intro.count(self.source), 1)
        description = DESCRIPTION.search(intro)
        self.assertIsNotNone(description)
        self.assertLess(description.end(), intro.index(self.source))
        return intro

    def test_existing_correct_order_is_unchanged(self):
        self.assertEqual(self.assert_source_below_description(prepare_epub(self.data)), self.intro)
        self.assertEqual(self.assert_source_below_description(self.export(self.data)), self.intro)

    def test_adding_description_to_empty_intro_places_source_last(self):
        empty = replace_intro(self.data, self.intro.replace(self.description, b''))
        description = '手动填写的简介第一段。\n第二段 & 特殊字符。'
        result = self.export(empty, description=description)
        intro = self.assert_source_below_description(result)
        self.assertIn('<div class="intro"><p>手动填写的简介第一段。</p><p>第二段 &amp; 特殊字符。</p></div>'.encode(), intro)
        self.assertEqual(Epub(result).metadata['description'], description)

    def test_export_repairs_old_order_without_metadata_changes(self):
        misplaced = self.intro.replace(self.description + b'\n' + self.source, self.source + b'\n' + self.description)
        result = self.export(replace_intro(self.data, misplaced))
        intro = self.assert_source_below_description(result)
        self.assertEqual(intro, self.intro)

    def test_preparation_only_moves_original_source_bytes_and_is_idempotent(self):
        source = self.source.replace(b'<p class="meta">', b'<p class="meta" data-note="keep">').replace(b'</p>', b'&amp;keep=1</p>')
        correct = self.intro.replace(self.source, source)
        misplaced = correct.replace(self.description + b'\n' + source, source + b'\n' + self.description)
        base = prepare_epub(replace_intro(self.data, misplaced))
        output = Epub(base)
        self.assertEqual(output.read(INTRO), correct)
        original = Epub(prepare_epub(replace_intro(self.data, correct)))
        for path in output.names:
            self.assertEqual(output.read(path), original.read(path), path)
        again = Epub(prepare_epub(base))
        for path in output.names:
            self.assertEqual(output.read(path), again.read(path), path)

    def test_updating_clearing_and_refilling_description_keeps_source_last(self):
        updated = self.export(self.data, description='更新后的简介。\n最后一段。')
        self.assert_source_below_description(updated)
        cleared = self.export(updated, description='')
        self.assertEqual(Epub(cleared).read(INTRO).count(self.source), 1)
        self.assertNotIn('更新后的简介。'.encode(), Epub(cleared).read(INTRO))
        self.assert_source_below_description(self.export(cleared, description='重新填写的简介。'))

    def test_missing_description_or_source_does_not_add_content(self):
        for content in [self.intro.replace(self.description, b''), self.intro.replace(self.source, b'')]:
            with self.subTest(content=content):
                data = replace_intro(self.data, content)
                self.assertEqual(Epub(prepare_epub(data)).read(INTRO), content)
                self.assertEqual(Epub(self.export(data)).read(INTRO), content)
