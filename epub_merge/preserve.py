"""Insert illustrations into the original mojimoon EPUB without reflowing text."""
import datetime as dt
import hashlib
import html
import io
import json
import posixpath
import re
import zipfile
from collections import defaultdict
from xml.etree import ElementTree as ET

from .locations import START, resolve_location, page_location, empty_chapter
from .books import BookError, Epub, local_name, xml
from .metadata import metadata_values, write_metadata
from .image_rotation import svg_image

OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
XHTML = 'http://www.w3.org/1999/xhtml'
NCX = 'http://www.daisy.org/z3986/2005/ncx/'


def dump(element):
    return ET.tostring(element, encoding='utf-8', xml_declaration=True)


def build_preserved(book, donor, settings, validated):
    decisions, title, author = validated
    source = Epub(book.source_epub)
    opf = source.opf
    manifest, spine, metadata = [opf.find('{%s}%s' % (OPF, name)) for name in ['manifest', 'spine', 'metadata']]
    if any(x is None for x in [manifest, spine, metadata]):
        raise BookError('底稿缺少 EPUB 主结构。')
    base = posixpath.dirname(source.opf_path)
    prefix = 'em_'
    while any(posixpath.basename(name).startswith(prefix) for name in source.names):
        prefix = 'x' + prefix
    extra, replacements, dropped = {}, {}, set()
    body_images = defaultdict(list)
    assets, cover, fronts = [], None, []
    for image in donor.illustrations:
        decision = decisions[image.id]
        role = decision['role']
        if role == 'ignore':
            continue
        assets.append(image)
        if role == 'cover':
            cover = image
        elif role == 'front':
            fronts.append(image)
        else:
            body_images[decision['after_id']].append(image)

    def item(id_, path, media, properties=''):
        attrs = {'id': id_, 'href': posixpath.relpath(path, base), 'media-type': media}
        if properties:
            attrs['properties'] = properties
        ET.SubElement(manifest, '{%s}item' % OPF, attrs)

    def picture_path(image):
        return posixpath.join(base, 'Images', prefix + image.id + '.' + image.ext)

    for image in assets:
        path = picture_path(image)
        extra[path] = image.data
        item(prefix + image.id, path, image.media_type, 'cover-image' if image is cover else '')

    patches = defaultdict(list)
    svg_paths = set()
    # Combine coincident offsets in donor order before patching source bytes.
    for image in donor.illustrations:
        decision = decisions[image.id]
        if decision['role'] != 'body': continue
        page = page_location(book, decision['after_id'])
        empty = empty_chapter(book, decision['after_id'])
        if empty:
            reference = book.chapter_boundaries[empty.id]
            path, offset = reference['path'], reference['start']
        elif page:
            reference, side = page
            path, offset = reference['path'], reference[side]
        else:
            paragraph,side = resolve_location(book,decision['after_id'])
            reference = book.anchors[paragraph.id]
            path = reference['path']
            offset = reference['start'] if side == 'before' else reference['end']
        image_ref = posixpath.relpath(picture_path(image), posixpath.dirname(path))
        if decision['rotation']:
            picture = svg_image(image, image_ref, decision['rotation'])
            svg_paths.add(path)
        else:
            picture = '<img src="%s" alt="插图"/>' % html.escape(image_ref, quote=True)
        markup = '\n<div class="illust" id="%s">%s</div>' % (prefix + image.id, picture)
        patches[(path,offset)].append(markup)
    by_path=defaultdict(list)
    for (path,offset),markups in patches.items():
        by_path[path].append((offset,''.join(markups).encode('utf-8')))
    for path,changes in by_path.items():
        content=source.read(path)
        for offset,payload in sorted(changes,key=lambda x:x[0],reverse=True):
            content=content[:offset]+payload+content[offset:]
        replacements[path]=content
    for element in manifest:
        if source.resolve(source.opf_path, element.get('href', '')) in svg_paths:
            properties = element.get('properties', '').split()
            if 'svg' not in properties:
                element.set('properties', ' '.join(properties + ['svg']))

    old_cover_docs, old_cover_ids = set(), set()
    for path, _, blocks in source.documents():
        images = [source.resolve(path, b.text) for b in blocks if b.kind == 'image']
        if source.cover_path and images == [source.cover_path] and not any(b.kind == 'text' for b in blocks):
            old_cover_docs.add(path)
    if cover:
        for element in list(manifest):
            path = source.resolve(source.opf_path, element.get('href', ''))
            if path in old_cover_docs or path == source.cover_path:
                old_cover_ids.add(element.get('id'))
                dropped.add(path)
                manifest.remove(element)
        for ref in list(spine):
            if ref.get('idref') in old_cover_ids:
                spine.remove(ref)
        for element in list(metadata):
            if local_name(element.tag) == 'meta' and element.get('name') == 'cover':
                metadata.remove(element)
        ET.SubElement(metadata, '{%s}meta' % OPF, {'name': 'cover', 'content': prefix + cover.id})

    leading = []
    art_images = ([cover] if cover else []) + fronts
    css_path = next(x['path'] for x in source.manifest.values() if (x.get('path') or '').endswith('/Styles/style.css'))
    for image in art_images:
        role = '封面' if image is cover else '卷首插图'
        path = posixpath.join(base, 'Text', prefix + image.id + '.xhtml')
        image_ref = posixpath.relpath(picture_path(image), posixpath.dirname(path))
        rotation = decisions[image.id]['rotation']
        picture = (svg_image(image, image_ref, rotation, role, full_page=True) if rotation else
                   '<img src="%s" alt="%s"/>' % (html.escape(image_ref, quote=True), role))
        css_ref = html.escape(posixpath.relpath(css_path, posixpath.dirname(path)), quote=True)
        content = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="%s" xml:lang="zh-CN" lang="zh-CN">'
                   '<head><title>%s</title><link href="%s" rel="stylesheet" type="text/css"/></head>'
                   '<body><div class="%s">%s</div></body></html>') % (
                       XHTML, role, css_ref, 'cover' if image is cover else 'illust', picture)
        extra[path] = content.encode('utf-8')
        item(prefix + 'page_' + image.id, path, 'application/xhtml+xml', 'svg' if rotation else '')
        leading.append((prefix + 'page_' + image.id, path, role))
    insertion = 0
    if not cover and old_cover_docs:
        old_ids = {key for key, value in source.manifest.items() if value['path'] in old_cover_docs}
        for index, ref in enumerate(spine):
            if ref.get('idref') in old_ids:
                insertion = index + 1
    for offset, (id_, _, _) in enumerate(leading):
        spine.insert(insertion + offset, ET.Element('{%s}itemref' % OPF, {'idref': id_}))

    # Retain the original nested contents and chapter hrefs.
    for source_item in source.manifest.values():
        path = source_item['path']
        if 'nav' in source_item.get('properties', '').split():
            nav = xml(source.read(path))
            toc = next((e for e in nav.iter() if local_name(e.tag) == 'nav' and
                        (e.get('{http://www.idpf.org/2007/ops}type') == 'toc' or e.get('id') == 'toc')), None)
            ol = next((e for e in list(toc) if local_name(e.tag) == 'ol'), None) if toc is not None else None
            if ol is None:
                raise BookError('底稿目录结构不符合 mojimoon 模板。')
            for li in list(ol):
                link = next((x for x in li if local_name(x.tag) == 'a'), None)
                if cover and link is not None and source.resolve(path, link.get('href', '')) in old_cover_docs:
                    ol.remove(li)
            nav_insert = 1 if not cover and old_cover_docs else 0
            for index, (_, art_path, label) in enumerate(leading):
                li = ET.Element('{%s}li' % XHTML)
                ET.SubElement(li, '{%s}a' % XHTML, {'href': posixpath.relpath(art_path, posixpath.dirname(path))}).text = label
                ol.insert(nav_insert + index, li)
            replacements[path] = dump(nav)
        elif source_item.get('media-type') == 'application/x-dtbncx+xml':
            ncx = xml(source.read(path))
            navmap = ncx.find('{%s}navMap' % NCX)
            if navmap is None:
                raise BookError('底稿 NCX 目录缺失。')
            for point in list(navmap):
                content = point.find('{%s}content' % NCX)
                if cover and content is not None and source.resolve(path, content.get('src', '')) in old_cover_docs:
                    navmap.remove(point)
            nav_insert = 1 if not cover and old_cover_docs else 0
            for index, (id_, art_path, label) in enumerate(leading):
                point = ET.Element('{%s}navPoint' % NCX, {'id': id_})
                node = ET.SubElement(point, '{%s}navLabel' % NCX)
                ET.SubElement(node, '{%s}text' % NCX).text = label
                ET.SubElement(point, '{%s}content' % NCX, {'src': posixpath.relpath(art_path, posixpath.dirname(path))})
                navmap.insert(nav_insert + index, point)
            for index, point in enumerate(ncx.iter('{%s}navPoint' % NCX), 1):
                point.set('playOrder', str(index))
            for element in ncx.iter('{%s}docTitle' % NCX):
                node = element.find('{%s}text' % NCX)
                if node is not None:
                    node.text = title
            replacements[path] = dump(ncx)

    values = metadata_values(book, settings)
    write_metadata(opf, title, author, values)
    # Update the existing introduction only if its displayed metadata changed.
    # Source prose pages and stylesheet remain byte-for-byte intact.
    if title != source.title or author != source.author or any(values[k] != source.metadata.get(k) for k in ['description','subjects']):
        for source_item in source.manifest.values():
            path = source_item['path']
            if not path or not re.search(r'/Text/(?:v\d+_)?Intro\.xhtml$', path, re.I):
                continue
            intro = source.read(path).decode('utf-8')
            intro = re.sub(r'<h1\b[^>]*>.*?</h1>', lambda m:'<h1>'+html.escape(title)+'</h1>', intro, count=1, flags=re.S)
            intro = re.sub(r'<p class="meta">.*?</p>', lambda m:'<p class="meta">'+html.escape(author)+'</p>', intro, count=1, flags=re.S)
            old_tags = '<p class="meta">'+html.escape(' '.join(source.metadata['subjects']), quote=False)+'</p>'
            new_tags = '<p class="meta">'+html.escape(' '.join(values['subjects']))+'</p>' if values['subjects'] else ''
            if source.metadata['subjects'] and old_tags in intro:
                intro = intro.replace(old_tags, new_tags, 1)
            elif new_tags:
                intro = intro.replace('</h1>', '</h1>\n'+new_tags, 1)
            description = '<div class="intro">'+''.join('<p>'+html.escape(p.strip())+'</p>' for p in values['description'].splitlines() if p.strip())+'</div>'
            if re.search(r'<div class="intro">', intro):
                intro = re.sub(r'<div class="intro">.*?</div>', lambda m:description, intro, count=1, flags=re.S)
            elif values['description']:
                intro = intro.replace('</body>', description+'</body>')
            replacements[path] = intro.encode('utf-8')

    modified = next((x for x in metadata if x.get('property') == 'dcterms:modified'), None)
    if modified is None:
        modified = ET.SubElement(metadata, '{%s}meta' % OPF, {'property': 'dcterms:modified'})
    modified.text = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    ET.SubElement(metadata, '{%s}source' % DC).text = '插图：' + donor.filename
    uid = 'urn:sha256:' + hashlib.sha256((book.fingerprint + donor.fingerprint + json.dumps(settings, sort_keys=True)).encode()).hexdigest()
    for element in metadata:
        if local_name(element.tag) == 'identifier' and element.get('id') == opf.get('unique-identifier'):
            element.text = uid
    for path, content in list(replacements.items()):
        if path.endswith('.ncx'):
            root = xml(content)
            for meta in root.iter('{%s}meta' % NCX):
                if meta.get('name') == 'dtb:uid':
                    meta.set('content', uid)
            replacements[path] = dump(root)
    replacements[source.opf_path] = dump(opf)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        for info in source.zip.infolist():
            if info.filename == 'mimetype' or info.filename in dropped:
                continue
            z.writestr(info, replacements.get(info.filename, source.read(info.filename)))
        for path, content in extra.items():
            z.writestr(path, content, compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue()
