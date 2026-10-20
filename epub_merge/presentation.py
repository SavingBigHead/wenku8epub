"""Reader-compatible XML, flat contents and illustration-only title pages."""
import io
import posixpath
import re
import zipfile
from xml.etree import ElementTree as ET

from .books import Epub, FlowParser, local_name, xml

OPF = 'http://www.idpf.org/2007/opf'
DC = 'http://purl.org/dc/elements/1.1/'
XHTML = 'http://www.w3.org/1999/xhtml'
NCX = 'http://www.daisy.org/z3986/2005/ncx/'
VOLUME_PAGE = re.compile(r'/Text/(?:v\d+_)?v\d+\.xhtml$', re.I)


def serialize_xml(element):
    """Keep default root namespace and conventional dc/epub prefixes.

    Work on one serialized document, without changing ElementTree's global
    namespace registry (exports run concurrently in different HTTP threads).
    """
    data = ET.tostring(element, encoding='unicode', xml_declaration=False)
    for uri, name in [(DC, 'dc'), ('http://www.idpf.org/2007/ops', 'epub')]:
        declaration = re.search(r'xmlns:([\w]+)="' + re.escape(uri) + '"', data)
        if declaration and declaration[1] != name:
            old = declaration[1]
            data = re.sub(r'(<\/?)' + old + ':', lambda m: m[1] + name + ':', data)
            data = data.replace('xmlns:' + old + '="' + uri + '"', 'xmlns:' + name + '="' + uri + '"')
            # Only qualified attribute names, never user-supplied attribute values.
            data = re.sub(r'(\s)' + old + r':([\w.-]+)=', lambda m: m[1] + name + ':' + m[2] + '=', data)
    root = re.match(r'<([\w]+):', data)
    if root:
        prefix = root[1]
        declaration = re.search(r'xmlns:' + prefix + r'="([^"]+)"', data)
        if declaration:
            uri = declaration[1]
            data = re.sub(r'(<\/?)' + prefix + ':', lambda m: m[1], data)
            has_attributes = re.search(r'\s' + prefix + r':[\w.-]+=', data)
            default = 'xmlns="' + uri + '"'
            if has_attributes:
                default += ' xmlns:' + prefix + '="' + uri + '"'
            data = data.replace(declaration[0], default, 1)
    return ('<?xml version="1.0" encoding="utf-8"?>\n' + data).encode('utf-8')


def flat_children(parent, tag):
    """Lift descendants in reading order, retaining each original link."""
    nodes = []
    def visit(node):
        nodes.append(node)
        for child in list(node):
            if local_name(child.tag) == tag:
                node.remove(child)
                visit(child)
            elif tag == 'li' and local_name(child.tag) == 'ol':
                node.remove(child)
                for li in list(child):
                    if local_name(li.tag) == 'li': visit(li)
    for child in list(parent):
        parent.remove(child)
        if local_name(child.tag) == tag: visit(child)
    parent.extend(nodes)


def prepare_epub(data):
    """Apply reader preferences without rewriting prose, CSS or image bytes.

    Empty title-page resources stay in the manifest for later illustration
    placement, but are absent from spine and contents until they have artwork.
    """
    source = Epub(data)
    replacements = {}
    opf = source.opf
    spine = opf.find('{%s}spine' % OPF)
    prefix = opf.get('prefix', '')
    if 'dcterms:' not in prefix:
        opf.set('prefix', (prefix + ' dcterms: http://purl.org/dc/terms/').strip())
    volumes, empty = [], set()
    items = list(source.manifest.items())
    for index, (id_, item) in enumerate(items):
        path = item['path']
        if not path or not VOLUME_PAGE.search(path): continue
        raw = source.read(path)
        def strip_heading(match):
            attrs = re.search(br'\bclass\s*=\s*["\']([^"\']*)["\']', match[0].split(b'>', 1)[0], re.I)
            return b'' if attrs and b'volume' in attrs[1].lower().split() else match[0]
        raw = re.sub(br'<h1\b[^>]*>.*?</h1\s*>', strip_heading, raw, count=1, flags=re.S | re.I)
        replacements[path] = raw
        root = xml(raw)
        label = next((''.join(n.itertext()).strip() for n in root.iter() if local_name(n.tag) == 'title'), '扉页')
        blocks = FlowParser().read(raw)
        active = bool(blocks)
        following = next((i for i, v in items[index+1:] if re.search(r'/Text/(?:v\d+_)?c\d+\.xhtml$', v.get('path') or '', re.I)), None)
        label = '插图'
        volumes.append((id_, path, label, following, active))
        if not active: empty.add(path)
        refs = [ref for ref in spine if ref.get('idref') == id_]
        if not active:
            for ref in refs: spine.remove(ref)
        elif not refs:
            insertion = next((i for i, ref in enumerate(spine) if ref.get('idref') == following), len(spine))
            spine.insert(insertion, ET.Element('{%s}itemref' % OPF, {'idref': id_}))

    for item in source.manifest.values():
        path = item['path']
        if not path: continue
        if re.search(r'/Text/(?:v\d+_)?Intro\.xhtml$', path, re.I):
            raw = source.read(path)
            description = re.search(br'<div class="intro">.*?</div>', raw, re.S)
            if description:
                credit = re.search(br'\s*<p class="meta"[^>]*>' + '来源：'.encode('utf-8') + br'.*?</p>', raw[:description.start()], re.S)
                if credit:
                    # Move the original source block, without rewriting its text or style.
                    replacements[path] = raw[:credit.start()] + raw[credit.end():description.end()] + credit[0] + raw[description.end():]
        elif 'nav' in item.get('properties', '').split():
            nav = xml(source.read(path))
            toc = next((n for n in nav.iter() if local_name(n.tag) == 'nav' and (n.get('{http://www.idpf.org/2007/ops}type') == 'toc' or n.get('id') == 'toc')), None)
            ol = next((n for n in toc if local_name(n.tag) == 'ol'), None) if toc is not None else None
            if ol is None: continue
            flat_children(ol, 'li')
            def target(li):
                a = next((n for n in li.iter() if local_name(n.tag) == 'a'), None)
                return source.resolve(path, a.get('href', '')) if a is not None else None
            volume_paths = {v[1] for v in volumes}
            for li in list(ol):
                if target(li) in empty: ol.remove(li)
                elif target(li) in volume_paths:
                    a = next(n for n in li.iter() if local_name(n.tag) == 'a')
                    a.text = '插图'
            for _, volume_path, label, following, active in volumes:
                if not active or any(target(li) == volume_path for li in ol): continue
                following_path = source.manifest.get(following, {}).get('path')
                insertion = next((i for i, li in enumerate(ol) if target(li) == following_path), len(ol))
                li = ET.Element('{%s}li' % XHTML)
                ET.SubElement(li, '{%s}a' % XHTML, {'href': posixpath.relpath(volume_path, posixpath.dirname(path))}).text = label
                ol.insert(insertion, li)
            replacements[path] = serialize_xml(nav)
        elif item.get('media-type') == 'application/x-dtbncx+xml':
            ncx = xml(source.read(path))
            navmap = ncx.find('{%s}navMap' % NCX)
            if navmap is None: continue
            flat_children(navmap, 'navpoint')
            def target(point):
                content = point.find('{%s}content' % NCX)
                return source.resolve(path, content.get('src', '')) if content is not None else None
            volume_paths = {v[1] for v in volumes}
            for point in list(navmap):
                if target(point) in empty: navmap.remove(point)
                elif target(point) in volume_paths:
                    node = point.find('{%s}navLabel/{%s}text' % (NCX,NCX))
                    if node is not None: node.text = '插图'
            for _, volume_path, label, following, active in volumes:
                if not active or any(target(p) == volume_path for p in navmap): continue
                following_path = source.manifest.get(following, {}).get('path')
                insertion = next((i for i, p in enumerate(navmap) if target(p) == following_path), len(navmap))
                point = ET.Element('{%s}navPoint' % NCX)
                node = ET.SubElement(point, '{%s}navLabel' % NCX)
                ET.SubElement(node, '{%s}text' % NCX).text = label
                ET.SubElement(point, '{%s}content' % NCX, {'src': posixpath.relpath(volume_path, posixpath.dirname(path))})
                navmap.insert(insertion, point)
            for index, point in enumerate(navmap, 1):
                point.set('id', 'navpoint_%d' % index)
                point.set('playOrder', str(index))
            for node in ncx.iter('{%s}meta' % NCX):
                if node.get('name') == 'dtb:depth': node.set('content', '1')
            replacements[path] = serialize_xml(ncx)
    replacements[source.opf_path] = serialize_xml(opf)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for info in source.zip.infolist():
            archive.writestr(info, replacements.get(info.filename, source.read(info.filename)))
    return output.getvalue()
