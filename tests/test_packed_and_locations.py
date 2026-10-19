import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.parse import parse_qs,urlparse

from epub_merge import wenku
from epub_merge.wenku_txt import decode_download,parse_catalog,split_chapters,fetch_packed
from epub_merge.locations import chapter_start,chapter_end
from epub_merge.books import Epub,BookError,parse_text,parse_donor,FlowParser
from epub_merge.demo import mojimoon_demo_files,demo_files
from epub_merge.align import Matcher,align
from epub_merge.export import initial_settings,build_epub,build_report


class PackedTextTests(unittest.TestCase):
    def setUp(self):
        self.volume={'title':'第一卷 测试','ordinal':1,'vid':50,'download_vid':50,'chapters':[{'title':'一 开端','cid':51},{'title':'二 后续','cid':52},{'title':'插图','cid':53}]}
        self.text=' ★☆★☆轻小说文库(Www.WenKu8.com)☆★☆★ \n\n  第一卷 测试 一 开端  \n    台版 转自 测试来源\n    正文包含 wenku8 的字样也不能被删。\n    二 后续\n  第一卷 测试 二 后续  \n    下一章正文。\n\n ◆◇◆◇◆◇ \n更多精彩热门日本轻小说、动漫小说，轻小说文库(http://www.wenku8.com) 为你一网打尽！ '

    def test_actual_bom_overrides_requested_utf8_label(self):
        for encoding in ['utf-8','utf-8-sig','utf-16','utf-16-be']:
            data=self.text.encode(encoding)
            if encoding=='utf-16-be':data=b'\xfe\xff'+data
            value,actual=decode_download(data)
            self.assertEqual(value,self.text)
        for data in [b'',b'<html>503 Service Unavailable</html>',b'\xffbad',b'bad\x00text','坏�字'.encode()]:
            with self.subTest(data=data),self.assertRaises(BookError):decode_download(data)

    def test_exact_toc_split_preserves_credits_and_title_like_prose(self):
        chapters=split_chapters(self.text,self.volume)
        self.assertEqual([c['title'] for c in chapters],['一 开端','二 后续'])
        self.assertEqual(chapters[0]['paragraphs'],['台版 转自 测试来源','正文包含 wenku8 的字样也不能被删。','二 后续'])
        self.assertEqual(chapters[1]['paragraphs'],['下一章正文。'])
        plain=self.text.replace('第一卷 测试 ','')
        self.assertEqual(split_chapters(plain,self.volume),chapters)

    def test_missing_or_reordered_chapters_fail_instead_of_silently_losing_text(self):
        for text in ['  二 后续\n    下一章正文。','  一 开端\n    第一章正文。','错误页：请登录']:
            with self.subTest(text=text),self.assertRaises(BookError):split_chapters(text,self.volume)

    def test_catalog_uses_actual_volume_id_without_reordering_illustrations(self):
        html='<td class="vcss" vid="50">第一卷 测试</td><td class="ccss"><a href="51.htm">一 开端</a></td><td class="ccss"><a href="53.htm">插图</a></td>'
        catalog=parse_catalog(html,'https://www.wenku8.net/novel/0/1/index.htm')
        self.assertEqual(catalog[0]['vid'],50)
        self.assertEqual(catalog[0]['chapters'][0]['cid'],51)
        self.assertEqual(catalog[0]['chapters'][1]['title'],'插图')

    def test_cache_reuses_only_valid_matching_volume_text(self):
        fetcher=Mock()
        fetcher.get_bytes.return_value=self.text.encode('utf-16')
        with tempfile.TemporaryDirectory() as folder:
            first=fetch_packed(fetcher,1163,self.volume,Path(folder),'2026-10-19')
            second=fetch_packed(fetcher,1163,self.volume,Path(folder),'2026-10-19')
            self.assertFalse(first[3]);self.assertTrue(second[3]);self.assertEqual(first[:3],second[:3])
            fetcher.get_bytes.assert_called_once()
            url=fetcher.get_bytes.call_args.args[0]
            self.assertEqual(urlparse(url).netloc,'dl.wenku8.com')
            self.assertEqual(parse_qs(urlparse(url).query),{'aid':['1163'],'vid':['50'],'charset':['utf-8']})
            fetch_packed(fetcher,1163,self.volume,Path(folder),'2026-10-20')
            self.assertEqual(fetcher.get_bytes.call_count,2)

    def test_detail_notice_does_not_override_valid_downloaded_prose(self):
        class Fetcher:
            def __init__(self):self.urls=[]
            def get(self,url,validate=None,encoding=None):
                self.urls.append(url)
                if '/book/' in url:return '<title>测试系列 - 文库</title><div id="content">小说作者：测试作者<br/>作品Tags：校园 悬疑<br/>本书公告：因版权问题，文库不再提供该小说的在线阅读与下载服务！<br/>内容简介：<br/>测试简介<br/>阅读<br/>小说目录</div>'
                if url.endswith('index.htm'):return '<td class="vcss" vid="50">第一卷 测试</td><td class="ccss"><a href="51.htm">一 开端</a></td><td class="ccss"><a href="52.htm">二 后续</a></td>'
                raise AssertionError('不应获取逐章网页：'+url)
            def get_bytes(self,url):
                self.urls.append(url)
                return '  一 开端\n    第一章正文。\n  二 后续\n    第二章正文。'.encode('utf-16')
        for aid,vid in [('1163',None),('https://dl.wenku8.com/packtxt.php?aid=1163&vid=51&charset=utf-8',None),('1163',51)]:
            with self.subTest(aid=aid,vid=vid),tempfile.TemporaryDirectory() as folder:
                fetcher=Fetcher()
                result=wenku.generate_no_images(aid,fetcher=fetcher,vid=vid,output_root=Path(folder)/'text')
                book=parse_text(Path(result['file']).read_bytes(),'v01.epub')
                self.assertEqual([p.text for p in book.paragraphs],['第一章正文。','第二章正文。'])
                self.assertEqual(result['vid'],50 if aid=='1163' and vid is None else 51)
                self.assertEqual(result['encoding'],'utf-16')
                self.assertEqual(book.author,'测试作者')
                self.assertEqual(book.metadata['subjects'],['校园','悬疑'])
                self.assertEqual(len(fetcher.urls),3)

    def test_conflicting_download_link_and_vid_rejected(self):
        with self.assertRaises(BookError):
            wenku.generate_no_images('https://dl.wenku8.com/packtxt.php?aid=1163&vid=51',vid=52)
        for invalid in ['0','../2',True,'1;echo x']:
            with self.subTest(invalid=invalid),self.assertRaises(BookError):wenku.volume_id(invalid)


class ChapterOwnershipTests(unittest.TestCase):
    def setUp(self):
        text,donor=mojimoon_demo_files()
        self.book=parse_text(text,'base.epub')
        self.donor=parse_donor(donor,'donor.epub')
        self.settings=initial_settings(self.book,self.donor)
        for image in self.settings['images']:image.update(role='ignore',confirmed=True)
        self.end_id=chapter_end(self.book.chapters[0].id)
        self.start_id=chapter_start(self.book.chapters[1].id)
        self.settings['images'][2].update(role='body',after_id=self.end_id)
        self.settings['images'][3].update(role='body',after_id=self.start_id)

    def test_same_text_gap_exports_into_different_original_chapter_files(self):
        result=Epub(build_epub(self.book,self.donor,self.settings))
        first=self.book.chapters[0].paragraphs[-1]
        second=self.book.chapters[1].paragraphs[0]
        a=result.read(self.book.anchors[first.id]['path'])
        b=result.read(self.book.anchors[second.id]['path'])
        self.assertIn(b'em_i0003',a);self.assertNotIn(b'em_i0004',a)
        self.assertIn(b'em_i0004',b);self.assertNotIn(b'em_i0003',b)
        self.assertLess(a.index(first.text.encode()),a.index(b'em_i0003'))
        self.assertLess(b.index(b'</h1>'),b.index(b'em_i0004'))
        self.assertLess(b.index(b'em_i0004'),b.index(second.text.encode()))
        report=build_report(self.book,self.donor,self.settings)
        self.assertIn('本章开头',report);self.assertIn('本章结尾',report)

    def test_generic_export_preserves_explicit_chapter_ownership(self):
        text,_=demo_files()
        book=parse_text(text,'source.txt')
        settings=initial_settings(book,self.donor)
        for i in settings['images']:i.update(role='ignore',confirmed=True)
        settings['images'][2].update(role='body',after_id=chapter_end(book.chapters[0].id))
        settings['images'][3].update(role='body',after_id=chapter_start(book.chapters[1].id))
        result=Epub(build_epub(book,self.donor,settings))
        first=result.read('OEBPS/text/c0001-001.xhtml')
        second=result.read('OEBPS/text/c0002-001.xhtml')
        self.assertIn(b'i0003.svg',first);self.assertNotIn(b'i0004.svg',first)
        self.assertIn(b'i0004.svg',second);self.assertNotIn(b'i0003.svg',second)
        self.assertLess(second.index(b'</h1>'),second.index(b'i0004.svg'))
        self.assertLess(second.index(b'i0004.svg'),second.index(book.chapters[1].paragraphs[0].text.encode()))

    def test_matcher_offers_both_chapter_sides_and_keeps_them_distinct(self):
        image=self.donor.illustrations[2]
        image.before=[self.book.chapters[0].paragraphs[-1].text]
        image.after=[self.book.chapters[1].paragraphs[0].text]
        image.chapter=self.book.chapters[1].title
        suggestions=Matcher(self.book).suggestions(image)
        ids=[s['after_id'] for s in suggestions]
        self.assertIn(self.end_id,ids);self.assertIn(self.start_id,ids)
        self.assertLess(ids.index(self.start_id),ids.index(self.end_id))

    def test_coincident_legacy_paragraph_and_end_anchor_keep_image_order(self):
        self.settings['images'][2]['after_id']=self.book.chapters[0].paragraphs[-1].id
        self.settings['images'][3]['after_id']=self.end_id
        result=Epub(build_epub(self.book,self.donor,self.settings))
        data=result.read(self.book.anchors[self.book.chapters[0].paragraphs[-1].id]['path'])
        self.assertLess(data.index(b'em_i0003'),data.index(b'em_i0004'))

    def test_invalid_chapter_token_rejected(self):
        self.settings['images'][2]['after_id']=chapter_start('missing')
        with self.assertRaises(BookError):build_epub(self.book,self.donor,self.settings)
