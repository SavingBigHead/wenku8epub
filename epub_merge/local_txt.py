"""Convert a local single-volume TXT into an image-free mojimoon EPUB."""
import hashlib
import io
import zipfile
import os.path
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace

from .books import BookError, MAX_FILE, MAX_PARAGRAPHS, decode_text, parse_text, Epub
from .metadata import metadata_values, normalize_volumes, volume_title, VOLUME, write_metadata
from .presentation import prepare_epub, serialize_xml
from .wenku_txt import packed_lines, split_chapters


def convert_txt(data, filename, settings=None):
    if not filename.lower().endswith('.txt') or not data or len(data) > MAX_FILE:
        raise BookError('请选择 80 MB 以内的非空 TXT 文件。')
    settings = settings if settings is not None else {}
    if not isinstance(settings, dict): raise BookError('TXT 书籍信息无效。')
    encoding = settings.get('encoding', 'auto')
    if encoding not in {'auto','utf-8','utf-16','gb18030'}: raise BookError('不支持这个 TXT 编码。')
    text = decode_text(data, None if encoding == 'auto' else ('utf-8-sig' if encoding == 'utf-8' else encoding))
    if '\ufffd' in text or '\x00' in text: raise BookError('TXT 编码不匹配，请选择正确的编码。')
    if re.match(r'\s*<(?:!doctype|html|head|body|\?xml)\b', text, re.I): raise BookError('这个文件是网页，不是小说 TXT。')
    title = settings.get('title') or re.sub(r'\.txt$', '', filename, flags=re.I)
    author = settings.get('author', '')
    if not isinstance(title,str) or not isinstance(author,str) or len(title)>300 or len(author)>300:
        raise BookError('书名或作者格式无效。')
    title, author = normalize_volumes(title.strip()), author.strip()
    if not title: raise BookError('请填写书名。')
    vol_label = settings.get('volume_title', '')
    if not isinstance(vol_label,str) or len(vol_label)>300: raise BookError('分卷标题格式无效。')
    number = VOLUME.search(title)
    vol_label = volume_title(vol_label or (number[0] if number else title))
    lines = packed_lines(text)
    banner = bool(re.match(r'\s*[★☆]+轻小说文库\(', text))
    headings = [line.strip() for line in lines if re.match(r'^ {1,2}\S', line) and len(line.strip())<=200]
    warnings = []
    if banner and headings:
        prefix = os.path.commonprefix(headings) if len(headings)>1 else ''
        prefix = prefix[:prefix.rfind(' ')+1] if ' ' in prefix else ''
        if not VOLUME.search(prefix): prefix = ''
        if prefix: vol_label = normalize_volumes(prefix.strip())
        labels = [h[len(prefix):].strip() if prefix else h for h in headings]
        if len(labels) != len(set(labels)): raise BookError('TXT 含重复章节标题，请按卷拆分后生成。')
        volume = {'title':prefix.strip() or vol_label,'chapters':[{'title':h} for h in labels]}
        chapters = split_chapters(text, volume)
    else:
        book = parse_text('\n'.join(lines).encode('utf-8'), filename)
        chapters = [{'title':c.title,'paragraphs':[p.text for p in c.paragraphs]} for c in book.chapters if c.paragraphs]
        warnings.append('按常见章节标题分章；不常见的标题可能保留在正文中，请检查目录。')
    count = sum(len(c['paragraphs']) for c in chapters)
    if not count or count>MAX_PARAGRAPHS: raise BookError('TXT 没有有效正文，或超过每卷 16000 段。')
    if not VOLUME.search(title) and not title.endswith(vol_label): title += ' ' + vol_label
    number = VOLUME.search(title)
    base = {'series':title[:number.start()].strip() if number else title,
            'series_index':''}
    values = metadata_values(SimpleNamespace(metadata=base), settings)
    from third_party.mojimoon_wenku8 import epub_maker as maker
    meta = maker.NovelMeta(title=title,author=author,description=values['description'],subjects=values['subjects'],
                          series=values['series'],series_index=values['series_index'],
                          identifier='urn:sha256:'+hashlib.sha256(data).hexdigest())
    vol = maker.Volume(vol_label,[maker.Chapter(c['title'],[('p',p) for p in c['paragraphs']]) for c in chapters])
    with tempfile.TemporaryDirectory() as folder:
        target = str(Path(folder)/'base.epub')
        maker.create_epub(meta,[vol],{},None,target,prefix='v01_')
        epub = Epub(Path(target).read_bytes())
        write_metadata(epub.opf,title,author,values)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            for info in epub.zip.infolist():
                archive.writestr(info,serialize_xml(epub.opf) if info.filename == epub.opf_path else epub.read(info.filename))
        result = prepare_epub(buffer.getvalue())
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip(' .')[:120] or '无图底稿'
    return result, safe+'.epub', {'chapters':len(chapters),'paragraphs':count,'warnings':warnings}
