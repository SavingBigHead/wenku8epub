"""Export bitmap pixels clockwise; retain vector rotation and project originals."""
import base64
import copy
import hashlib
import io
import json
import re
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from dataclasses import replace
from html.parser import HTMLParser
from unittest.mock import patch
from xml.etree import ElementTree as ET

from PIL import Image, ImageOps

from epub_merge.books import BookError, Epub, parse_donor, parse_text, image_info
from epub_merge.demo import demo_files, mojimoon_demo_files
from epub_merge.export import build_epub, build_report, initial_settings, validate
from epub_merge.image_order import prepare_images
from epub_merge.locations import START, chapter_start, chapter_end, page_start, page_end
from epub_merge.presentation import prepare_epub
from epub_merge.server import make_server
from test_added_images import png

SVG = 'http://www.w3.org/2000/svg'
XLINK = 'http://www.w3.org/1999/xlink'
OPF = 'http://www.idpf.org/2007/opf'


def rewrite_epub(data, replacements):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, 'w') as target:
        for info in source.infolist():
            target.writestr(info, replacements.get(info.filename, source.read(info.filename)))
    return output.getvalue()


class ReportTable(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.rows, self.row, self.cell = [], [], None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.row = []
        if tag in ('td', 'th'): self.cell = []
        if tag == 'br' and self.cell is not None: self.cell.append(' ')

    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(''.join(self.cell).strip())
            self.cell = None
        if tag == 'tr': self.rows.append(self.row)


class ImageRotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text, art = mojimoon_demo_files()
        cls.fixtures = {
            'generic': parse_text(demo_files()[0], 'plain.txt'),
            'mojimoon': parse_text(text, 'base.epub'),
        }
        cls.art = parse_donor(art, 'donor.epub')
        cls.png_data = png()  # Non-square 24 x 32 local-image fixture.

    def project(self, layout, local=False):
        book, donor = copy.deepcopy((self.fixtures[layout], self.art))
        settings = initial_settings(book, donor)
        for image in settings['images']:
            image.update(role='ignore', confirmed=True, rotation=0)
        if local:
            settings['added_images'] = [{
                'id': 'local_rotation', 'filename': '旋转新图.png',
                'data': base64.b64encode(self.png_data).decode(),
                'sha256': hashlib.sha256(self.png_data).hexdigest(),
            }]
            settings['image_order'].append('local_rotation')
            settings['images'].append({'id': 'local_rotation', 'role': 'ignore',
                                       'confirmed': True, 'rotation': 0})
        return book, donor, settings

    @staticmethod
    def decision(settings, id_):
        return next(item for item in settings['images'] if item['id'] == id_)

    def asset(self, result, data):
        paths = [item['path'] for item in result.manifest.values()
                 if item.get('media-type', '').startswith('image/')
                 and result.read(item['path']) == data]
        self.assertEqual(len(paths), 1, 'Expected exactly one unchanged image resource')
        return paths[0]

    @staticmethod
    def references(result, asset):
        found = []
        for item in result.manifest.values():
            if item.get('media-type') != 'application/xhtml+xml': continue
            root = ET.fromstring(result.read(item['path']))
            for node in root.iter():
                if node.tag.rsplit('}', 1)[-1] not in ('img', 'image'): continue
                href = node.get('src') or node.get('href') or node.get('{%s}href' % XLINK)
                if href and result.resolve(item['path'], href) == asset:
                    found.append((item['path'], root, node))
        return found

    def assert_rotation(self, result, data, width, height, angle):
        if image_info(data)[0] != 'image/svg+xml':
            return self.assert_raster_rotation(result, data, angle)
        asset = self.asset(result, data)
        refs = self.references(result, asset)
        self.assertEqual(len(refs), 1, 'Each selected image must be rendered exactly once')
        path, root, image = refs[0]
        self.assertEqual(image.tag, '{%s}image' % SVG)
        parents = {child: parent for parent in root.iter() for child in parent}
        wrapper = parents.get(image)
        while wrapper is not None and wrapper.tag != '{%s}svg' % SVG:
            wrapper = parents.get(wrapper)
        self.assertIsNotNone(wrapper, 'Rotated artwork needs an SVG-namespace wrapper')
        numbers = lambda value: tuple(float(v) for v in re.split(r'[\s,]+', value.strip()))
        expected_size = (height, width) if angle in (90, 270) else (width, height)
        self.assertEqual(numbers(wrapper.get('viewBox', '')), (0, 0) + expected_size)
        self.assertEqual((float(image.get('width')), float(image.get('height'))), (width, height))
        matrix = re.fullmatch(r'\s*matrix\s*\(([^)]+)\)\s*', image.get('transform', ''))
        self.assertIsNotNone(matrix, 'Rotation belongs on the inner image as a matrix')
        expected = {90: (0, 1, -1, 0, height, 0),
                    180: (-1, 0, 0, -1, width, height),
                    270: (0, -1, 1, 0, 0, width)}[angle]
        self.assertEqual(numbers(matrix[1]), expected)
        item = next(item for item in result.manifest.values() if item['path'] == path)
        self.assertEqual(item.get('properties', '').split().count('svg'), 1)
        return path, asset


    def assert_raster_rotation(self, result, data, angle, jpeg=None):
        with Image.open(io.BytesIO(data)) as source:
            if jpeg is None: jpeg = 'either' if source.format == 'JPEG' else False
            pixels = source.convert('RGBA')
            width, height = pixels.size
            # Independent coordinate mapping, so reversed turns cannot pass.
            size = (height, width) if angle in (90, 270) else (width, height)
            expected = Image.new('RGBA', size)
            for y in range(height):
                for x in range(width):
                    target = {90: (height - 1 - y, x),
                              180: (width - 1 - x, height - 1 - y),
                              270: (y, width - 1 - x)}[angle]
                    expected.putpixel(target, pixels.getpixel((x, y)))
        return self.assert_bitmap_resource(result, expected, jpeg=jpeg)

    def assert_bitmap_resource(self, result, expected, jpeg=False):
        matches = []
        formats = ('image/jpeg', 'image/png') if jpeg == 'either' else ('image/jpeg' if jpeg else 'image/png',)
        for item in result.manifest.values():
            if item.get('media-type') not in formats: continue
            encoded_jpeg = item['media-type'] == 'image/jpeg'
            with Image.open(io.BytesIO(result.read(item['path']))) as image:
                if image.size != expected.size: continue
                actual = image.convert('RGBA')
                if encoded_jpeg:
                    # DCT transforms can change RGB rounding; coefficients are checked separately.
                    errors = [abs(a - b) for a, b in zip(actual.tobytes(), expected.tobytes())]
                    matches_pixels = max(errors) <= 24 and sum(errors) / len(errors) <= 6
                else:
                    matches_pixels = actual.tobytes() == expected.tobytes()
                if matches_pixels:
                    self.assertEqual(image.format, 'JPEG' if encoded_jpeg else 'PNG')
                    self.assertNotIn(274, image.getexif(), 'EXIF must not rotate exported pixels again')
                    matches.append((item['path'], encoded_jpeg))
        self.assertEqual(len(matches), 1, 'Expected one resource with physically rotated pixels')
        asset, encoded_jpeg = matches[0]
        self.assertTrue(asset.endswith('.jpg' if encoded_jpeg else '.png'))
        refs = self.references(result, asset)
        self.assertEqual(len(refs), 1)
        path, root, image = refs[0]
        self.assertEqual(image.tag, '{http://www.w3.org/1999/xhtml}img')
        self.assertIsNone(image.get('transform'))
        parents = {child: parent for parent in root.iter() for child in parent}
        parent = parents.get(image)
        while parent is not None:
            self.assertFalse(parent.tag.startswith('{%s}' % SVG))
            parent = parents.get(parent)
        return path, asset

    @staticmethod
    def bitmap_data(format='PNG', mode='RGB', exif=None):
        image = Image.new('RGB', (3, 2))
        image.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255),
                       (255, 255, 0), (255, 0, 255), (0, 255, 255)])
        image = image.convert(mode)
        if mode == 'RGBA':
            image.putpixel((0, 0), (255, 0, 0, 37))
        output = io.BytesIO()
        options = {'exif': exif} if exif else {}
        if format == 'WEBP': options['lossless'] = True
        image.save(output, format=format, **options)
        return output.getvalue()

    def test_raster_pixels_cover_resource_and_markup_in_both_exporters(self):
        for layout in self.fixtures:
            for format, mode in [('JPEG', 'RGB'), ('JPEG', 'CMYK'), ('PNG', 'RGBA'),
                                 ('PNG', 'RGB'), ('GIF', 'P'), ('WEBP', 'RGBA')]:
                data = self.bitmap_data(format, mode)
                media, ext, width, height = image_info(data)
                for role in ('cover', 'front', 'body'):
                    for angle in (90, 180, 270):
                        with self.subTest(layout=layout, format=format, mode=mode, role=role, angle=angle):
                            book, donor, settings = self.project(layout)
                            donor.illustrations[0] = replace(donor.illustrations[0], data=data,
                                media_type=media, ext=ext, width=width, height=height)
                            settings['images'][0].update(role=role, rotation=angle,
                                after_id=book.paragraphs[2].id)
                            snapshot = copy.deepcopy((donor, settings))
                            result = Epub(build_epub(book, donor, settings))
                            _, asset = self.assert_raster_rotation(result, data, angle)
                            if role == 'cover':
                                self.assertEqual(result.cover_path, asset)
                            self.assertEqual((donor, settings), snapshot)

    def test_raster_chapter_and_title_page_anchors_use_plain_images(self):
        for layout in self.fixtures:
            book, donor, settings = self.project(layout, local=True)
            anchors = [START, chapter_start(book.chapters[0].id), chapter_end(book.chapters[0].id)]
            if book.pages: anchors += [page_start(book.pages[0]['id']), page_end(book.pages[0]['id'])]
            for anchor in anchors:
                with self.subTest(layout=layout, anchor=anchor):
                    self.decision(settings, 'local_rotation').update(
                        role='body', rotation=90, after_id=anchor)
                    result = Epub(build_epub(book, donor, settings))
                    self.assert_raster_rotation(result, self.png_data, 90)

    def test_jpeg_exif_orientation_is_applied_once_before_user_rotation(self):
        # Row-major index orders for EXIF orientations, including reflections.
        orders = {1: [0, 1, 2, 3, 4, 5], 2: [2, 1, 0, 5, 4, 3],
                  3: [5, 4, 3, 2, 1, 0], 4: [3, 4, 5, 0, 1, 2],
                  5: [0, 3, 1, 4, 2, 5], 6: [3, 0, 4, 1, 5, 2],
                  7: [5, 2, 4, 1, 3, 0], 8: [2, 5, 1, 4, 0, 3]}
        for orientation, order in orders.items():
            exif = Image.Exif()
            exif[274] = orientation
            data = self.bitmap_data('JPEG', exif=exif)
            media, ext, width, height = image_info(data)
            with Image.open(io.BytesIO(data)) as source:
                pixels = list(source.convert('RGBA').get_flattened_data())
            oriented = Image.new('RGBA', (2, 3) if orientation >= 5 else (3, 2))
            oriented.putdata([pixels[index] for index in order])
            expected_buffer = io.BytesIO()
            oriented.save(expected_buffer, format='PNG')
            for layout in self.fixtures:
                for angle in (90, 180, 270):
                    with self.subTest(layout=layout, orientation=orientation, angle=angle):
                        book, donor, settings = self.project(layout)
                        donor.illustrations[0] = replace(donor.illustrations[0], data=data,
                            media_type=media, ext=ext, width=width, height=height)
                        settings['images'][0].update(role='cover', rotation=angle)
                        with patch('epub_merge.image_rotation.shutil.which', return_value=None):
                            result = Epub(build_epub(book, donor, settings))
                        self.assert_raster_rotation(result, expected_buffer.getvalue(), angle, jpeg=False)

    def test_animated_and_broken_rotated_bitmaps_fail_without_changing_originals(self):
        frames = [Image.new('RGB', (3, 2), color) for color in ('red', 'blue')]
        output = io.BytesIO()
        frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:],
                       duration=100, loop=0)
        for layout in self.fixtures:
            for data, error in [(output.getvalue(), '动图'), (self.png_data[:24], '无法旋转')]:
                with self.subTest(layout=layout, error=error):
                    media, ext, width, height = image_info(data)
                    book, donor, settings = self.project(layout)
                    donor.illustrations[0] = replace(donor.illustrations[0], data=data,
                        media_type=media, ext=ext, width=width, height=height)
                    settings['images'][0].update(role='cover', rotation=90)
                    snapshot = copy.deepcopy((donor, settings))
                    with self.assertRaisesRegex(BookError, error):
                        build_epub(book, donor, settings)
                    self.assertEqual((donor, settings), snapshot)
                    settings['images'][0]['rotation'] = 0
                    result = Epub(build_epub(book, donor, settings))
                    self.assertEqual(result.read(result.cover_path), data)


    @staticmethod
    def aligned_jpeg(mode='RGB', orientation=1):
        image = Image.new('RGB', (32, 16))
        colors = [(220, 20, 30), (20, 210, 40), (30, 40, 220), (210, 190, 20)]
        for y in range(image.height):
            for x in range(image.width):
                image.putpixel((x, y), colors[(x // 8 + y // 8) % len(colors)])
        image = image.convert(mode)
        exif = Image.Exif()
        exif[274] = orientation
        output = io.BytesIO()
        image.save(output, format='JPEG', quality=90, subsampling=0, exif=exif,
                   icc_profile=b'rotation-test-profile')
        return output.getvalue()

    def jpeg_project(self, data, angle=90, layout='generic'):
        book, donor, settings = self.project(layout)
        media, ext, width, height = image_info(data)
        donor.illustrations[0] = replace(donor.illustrations[0], data=data,
            media_type=media, ext=ext, width=width, height=height)
        settings['images'][0].update(role='cover', rotation=angle)
        return book, donor, settings

    @unittest.skipUnless(shutil.which('jpegtran'), 'Optional jpegtran is unavailable')
    def test_perfect_jpeg_rotation_preserves_coefficients_without_pixel_encoding(self):
        executable = shutil.which('jpegtran')
        for mode in ('RGB', 'L', 'CMYK'):
            for angle in (90, 180, 270):
                for layout in self.fixtures:
                    with self.subTest(mode=mode, angle=angle, layout=layout):
                        data = self.aligned_jpeg(mode)
                        project = self.jpeg_project(data, angle, layout)
                        with patch('epub_merge.image_rotation.ImageOps.exif_transpose',
                                   side_effect=AssertionError('Lossless rotation must not decode pixels')):
                            result = Epub(build_epub(*project))
                        rotated = result.read(result.cover_path)
                        self.assert_raster_rotation(result, data, angle, jpeg=True)
                        with Image.open(io.BytesIO(rotated)) as output:
                            self.assertEqual(output.mode, mode)
                            self.assertEqual(output.info.get('icc_profile'), b'rotation-test-profile')
                        # Canonical Huffman coding makes an inverse transform
                        # byte-identical iff the quantized coefficients survived.
                        common = [executable, '-copy', 'icc', '-perfect', '-optimize']
                        restored = subprocess.run(common + ['-rotate', str(360 - angle)],
                            input=rotated, capture_output=True, check=True).stdout
                        canonical = subprocess.run(common, input=data, capture_output=True, check=True).stdout
                        self.assertEqual(restored, canonical)

    @unittest.skipUnless(shutil.which('jpegtran'), 'Optional jpegtran is unavailable')
    def test_perfect_jpeg_composes_all_exif_orientations_without_reencoding(self):
        for orientation in range(1, 9):
            data = self.aligned_jpeg(orientation=orientation)
            with Image.open(io.BytesIO(data)) as source:
                # Pillow supplies the independent reference for all reflections.
                oriented = ImageOps.exif_transpose(source)
                buffer = io.BytesIO()
                oriented.save(buffer, format='PNG')
            for angle in (90, 180, 270):
                with self.subTest(orientation=orientation, angle=angle):
                    with patch('epub_merge.image_rotation.ImageOps.exif_transpose',
                               side_effect=AssertionError('Expected a combined lossless transform')):
                        result = Epub(build_epub(*self.jpeg_project(data, angle)))
                    self.assert_raster_rotation(result, buffer.getvalue(), angle, jpeg=True)

    @unittest.skipUnless(shutil.which('jpegtran'), 'Optional jpegtran is unavailable')
    def test_imperfect_jpeg_blocks_fall_back_without_cropping(self):
        data = self.bitmap_data('JPEG')  # 3 x 2: cannot rotate the full MCU perfectly.
        for angle in (90, 180, 270):
            with self.subTest(angle=angle):
                with patch('epub_merge.image_rotation.ImageOps.exif_transpose',
                           wraps=ImageOps.exif_transpose) as decoded:
                    result = Epub(build_epub(*self.jpeg_project(data, angle)))
                    decoded.assert_called_once()
                self.assert_raster_rotation(result, data, angle, jpeg=False)

    def test_missing_failed_timed_out_and_invalid_tool_output_use_png_fallback(self):
        data = self.aligned_jpeg()
        cases = {
            'missing': None,
            'failure': subprocess.CompletedProcess([], 1, b'', b'not perfect'),
            'bad output': subprocess.CompletedProcess([], 0, b'broken', b''),
            'wrong dimensions': subprocess.CompletedProcess([], 0, data, b''),
            'timeout': subprocess.TimeoutExpired('jpegtran', 30),
            'disappeared': FileNotFoundError('jpegtran'),
        }
        for name, response in cases.items():
            with self.subTest(reason=name):
                with patch('epub_merge.image_rotation.shutil.which',
                           return_value=None if name == 'missing' else '/tools/jpegtran'), \
                     patch('epub_merge.image_rotation.subprocess.run') as run, \
                     patch('epub_merge.image_rotation.ImageOps.exif_transpose',
                           wraps=ImageOps.exif_transpose) as decoded:
                    if isinstance(response, Exception):
                        run.side_effect = response
                    else:
                        run.return_value = response
                    result = Epub(build_epub(*self.jpeg_project(data)))
                    decoded.assert_called_once()
                    if name == 'missing':
                        run.assert_not_called()
                    else:
                        command = run.call_args.args[0]
                        self.assertIn('-perfect', command)
                        self.assertNotIn('-trim', command)
                    self.assert_raster_rotation(result, data, 90, jpeg=False)
                    with Image.open(io.BytesIO(result.read(result.cover_path))) as output:
                        self.assertEqual(output.info.get('icc_profile'), b'rotation-test-profile')

    def test_zero_and_ignored_jpegs_never_invoke_transform_tools(self):
        data = self.aligned_jpeg()
        for role, angle in [('cover', 0), ('ignore', 90)]:
            with self.subTest(role=role):
                book, donor, settings = self.jpeg_project(data, angle)
                settings['images'][0]['role'] = role
                with patch('epub_merge.image_rotation.subprocess.run',
                           side_effect=AssertionError('Unexpected transform')):
                    result = Epub(build_epub(book, donor, settings))
                if role == 'cover':
                    self.assertEqual(result.read(result.cover_path), data)

    @staticmethod
    def events(result, only_path=None):
        events = []
        for id_ in result.spine:
            item = result.manifest[id_]
            path = item['path']
            if item.get('media-type') != 'application/xhtml+xml' or (only_path and path != only_path):
                continue
            for node in ET.fromstring(result.read(path)).iter():
                tag = node.tag.rsplit('}', 1)[-1]
                if tag == 'p' or re.fullmatch(r'h[1-6]', tag):
                    text = ''.join(node.itertext()).strip()
                    if text: events.append(('text' if tag == 'p' else 'heading', text))
                elif tag in ('img', 'image'):
                    href = node.get('src') or node.get('href') or node.get('{%s}href' % XLINK)
                    if href: events.append(('image', result.resolve(path, href)))
        return events

    def test_initial_settings_explicitly_defaults_each_rotation_to_integer_zero(self):
        for layout, book in self.fixtures.items():
            with self.subTest(layout=layout):
                settings = initial_settings(book, self.art)
                self.assertTrue(settings['images'])
                for image in settings['images']:
                    self.assertIn('rotation', image)
                    self.assertIs(type(image['rotation']), int)
                    self.assertEqual(image['rotation'], 0)

    def test_valid_angles_and_missing_angle_are_accepted_for_every_role(self):
        for layout in self.fixtures:
            book, donor, settings = self.project(layout)
            item = settings['images'][0]
            for role in ('cover', 'front', 'body', 'ignore'):
                for angle in (None, 0, 90, 180, 270):
                    with self.subTest(layout=layout, role=role, angle=angle):
                        item.update(role=role, after_id=START)
                        if angle is None: item.pop('rotation', None)
                        else: item['rotation'] = angle
                        validate(book, donor, settings)
                        build_report(book, donor, settings)
                        result = Epub(build_epub(book, donor, settings))
                        if role == 'ignore':
                            self.assertFalse(any(result.read(v['path']) == donor.illustrations[0].data
                                                 for v in result.manifest.values()
                                                 if v.get('media-type', '').startswith('image/')))

    def test_invalid_rotations_are_rejected_including_ignored_images(self):
        invalid = [False, True, 0.0, 90.0, '0', '90', '', None, 45, -90, 360,
                   [], [90], {}, {'angle': 90}]
        for layout in self.fixtures:
            book, donor, settings = self.project(layout)
            for role in ('cover', 'front', 'body', 'ignore'):
                for angle in invalid:
                    for operation in (validate, build_epub, build_report):
                        with self.subTest(layout=layout, role=role, angle=angle, operation=operation.__name__):
                            settings['images'][0].update(role=role, after_id=START, rotation=angle)
                            with self.assertRaisesRegex(BookError, r'旋转|角度|rotation'):
                                operation(book, donor, settings)

    def test_legacy_missing_angles_keep_zero_rotation_resources_and_layout(self):
        for layout in self.fixtures:
            with self.subTest(layout=layout):
                book, donor, settings = self.project(layout, local=True)
                for item, role in zip(settings['images'], ('cover', 'front', 'body', 'body', 'body')):
                    item.update(role=role, after_id=book.paragraphs[2].id)
                zero = Epub(build_epub(book, donor, settings))
                legacy = copy.deepcopy(settings)
                for item in legacy['images']: item.pop('rotation')
                old = Epub(build_epub(book, donor, legacy))
                self.assertEqual(old.names, zero.names)
                self.assertEqual(old.manifest, zero.manifest)
                self.assertEqual(old.spine, zero.spine)
                self.assertEqual(old.cover_path, zero.cover_path)
                # OPF/NCX identifiers intentionally hash settings, including the optional field.
                for path in zero.names - {zero.opf_path}:
                    if not path.endswith('.ncx'):
                        self.assertEqual(old.read(path), zero.read(path), path)

    def test_cover_and_front_rotate_clockwise_in_both_exporters(self):
        for layout in self.fixtures:
            for angle in (90, 180, 270):
                with self.subTest(layout=layout, angle=angle):
                    book, donor, settings = self.project(layout)
                    for item, role in zip(settings['images'], ('cover', 'front')):
                        item.update(role=role, rotation=angle)
                    result = Epub(build_epub(book, donor, settings))
                    rendered = [self.assert_rotation(result, img.data, img.width, img.height, angle)
                                for img in donor.illustrations[:2]]
                    self.assertEqual(result.cover_path, rendered[0][1])
                    paths = [result.manifest[id_]['path'] for id_ in result.spine]
                    self.assertLess(paths.index(rendered[0][0]), paths.index(rendered[1][0]))

    def check_body_anchors(self, layout):
        for name in ('START', 'chapter_start', 'chapter_end', 'paragraph', 'last_paragraph'):
            for angle in (90, 180, 270):
                with self.subTest(layout=layout, anchor=name, angle=angle):
                    book, donor, settings = self.project(layout)
                    first, second = book.chapters[:2]
                    cases = {
                        'START': (START, None, book.paragraphs[0]),
                        'chapter_start': (chapter_start(second.id), first.paragraphs[-1], second.paragraphs[0]),
                        'chapter_end': (chapter_end(first.id), first.paragraphs[-1], second.paragraphs[0]),
                        'paragraph': (book.paragraphs[2].id, book.paragraphs[2], book.paragraphs[3]),
                        'last_paragraph': (first.paragraphs[-1].id, first.paragraphs[-1], second.paragraphs[0]),
                    }
                    anchor, before, after = cases[name]
                    settings['images'][2].update(role='body', after_id=anchor, rotation=angle)
                    result = Epub(build_epub(book, donor, settings))
                    img = donor.illustrations[2]
                    path, asset = self.assert_rotation(result, img.data, img.width, img.height, angle)
                    events = self.events(result)
                    pos = events.index(('image', asset))
                    if before: self.assertLess(events.index(('text', before.text)), pos)
                    self.assertLess(pos, events.index(('text', after.text)))
                    if name == 'chapter_start':
                        self.assertLess(events.index(('heading', second.title)), pos)
                    if name in ('chapter_end', 'last_paragraph'):
                        self.assertLess(pos, events.index(('heading', second.title)))
                    if layout == 'mojimoon':
                        target = after if name in ('START', 'chapter_start') else before
                        self.assertEqual(path, book.anchors[target.id]['path'])

    def test_generic_start_chapter_boundaries_and_paragraph_paths(self):
        self.check_body_anchors('generic')

    def test_preserved_start_chapter_boundaries_and_paragraph_paths(self):
        self.check_body_anchors('mojimoon')

    def test_title_page_start_and_end_surround_existing_content(self):
        for angle in (90, 180, 270):
            with self.subTest(angle=angle):
                book, donor, _ = self.project('mojimoon')
                page = book.pages[0]
                source = Epub(book.source_epub)
                content = source.read(page['path']).replace(b'</body>', '<p>扉页署名</p></body>'.encode())
                book = parse_text(rewrite_epub(book.source_epub, {page['path']: content}), 'base.epub')
                settings = initial_settings(book, donor)
                for item in settings['images']: item.update(role='ignore', confirmed=True, rotation=0)
                for item, anchor in zip(settings['images'][2:], (page_start(page['id']), page_end(page['id']))):
                    item.update(role='body', after_id=anchor, rotation=angle)
                result = Epub(build_epub(book, donor, settings))
                assets = []
                for img in donor.illustrations[2:]:
                    path, asset = self.assert_rotation(result, img.data, img.width, img.height, angle)
                    self.assertEqual(path, page['path'])
                    assets.append(asset)
                events = self.events(result, page['path'])
                self.assertLess(events.index(('image', assets[0])), events.index(('text', '扉页署名')))
                self.assertLess(events.index(('text', '扉页署名')), events.index(('image', assets[1])))
                self.assertIn(page['path'], [result.manifest[id_]['path'] for id_ in result.spine])

    def test_same_anchor_mixes_original_and_local_art_in_saved_order(self):
        for layout in self.fixtures:
            for alias in (False, True):
                with self.subTest(layout=layout, coincident_boundary=alias):
                    book, donor, settings = self.project(layout, local=True)
                    chapter = book.chapters[0]
                    anchor = chapter_end(chapter.id) if alias else book.paragraphs[2].id
                    ids = ['local_rotation', 'i0004', 'i0003']
                    settings['image_order'] = ['i0001', 'i0002'] + ids
                    for id_, angle in zip(ids, (90, 180, 270)):
                        self.decision(settings, id_).update(role='body', after_id=anchor, rotation=angle)
                    if alias: self.decision(settings, 'i0004')['after_id'] = chapter.paragraphs[-1].id
                    restored = json.loads(json.dumps(settings))
                    result = Epub(build_epub(book, donor, restored))
                    originals = {img.id: (img.data, img.width, img.height) for img in donor.illustrations}
                    originals['local_rotation'] = (self.png_data, 24, 32)
                    events = self.events(result)
                    positions = []
                    for id_, angle in zip(ids, (90, 180, 270)):
                        _, asset = self.assert_rotation(result, *originals[id_], angle)
                        positions.append(events.index(('image', asset)))
                    self.assertEqual(positions, sorted(positions))
                    rows = ReportTable(build_report(book, donor, restored)).rows[1:]
                    self.assertEqual([row[0].split()[0] for row in rows if row[0].split()[0] in ids], ids)

    def test_added_png_angles_and_original_dimensions_survive_json_restore(self):
        for layout in self.fixtures:
            for role, angle in (('cover', 90), ('front', 180), ('body', 270)):
                with self.subTest(layout=layout, role=role, angle=angle):
                    book, donor, settings = self.project(layout, local=True)
                    anchor = page_start(book.pages[0]['id']) if book.pages else chapter_start(book.chapters[0].id)
                    self.decision(settings, 'local_rotation').update(role=role, rotation=angle, after_id=anchor)
                    restored = json.loads(json.dumps(settings, ensure_ascii=False))
                    result = Epub(build_epub(book, donor, restored))
                    self.assert_rotation(result, self.png_data, 24, 32, angle)
                    prepared = prepare_images(donor, restored)
                    self.assertEqual([(img.width, img.height) for img in prepared.illustrations],
                                     [(img.width, img.height) for img in donor.illustrations] + [(24, 32)])
                    self.assertEqual(prepared.illustrations[-1].data, self.png_data)
                    self.assertEqual(restored, settings)
                    for invalid in (True, 90.0, '90', None):
                        self.decision(restored, 'local_rotation')['rotation'] = invalid
                        with self.subTest(invalid=invalid), self.assertRaisesRegex(BookError, r'旋转|角度|rotation'):
                            build_epub(book, donor, restored)

    def test_report_has_angle_column_for_every_role_and_legacy_zero(self):
        for layout in self.fixtures:
            with self.subTest(layout=layout):
                book, donor, settings = self.project(layout, local=True)
                for item, role, angle in zip(settings['images'], ('cover', 'front', 'body', 'ignore'), (0, 90, 180, 270)):
                    item.update(role=role, rotation=angle, after_id=START)
                self.decision(settings, 'local_rotation').pop('rotation')
                report = build_report(book, donor, json.loads(json.dumps(settings)))
                rows = ReportTable(report).rows
                columns = [i for i, cell in enumerate(rows[0]) if re.search(r'旋转|角度', cell)]
                self.assertEqual(len(columns), 1, 'Report must have a dedicated rotation column')
                for item in settings['images']:
                    row = next(row for row in rows[1:] if row[0].split()[0] == item['id'])
                    self.assertRegex(row[columns[0]], r'(?<!\d)%d(?!\d)' % item.get('rotation', 0))
                self.assertIn('本地图片：旋转新图.png', report)

    def test_nonzero_rotation_with_invalid_dimensions_raises_clear_book_error(self):
        # Inject bad metadata at the export boundary; format parsing has its own suite.
        for layout in self.fixtures:
            for size in ((0, 1080), (720, 0), (-1, 1080), (None, 1080), (float('nan'), 1080), (720, float('inf'))):
                for role in ('cover', 'front', 'body'):
                    for angle in (90, 180, 270):
                        with self.subTest(layout=layout, size=size, role=role, angle=angle):
                            book, donor, settings = self.project(layout)
                            img = replace(donor.illustrations[0], width=size[0], height=size[1])
                            donor = replace(donor, illustrations=[img] + donor.illustrations[1:])
                            settings['images'][0].update(role=role, after_id=START, rotation=angle)
                            with self.assertRaisesRegex(BookError, r'尺寸|宽|高|dimension'):
                                build_epub(book, donor, settings)

    def test_zero_or_missing_rotation_keeps_legacy_unknown_dimension_fallback(self):
        for layout in self.fixtures:
            for angle in (None, 0):
                for role in ('cover', 'front', 'body'):
                    with self.subTest(layout=layout, angle=angle, role=role):
                        book, donor, settings = self.project(layout)
                        img = replace(donor.illustrations[0], width=0, height=0)
                        donor = replace(donor, illustrations=[img] + donor.illustrations[1:])
                        settings['images'][0].update(role=role, after_id=START)
                        if angle is None: settings['images'][0].pop('rotation')
                        result = Epub(build_epub(book, donor, settings))
                        refs = self.references(result, self.asset(result, img.data))
                        self.assertEqual(len(refs), 1)
                        self.assertEqual(refs[0][2].tag.rsplit('}', 1)[-1], 'img')

    def test_svg_manifest_property_is_appended_without_losing_existing_tokens(self):
        book, donor, _ = self.project('mojimoon')
        source = Epub(book.source_epub)
        paths = [book.anchors[book.paragraphs[2].id]['path'], book.pages[0]['path']]
        for item in source.opf.iter('{%s}item' % OPF):
            if source.resolve(source.opf_path, item.get('href', '')) in paths:
                item.set('properties', 'mathml scripted')
        book = parse_text(rewrite_epub(book.source_epub, {source.opf_path: ET.tostring(source.opf)}), 'base.epub')
        settings = initial_settings(book, donor)
        for item in settings['images']: item.update(role='ignore', confirmed=True, rotation=0)
        settings['images'][2].update(role='body', after_id=book.paragraphs[2].id, rotation=90)
        settings['images'][3].update(role='body', after_id=page_start(book.pages[0]['id']), rotation=270)
        result = Epub(build_epub(book, donor, settings))
        for img, angle in zip(donor.illustrations[2:], (90, 270)):
            self.assert_rotation(result, img.data, img.width, img.height, angle)
        for path in paths:
            props = next(v for v in result.manifest.values() if v['path'] == path).get('properties', '').split()
            self.assertTrue({'mathml', 'scripted', 'svg'}.issubset(props))
            self.assertEqual(props.count('svg'), 1)
        untouched = book.anchors[book.chapters[1].paragraphs[0].id]['path']
        self.assertEqual(next(v for v in result.manifest.values() if v['path'] == untouched).get('properties', ''),
                         next(v for v in source.manifest.values() if v['path'] == untouched).get('properties', ''))

    def test_exports_do_not_mutate_settings_session_prose_css_or_source_pixels(self):
        for layout in self.fixtures:
            with self.subTest(layout=layout):
                book, donor, settings = self.project(layout, local=True)
                for item, role in zip(settings['images'], ('cover', 'front', 'body', 'body', 'body')):
                    item.update(role=role, after_id=book.paragraphs[2].id, rotation=90)
                original_book, original_donor = copy.deepcopy((book, donor))
                for angle in (90, 270, 90):
                    for item in settings['images']: item['rotation'] = angle
                    snapshot = copy.deepcopy(settings)
                    validate(book, donor, settings)
                    build_report(book, donor, settings)
                    result = Epub(build_epub(book, donor, settings))
                    for img in donor.illustrations:
                        self.assert_rotation(result, img.data, img.width, img.height, angle)
                    self.assert_rotation(result, self.png_data, 24, 32, angle)
                    self.assertEqual((book, donor, settings), (original_book, original_donor, snapshot))
                    # Illustrated mojimoon books are intentionally rejected as new
                    # text bases; verify their untouched prose bytes below instead.
                    if not book.source_epub:
                        self.assertEqual([p.text for p in parse_text(build_epub(book, donor, settings), 'out.epub').paragraphs],
                                         [p.text for p in book.paragraphs])
                    if book.source_epub:
                        source = Epub(book.source_epub)
                        for paragraph in book.paragraphs:
                            ref = book.anchors[paragraph.id]
                            self.assertIn(source.read(ref['path'])[ref['start']:ref['end']], result.read(ref['path']))
                        for path in source.names:
                            if path.endswith('.css'): self.assertEqual(result.read(path), source.read(path))

    def test_prepare_epub_is_idempotent_and_preserves_rotated_svg_resources(self):
        for layout in self.fixtures:
            with self.subTest(layout=layout):
                book, donor, settings = self.project(layout, local=True)
                for item, role, angle in zip(settings['images'], ('cover', 'front', 'body', 'body', 'body'), (90, 180, 270, 90, 180)):
                    item.update(role=role, rotation=angle, after_id=chapter_start(book.chapters[0].id))
                if book.pages: self.decision(settings, 'local_rotation')['after_id'] = page_end(book.pages[0]['id'])
                merged = build_epub(book, donor, settings)
                once = prepare_epub(merged)
                versions = [Epub(merged), Epub(once), Epub(prepare_epub(once))]
                for result in versions[1:]:
                    self.assertEqual(versions[0].names, result.names)
                    for path in result.names: self.assertEqual(versions[0].read(path), result.read(path), path)
                for img, angle in zip(donor.illustrations, (90, 180, 270, 90)):
                    self.assert_rotation(versions[-1], img.data, img.width, img.height, angle)
                self.assert_rotation(versions[-1], self.png_data, 24, 32, 180)

    def test_http_rejects_invalid_angles_and_image_endpoint_keeps_original_bytes(self):
        server = make_server(0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = 'http://127.0.0.1:%d' % server.server_address[1]
        try:
            with patch('epub_merge.server.Handler.log_message'):
                for layout in self.fixtures:
                    book, donor, settings = self.project(layout)
                    original = copy.deepcopy((book, donor))
                    token = server.state.add(book, donor)
                    for angle in (False, 90.0, '90', None, 45, []):
                        settings['images'][0]['rotation'] = angle  # The ignored role is validated too.
                        for endpoint in ('export', 'report'):
                            with self.subTest(layout=layout, angle=angle, endpoint=endpoint):
                                request = urllib.request.Request(base + '/api/' + endpoint,
                                    json.dumps({'session': token, 'settings': settings}).encode(),
                                    {'Content-Type': 'application/json'})
                                with self.assertRaises(urllib.error.HTTPError) as error:
                                    opener.open(request, timeout=5)
                                with error.exception as response:
                                    self.assertEqual(response.code, 400)
                                    self.assertRegex(json.load(response)['error'], r'旋转|角度|rotation')
                    settings['images'][0].update(role='cover', rotation=90)
                    for endpoint in ('export', 'report'):
                        request = urllib.request.Request(base + '/api/' + endpoint,
                            json.dumps({'session': token, 'settings': settings}).encode(),
                            {'Content-Type': 'application/json'})
                        with opener.open(request, timeout=5) as response:
                            data = response.read()
                            self.assertEqual(response.status, 200)
                        if endpoint == 'export':
                            img = donor.illustrations[0]
                            self.assert_rotation(Epub(data), img.data, img.width, img.height, 90)
                    img = donor.illustrations[0]
                    with opener.open(base + '/api/image/' + token + '/' + img.id, timeout=5) as response:
                        self.assertEqual(response.read(), img.data)
                        self.assertEqual(response.headers.get_content_type(), img.media_type)
                    self.assertEqual(server.state.get(token), original)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
