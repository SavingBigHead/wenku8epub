"""Fetch packed volume text; use website HTML only for metadata and the TOC."""
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urlparse, parse_qs, urlencode

from .books import BookError, MAX_FILE


def download_parameters(value):
    parts = urlparse(str(value).strip())
    if parts.scheme not in {'http','https'} or parts.hostname != 'dl.wenku8.com' or parts.path != '/packtxt.php':
        return None
    query = parse_qs(parts.query)
    try:
        return int(query['aid'][0]), int(query['vid'][0])
    except (KeyError,ValueError,IndexError):
        raise BookError('TXT 下载链接需要有效的 aid 和 vid。')


def volume_id(value):
    if value in (None, ''): return None
    if isinstance(value,bool) or not str(value).isdigit() or not 0 < int(value) < 1000000000:
        raise BookError('分卷 ID（vid）必须是正整数；也可以留空自动取得。')
    return int(value)


def parse_catalog(html, base):
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    volumes=[]
    for td in BeautifulSoup(html,'html.parser').select('td.vcss,td.ccss'):
        if 'vcss' in td.get('class',[]):
            volumes.append({'title':td.get_text(' ',strip=True),'vid':volume_id(td.get('vid')),'chapters':[],'ordinal':len(volumes)+1})
        elif volumes:
            a=td.find('a',href=True)
            if a:
                match=re.search(r'/(\d+)\.htm(?:$|[?#])',urljoin(base,a['href']))
                if not match: raise BookError('目录中的章节编号无效。')
                volumes[-1]['chapters'].append({'title':a.get_text(' ',strip=True),'cid':int(match[1])})
    volumes=[v for v in volumes if v['chapters']]
    for v in volumes:
        v['vid']=v['vid'] or v['chapters'][0]['cid']
    if not volumes: raise BookError('源站目录为空，无法确认分卷与章节。')
    return volumes


def decode_download(data):
    if not data or len(data)>MAX_FILE: raise BookError('分卷 TXT 为空或超过 80 MB。')
    if data.startswith((b'\xff\xfe',b'\xfe\xff')):
        encoding='utf-16'
    elif data.startswith(b'\xef\xbb\xbf'):
        encoding='utf-8-sig'
    else:
        encoding='utf-8'
    try:
        text=data.decode(encoding)
    except UnicodeError as exc:
        raise BookError('下载的 TXT 编码无效，已停止生成，避免产生乱码。') from exc
    if '\ufffd' in text or '\x00' in text:
        raise BookError('下载的 TXT 包含无效字符，已停止生成。')
    if re.match(r'\s*<(?:!doctype|html|head|body|\?xml)\b',text,re.I):
        raise BookError('TXT 接口返回了网页或错误提示，请稍后重试。')
    if '因版权问题，文库不再提供该小说' in text[:1500] and len(text.strip())<600:
        raise BookError('TXT 接口没有返回小说正文。')
    return text,encoding


def normalized(value):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',value))


def packed_lines(text):
    lines=text.replace('\r\n','\n').replace('\r','\n').split('\n')
    while lines and not lines[0].strip(): lines.pop(0)
    if lines and re.fullmatch(r'[ ★☆]*轻小说文库\([^\n]*\)[ ★☆]*',lines[0]): lines.pop(0)
    while lines and not lines[-1].strip(): lines.pop()
    if lines and re.fullmatch(r'更多精彩热门日本轻小说、动漫小说，轻小说文库\(https?://www\.wenku8\.com\) 为你一网打尽！',lines[-1].strip(),re.I):
        lines.pop()
        while lines and not lines[-1].strip(): lines.pop()
        if lines and re.fullmatch(r'[ ◆◇]+',lines[-1]): lines.pop()
    return lines


def split_chapters(text, volume):
    # Exact source TOC labels avoid treating dialogue or numbered prose as headings.
    lines=packed_lines(text)
    expected=volume['chapters']
    labels=[{normalized(c['title']),normalized(volume['title']+c['title'])} for c in expected]
    chapters=[]
    current=None
    next_index=0
    found=set()
    for line in lines:
        stripped=line.strip(' \t\u3000\xa0')
        if not stripped: continue
        # Packed headings have two leading spaces; paragraphs normally have four.
        indent=len(line)-len(line.lstrip(' '))
        candidate=normalized(stripped)
        match=next((i for i in range(next_index,len(expected)) if candidate in labels[i]),None) if indent<=2 else None
        if match is not None:
            skipped=[c['title'] for c in expected[next_index:match] if c['title'] not in {'插图','插圖'}]
            if skipped: raise BookError('TXT 缺少目录章节：'+'、'.join(skipped[:3]))
            current={'title':expected[match]['title'],'paragraphs':[]}
            chapters.append(current);found.add(match);next_index=match+1
        elif current is not None:
            current['paragraphs'].append(stripped)
        elif candidate == normalized(volume['title']):
            continue
        else:
            raise BookError('TXT 开头与本卷目录不一致，无法安全切分章节。')
    missing=[c['title'] for i,c in enumerate(expected) if i not in found and c['title'] not in {'插图','插圖'}]
    if missing: raise BookError('TXT 缺少目录章节：'+'、'.join(missing[:3]))
    chapters=[c for c in chapters if c['paragraphs']]
    if not chapters: raise BookError('TXT 中没有可用的正文段落。')
    return chapters


def fetch_packed(fetcher, aid, volume, cache_root, update='', *, include_raw=False, parse_chapters=True):
    vid=volume['download_vid']
    url='https://dl.wenku8.com/packtxt.php?'+urlencode({'aid':aid,'vid':vid,'charset':'utf-8'})
    cache=Path(cache_root)/str(aid)/'packtxt'/str(vid)
    signature=hashlib.sha256(json.dumps([update,volume['title'],volume['chapters']],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    data=None
    try:
        meta=json.loads(cache.with_suffix('.json').read_text())
        if meta.get('signature')==signature and time.time()-meta.get('saved',0)<86400:
            candidate=cache.with_suffix('.bin').read_bytes()
            if hashlib.sha256(candidate).hexdigest()==meta.get('sha256'): data=candidate
    except (OSError,ValueError,TypeError): pass
    cached=data is not None
    if data is None: data=fetcher.get_bytes(url)
    text,encoding=decode_download(data)
    chapters=split_chapters(text,volume) if parse_chapters else []
    if not cached:
        cache.parent.mkdir(parents=True,exist_ok=True)
        cache.with_suffix('.bin').write_bytes(data)
        cache.with_suffix('.json').write_text(json.dumps({'signature':signature,'saved':time.time(),'sha256':hashlib.sha256(data).hexdigest(),'url':url,'encoding':encoding},ensure_ascii=False),encoding='utf-8')
    result = (chapters,url,encoding,cached)
    return result + (data,) if include_raw else result
