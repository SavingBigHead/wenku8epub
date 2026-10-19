"""Validate a portable image order and apply it without mutating the source."""
from dataclasses import replace
from .added_images import with_added_images
from .books import BookError


def prepare_images(donor, settings):
    donor = with_added_images(donor, settings)
    if 'image_order' not in settings:
        return donor
    order = settings['image_order']
    by_id = {image.id: image for image in donor.illustrations}
    if (not isinstance(order, list) or len(order) != len(by_id)
            or any(not isinstance(id_, str) for id_ in order)
            or len(set(order)) != len(order) or set(order) != set(by_id)):
        raise BookError('图片排序无效，请确保所有图片各出现一次。')
    return replace(donor, illustrations=[by_id[id_] for id_ in order])
