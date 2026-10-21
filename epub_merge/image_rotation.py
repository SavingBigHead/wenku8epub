"""Quarter-turn presentation, leaving the source illustration bytes untouched."""
import html
import math

from .books import BookError


def rotation_value(decision):
    value = decision.get('rotation', 0)
    if type(value) is not int or value not in (0, 90, 180, 270):
        raise BookError('图片旋转角度无效，只能是整数 0、90、180 或 270。')
    return value


def valid_dimensions(image):
    return all(type(n) in (int, float) and 1e-6 <= n <= (1 << 24) and math.isfinite(n)
               for n in (image.width, image.height))


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
