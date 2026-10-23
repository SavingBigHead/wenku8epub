"""Build one consistently styled, reflowable EPUB 3 per input volume."""
import datetime as dt
import hashlib
import html
import io
import json
import re
import zipfile
from collections import defaultdict

from .locations import START, chapter_start, chapter_end, valid_locations, location_label, boundary_index, canonical_location
from .edits import apply_edits, paragraph_content
from .image_order import prepare_images
from .image_rotation import rotation_value, valid_dimensions, svg_image, export_images
from .books import BookError
from .presentation import prepare_epub
from .metadata import normalize_volumes, metadata_values, write_metadata

CSS = '''
html { writing-mode: horizontal-tb; }
body { margin: 0 5%; font-family: serif; font-size: 1em; line-height: 1.85; color: inherit; }
h1 { font-size: 1.45em; font-weight: bold; text-align: center; line-height: 1.5; margin: 2em 0 1.5em; }
p { margin: 0.35em 0; text-indent: 2em; widows: 2; orphans: 2; overflow-wrap: break-word; }
p.scene { text-indent: 0; text-align: center; margin: 1.5em 0; }
.illust { text-align:center; margin:1em 0; }
.illust img { max-width:100%; max-height:95vh; object-fit:contain; }
body.art { margin: 0; padding: 0; text-align: center; line-height: 1; }
.art svg { display: block; width: 100%; height: 100vh; }
.art img { display: block; max-width: 100%; max-height: 98vh; width: auto; height: auto; margin: auto; }
'''.strip()


def escape(value):
    return html.escape(str(value), quote=True)


def validate(book, donor, settings, prepared=False):
    if not prepared:
        book = apply_edits(book, settings)
        donor = prepare_images(donor, settings)
    if not isinstance(settings, dict):
        raise BookError('项目设置无效。')
    if settings.get('text_fingerprint') != book.fingerprint or settings.get('donor_fingerprint') != donor.fingerprint:
        raise BookError('项目与输入文件不一致，请重新导入同一组原始文件。')
    if book.source_epub is not None and settings.get('layout') != 'mojimoon':
        raise BookError('这个项目来自旧的重排模式，请按保留 mojimoon 底稿的新模式重新校对。')
    raw = settings.get('images')
    if not isinstance(raw, list) or len(raw) != len(donor.illustrations):
        raise BookError('插图设置不完整。')
    ids = {x.id for x in donor.illustrations}
    image_by_id = {x.id: x for x in donor.illustrations}
    by_id = {}
    paragraph_ids = valid_locations(book)
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] not in ids or item['id'] in by_id:
            raise BookError('项目含无效或重复的插图 ID。')
        role = item.get('role')
        if role not in {'cover', 'front', 'body', 'ignore'}:
            raise BookError('插图类型无效。')
        rotation = rotation_value(item)
        if rotation and role != 'ignore' and not valid_dimensions(image_by_id[item['id']]):
            raise BookError('无法读取图片 %s 的有效尺寸，请换用尺寸可识别的图片后再旋转。' % item['id'])
        if role == 'body':
            if item.get('after_id') not in paragraph_ids:
                raise BookError('请为每张正文插图指定有效的位置，或将其设为忽略。')
            if item.get('confirmed') is not True:
                raise BookError('仍有未确认的正文插图，请先校对或忽略后再导出。')
        by_id[item['id']] = {**item, 'rotation': rotation}
    if sum(item['role'] == 'cover' for item in by_id.values()) > 1:
        raise BookError('只能选择一张封面，请将其余封面改为卷首图或忽略。')
    title = normalize_volumes(str(settings.get('title', book.title)).strip())[:300]
    author = str(settings.get('author', book.author)).strip()[:300]
    metadata_values(book, settings)
    if not title:
        raise BookError('请填写书名／卷名。')
    return by_id, title, author


def initial_settings(book, donor):
    return {
        'version': 1, 'layout': 'mojimoon' if book.source_epub else 'rebuild', 'text_fingerprint': book.fingerprint, 'donor_fingerprint': donor.fingerprint,
        'title': normalize_volumes(book.title), 'author': book.author, 'text_edits': [], 'added_images': [], 'image_order': [x.id for x in donor.illustrations],
        **metadata_values(book, {}),
        'images': [{**{k: getattr(image, k) for k in ['id', 'role', 'after_id', 'confirmed']}, 'rotation': 0} for image in donor.illustrations],
    }


def document(title, body, art=False):
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">'
            '<head><title>' + escape(title) + '</title><link rel="stylesheet" type="text/css" href="../styles/book.css"/></head>'
            '<body' + (' class="art"' if art else '') + '>' + body + '</body></html>')


def build_epub(book, donor, settings):
    book = apply_edits(book, settings)
    donor = prepare_images(donor, settings)
    decisions, title, author = validate(book, donor, settings, prepared=True)
    donor, decisions = export_images(donor, decisions)
    if book.source_epub is not None:
        from .preserve import build_preserved
        return prepare_epub(build_preserved(book, donor, settings, (decisions, title, author)))
    parts, assets, navigation = [], [], []
    by_anchor = defaultdict(list)
    cover = None
    for image in donor.illustrations:
        decision = decisions[image.id]
        role = decision['role']
        if role == 'ignore':
            continue
        assets.append(image)
        if role == 'cover':
            cover = image
        elif role == 'body':
            by_anchor[canonical_location(book, decision['after_id'])].append(image)

    def add_art(image, label):
        path = 'text/' + image.id + '.xhtml'
        src = '../images/' + image.id + '.' + image.ext
        if image.media_type == 'image/svg+xml' and valid_dimensions(image):
            body = svg_image(image, src, decisions[image.id]['rotation'], label, full_page=True)
            properties = 'svg'
        else:
            body, properties = '<img src="%s" alt="%s"/>' % (escape(src), escape(label)), ''
        parts.append((path, document(label, body, art=True), properties))
        return path

    if cover:
        navigation.append(('封面', add_art(cover, '封面')))
    fronts = [x for x in donor.illustrations if decisions[x.id]['role'] == 'front']
    for index, image in enumerate(fronts):
        path = add_art(image, '卷首插图 %d' % (index + 1))
        if index == 0:
            navigation.append(('卷首插图', path))
    for image in by_anchor[START]:
        add_art(image, '正文前插图')
    for chapter in book.chapters:
        chunk = ['<h1>' + escape(chapter.title) + '</h1>']
        chunk_svg = False
        segment = 0
        first_path = None

        def flush():
            nonlocal chunk, chunk_svg, segment, first_path
            if not chunk:
                return
            segment += 1
            path = 'text/%s-%03d.xhtml' % (chapter.id, segment)
            first_path = first_path or path
            parts.append((path, document(chapter.title, ''.join(chunk)), 'svg' if chunk_svg else ''))
            chunk = []
            chunk_svg = False

        def inline_art(image):
            nonlocal chunk_svg
            src = '../images/%s.%s' % (image.id, image.ext)
            rotation = decisions[image.id]['rotation']
            if rotation:
                chunk.append('<div class="illust">' + svg_image(image, src, rotation) + '</div>')
                chunk_svg = True
            else:
                chunk.append('<div class="illust"><img src="%s" alt="插图"/></div>' % src)
        for image in by_anchor[chapter_start(chapter.id)]: inline_art(image)
        for paragraph in chapter.paragraphs:
            scene = bool(paragraph.text.strip()) and bool(re.fullmatch(r'[\s＊*※◇◆☆★○●—─…·・\-]+', paragraph.text))
            chunk.append('<p id="%s"%s>%s</p>' % (paragraph.id, ' class="scene"' if scene else '', paragraph_content(paragraph.text)))
            if paragraph.id in by_anchor:
                flush()
                for image in by_anchor[paragraph.id]:
                    add_art(image, '插图 ' + image.id)
        for image in by_anchor[chapter_end(chapter.id)]: inline_art(image)
        flush()
        if first_path:
            navigation.append((chapter.title, first_path))

    content_hash = hashlib.sha256((book.fingerprint + donor.fingerprint + json.dumps(settings, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
    uid = 'urn:sha256:' + content_hash
    modified = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
                '<item id="css" href="styles/book.css" media-type="text/css"/>']
    spine = []
    for index, (path, _, properties) in enumerate(parts):
        id_ = 'doc%d' % index
        manifest.append('<item id="%s" href="%s" media-type="application/xhtml+xml"%s/>' % (
            id_, escape(path), ' properties="%s"' % properties if properties else ''))
        spine.append('<itemref idref="%s"/>' % id_)
    for image in assets:
        manifest.append('<item id="%s" href="images/%s.%s" media-type="%s"%s/>' % (
            image.id, image.id, image.ext, image.media_type, ' properties="cover-image"' if image is cover else ''))
    opf = ('<?xml version="1.0" encoding="utf-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id" prefix="dcterms: http://purl.org/dc/terms/">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:identifier id="book-id">%s</dc:identifier><dc:title>%s</dc:title><dc:creator>%s</dc:creator>'
           '<dc:language>zh-CN</dc:language><meta property="dcterms:modified">%s</meta>'
           '<dc:source>%s</dc:source><dc:source>%s</dc:source>%s</metadata>'
           '<manifest>%s</manifest><spine toc="ncx">%s</spine></package>') % (
               escape(uid), escape(title), escape(author), modified,
               escape('正文：' + book.filename), escape('插图：' + donor.filename),
               '<meta name="cover" content="%s"/>' % cover.id if cover else '', ''.join(manifest), ''.join(spine))
    from xml.etree import ElementTree as ET
    opf_node = ET.fromstring(opf)
    write_metadata(opf_node, title, author, metadata_values(book, settings))
    opf = ET.tostring(opf_node, encoding='utf-8', xml_declaration=True)
    nav = ('<?xml version="1.0" encoding="utf-8"?>'
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN">'
           '<head><title>目录</title></head><body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>%s</ol></nav></body></html>') % ''.join(
               '<li><a href="%s">%s</a></li>' % (escape(path), escape(label)) for label, path in navigation)
    ncx = ('<?xml version="1.0" encoding="utf-8"?>'
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="%s"/>'
           '<meta name="dtb:depth" content="1"/><meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/>'
           '</head><docTitle><text>%s</text></docTitle><navMap>%s</navMap></ncx>') % (escape(uid), escape(title), ''.join(
               '<navPoint id="n%d" playOrder="%d"><navLabel><text>%s</text></navLabel><content src="%s"/></navPoint>' % (
                   i, i, escape(label), escape(path)) for i, (label, path) in enumerate(navigation, 1)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        def write(path, data):
            z.writestr(path, data, compress_type=zipfile.ZIP_DEFLATED)
        write('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        write('OEBPS/content.opf', opf)
        write('OEBPS/nav.xhtml', nav)
        write('OEBPS/toc.ncx', ncx)
        write('OEBPS/styles/book.css', CSS)
        for path, content, _ in parts:
            write('OEBPS/' + path, content)
        for image in assets:
            write('OEBPS/images/' + image.id + '.' + image.ext, image.data)
    return prepare_epub(buf.getvalue())


def build_report(book, donor, settings):
    book = apply_edits(book, settings, preserve_source=False)
    donor = prepare_images(donor, settings)
    decisions, title, author = validate(book, donor, settings, prepared=True)
    by_id = {p.id: p for p in book.paragraphs}
    rows = []
    labels = {'cover': '封面', 'front': '卷首图', 'body': '正文插图', 'ignore': '忽略'}
    previous = -1
    for image in donor.illustrations:
        decision = decisions[image.id]
        location, warning = '', ''
        if decision['role'] == 'body':
            anchor = decision['after_id']
            location, index = location_label(book,anchor), boundary_index(book,anchor)
            if index < previous:
                warning = '（已人工调整为与插图源不同的先后顺序）'
            previous = index
        rows.append('<tr><td>%s<br/><small>%s</small></td><td>%s</td><td>%s %s</td><td>%s°</td><td>%s</td></tr>' % (
            escape(image.id), escape(image.source_path), labels[decision['role']], escape(location), escape(warning),
            decision['rotation'], escape('%.2f' % image.score)))
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>%s · 合并记录</title>'
            '<style>body{max-width:1100px;margin:40px auto;padding:0 20px;font:16px/1.7 system-ui;color:#263634}'
            'table{border-collapse:collapse;width:100%%}td,th{text-align:left;padding:14px;border:1px solid #d4dedb;vertical-align:top}'
            'small{word-break:break-all}code{overflow-wrap:anywhere}</style><h1>%s · 合并记录</h1>'
            '<p>作者：%s<br/>正文：%s<br/>插图：%s</p><p>正文 SHA-256：<code>%s</code><br/>插图源 SHA-256：<code>%s</code></p>'
            '<p>正文 %d 段；未旋转图片保持源文件字节；旋转 JPEG 优先无损变换，无法无损变换时旋转像素并无损保存为 PNG；其他旋转位图无损保存为 PNG，SVG 仍通过展示层旋转。角度按顺时针记录。匹配分数是文本相似度，不是准确率。正文插图均由用户确认。同一位置的图片按本记录出现顺序插入。</p>'
            '<p>段落修改记录：%d 条（已应用于导出的正文）。<br/>章节标题修改记录：%d 条（已同步正文与目录）。</p>'
            '<table><tr><th>图片</th><th>用途</th><th>插入位置</th><th>旋转角度</th><th>原始建议分数</th></tr>%s</table></html>') % (
                escape(title), escape(title), escape(author), escape(book.filename), escape(donor.filename),
                book.fingerprint, donor.fingerprint, len(book.paragraphs), sum(e['op'] != 'rename_chapter' for e in settings.get('text_edits', [])),
                sum(e['op'] == 'rename_chapter' for e in settings.get('text_edits', [])), ''.join(rows))
