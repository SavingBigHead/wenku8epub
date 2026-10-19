"""Insertion anchors include chapter ownership, not only a global text gap."""
START = '__start__'
CHAPTER_START = '__chapter_start__:'
CHAPTER_END = '__chapter_end__:'
PAGE_START = '__page_start__:'
PAGE_END = '__page_end__:'


def page_start(id_): return PAGE_START+id_

def page_end(id_): return PAGE_END+id_


def page_location(book, anchor):
    for page in book.pages:
        if anchor == page_start(page['id']): return page, 'start'
        if anchor == page_end(page['id']): return page, 'end'
    return None


def chapter_start(id_): return CHAPTER_START+id_

def chapter_end(id_): return CHAPTER_END+id_


def valid_locations(book):
    return {anchor(p['id']) for p in book.pages for anchor in [page_start,page_end]} | {START} | {p.id for p in book.paragraphs} | {anchor(c.id) for c in book.chapters for anchor in [chapter_start,chapter_end]}


def resolve_location(book, anchor):
    if anchor == START: return book.paragraphs[0], 'before'
    for chapter in book.chapters:
        if chapter.paragraphs:
            if anchor == chapter_start(chapter.id): return chapter.paragraphs[0], 'before'
            if anchor == chapter_end(chapter.id): return chapter.paragraphs[-1], 'after'
    return next(p for p in book.paragraphs if p.id == anchor), 'after'


def empty_chapter(book, anchor):
    for c in book.chapters:
        if not c.paragraphs and anchor in {chapter_start(c.id), chapter_end(c.id)}:
            return c
    if anchor == START and not book.paragraphs:
        return book.chapters[0]
    return None


def boundary_index(book, anchor):
    empty = empty_chapter(book, anchor)
    if empty:
        return sum(len(c.paragraphs) for c in book.chapters[:book.chapters.index(empty)])
    page = page_location(book, anchor)
    if page: return page[0]['index']
    paragraph,side=resolve_location(book,anchor)
    return paragraph.index+(side=='after')


def location_label(book,anchor):
    if anchor == START: return '正文开始前'
    page = page_location(book, anchor)
    if page: return page[0]['title'] + (' · 扉页开头' if page[1] == 'start' else ' · 页面末尾')
    empty = empty_chapter(book, anchor)
    if empty:
        return empty.title + (' · 本章开头' if anchor.startswith(CHAPTER_START) else ' · 本章结尾')
    paragraph,side=resolve_location(book,anchor)
    if anchor.startswith(CHAPTER_START): return paragraph.chapter_title+' · 本章开头（标题之后、首段之前）'
    if anchor.startswith(CHAPTER_END): return paragraph.chapter_title+' · 本章结尾（末段之后）'
    return paragraph.chapter_title+' · 在下段之后：'+paragraph.text


def canonical_location(book, anchor):
    for chapter in book.chapters:
        if chapter.paragraphs and anchor == chapter.paragraphs[-1].id:
            return chapter_end(chapter.id)
    return anchor
