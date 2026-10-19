import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from epub_merge.books import BookError, Epub, parse_text, parse_donor
from epub_merge.demo import mojimoon_demo_files
from epub_merge.export import initial_settings, build_epub
from epub_merge.local_txt import convert_txt
from epub_merge.locations import page_start
from epub_merge.server import make_server
from epub_merge import wenku
from epub_merge.presentation import NCX, XHTML


class TxtConversionTests(unittest.TestCase):
    def test_utf16_and_utf8_preserve_same_unicode_text_in_utf8_epub(self):
        text = '第一章 开始\n正文𠀀与emoji😀。\n第二章 结尾\n第二段 & <符号>。'
        contents = []
        for encoding in ['utf-16','utf-8','utf-8-sig']:
            data, filename, info = convert_txt(text.encode(encoding), '测试 第01卷.txt', {'author':'作者','description':'简介','subjects':['校园']})
            book = Epub(data)
            parsed = parse_text(data,filename)
            contents.append([p.text for p in parsed.paragraphs])
            self.assertEqual(len(parsed.chapters),2)
            self.assertEqual(parsed.author,'作者')
            self.assertEqual(book.metadata['series'],'测试')
            self.assertEqual(book.metadata['description'],'简介')
            self.assertEqual(book.metadata['subjects'],['校园'])
            self.assertFalse(any(v.get('media-type','').startswith('image/') for v in book.manifest.values()))
            for path in book.names:
                if path.endswith(('.xhtml','.opf','.ncx')): book.read(path).decode('utf-8')
        self.assertTrue(all(texts==['正文𠀀与emoji😀。','第二段 & <符号>。'] for texts in contents))

    def test_packed_headings_credits_and_volume_prefix(self):
        text = ' ★☆轻小说文库(Www.WenKu8.com)☆★ \n\n  第一卷 冰果 一 来信\n    台版 转自 来源\n    正文。\n  第一卷 冰果 二 重生\n    第二章正文。\n ◆◇◆◇ \n更多精彩热门日本轻小说、动漫小说，轻小说文库(http://www.wenku8.com) 为你一网打尽！'
        data, filename, info = convert_txt(text.encode('utf-16'), '古典部 第01卷.txt')
        parsed = parse_text(data,filename)
        self.assertEqual([c.title for c in parsed.chapters],['一 来信','二 重生'])
        self.assertEqual([p.text for p in parsed.paragraphs],['台版 转自 来源','正文。','第二章正文。'])
        self.assertEqual(info['chapters'],2)
        self.assertEqual(info['warnings'],[])

    def test_gbk_and_invalid_input(self):
        source = '第一章 开始\n文库正文。'.encode('gbk')
        for encoding in ['auto','gb18030']:
            data, filename, _ = convert_txt(source,'测试.txt',{'encoding':encoding})
            self.assertEqual(parse_text(data,filename).paragraphs[0].text,'文库正文。')
        for data, name, settings in [(b'', 'test.txt', {}),(b'<html>Error</html>','test.txt',{}),(source,'test.epub',{}),(source,'test.txt',{'encoding':'bad'}),(b'bad\x00text','test.txt',{}),(source,'test.txt',[]),(source,'test.txt',{'encoding':'utf-8'})]:
            with self.subTest(settings=settings),self.assertRaises(BookError): convert_txt(data,name,settings)

    def test_both_contents_label_title_page_as_illustrations(self):
        text, art = mojimoon_demo_files()
        book, donor = parse_text(text,'base.epub'),parse_donor(art,'art.epub')
        settings = initial_settings(book,donor)
        for image in settings['images']:image.update(role='ignore',confirmed=True)
        settings['images'][2].update(role='body',after_id=page_start(book.pages[0]['id']))
        output = Epub(build_epub(book,donor,settings))
        nav_item = next(v for v in output.manifest.values() if 'nav' in v.get('properties','').split())
        ncx_item = next(v for v in output.manifest.values() if v.get('media-type')=='application/x-dtbncx+xml')
        nav = ET.fromstring(output.read(nav_item['path']))
        ncx = ET.fromstring(output.read(ncx_item['path']))
        label = next(a.text for a in nav.iter('{%s}a'%XHTML) if output.resolve(nav_item['path'],a.get('href'))==book.pages[0]['path'])
        point = next(p for p in ncx.iter('{%s}navPoint'%NCX) if output.resolve(ncx_item['path'],p.find('{%s}content'%NCX).get('src'))==book.pages[0]['path'])
        self.assertEqual(label,'插图')
        self.assertEqual(point.find('{%s}navLabel/{%s}text'%(NCX,NCX)).text,'插图')


class RawTxtTests(unittest.TestCase):
    def test_txt_download_is_byte_identical_without_epub_generation(self):
        raw = ' ★☆轻小说文库(Www.WenKu8.com)☆★ \n  一 开始\n    正文𠀀。'.encode('utf-16')
        class Fetcher:
            def get(self,url,validate=None,encoding=None):
                if '/book/' in url:return '<title>测试系列 - 文库</title><div id="content">小说作者：测试作者<br/>内容简介：<br/>简介<br/>阅读<br/>小说目录</div>'
                return '<td class="vcss" vid="50">第一卷</td><td class="ccss"><a href="51.htm">一 开始</a></td>'
            def get_bytes(self,url): return raw
        with tempfile.TemporaryDirectory() as folder:
            upstream = wenku.load_upstream()
            with patch.object(upstream,'create_epub',side_effect=AssertionError('不能生成 EPUB')):
                result = wenku.generate_no_images('1163',fetcher=Fetcher(),output_root=Path(folder)/'text',output_format='txt')
            self.assertEqual(Path(result['file']).read_bytes(),raw)
            self.assertEqual(result['encoding'],'utf-16')
            self.assertTrue(result['filename'].endswith('第01卷.txt'))
            self.assertTrue(result['filename'].startswith('测试系列 '))
            self.assertEqual(result['author'],'测试作者')
            self.assertFalse(list(Path(folder).rglob('*.epub')))

    def test_raw_download_does_not_require_epub_chapter_splitting(self):
        from epub_merge.wenku_txt import fetch_packed
        raw = '正文可以保存，但与目录的章节标题不一致。'.encode('utf-16')
        fetcher = Mock(); fetcher.get_bytes.return_value = raw
        volume = {'download_vid':50,'title':'第一卷','chapters':[{'title':'第一章'}]}
        with tempfile.TemporaryDirectory() as folder:
            result = fetch_packed(fetcher,1163,volume,Path(folder),include_raw=True,parse_chapters=False)
            self.assertEqual(result[0],[])
            self.assertEqual(result[4],raw)
            with self.assertRaises(BookError): fetch_packed(fetcher,1163,volume,Path(folder))

    def test_background_job_passes_txt_format_to_cli(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(wenku,'OUTPUT',Path(folder)):
            path = Path(folder)/'1163'/'v01.txt';path.parent.mkdir();path.write_bytes('原始'.encode('utf-16'))
            result = {'file':str(path),'filename':'第01卷.txt','encoding':'utf-16','format':'txt'}
            process = Mock(stdout=io.StringIO('RESULT:'+json.dumps(result)+'\n'));process.wait.return_value=0
            jobs = wenku.GeneratorJobs()
            with patch.object(wenku.subprocess,'Popen',return_value=process) as popen:
                token=jobs.start('1163','1','requests',vid='50',output_format='txt')
                until=time.monotonic()+2
                while jobs.get(token)['status']=='running' and time.monotonic()<until:time.sleep(.01)
                self.assertEqual(jobs.get(token)['status'],'done')
                command=popen.call_args.args[0]
                self.assertEqual(command[command.index('--format')+1],'txt')
                self.assertEqual(command[command.index('--vid')+1],'50')
                self.assertEqual(jobs.download(token),(path.read_bytes(),'第01卷.txt'))


class TxtHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=make_server(0);cls.port=cls.server.server_address[1]
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def request(self,method,path,data=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        conn.request(method,path,data,headers or {})
        response=conn.getresponse();value=response.status,response.read(),dict(response.headers);conn.close();return value
    def test_local_txt_http_does_not_need_donor_or_network(self):
        boundary='txt-fixture'
        data=('--'+boundary+'\r\nContent-Disposition: form-data; name="text"; filename="local.txt"\r\nContent-Type: text/plain\r\n\r\n').encode()+'第一章\n正文😀。'.encode('utf-16')+('\r\n--'+boundary+'\r\nContent-Disposition: form-data; name="settings"\r\n\r\n'+json.dumps({'title':'本地 第01卷','author':'作者'},ensure_ascii=False)+'\r\n--'+boundary+'--\r\n').encode()
        with patch.object(wenku,'source_fetcher',side_effect=AssertionError('不能联网')):
            status,body,headers=self.request('POST','/api/txt/build',data,{'Content-Type':'multipart/form-data; boundary='+boundary})
        self.assertEqual(status,200)
        self.assertEqual(headers['Content-Type'],'application/epub+zip')
        self.assertEqual(Epub(body).author,'作者')
        self.assertEqual(parse_text(body,'local.epub').paragraphs[0].text,'正文😀。')
    def test_txt_job_download_preserves_bytes_and_enforces_requested_format(self):
        with patch.object(self.server.state.generator,'start',return_value='txt-fixture') as start:
            status,_,_=self.request('POST','/api/wenku/build',json.dumps({'aid':'1163','format':'txt'}),{'Content-Type':'application/json'})
            self.assertEqual(status,202);start.assert_called_once_with('1163',1,'requests',output_format='txt')
        raw='原始文字'.encode('utf-16')
        with patch.object(self.server.state.generator,'download',return_value=(raw,'原始.txt')):
            status,body,headers=self.request('GET','/api/wenku/jobs/txt-fixture/txt')
            self.assertEqual(status,200);self.assertEqual(body,raw)
            self.assertEqual(self.request('GET','/api/wenku/jobs/txt-fixture/epub')[0],400)
        self.assertEqual(self.request('POST','/api/wenku/build',json.dumps({'aid':'1163','format':'bad'}),{'Content-Type':'application/json'})[0],400)
