"""Replay review edits on a copy, retaining untouched source XHTML bytes."""
import copy
import html
import io
import re
import zipfile
from collections import defaultdict

from .books import BookError, Paragraph, MAX_PARAGRAPHS


def paragraph_content(text):
    # A line break keeps an intentional blank paragraph visible in EPUB readers.
    return html.escape(text) if text.strip() else '<br/>'


def apply_edits(book, settings, preserve_source=True):
    if not isinstance(settings, dict):
        raise BookError('项目设置无效。')
    edits = settings.get('text_edits', [])
    if not isinstance(edits, list) or len(edits) > 10000:
        raise BookError('段落修改记录无效或超过 10000 条。')
    if not edits:
        return book
    result = copy.deepcopy(book)
    chapters = {c.id: c for c in result.chapters}
    paragraphs = {p.id: p for p in result.paragraphs}
    used = set(paragraphs)
    total = 0
    for edit in edits:
        if not isinstance(edit, dict) or edit.get('op') not in {'add', 'edit', 'delete'}:
            raise BookError('段落修改操作无效。')
        kind, id_ = edit['op'], edit.get('id')
        if not isinstance(id_, str):
            raise BookError('段落编号无效。')
        if kind != 'delete':
            text = edit.get('text')
            if not isinstance(text, str) or len(text) > 20000:
                raise BookError('每段最多 20000 字，可留空；删除请使用删除按钮。')
            if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', text):
                raise BookError('段落中含有无法写入 EPUB 的字符。')
            total += len(text)
            if total > 2000000:
                raise BookError('段落修改文字超过 200 万字。')
        if kind == 'add':
            if not re.fullmatch(r'u_[a-zA-Z0-9_-]{1,80}', id_) or id_ in used:
                raise BookError('新增段落编号无效或重复。')
            chapter = chapters.get(edit.get('chapter_id')) if isinstance(edit.get('chapter_id'), str) else None
            if chapter is None:
                raise BookError('新增段落的章节不存在。')
            after = edit.get('after_id')
            if after is None:
                index = 0
            elif isinstance(after, str) and after in paragraphs and paragraphs[after].chapter_id == chapter.id:
                index = chapter.paragraphs.index(paragraphs[after]) + 1
            else:
                raise BookError('新增段落的相邻位置不存在或跨越章节。')
            p = Paragraph(id_, text, chapter.id, chapter.title, 0)
            chapter.paragraphs.insert(index, p)
            paragraphs[id_] = p
            used.add(id_)
            if len(paragraphs) > MAX_PARAGRAPHS:
                raise BookError('正文段落超过上限。')
        else:
            p = paragraphs.get(id_)
            if p is None:
                raise BookError('修改的段落不存在，可能已被删除。')
            if kind == 'edit':
                p.text = text
            else:
                chapters[p.chapter_id].paragraphs.remove(p)
                del paragraphs[id_]
    result.paragraphs = [p for c in result.chapters for p in c.paragraphs]
    for index, p in enumerate(result.paragraphs):
        p.index = index
    if book.source_epub is not None and preserve_source:
        patch_source(book, result)
    return result


def patch_source(original, result):
    # Replace only paragraph spans; keep original headings, gaps, styling and pages.
    source = zipfile.ZipFile(io.BytesIO(original.source_epub))
    original_by_id = {p.id: p for p in original.paragraphs}
    surviving = {p.id: p for p in result.paragraphs}
    leading, trailing = defaultdict(list), defaultdict(list)
    for old_chapter, chapter in zip(original.chapters, result.chapters):
        pending = []
        for p in chapter.paragraphs:
            if p.id in original_by_id:
                leading[p.id].extend(pending)
                pending = []
            else:
                pending.append(p)
        if pending:
            if old_chapter.paragraphs:
                trailing[old_chapter.paragraphs[-1].id].extend(pending)
    events = defaultdict(list)
    contents = {}
    for old in original.paragraphs:
        a = original.anchors[old.id]
        if a['path'] not in contents:
            contents[a['path']] = source.read(a['path'])
        raw = contents[a['path']][a['start']:a['end']]
        payload, offsets = bytearray(), []
        def append(p, markup):
            start = len(payload)
            payload.extend(markup)
            offsets.append((p.id, start, len(payload)))
        for p in leading[old.id]:
            append(p, ('<p>'+paragraph_content(p.text)+'</p>').encode('utf-8'))
        if old.id in surviving:
            p = surviving[old.id]
            markup = raw if p.text == old.text else raw[:raw.index(b'>')+1] + paragraph_content(p.text).encode('utf-8') + b'</p>'
            append(p, markup)
        for p in trailing[old.id]:
            append(p, ('<p>'+paragraph_content(p.text)+'</p>').encode('utf-8'))
        events[a['path']].append((a['start'], a['end'], bytes(payload), offsets))
    for old_chapter, chapter in zip(original.chapters, result.chapters):
        if old_chapter.paragraphs:
            continue
        a = original.chapter_boundaries.get(chapter.id)
        if a is None:
            raise BookError('空章节缺少正文位置。')
        payload, offsets = bytearray(), []
        for p in chapter.paragraphs:
            start = len(payload)
            payload.extend(('<p>'+paragraph_content(p.text)+'</p>').encode('utf-8'))
            offsets.append((p.id, start, len(payload)))
        events[a['path']].append((a['start'], a['start'], bytes(payload), offsets))
    replacements, result.anchors = {}, {}
    boundaries = {}
    for path, changes in events.items():
        content, cursor, output = source.read(path), 0, bytearray()
        starts = {}
        for start, end, payload, offsets in sorted(changes):
            output.extend(content[cursor:start])
            starts[start] = len(output)
            base = len(output)
            output.extend(payload)
            for id_, lo, hi in offsets:
                result.anchors[id_] = {'path': path, 'start': base+lo, 'end': base+hi}
            cursor = end
        output.extend(content[cursor:])
        replacements[path] = bytes(output)
        for chapter in original.chapters:
            a = original.anchors[chapter.paragraphs[0].id] if chapter.paragraphs else original.chapter_boundaries.get(chapter.id)
            if a and a['path'] == path:
                boundaries[chapter.id] = {'path': path, 'start': starts[a['start']], 'end': starts[a['start']]}
    result.chapter_boundaries = boundaries
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for info in source.infolist():
            z.writestr(info, replacements.get(info.filename, source.read(info.filename)))
    result.source_epub = buf.getvalue()
