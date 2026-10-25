"""Thin adapter around an unmodified, pinned copy of mojimoon/wenku8."""
import argparse
from collections import deque
import importlib
import io
import zipfile
from xml.etree import ElementTree as ET
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time

from .books import BookError, Epub
from .presentation import prepare_epub, serialize_xml
from .metadata import volume_title, read_metadata, write_metadata, OPF
from .wenku_txt import download_parameters, volume_id, parse_catalog, fetch_packed, clean_download

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / 'third_party' / 'mojimoon_wenku8'
OUTPUT = ROOT / 'out' / 'text'
CACHE = ROOT / 'out' / 'cache'


def book_id(value):
    if isinstance(value, bool):
        raise BookError('请输入文库小说 ID 或 wenku8 小说链接。')
    text = str(value).strip()
    packed = download_parameters(text)
    if packed:
        aid = packed[0]
    elif text.isdigit():
        aid = int(text)
    else:
        match = re.fullmatch(r'https?://(?:www\.)?wenku8\.(?:net|com|cc)/(?:book/(\d+)\.htm|modules/article/articleinfo\.php\?id=(\d+))/?', text, re.I)
        if not match:
            raise BookError('请输入小说数字 ID 或文库的 book/数字.htm 链接。')
        aid = int(next(x for x in match.groups() if x))
    if not 0 < aid < 10000000:
        raise BookError('小说 ID 无效。')
    return aid


def volume_number(value):
    text = str(value)
    if not text.isdigit() or not 1 <= int(text) <= 2000:
        raise BookError('目录序号必须是 1～2000 的整数，对应文库目录中的第几本。')
    return int(text)


def load_upstream():
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    try:
        module = importlib.import_module('gen_epub')
        importlib.import_module('utils.fetcher').COOKIE_FILE = str(ROOT / 'COOKIE')
        return module
    except ImportError as exc:
        raise BookError('缺少正文生成依赖，请先运行：uv sync --locked') from exc


def source_fetcher(upstream, scraper):
    class WenkuFetcher(upstream.Fetcher):
        def get(self, url, validate=None, encoding='gb18030'):
            # The upstream detail request omitted its GBK encoding. GB18030 also
            # covers characters outside GBK used by some chapter pages.
            html = super().get(url, validate, encoding='gb18030' if encoding == 'gbk' else encoding)
            if '\ufffd' in html:
                raise BookError('源站网页解码失败，已停止生成，避免把乱码写入电子书。')
            return html
        def get_bytes(self, url):
            from .books import MAX_FILE
            for attempt in range(3):
                try:
                    if self.scraper == 'curl_cffi':
                        if self._cffi is None:
                            from curl_cffi import requests as cffi
                            self._cffi = cffi.Session(impersonate='chrome', headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.wenku8.net/'})
                        session = self._cffi
                    else:
                        session = self.session
                    response = session.get(url, timeout=30)
                    if response.status_code != 200:
                        raise BookError('TXT 下载失败：HTTP %s' % response.status_code)
                    if len(response.content)>MAX_FILE:
                        raise BookError('分卷 TXT 超过 80 MB。')
                    return response.content
                except Exception as exc:
                    if attempt == 2: raise BookError('无法下载分卷 TXT：'+str(exc)) from exc
                    time.sleep(1+attempt)
    return WenkuFetcher(scraper, fallback=False)


def generate_no_images(aid, volume=1, scraper='requests', *, vid=None, fetcher=None, output_root=None, output_format='epub'):
    if output_format not in {'epub','txt'}: raise BookError('输出格式无效。')
    packed = download_parameters(aid)
    if packed:
        if vid not in (None,'') and volume_id(vid) != packed[1]:
            raise BookError('填写的分卷 ID 与下载链接中的 vid 不一致。')
        vid = packed[1]
    aid, volume, vid = book_id(aid), volume_number(volume), volume_id(vid)
    if scraper not in {'requests','curl_cffi'}:
        raise BookError('只支持普通连接或 curl_cffi。')
    upstream = load_upstream()
    output = Path(output_root) if output_root else OUTPUT
    cache_root = output.parent/'cache' if output_root else CACHE
    owned = fetcher is None
    try:
        if owned: fetcher = source_fetcher(upstream, scraper)
        detail = upstream.parse_detail(fetcher.get(upstream.book_url(aid),lambda h:'id="content"' in h))
        if '\ufffd' in detail.get('title',''):
            raise BookError('源站书名解码失败，已停止生成。')
        # The HTML supplies catalogue/metadata only. Its availability notice does
        # not describe whether the user's packed-TXT source returns prose.
        catalog = parse_catalog(fetcher.get(upstream.toc_url(aid),lambda h:'vcss' in h,encoding='gbk'),upstream.toc_url(aid))
        if vid is not None:
            selected = next((v for v in catalog if vid in {v['vid'],v['chapters'][0]['cid']}),None)
            if selected is None: raise BookError('该分卷 ID 不在本书目录中；请使用目录卷 ID 或首章 ID。')
        else:
            selected = next((v for v in catalog if v['ordinal']==volume),None)
            if selected is None: raise BookError('指定的卷不存在。')
        volume = selected['ordinal']
        selected['download_vid'] = vid if vid is not None else selected['vid']
        print('    从分卷 TXT 获取正文：aid=%s，vid=%s' % (aid,selected['download_vid']),flush=True)
        chapters,txt_url,encoding,cached,raw_txt = fetch_packed(fetcher,aid,selected,cache_root,detail.get('update',''),include_raw=True,parse_chapters=output_format != 'txt')
        print('    %s，实际编码 %s%s' % ('复用 TXT 缓存' if cached else '已下载 TXT',encoding,('，%s 章' % len(chapters)) if output_format=='epub' else '，保存时还原字符实体并移除插图占位段落'),flush=True)
        vol_title = volume_title(selected['title'])
        title = (detail.get('title') or str(aid))+' '+vol_title
        description = detail.get('description','') if volume == 1 else ''
        origin = '全书简介' if description else ''
        series_index = str(volume)
        if output_format == 'txt':
            saved_txt = clean_download(raw_txt)
            path = output/str(aid)/('v%02d.txt' % volume)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(saved_txt)
            return {'aid':aid,'volume':volume,'vid':selected['download_vid'],'title':title,'file':str(path.resolve()),
                    'filename':re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',title)[:120]+'.txt',
                    'size':len(saved_txt),'encoding':encoding,'format':'txt','author':detail.get('author',''),
                    'description':description,'description_source':origin,'series':detail.get('title',''),'series_index':series_index,'subjects':detail.get('tags','').split()}
        meta = upstream.NovelMeta(title=title,author=detail.get('author',''),source_url=upstream.book_url(aid),
            description=description,publisher=detail.get('publisher',''),subjects=detail.get('tags','').split(),
            status=detail.get('status',''),modified=detail.get('update',''),identifier='urn:wenku8:%s:v%s:packtxt' % (aid,volume),
            series=detail.get('title') or str(aid),series_index=series_index)
        vol = upstream.Volume(vol_title,[upstream.Chapter(c['title'],[('p',p) for p in c['paragraphs']]) for c in chapters])
        path=output/str(aid)/('v%02d.epub' % volume)
        path.parent.mkdir(parents=True,exist_ok=True)
        upstream.create_epub(meta,[vol],{},None,str(path),prefix='v%02d_' % volume)
        epub=Epub(path.read_bytes())
        write_metadata(epub.opf,title,meta.author,{**read_metadata(epub.opf),'description_source':origin})
        md=epub.opf.find('{%s}metadata' % OPF)
        ET.SubElement(md,'{http://purl.org/dc/elements/1.1/}source').text=txt_url
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as archive:
            for info in epub.zip.infolist():
                archive.writestr(info,serialize_xml(epub.opf) if info.filename==epub.opf_path else epub.read(info.filename))
        path.write_bytes(prepare_epub(buf.getvalue()))
        entry={'index':volume,'title':vol_title,'vid':selected['download_vid'],'file':path.name,'chapters':len(chapters),'size':path.stat().st_size,'images':0}
        index_path=path.parent/'index.json'
        try: index=json.loads(index_path.read_text(encoding='utf-8'))
        except (OSError,ValueError): index={}
        previous=[v for v in index.get('volumes',[]) if v.get('index')!=volume]
        index.update(aid=aid,title=meta.series,author=meta.author,total_volumes=len(catalog),volumes=sorted(previous+[entry],key=lambda v:v['index']))
        index_path.write_text(json.dumps(index,ensure_ascii=False,indent=1),encoding='utf-8')
        print('    完成：%s / %s / %s' % (title,meta.author,origin),flush=True)
        return {'aid':aid,'volume':volume,'vid':selected['download_vid'],'title':title,'file':str(path.resolve()),
            'filename':re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',title)[:120]+'.epub','size':path.stat().st_size,
            'volume_title':vol_title,'author':meta.author,'text_source':'packtxt','encoding':encoding,**read_metadata(epub.opf)}
    finally:
        if owned and fetcher is not None: fetcher.close()


def main():
    parser = argparse.ArgumentParser(description='使用 mojimoon/wenku8 生成真正无图的分卷 EPUB。')
    parser.add_argument('--aid', required=True, help='小说 ID 或文库书籍链接')
    parser.add_argument('--volume', default='1', help='文库目录中第几本（包含短篇集和外传），默认 1')
    parser.add_argument('--vid', help='可选的分卷 ID，留空时按卷序号自动取得')
    parser.add_argument('--scraper', choices=['requests', 'curl_cffi'], default='requests')
    parser.add_argument('--format', dest='output_format', choices=['epub','txt'], default='epub', help='清理并保存 TXT 或生成无图 EPUB')
    args = parser.parse_args()
    try:
        result = generate_no_images(args.aid, args.volume, args.scraper, vid=args.vid, output_format=args.output_format)
    except Exception as exc:
        print('[ERROR] ' + str(exc), flush=True)
        parser.exit(1)
    print('RESULT:' + json.dumps(result, ensure_ascii=False), flush=True)


class GeneratorJobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None

    def start(self, aid, volume, scraper, vid=None, output_format='epub'):
        if output_format not in {'epub','txt'}: raise BookError('输出格式无效。')
        packed = download_parameters(aid)
        if packed:
            if vid not in (None,'') and volume_id(vid) != packed[1]: raise BookError('分卷 ID 与下载链接不一致。')
            vid = packed[1]
        aid, volume, vid = book_id(aid), volume_number(volume), volume_id(vid)
        if scraper not in {'requests', 'curl_cffi'}:
            raise BookError('连接方式无效。')
        with self.lock:
            if self.active:
                raise BookError('正在生成另一卷，请等待完成。')
            now = time.monotonic()
            self.jobs = {k: v for k, v in self.jobs.items() if now - v['created'] < 4 * 3600}
            while len(self.jobs) >= 8:
                del self.jobs[min(self.jobs, key=lambda k: self.jobs[k]['created'])]
            token = secrets.token_urlsafe(24)
            self.jobs[token] = {'status': 'running', 'aid': aid, 'volume': volume,
                                'created': now, 'lines': deque(maxlen=8), 'result': None, 'error': None}
            self.active = token
        threading.Thread(target=self._run, args=(token, aid, volume, scraper, vid, output_format), daemon=True).start()
        return token

    def _run(self, token, aid, volume, scraper, vid=None, output_format='epub'):
        command = [sys.executable, '-u', str(ROOT / 'generate.py'),
                   '--aid', str(aid), '--volume', str(volume), '--scraper', scraper]
        if output_format != 'epub': command.extend(['--format',output_format])
        if vid is not None: command.extend(['--vid',str(vid)])
        process = None
        result = None
        try:
            process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace',
                                       **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
            for line in process.stdout:
                line = line.strip()
                if line.startswith('RESULT:'):
                    result = json.loads(line[7:])
                elif line:
                    with self.lock:
                        self.jobs[token]['lines'].append(line[:500])
            code = process.wait()
            if code or not result:
                raise BookError('生成失败，请查看进度中的错误；登录失效、源站验证或不存在的卷会导致失败。')
            path = Path(result['file']).resolve()
            if not path.is_relative_to(OUTPUT.resolve()) or not path.is_file():
                raise BookError('生成文件路径无效。')
            with self.lock:
                self.jobs[token]['status'] = 'done'
                self.jobs[token]['result'] = result
        except Exception as exc:
            with self.lock:
                self.jobs[token]['status'] = 'error'
                self.jobs[token]['error'] = str(exc)
        finally:
            if process and process.stdout:
                process.stdout.close()
            with self.lock:
                self.active = None

    def get(self, token, *, private=False):
        with self.lock:
            job = self.jobs.get(token)
            if not job:
                raise BookError('生成任务不存在，请重新生成。')
            result = dict(job['result']) if job['result'] else None
            if result and not private:
                result.pop('file', None)
            return {'status': job['status'], 'aid': job['aid'], 'volume': job['volume'],
                    'lines': list(job['lines']), 'result': result, 'error': job['error']}

    def download(self, token):
        job = self.get(token, private=True)
        if job['status'] != 'done':
            raise BookError('文件尚未准备完成。')
        path = Path(job['result']['file']).resolve()
        if not path.is_relative_to(OUTPUT.resolve()) or not path.is_file():
            raise BookError('生成文件已不存在，请重新生成。')
        return path.read_bytes(), job['result']['filename']
