"""Synthetic image headers only: no image libraries, network or private fixtures."""
import hashlib
import random
import struct
import unittest

from epub_merge.books import BookError, image_info


def segment(marker, payload):
    return b'\xff' + bytes([marker]) + struct.pack('>H', len(payload) + 2) + payload


def jpeg_frame(width=640, height=480, marker=0xC0):
    return segment(marker, struct.pack('>BHHB', 8, height, width, 1) + b'\x01\x11\x00')


def exif(orientation=1, endian='<', tag=0x0112, kind=3, count=1):
    entry = struct.pack(endian + 'HHI', tag, kind, count)
    entry += struct.pack(endian + 'H', orientation) + b'\x00\x00'
    tiff = (b'II' if endian == '<' else b'MM') + struct.pack(endian + 'HI', 42, 8)
    tiff += struct.pack(endian + 'H', 1) + entry + b'\x00' * 4
    return segment(0xE1, b'Exif\x00\x00' + tiff)


def chunk(kind, payload):
    return kind + struct.pack('<I', len(payload)) + payload + (b'\x00' if len(payload) & 1 else b'')


def webp(*chunks):
    body = b'WEBP' + b''.join(chunks)
    return b'RIFF' + struct.pack('<I', len(body)) + body


def vp8(width, height):
    return b'\x10\x00\x00\x9d\x01\x2a' + struct.pack('<HH', width, height)


def vp8l(width, height, alpha=0, version=0):
    bits = width - 1 | ((height - 1) << 14) | (alpha << 28) | (version << 29)
    return b'\x2f' + struct.pack('<I', bits)


def vp8x(width, height, flags=0):
    return bytes([flags]) + b'\x00' * 3 + (width - 1).to_bytes(3, 'little') + (height - 1).to_bytes(3, 'little')


def svg(attributes='', content=''):
    return ('<svg xmlns="http://www.w3.org/2000/svg" ' + attributes + '>' + content + '</svg>').encode()


class RasterDimensionTests(unittest.TestCase):
    def test_png_and_gif_keep_header_dimensions(self):
        png = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' + struct.pack('>II', 720, 1080)
        gif = b'GIF89a' + struct.pack('<HH', 320, 240)
        self.assertEqual(image_info(png), ('image/png', 'png', 720, 1080))
        self.assertEqual(image_info(gif), ('image/gif', 'gif', 320, 240))

    def test_zero_and_excessive_raster_geometry_is_unknown(self):
        for width, height in [(0, 80), (80, 0), (0xFFFFFFFF, 80)]:
            data = b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' + struct.pack('>II', width, height)
            with self.subTest(width=width, height=height):
                self.assertEqual(image_info(data)[2:], (0, 0))
        self.assertEqual(image_info(b'GIF87a' + struct.pack('<HH', 0, 10))[2:], (0, 0))

    def test_declared_media_type_cannot_override_sniffing(self):
        self.assertEqual(image_info(webp(chunk(b'VP8L', vp8l(19, 27))), 'image/jpeg'),
                         ('image/webp', 'webp', 19, 27))
        with self.assertRaises(BookError):
            image_info(b'not an image', 'image/png')


class WebPDimensionTests(unittest.TestCase):
    def test_lossy_keyframe_dimensions_and_scale_bits(self):
        data = webp(chunk(b'VP8 ', vp8(1234 | 0x8000, 567 | 0xC000)))
        self.assertEqual(image_info(data), ('image/webp', 'webp', 1234, 567))

    def test_lossless_packed_dimensions_and_alpha(self):
        for width, height, alpha in [(1, 1, 0), (123, 456, 0), (16384, 16384, 1)]:
            with self.subTest(width=width, height=height, alpha=alpha):
                self.assertEqual(image_info(webp(chunk(b'VP8L', vp8l(width, height, alpha))))[2:],
                                 (width, height))

    def test_extended_canvas_and_24_bit_limit(self):
        for width, height, flags in [(1, 1, 0), (70000, 60000, 0x10), (1 << 24, 255, 0x3E),
                                     (65535, 65537, 0)]:
            with self.subTest(width=width, height=height):
                self.assertEqual(image_info(webp(chunk(b'VP8X', vp8x(width, height, flags))))[2:],
                                 (width, height))

    def test_extended_canvas_area_must_fit_the_container_limit(self):
        for width, height in [(65536, 65536), (1 << 24, 1 << 24)]:
            with self.subTest(width=width, height=height):
                self.assertEqual(image_info(webp(chunk(b'VP8X', vp8x(width, height))))[2:], (0, 0))

    def test_animation_uses_canvas_not_first_frame_or_frame_offset(self):
        frame = (b'\x05\x00\x00\x07\x00\x00' + (200 - 1).to_bytes(3, 'little')
                 + (100 - 1).to_bytes(3, 'little') + b'\x64\x00\x00\x00'
                 + chunk(b'VP8 ', vp8(200, 100)))
        data = webp(chunk(b'VP8X', vp8x(1200, 800, 0x02)),
                    chunk(b'ANIM', b'\x00' * 6), chunk(b'ANMF', frame))
        self.assertEqual(image_info(data)[2:], (1200, 800))

    def test_chunk_padding_and_unknown_chunks_are_respected(self):
        data = webp(chunk(b'JUNK', b'x'), chunk(b'VP8L', vp8l(43, 67)))
        self.assertEqual(image_info(data)[2:], (43, 67))
        embedded_header = chunk(b'VP8X', vp8x(800, 600))
        self.assertEqual(image_info(webp(chunk(b'JUNK', embedded_header)))[2:], (0, 0))

    def test_malformed_chunk_headers_and_riff_bounds_are_unknown(self):
        valid = webp(chunk(b'VP8X', vp8x(123, 456)))
        cases = [
            b'RIFF\x04\x00\x00\x00WEBP',
            b'RIFF\x03\x00\x00\x00WEBP',
            b'RIFF\xff\xff\xff\xffWEBP',
            valid[:-1],
            valid[:4] + struct.pack('<I', len(valid) - 9) + valid[8:],
            webp(b'VP8X' + struct.pack('<I', 0xFFFFFFFF) + b'\x00' * 10),
            webp(b'VP8L' + struct.pack('<I', 5) + vp8l(10, 20)),  # Missing odd-size padding.
            webp(chunk(b'VP8X', vp8x(10, 20)[:-1])),
            webp(chunk(b'VP8X', vp8x(10, 20) + b'\x00')),
            webp(chunk(b'JUNK', b''), chunk(b'VP8X', vp8x(10, 20))),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(image_info(data)[2:], (0, 0))

    def test_invalid_bitstream_headers_are_unknown(self):
        cases = [
            chunk(b'VP8 ', vp8(0, 20)),
            chunk(b'VP8 ', b'\x11' + vp8(10, 20)[1:]),  # Interframe, not a keyframe.
            chunk(b'VP8 ', vp8(10, 20)[:3] + b'bad' + vp8(10, 20)[6:]),
            chunk(b'VP8 ', vp8(10, 20)[:-1]),
            chunk(b'VP8L', b'\x30' + vp8l(10, 20)[1:]),
            chunk(b'VP8L', vp8l(10, 20, version=1)),
            chunk(b'VP8L', vp8l(10, 20)[:-1]),
            chunk(b'VP8X', vp8x(10, 20, 0x01)),
            chunk(b'VP8X', vp8x(10, 20, 0x40)),
            chunk(b'VP8X', vp8x(10, 20, 0x80)),
            chunk(b'VP8X', b'\x00\x01' + vp8x(10, 20)[2:]),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(image_info(webp(data))[2:], (0, 0))


class JPEGDimensionTests(unittest.TestCase):
    def test_sof_variants_and_unoriented_dimensions(self):
        for marker in [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF]:
            with self.subTest(marker=marker):
                self.assertEqual(image_info(b'\xff\xd8' + jpeg_frame(marker=marker) + b'\xff\xd9'),
                                 ('image/jpeg', 'jpg', 640, 480))

    def test_all_exif_orientations_in_both_byte_orders(self):
        for endian in ['<', '>']:
            for orientation in range(1, 9):
                expected = (480, 640) if orientation >= 5 else (640, 480)
                for after_frame in [False, True]:
                    metadata, frame = exif(orientation, endian), jpeg_frame()
                    data = b'\xff\xd8' + (frame + metadata if after_frame else metadata + frame) + b'\xff\xd9'
                    before = hashlib.sha256(data).digest()
                    with self.subTest(endian=endian, orientation=orientation, after_frame=after_frame):
                        self.assertEqual(image_info(data)[2:], expected)
                        self.assertEqual(hashlib.sha256(data).digest(), before)

    def test_only_short_count_one_orientation_is_used(self):
        for kwargs in [dict(orientation=0), dict(orientation=9), dict(orientation=65535),
                       dict(kind=4), dict(kind=1), dict(count=0), dict(count=2), dict(count=0xFFFFFFFF)]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(image_info(b'\xff\xd8' + exif(**kwargs) + jpeg_frame())[2:], (0, 0))

    def test_missing_orientation_and_non_exif_app1_are_ignored(self):
        for metadata in [exif(8, tag=0x0100), segment(0xE1, b'not EXIF'), segment(0xE1, b'Exif\x00x')]:
            with self.subTest(metadata=metadata):
                self.assertEqual(image_info(b'\xff\xd8' + metadata + jpeg_frame())[2:], (640, 480))

    def test_exif_offsets_counts_and_truncation_are_checked(self):
        valid_tiff = exif(6)[10:]
        cases = [
            b'', b'II', b'ZZ' + valid_tiff[2:], b'II\x2b\x00' + valid_tiff[4:],
            b'II\x2a\x00\x00\x00\x00\x00',
            b'II\x2a\x00\xff\xff\xff\xff',
            b'II\x2a\x00\x08\x00\x00\x00\xff\xff',
            valid_tiff[:-1], valid_tiff[:15],
        ]
        for tiff in cases:
            data = b'\xff\xd8' + segment(0xE1, b'Exif\x00\x00' + tiff) + jpeg_frame()
            with self.subTest(tiff=tiff):
                self.assertEqual(image_info(data)[2:], (0, 0))

    def test_thumbnail_ifd_and_unrelated_tag_offsets_are_not_followed(self):
        # IFD0 has an unrelated tag with an impossible count/offset and a safe Orientation.
        tiff = b'II\x2a\x00\x08\x00\x00\x00\x02\x00'
        tiff += struct.pack('<HHII', 0x8769, 4, 0xFFFFFFFF, 0xFFFFFFFF)
        tiff += struct.pack('<HHIHH', 0x0112, 3, 1, 6, 0)
        tiff += struct.pack('<I', 38)
        tiff += b'\x01\x00' + struct.pack('<HHIHH', 0x0112, 3, 1, 1, 0) + b'\x00' * 4
        data = b'\xff\xd8' + segment(0xE1, b'Exif\x00\x00' + tiff) + jpeg_frame()
        self.assertEqual(image_info(data)[2:], (480, 640))

    def test_ambiguous_orientation_is_unknown(self):
        for second in [exif(1), exif(8), segment(0xE1, b'Exif\x00\x00bad')]:
            data = b'\xff\xd8' + exif(6) + jpeg_frame() + second
            with self.subTest(second=second):
                self.assertEqual(image_info(data)[2:], (0, 0))
        self.assertEqual(image_info(b'\xff\xd8' + exif(6) + exif(6) + jpeg_frame())[2:], (480, 640))
        entry = struct.pack('<HHIHH', 0x0112, 3, 1, 6, 0)
        tiff = b'II\x2a\x00\x08\x00\x00\x00\x02\x00' + entry * 2 + b'\x00' * 4
        self.assertEqual(image_info(b'\xff\xd8' + segment(0xE1, b'Exif\x00\x00' + tiff) + jpeg_frame())[2:], (0, 0))

    def test_scan_data_and_post_eoi_metadata_are_not_read(self):
        for terminator in [b'\xff\xda', b'\xff\xd9']:
            data = b'\xff\xd8' + jpeg_frame() + terminator + exif(6)
            with self.subTest(terminator=terminator):
                self.assertEqual(image_info(data)[2:], (640, 480))
        # Marker fill bytes and standalone TEM/restart markers must not desynchronize parsing.
        data = b'\xff\xd8\xff\xff\x01\xff\xd0' + exif(8) + jpeg_frame()
        self.assertEqual(image_info(data)[2:], (480, 640))

    def test_malformed_segments_fail_closed_without_struct_or_index_errors(self):
        cases = [
            b'', b'\xff', b'\xff\xff', b'\xff\xe1', b'\xff\xe1\x00',
            b'\xff\xe1\x00\x00', b'\xff\xe1\x00\x01', b'\xff\xe1\xff\xffabc',
            segment(0xC0, b'\x08\x00\x10\x00'),
            b'\xff\xc0\x00\x08\x08\x00\x10\x00\x20',
            jpeg_frame() + b'\xff\xe1\xff\xff',
            jpeg_frame(0, 480), jpeg_frame(640, 0),
        ]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(image_info(b'\xff\xd8' + body)[2:], (0, 0))


class SVGDimensionTests(unittest.TestCase):
    def assertDimensions(self, attributes, expected):
        info = image_info(svg(attributes, '<rect width="10" height="10"/>'))
        self.assertEqual(info[:2], ('image/svg+xml', 'svg'))
        self.assertAlmostEqual(info[2], expected[0])
        self.assertAlmostEqual(info[3], expected[1])

    def test_unitless_pixels_decimals_and_exponents(self):
        for attributes, expected in [
            ('width="720" height="1080"', (720, 1080)),
            ('width="720px" height="1080px"', (720, 1080)),
            ('width="+12.5px" height=".25px"', (12.5, .25)),
            ('width="1.2e2" height="6E1px"', (120, 60)),
            ('width=" 720px " height="&#x9;1080px&#xA;"', (720, 1080)),
        ]:
            with self.subTest(attributes=attributes):
                self.assertDimensions(attributes, expected)

    def test_absolute_units_use_css_96_dpi(self):
        for width, height in [('1in', '1.5in'), ('2.54cm', '3.81cm'), ('25.4mm', '38.1mm'),
                              ('101.6Q', '152.4Q'), ('72pt', '108pt'), ('6pc', '9pc')]:
            with self.subTest(width=width, height=height):
                self.assertDimensions('width="%s" height="%s"' % (width, height), (96, 144))

    def test_viewbox_only_and_valid_separators(self):
        for viewbox in ['0 0 720 1080', '-10,-20,720,1080', '0, 0 720,1080', '+1e1 -2e1 7.2e2 1.08e3',
                        ' 0&#x9;0,720&#xA;1080 ']:
            with self.subTest(viewbox=viewbox):
                self.assertDimensions('viewBox="%s"' % viewbox, (720, 1080))

    def test_one_absolute_dimension_uses_viewbox_ratio(self):
        for attributes, expected in [
            ('width="200px" viewBox="0 0 800 600"', (200, 150)),
            ('height="2in" viewBox="0 0 800 600"', (256, 192)),
            ('width="200px" height="auto" viewBox="0 0 800 600"', (200, 150)),
            ('width="50%" height="300px" viewBox="0 0 800 600"', (400, 300)),
        ]:
            with self.subTest(attributes=attributes):
                self.assertDimensions(attributes, expected)

    def test_explicit_dimensions_take_precedence_over_viewbox(self):
        self.assertDimensions('width="200" height="400" viewBox="0 0 800 600"', (200, 400))

    def test_percentages_are_never_treated_as_pixels(self):
        self.assertDimensions('width="100%" height="50%"', (0, 0))
        self.assertDimensions('width="100%" height="200"', (0, 0))
        self.assertDimensions('width="100%" height="50%" viewBox="0 0 800 600"', (800, 600))

    def test_unknown_relative_lengths_and_css_overrides_are_unknown(self):
        for attributes in ['', 'width="400"', 'height="200"',
                           'width="2em" height="3em" viewBox="0 0 800 600"',
                           'width="10vw" height="20vh"',
                           'width="400" height="200" style="width:800px;height:600px"']:
            with self.subTest(attributes=attributes):
                self.assertDimensions(attributes, (0, 0))

    def test_invalid_and_hostile_lengths_are_unknown(self):
        values = ['0', '-1', 'NaN', 'Infinity', '1e999', '1e-999', '1e30px', '0%', '-1%',
                  'calc(2px + 1px)', '1 px', '12garbage', '', '&#160;100', '9' * 1000]
        for value in values:
            with self.subTest(value=value[:30]):
                self.assertDimensions('width="%s" height="100" viewBox="0 0 800 600"' % value, (0, 0))
        self.assertDimensions('width="1e6in" height="1in"', (0, 0))

    def test_invalid_and_hostile_viewboxes_are_unknown(self):
        values = ['0 0 0 20', '0 0 20 -1', '0 0 NaN 20', '0 0 1e999 20',
                  '0 0 1e-999 20', '0 0 1e30 20', '1e999 0 20 30', '0 0 20',
                  '0 0 20 30 40', '0,,0,20,30', ',0 0 20 30', '0 0 20 30,',
                  '0&#160;0&#160;20&#160;30', '1' * 1000]
        for value in values:
            with self.subTest(value=value[:30]):
                self.assertDimensions('viewBox="%s"' % value, (0, 0))
        self.assertDimensions('width="100" viewBox="0 0 .000001 16000000"', (0, 0))
        self.assertDimensions('viewbox="0 0 800 600"', (0, 0))

    def test_existing_active_content_and_external_reference_checks_remain(self):
        for tag in ['script', 'foreignObject', 'iframe', 'use', 'image', 'style']:
            for dimensions in ['width="200" height="100"', 'width="bad"']:
                with self.subTest(tag=tag, dimensions=dimensions), self.assertRaises(BookError):
                    image_info(svg(dimensions, '<%s/>' % tag))
        for attributes in ['onload="alert(1)"', 'href="other.svg"', 'src="file.png"',
                           'xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="#id"',
                           'fill="URL(https://example.invalid/paint)"']:
            with self.subTest(attributes=attributes), self.assertRaises(BookError):
                image_info(svg('width="200" height="100"', '<rect ' + attributes + '/>'))
        with self.assertRaises(BookError):
            image_info(b'<!DOCTYPE svg [<!ENTITY x "bad">]><svg>&x;</svg>')
        with self.assertRaises(BookError):
            image_info(b'<not-svg><svg/></not-svg>')


class MalformedHeaderTests(unittest.TestCase):
    def test_deterministic_random_binary_headers_never_leak_parser_errors(self):
        generator = random.Random(104729)
        for size in range(128):
            payload = generator.randbytes(size)
            for data in [b'\xff\xd8' + payload, webp(payload)]:
                with self.subTest(size=size, signature=data[:4]):
                    self.assertEqual(image_info(data)[2:], (0, 0))

    def test_every_truncated_jpeg_and_webp_prefix_is_safe(self):
        cases = [b'\xff\xd8' + exif(6) + jpeg_frame() + b'\xff\xd9',
                 webp(chunk(b'VP8X', vp8x(123, 456))),
                 webp(chunk(b'VP8L', vp8l(123, 456))),
                 webp(chunk(b'VP8 ', vp8(123, 456)))]
        for data in cases:
            for end in range(len(data)):
                with self.subTest(signature=data[:4], end=end):
                    try:
                        info = image_info(data[:end])
                    except BookError:
                        continue  # Too short to identify a supported format at all.
                    self.assertEqual(len(info), 4)
                    self.assertIn(info[2:], [(0, 0), (480, 640)])


if __name__ == '__main__':
    unittest.main()
