"""Bake bitmap quarter turns into export copies; retain SVG presentation."""
import html
import io
import math
import shutil
import subprocess
import warnings
from dataclasses import replace

from PIL import Image, ImageOps

from .books import BookError


def rotation_value(decision):
    value = decision.get('rotation', 0)
    if type(value) is not int or value not in (0, 90, 180, 270):
        raise BookError('图片旋转角度无效，只能是整数 0、90、180 或 270。')
    return value


def valid_dimensions(image):
    return all(type(n) in (int, float) and 1e-6 <= n <= (1 << 24) and math.isfinite(n)
               for n in (image.width, image.height))


# Linear parts of the eight EXIF orientations; translations follow from size.
_ORIENTATIONS = {
    1: (1, 0, 0, 1), 2: (-1, 0, 0, 1), 3: (-1, 0, 0, -1),
    4: (1, 0, 0, -1), 5: (0, 1, 1, 0), 6: (0, -1, 1, 0),
    7: (0, -1, -1, 0), 8: (0, 1, -1, 0),
}
_JPEG_TRANSFORMS = {
    1: (), 2: ('-flip', 'horizontal'), 3: ('-rotate', '180'),
    4: ('-flip', 'vertical'), 5: ('-transpose',), 6: ('-rotate', '90'),
    7: ('-transverse',), 8: ('-rotate', '270'),
}


def _lossless_jpeg(data, source, angle):
    """Try a perfect DCT transform, returning None for the pixel fallback.

    Compose EXIF and user rotation into one transform: applying them separately
    could reject partial blocks even when their combined transform is perfect.
    Never trim, and never rely on a reader applying EXIF after the transform.
    """
    executable = shutil.which('jpegtran')
    orientation = source.getexif().get(274, 1)
    if executable is None or orientation not in _ORIENTATIONS:
        return None
    a, b, c, d = _ORIENTATIONS[{90: 6, 180: 3, 270: 8}[angle]]
    e, f, g, h = _ORIENTATIONS[orientation]
    combined = (a * e + b * g, a * f + b * h, c * e + d * g, c * f + d * h)
    transform = next(key for key, matrix in _ORIENTATIONS.items() if matrix == combined)
    width, height = source.size[::-1] if orientation >= 5 else source.size
    expected_size = (height, width) if angle in (90, 270) else (width, height)
    try:
        # Copy ICC colour profiles, omitting stale EXIF/XMP orientation and size.
        result = subprocess.run(
            [executable, '-copy', 'icc', '-perfect', '-strict', '-optimize',
             *_JPEG_TRANSFORMS[transform]],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            return None
        with Image.open(io.BytesIO(result.stdout)) as output:
            if output.format != 'JPEG' or output.size != expected_size or output.getexif().get(274, 1) != 1:
                return None
        return result.stdout
    except (OSError, ValueError, SyntaxError, subprocess.TimeoutExpired):
        return None


def _rotate_bitmap(image, angle):
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(image.data)) as source:
            if getattr(source, 'n_frames', 1) != 1:
                raise BookError('图片 %s 是动图，请改用静态图片后再旋转。' % image.id)
            jpeg = source.format == 'JPEG'
            data = _lossless_jpeg(image.data, source, angle) if jpeg else None
            if data is not None:
                with Image.open(io.BytesIO(data)) as output:
                    return replace(image, data=data, media_type='image/jpeg',
                                   ext='jpg', width=output.width, height=output.height)
            # Decode only when no perfect JPEG transform is available.
            pixels = ImageOps.exif_transpose(source)
            pixels = pixels.transpose({90: Image.Transpose.ROTATE_270,
                                       180: Image.Transpose.ROTATE_180,
                                       270: Image.Transpose.ROTATE_90}[angle])
            icc = pixels.info.get('icc_profile')
            modes = ('1', 'L', 'LA', 'P', 'RGB', 'RGBA', 'I;16')
            if pixels.mode not in modes:
                pixels = pixels.convert('RGB')
                icc = None  # A source profile may not describe converted RGB.
            transparency = pixels.info.get('transparency')
            pixels.info.clear()
            options = {}
            if icc:
                options['icc_profile'] = icc
            if transparency is not None:
                options['transparency'] = transparency
            output = io.BytesIO()
            pixels.save(output, format='PNG', **options)
            return replace(image, data=output.getvalue(),
                           media_type='image/png', ext='png',
                           width=pixels.width, height=pixels.height)


def export_images(donor, decisions):
    """Rotate export copies without changing sessions or project originals."""
    images, exported = [], dict(decisions)
    for image in donor.illustrations:
        decision = decisions[image.id]
        angle = decision['rotation']
        if angle and decision['role'] != 'ignore' and image.media_type != 'image/svg+xml':
            try:
                image = _rotate_bitmap(image, angle)
            except BookError:
                raise
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
                    Image.DecompressionBombWarning) as error:
                raise BookError('无法旋转图片 %s，请换用有效的静态图片。' % image.id) from error
            exported[image.id] = {**decision, 'rotation': 0}
        images.append(image)
    return replace(donor, illustrations=images), exported


def svg_image(image, src, rotation, label='插图', full_page=False):
    """Fit the rotated bounding box, using the image's normal display dimensions.

    JPEG dimensions already account for EXIF; the image renderer handles its
    orientation. This transform adds only the user's clockwise quarter turns.
    """
    if not valid_dimensions(image):
        raise BookError('无法读取图片 %s 的有效尺寸，请换用尺寸可识别的图片后再旋转。' % image.id)
    width, height = image.width, image.height
    out_width, out_height = (height, width) if rotation in (90, 270) else (width, height)
    matrices = {90: (0, 1, -1, 0, height, 0),
                180: (-1, 0, 0, -1, width, height),
                270: (0, -1, 1, 0, 0, width)}
    number = lambda value: format(value, '.12g')
    transform = (' transform="matrix(%s)"' % ' '.join(map(number, matrices[rotation]))) if rotation else ''
    style = ('display:block;width:100%;height:98vh;margin:0 auto' if full_page else
             'display:block;max-width:100%;max-height:95vh;width:auto;height:auto;margin:0 auto')
    escape = lambda value: html.escape(str(value), quote=True)
    return ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            'version="1.1" width="%s" height="%s" viewBox="0 0 %s %s" '
            'preserveAspectRatio="xMidYMid meet" role="img" aria-label="%s" style="%s">'
            '<title>%s</title><image width="%s" height="%s" xlink:href="%s" '
            'preserveAspectRatio="xMidYMid meet"%s/></svg>') % (
                number(out_width), number(out_height), number(out_width), number(out_height),
                escape(label), style, escape(label), number(width), number(height), escape(src), transform)
