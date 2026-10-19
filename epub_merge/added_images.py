"""Restore local image attachments from portable review projects."""
import base64
import binascii
import hashlib
import re
from dataclasses import replace

from .books import BookError, Illustration, MAX_IMAGES, image_info

MAX_IMAGE = 20 * 1024 * 1024
MAX_ADDED = 40 * 1024 * 1024
MAX_PROJECT = 64 * 1024 * 1024


def with_added_images(donor, settings):
    if not isinstance(settings, dict):
        raise BookError('项目设置无效。')
    added = settings.get('added_images', [])
    if not isinstance(added, list) or len(added) + len(donor.illustrations) > MAX_IMAGES:
        raise BookError('新增图片无效，或图片总数超过 400 张。')
    if not added:
        return donor
    images, used, total = [], {p.id for p in donor.illustrations}, 0
    for item in added:
        if not isinstance(item, dict):
            raise BookError('新增图片记录无效。')
        id_ = item.get('id')
        if not isinstance(id_, str) or not re.fullmatch(r'local_[a-zA-Z0-9_-]{1,80}', id_) or id_ in used:
            raise BookError('新增图片编号无效或重复。')
        name, encoded, digest = item.get('filename'), item.get('data'), item.get('sha256')
        if not isinstance(name, str) or not name.strip() or len(name) > 300:
            raise BookError('新增图片文件名无效。')
        if not isinstance(encoded, str) or len(encoded) > ((MAX_IMAGE + 2) // 3) * 4:
            raise BookError('每张新增图片最多 20 MB。')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BookError('新增图片数据损坏。') from exc
        total += len(data)
        if not data or len(data) > MAX_IMAGE or total > MAX_ADDED:
            raise BookError('每张新增图片最多 20 MB，合计最多 40 MB。')
        if not isinstance(digest, str) or hashlib.sha256(data).hexdigest() != digest:
            raise BookError('新增图片校验失败，请重新添加原图。')
        media, ext, width, height = image_info(data)
        if ext not in {'jpg', 'png', 'gif', 'webp'}:
            raise BookError('新增图片支持 JPEG、PNG、GIF、WebP。')
        filename = name.replace('\\', '/').rsplit('/', 1)[-1]
        image = Illustration(id_, '本地图片：' + filename, data, media, ext, width, height,
                             'body', '', [], [], 1, note='本地添加的图片，请手动选择插入位置并确认。')
        image.local = True
        images.append(image)
        used.add(id_)
    return replace(donor, illustrations=[*donor.illustrations, *images])
