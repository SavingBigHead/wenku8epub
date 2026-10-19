"""Read EPUB in spine order, without extracting archives to disk."""
from __future__ import annotations

import base64
import hashlib
import io
import posixpath
import re
import struct
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

MAX_FILE = 80 * 1024 * 1024
MAX_EXPANDED = 350 * 1024 * 1024
MAX_ENTRIES = 12000
MAX_PARAGRAPHS = 16000
MAX_IMAGES = 400


class BookError(ValueError):
    pass


def local_name(tag):
    return tag.rsplit('}', 1)[-1].split(':')[-1].lower()


def xml(data):
    if b'<!ENTITY' in data.upper() or b'<!DOCTYPE' in data.upper():
        # EPUB 2 may declare the standard XHTML doctype; entities are never needed.
        if b'<!ENTITY' in data.upper() or b'[' in data.split(b'<!DOCTYPE')[-1].split(b'>', 1)[0]:
            raise BookError('不支持包含自定义实体的 XML。')
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise BookError('EPUB 的 XML 结构损坏：' + str(exc)) from exc


def decode_text(data, encoding=None):
    if encoding:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeError) as exc:
            raise BookError('无法按指定编码读取文件。') from exc
    encodings = ['utf-8-sig', 'gb18030']
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings.insert(0, 'utf-16')
    decl = re.search(br'encoding=[\'"]([a-zA-Z0-9_-]+)', data[:200])
    if decl:
        encodings.insert(0, decl.group(1).decode('ascii'))
    for name in encodings:
        try:
            return data.decode(name)
        except (LookupError, UnicodeError):
            pass
    raise BookError('无法识别文字编码，请将 TXT 另存为 UTF-8。')


@dataclass
class Block:
    kind: str
    text: str


class FlowParser(HTMLParser):
    """Flatten prose, retaining heading, text, image and scene-break order."""
    BLOCKS = {'p', 'div', 'li', 'blockquote', 'section', 'article', 'pre', 'dt', 'dd', 'tr'}
    SKIP = {'head', 'title', 'desc', 'script', 'style', 'nav', 'rt', 'rp', 'audio', 'video', 'noscript'}
    VOID = {'img', 'image', 'br', 'hr', 'meta', 'link', 'input', 'source', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.parts = []
        self.heading = None
        self.skip = []

    def flush(self):
        text = ''.join(self.parts).strip(' \t\r\n\u3000\xa0')
        self.parts = []
        if text:
            self.blocks.append(Block('heading' if self.heading else 'text', text))

    def handle_starttag(self, tag, attrs):
        tag = local_name(tag)
        attrs = dict(attrs)
        if self.skip:
            if tag not in self.VOID:
                self.skip.append(tag)
            return
        if tag in self.SKIP:
            self.skip.append(tag)
            return
        if re.fullmatch('h[1-6]', tag):
            self.flush()
            self.heading = tag
        elif tag in self.BLOCKS or tag == 'br':
            self.flush()
        elif tag == 'hr':
            self.flush()
            self.blocks.append(Block('text', '＊　＊　＊'))
        elif tag in {'img', 'image'}:
            self.flush()
            src = attrs.get('src') or attrs.get('xlink:href') or attrs.get('href')
            if src:
                self.blocks.append(Block('image', src))

    def handle_endtag(self, tag):
        tag = local_name(tag)
        if self.skip:
            if tag in self.skip:
                index = len(self.skip) - 1 - self.skip[::-1].index(tag)
                self.skip = self.skip[:index]
            return
        if tag in self.BLOCKS or re.fullmatch('h[1-6]', tag):
            self.flush()
        if tag == self.heading:
            self.heading = None

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if local_name(tag) not in self.VOID:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def read(self, data):
        self.feed(decode_text(data))
        self.close()
        self.flush()
        return self.blocks


class Epub:
    def __init__(self, data):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self.zip.infolist()
            if len(infos) > MAX_ENTRIES or sum(x.file_size for x in infos) > MAX_EXPANDED:
                raise BookError('EPUB 解压后过大，原型最多支持 350 MB、12000 个文件。')
            if len({x.filename for x in infos}) != len(infos):
                raise BookError('EPUB 含重复的文件路径。')
            self.names = set(self.zip.namelist())
            container = xml(self.read('META-INF/container.xml'))
            rootfiles = [x for x in container.iter() if local_name(x.tag) == 'rootfile']
            if not rootfiles:
                raise BookError('EPUB 缺少主文档声明。')
            self.opf_path = unquote(rootfiles[0].get('full-path', ''))
            self.opf = xml(self.read(self.opf_path))
            self.manifest = {}
            self.spine = []
            self.title = ''
            self.author = ''
            from .metadata import read_metadata
            self.metadata = read_metadata(self.opf)
            self.cover_path = None
            self.toc_names = {}
            for element in self.opf.iter():
                tag = local_name(element.tag)
                if tag == 'title' and not self.title:
                    self.title = ''.join(element.itertext()).strip()
                elif tag == 'creator' and not self.author:
                    self.author = ''.join(element.itertext()).strip()
                elif tag == 'item':
                    item = dict(element.attrib)
                    item['path'] = self.resolve(self.opf_path, item.get('href', ''))
                    self.manifest[item.get('id')] = item
                elif tag == 'itemref' and element.get('linear', 'yes') != 'no':
                    self.spine.append(element.get('idref'))
            for element in self.opf.iter():
                if local_name(element.tag) == 'meta' and element.get('name') == 'cover':
                    self.cover_path = self.manifest.get(element.get('content'), {}).get('path')
            for item in self.manifest.values():
                if 'cover-image' in item.get('properties', '').split():
                    self.cover_path = item['path']
                if 'nav' in item.get('properties', '').split():
                    self._read_nav(item)
            for item in self.manifest.values():
                if item.get('media-type') == 'application/x-dtbncx+xml':
                    self._read_ncx(item)
            if not self.spine:
                raise BookError('EPUB 缺少阅读顺序（spine）。')
        except (zipfile.BadZipFile, KeyError, RuntimeError, OSError) as exc:
            raise BookError('无法读取 EPUB：' + str(exc)) from exc

    def read(self, path):
        try:
            return self.zip.read(path)
        except (KeyError, RuntimeError, zipfile.BadZipFile) as exc:
            raise BookError('EPUB 资源缺失或损坏：' + str(path)) from exc

    @staticmethod
    def resolve(base, ref):
        parts = urlsplit(ref)
        if parts.scheme or parts.netloc:
            return None
        path = unquote(parts.path)
        result = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
        if result.startswith('../') or result.startswith('/'):
            return None
        return result

    def _read_nav(self, item):
        try:
            root = xml(self.read(item['path']))
            for a in root.iter():
                if local_name(a.tag) == 'a' and a.get('href'):
                    path = self.resolve(item['path'], a.get('href'))
                    self.toc_names.setdefault(path, ''.join(a.itertext()).strip())
        except BookError:
            pass

    def _read_ncx(self, item):
        try:
            root = xml(self.read(item['path']))
            for point in root.iter():
                if local_name(point.tag) != 'navpoint':
                    continue
                label, ref = '', None
                for child in point:
                    if local_name(child.tag) == 'navlabel':
                        label = ''.join(child.itertext()).strip()
                    elif local_name(child.tag) == 'content':
                        ref = child.get('src')
                if label and ref:
                    self.toc_names.setdefault(self.resolve(item['path'], ref), label)
        except BookError:
            pass

    def documents(self):
        for idref in self.spine:
            item = self.manifest.get(idref)
            if not item:
                raise BookError('阅读顺序引用了不存在的章节：' + str(idref))
            if 'nav' in item.get('properties', '').split():
                continue
            if item.get('media-type') not in {'application/xhtml+xml', 'text/html'}:
                continue
            path = item['path']
            if not path:
                raise BookError('EPUB 章节路径无效。')
            yield path, self.toc_names.get(path, ''), FlowParser().read(self.read(path))


@dataclass
class Paragraph:
    id: str
    text: str
    chapter_id: str
    chapter_title: str
    index: int


@dataclass
class Chapter:
    id: str
    title: str
    paragraphs: list = field(default_factory=list)


@dataclass
class TextBook:
    title: str
    author: str
    chapters: list
    paragraphs: list
    filename: str
    fingerprint: str
    warnings: list = field(default_factory=list)
    source_epub: bytes | None = field(default=None, repr=False)
    anchors: dict = field(default_factory=dict, repr=False)

    metadata: dict = field(default_factory=dict)

    pages: list = field(default_factory=list, repr=False)
    chapter_boundaries: dict = field(default_factory=dict, repr=False)

    def public(self):
        return {
            'pages': [{k: p[k] for k in ['id', 'title', 'preview']} for p in self.pages],
            'metadata': self.metadata,
            'title': self.title, 'author': self.author, 'filename': self.filename,
            'fingerprint': self.fingerprint, 'warnings': self.warnings,
            'layout': 'mojimoon' if self.source_epub else 'rebuild',
            'chapters': [{'id': c.id, 'title': c.title, 'count': len(c.paragraphs)} for c in self.chapters],
            'paragraphs': [vars(p) for p in self.paragraphs],
        }


CHAPTER_RE = re.compile(
    r'^(?:第[零〇一二三四五六七八九十百千万两\d０-９]+[卷章话話节節幕篇部].*|'
    r'(?:序章|序幕|终章|終章|尾声|尾聲|后记|後記|间章|間章|幕间|幕間|插图|插圖|前言|楔子)(?:\s.*|[：:].*)?|'
    r'(?:chapter\s+\w+|prologue|epilogue|afterword).*)$', re.I)


def parse_text(data, filename, encoding=None):
    if len(data) > MAX_FILE:
        raise BookError('单个输入文件不能超过 80 MB。')
    chapters, paragraphs, warnings = [], [], []
    title = re.sub(r'\.(epub|txt)$', '', filename, flags=re.I)
    author = ''

    def new_chapter(name):
        chapter = Chapter('c%04d' % (len(chapters) + 1), name or '正文')
        chapters.append(chapter)
        return chapter

    def add(chapter, text):
        if len(paragraphs) >= MAX_PARAGRAPHS:
            raise BookError('正文段落过多，原型每卷最多支持 16000 段。')
        p = Paragraph('p%05d' % (len(paragraphs) + 1), text, chapter.id, chapter.title, len(paragraphs))
        chapter.paragraphs.append(p)
        paragraphs.append(p)

    if filename.lower().endswith('.epub'):
        book = Epub(data)
        if is_mojimoon(book):
            return parse_mojimoon(data, filename, book)
        title, author = book.title or title, book.author
        image_count = 0
        chapter = None
        for path, label, blocks in book.documents():
            if label and (chapter is None or chapter.title != label):
                chapter = None
            for block in blocks:
                if block.kind == 'image':
                    image_count += 1
                elif block.kind == 'heading':
                    if chapter and not chapter.paragraphs:
                        # Preserve consecutive headings instead of overwriting one.
                        if chapter.title == block.text:
                            continue
                    chapter = new_chapter(block.text)
                else:
                    if not chapter:
                        chapter = new_chapter(label or '正文 %d' % (len(chapters) + 1))
                    if not chapter.paragraphs and block.text == chapter.title:
                        continue
                    add(chapter, block.text)
        if image_count:
            warnings.append('正文源的 %d 处图片引用未迁入；请在右侧使用插图源选择图片。' % image_count)
        warnings.append('正文 EPUB 会转换为统一段落；原有字体、加粗、超链接、ruby 注音和脚注跳转样式不保留，脚注文字保留在原阅读顺序。')
    elif filename.lower().endswith('.txt'):
        text = decode_text(data, encoding).replace('\r\n', '\n').replace('\r', '\n')
        chapter = None
        for line in text.split('\n'):
            line = line.strip(' \t\u3000\xa0')
            if not line:
                continue
            if len(line) <= 80 and CHAPTER_RE.match(line):
                chapter = new_chapter(line)
            else:
                if not chapter:
                    chapter = new_chapter('正文')
                add(chapter, line)
        warnings.append('TXT 按非空行分段、按常见章节标题分章；建议导入每卷一个文件，正文文字不会自动删改。')
    else:
        raise BookError('正文仅支持 .epub 或 .txt 文件。')
    # Drop empty illustration-only sections, while retaining other standalone headings.
    chapters = [c for c in chapters if c.paragraphs or c.title not in {'插图', '插圖'}]
    if not paragraphs:
        raise BookError('没有读到正文段落；暂不支持扫描图片型或固定版式 EPUB。')
    return TextBook(title, author, chapters, paragraphs, filename, hashlib.sha256(data).hexdigest(), warnings, metadata=book.metadata if filename.lower().endswith('.epub') else {})


def image_info(data, declared=''):
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 24:
        return 'image/png', 'png', *struct.unpack('>II', data[16:24])
    if data[:3] == b'GIF' and len(data) >= 10:
        return 'image/gif', 'gif', *struct.unpack('<HH', data[6:10])
    if data[:2] == b'\xff\xd8':
        i = 2
        width = height = 0
        while i + 4 <= len(data):
            if data[i] != 255:
                i += 1
                continue
            marker = data[i + 1]
            if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            length = int.from_bytes(data[i + 2:i + 4], 'big')
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and i + 9 <= len(data):
                height, width = struct.unpack('>HH', data[i + 5:i + 9])
                break
            if length < 2:
                break
            i += length + 2
        return 'image/jpeg', 'jpg', width, height
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', 'webp', 0, 0
    if b'<svg' in data[:1000]:
        root = xml(data)
        if local_name(root.tag) != 'svg':
            raise BookError('无效的 SVG 图片。')
        # Only self-contained, non-active SVG artwork is accepted.
        for element in root.iter():
            if local_name(element.tag) in {'script', 'foreignobject', 'iframe', 'use', 'image', 'style'}:
                raise BookError('SVG 包含外部引用或活动内容，请改用 JPEG/PNG 插图源。')
            for key, value in element.attrib.items():
                if local_name(key).startswith('on') or local_name(key) in {'href', 'src'} or 'url(' in value.lower():
                    raise BookError('SVG 包含外部引用或活动内容。')
        return 'image/svg+xml', 'svg', 0, 0
    raise BookError('不支持或无法识别的图片格式。')


def is_japanese(text):
    kana = len(re.findall(r'[\u3040-\u30ff]', text))
    return kana >= 2 or (kana and kana / max(1, len(text)) > .05)


@dataclass
class Illustration:
    id: str
    source_path: str
    data: bytes
    media_type: str
    ext: str
    width: int
    height: int
    role: str
    chapter: str
    before: list
    after: list
    ordinal: float
    suggestions: list = field(default_factory=list)
    after_id: str | None = None
    score: float = 0
    confirmed: bool = False
    note: str = ''

    def public(self):
        return {k: v for k, v in vars(self).items() if k not in {'data', 'ext'}} | {'size': len(self.data)}


@dataclass
class DonorBook:
    title: str
    filename: str
    fingerprint: str
    illustrations: list
    warnings: list

    def public(self):
        return {
            'title': self.title, 'filename': self.filename, 'fingerprint': self.fingerprint,
            'warnings': self.warnings, 'illustrations': [x.public() for x in self.illustrations],
        }


def parse_donor(data, filename):
    if len(data) > MAX_FILE or not filename.lower().endswith('.epub'):
        raise BookError('插图源必须是 80 MB 以内的 EPUB。')
    book = Epub(data)
    stream, seen, illustrations, warnings = [], set(), [], []
    chapter, prose_count = '', 0
    for path, label, blocks in book.documents():
        chapter = label or chapter
        for block in blocks:
            if block.kind == 'heading':
                chapter = block.text
            elif block.kind == 'text':
                if not is_japanese(block.text):
                    stream.append(('text', block.text))
                    prose_count += 1
            else:
                ref = book.resolve(path, block.text)
                if ref in seen:
                    continue
                if not ref or ref not in book.names:
                    warnings.append('无法读取插图引用：' + block.text[:120])
                    continue
                seen.add(ref)
                try:
                    picture = book.read(ref)
                    media, ext, width, height = image_info(picture)
                except BookError as exc:
                    warnings.append('跳过 ' + ref + '：' + str(exc))
                    continue
                role = 'cover' if ref == book.cover_path else ('front' if prose_count == 0 else 'body')
                note = ''
                if width and height and max(width, height) < 180:
                    role, note = 'ignore', '尺寸很小，默认忽略；可手动恢复。'
                image = Illustration('i%04d' % (len(illustrations) + 1), ref, picture, media, ext,
                                     width, height, role, chapter, [], [], prose_count,
                                     confirmed=role != 'body', note=note)
                stream.append(('image', image))
                illustrations.append(image)
                if len(illustrations) > MAX_IMAGES:
                    raise BookError('插图过多，原型每卷最多支持 400 张。')
    # The cover may exist only in metadata rather than the spine.
    if book.cover_path and book.cover_path not in seen:
        try:
            picture = book.read(book.cover_path)
            media, ext, width, height = image_info(picture)
            illustrations.insert(0, Illustration('i0000', book.cover_path, picture, media, ext,
                                  width, height, 'cover', '', [], [], 0, confirmed=True))
        except BookError as exc:
            warnings.append('无法读取封面：' + str(exc))
    texts_before = []
    total = max(1, prose_count)
    for kind, value in stream:
        if kind == 'text':
            texts_before.append(value)
        else:
            value.before = texts_before[-2:]
            value.ordinal /= total
    texts_after = []
    for kind, value in reversed(stream):
        if kind == 'text':
            texts_after.insert(0, value)
            texts_after = texts_after[:2]
        else:
            value.after = list(texts_after)
    if not illustrations:
        raise BookError('没有提取到可用插图；请使用含内嵌图片的 EPUB。')
    if not prose_count:
        warnings.append('插图源缺少可匹配的中文正文，无法自动定位；请使用中文／双语 EPUB，或手动选择位置。')
    warnings.append('图片保持原始字节，不压缩、不去水印；图片前后文字仅用于定位，不写入最终正文。')
    return DonorBook(book.title or filename, filename, hashlib.sha256(data).hexdigest(), illustrations, warnings)


MOJI_CHAPTER = re.compile(r'/Text/(?:v\d+_)?c\d+\.xhtml$', re.I)


def is_mojimoon(book):
    return any(MOJI_CHAPTER.search(x.get('path') or '') for x in book.manifest.values()) and any(
        (x.get('path') or '').endswith('/Styles/style.css') for x in book.manifest.values())


def parse_mojimoon(data, filename, epub):
    """Keep source bytes and exact insertion boundaries from the upstream maker."""
    chapters, paragraphs, anchors, pages, boundaries = [], [], {}, [], {}
    # Illustration slots remain available even when an empty title page is
    # omitted from reading order. Keep paragraph IDs stable for saved projects.
    documents = list(epub.documents())
    paths = {d[0] for d in documents}
    manifest_items = list(epub.manifest.values())
    for index, item in enumerate(manifest_items):
        path = item.get('path') or ''
        if path in paths or not re.search(r'/Text/(?:v\d+_)?v\d+\.xhtml$', path, re.I): continue
        next_path = next((v['path'] for v in manifest_items[index+1:] if v.get('path') in paths), None)
        insertion = next((i for i, doc in enumerate(documents) if doc[0] == next_path), len(documents))
        documents.insert(insertion, (path, '', FlowParser().read(epub.read(path))))
        paths.add(path)
    for path, label, blocks in documents:
        if re.search(r'/Text/(?:v\d+_)?v\d+\.xhtml$', path, re.I):
            raw = epub.read(path)
            heading = re.search(br'<h1\b[^>]*>.*?</h1\s*>', raw, re.S | re.I)
            end = re.search(br'</body\s*>', raw, re.I)
            body = re.search(br'<body\b[^>]*>', raw, re.I)
            page_title = next((''.join(n.itertext()).strip() for n in xml(raw).iter() if local_name(n.tag) == 'title'), '分卷')
            if body and end:
                pages.append({'id': 'page_' + hashlib.sha256(path.encode()).hexdigest()[:12],
                              'title': '插图 · 扉页',
                              'preview': [b.text for b in blocks if b.kind == 'text'],
                              'path': path, 'start': heading.end() if heading else body.end(), 'end': end.start(),
                              'index': len(paragraphs)})
        if not MOJI_CHAPTER.search(path):
            continue
        if any(b.kind == 'image' for b in blocks):
            raise BookError('mojimoon 正文仍含插图，请先生成无图底稿再合并。')
        headings = [b.text for b in blocks if b.kind == 'heading']
        title = headings[0] if headings else label or '正文'
        chapter = Chapter('c%04d' % (len(chapters) + 1), title)
        raw = epub.read(path)
        heading = re.search(br'<h1\b[^>]*>.*?</h1\s*>', raw, re.S | re.I)
        body = re.search(br'<body\b[^>]*>', raw, re.I)
        if heading or body:
            point = heading.end() if heading else body.end()
            boundaries[chapter.id] = {'path': path, 'start': point, 'end': point}
        matches = list(re.finditer(br'<p(?=[\s>])[^>]*>.*?</p\s*>', raw, re.S | re.I))
        extracted = []
        for match in matches:
            text_blocks = [b for b in FlowParser().read(match.group()) if b.kind == 'text']
            if not text_blocks:
                continue
            if len(text_blocks) != 1:
                raise BookError('无图底稿含无法安全定位的段内换行，请使用内置的 mojimoon 生成器。')
            text = text_blocks[0].text
            pid = 'p%05d' % (len(paragraphs) + 1)
            paragraph = Paragraph(pid, text, chapter.id, title, len(paragraphs))
            chapter.paragraphs.append(paragraph)
            paragraphs.append(paragraph)
            anchors[pid] = {'path': path, 'start': match.start(), 'end': match.end()}
            extracted.append(text)
        expected = [b.text for b in blocks if b.kind == 'text']
        if extracted != expected:
            raise BookError('底稿结构与 mojimoon 模板不一致，无法保证原样插图；请重新生成无图 EPUB。')
        chapters.append(chapter)
    if not paragraphs or len(paragraphs) > MAX_PARAGRAPHS:
        raise BookError('没有读到有效正文，或正文超过 16000 段。')
    warnings = ['已识别 mojimoon/wenku8 无图底稿：保留原有简介与正文 CSS；导出时目录改为同级，扉页保留插图并去除重复标题。']
    if epub.cover_path:
        warnings.append('底稿自带封面：选择新的高清封面后会替换原封面；否则保留原封面。')
    return TextBook(epub.title or filename, epub.author, chapters, paragraphs, filename,
                    hashlib.sha256(data).hexdigest(), warnings, data, anchors, epub.metadata, pages, boundaries)
