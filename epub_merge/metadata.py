"""EPUB metadata and human-readable volume numbering shared by both exporters."""
import re
from xml.etree import ElementTree as ET

DC = 'http://purl.org/dc/elements/1.1/'
OPF = 'http://www.idpf.org/2007/opf'
DIGITS = dict(zip('零〇一二三四五六七八九两', [0,0,1,2,3,4,5,6,7,8,9,2]))
VOLUME = re.compile(r'第\s*([零〇一二三四五六七八九十百千两０-９0-9]+(?:[.．点點][零〇一二三四五六七八九０-９0-9]+)?)\s*[卷巻]')


def number(value):
    value = value.translate(str.maketrans('０１２３４５６７８９．点點', '0123456789...'))
    parts = value.split('.')
    text = parts[0]
    if text.isascii() and text.isdigit():
        whole = int(text)
    elif not any(c in text for c in '十百千'):
        whole = int(''.join(str(DIGITS[c] if c in DIGITS else int(c)) for c in text))
    else:
        whole, digit = 0, 0
        for c in text:
            if c in '十百千':
                whole += (digit or 1) * {'十':10,'百':100,'千':1000}[c]
                digit = 0
            else:
                digit = DIGITS[c] if c in DIGITS else int(c)
        whole += digit
    tail = ''.join(str(DIGITS.get(c, c)) for c in parts[1]) if len(parts) > 1 else ''
    return str(whole).zfill(2) + ('.' + tail if tail else '')


def normalize_volumes(value):
    return VOLUME.sub(lambda m: '第' + number(m[1]) + '卷', str(value))


def volume_title(value, ordinal):
    title = normalize_volumes(value.strip())
    return title if VOLUME.search(title) else '第%02d卷 %s' % (ordinal, title)


def read_metadata(opf):
    md = opf.find('{%s}metadata' % OPF)
    result = {'description':'', 'series':'', 'series_index':'', 'subjects':[], 'description_source':''}
    if md is None:
        return result
    for node in md:
        text = ''.join(node.itertext()).strip()
        if node.tag == '{%s}description' % DC:
            result['description'] = text
        elif node.tag == '{%s}subject' % DC and text:
            result['subjects'].append(text)
        elif node.get('property') == 'belongs-to-collection' and not node.get('refines'):
            result['series'] = result['series'] or text
        elif node.get('property') == 'group-position':
            result['series_index'] = result['series_index'] or text
        elif node.get('name') == 'calibre:series':
            result['series'] = result['series'] or node.get('content', '')
        elif node.get('name') == 'calibre:series_index':
            result['series_index'] = result['series_index'] or node.get('content', '')
        elif node.get('name') == 'wenku8:description-source':
            result['description_source'] = node.get('content', '')
    result['subjects'] = list(dict.fromkeys(result['subjects']))
    return result


def metadata_values(book, settings):
    from .books import BookError
    values = {**book.metadata, **{k: settings[k] for k in ['description','series','series_index','subjects','description_source'] if k in settings}}
    defaults = {'description':'','series':'','series_index':'','subjects':[], 'description_source':''}
    values = {**defaults, **values}
    for key, limit in [('description',20000),('series',300),('series_index',30),('description_source',100)]:
        if not isinstance(values[key], str) or len(values[key]) > limit:
            raise BookError('书籍元数据格式无效：' + key)
        values[key] = values[key].strip()
    if values['series_index'] and not re.fullmatch(r'\d{1,4}(?:\.\d{1,3})?', values['series_index']):
        raise BookError('系列卷号请填写数字，例如 1 或 6.5。')
    if not isinstance(values['subjects'], list) or len(values['subjects']) > 100 or any(not isinstance(s,str) or len(s)>100 for s in values['subjects']):
        raise BookError('标签格式无效，每个标签最多 100 字。')
    values['subjects'] = list(dict.fromkeys(s.strip() for s in values['subjects'] if s.strip()))
    return values


def write_metadata(opf, title, author, values):
    md = opf.find('{%s}metadata' % OPF)
    for key, value in [('title',title),('creator',author),('description',values['description'])]:
        nodes = md.findall('{%s}%s' % (DC,key))
        node = nodes[0] if nodes else ET.SubElement(md,'{%s}%s' % (DC,key))
        node.text = value
        for duplicate in nodes[1:]:
            md.remove(duplicate)
    # Replace only the series collection, retaining unrelated collection metadata.
    collection_ids = {x.get('id') for x in md if x.get('property')=='belongs-to-collection' and
                      any(y.get('refines')=='#'+x.get('id','') and y.get('property')=='collection-type' and y.text=='series' for y in md)}
    collection_ids |= {x.get('id') for x in md if x.get('property')=='belongs-to-collection' and ''.join(x.itertext()).strip()==read_metadata(opf)['series']}
    for node in list(md):
        if node.tag == '{%s}subject' % DC or node.get('name') in {'calibre:series','calibre:series_index','wenku8:description-source'} or (node.get('property')=='belongs-to-collection' and node.get('id') in collection_ids) or node.get('refines') in {'#'+x for x in collection_ids if x}:
            md.remove(node)
    for tag in values['subjects']:
        ET.SubElement(md,'{%s}subject' % DC).text = tag
    if values['series']:
        existing_ids = {x.get('id') for x in opf.iter()}
        sid='em_series'
        while sid in existing_ids: sid='x'+sid
        ET.SubElement(md,'{%s}meta' % OPF,{'property':'belongs-to-collection','id':sid}).text=values['series']
        ET.SubElement(md,'{%s}meta' % OPF,{'refines':'#'+sid,'property':'collection-type'}).text='series'
        ET.SubElement(md,'{%s}meta' % OPF,{'name':'calibre:series','content':values['series']})
        if values['series_index']:
            ET.SubElement(md,'{%s}meta' % OPF,{'refines':'#'+sid,'property':'group-position'}).text=values['series_index']
            ET.SubElement(md,'{%s}meta' % OPF,{'name':'calibre:series_index','content':values['series_index']})
    if values['description_source']:
        ET.SubElement(md,'{%s}meta' % OPF,{'name':'wenku8:description-source','content':values['description_source']})
