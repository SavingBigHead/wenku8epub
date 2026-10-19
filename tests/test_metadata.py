import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
import zipfile

from epub_merge import wenku
from epub_merge.books import Epub, parse_text, parse_donor, BookError
from epub_merge.demo import mojimoon_demo_files
from epub_merge.export import build_epub, initial_settings
from epub_merge.metadata import normalize_volumes, volume_title


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.data, self.donor_data = mojimoon_demo_files()
        self.book = parse_text(self.data, 'base.epub')
        self.donor = parse_donor(self.donor_data, 'donor.epub')
        self.settings = initial_settings(self.book, self.donor)
        for image in self.settings['images']:
            image['role'] = 'ignore'

    def test_volume_numbers_preserve_actual_decimal_volume(self):
        for title, expected in [('第一卷','第01卷'),('第十一卷','第11卷'),('第两百零一卷','第201卷'),('第6.5卷','第06.5卷'),('第六点五卷','第06.5卷'),('第０２卷 副标题','第02卷 副标题'),('故事 第01卷','故事 第01卷')]:
            self.assertEqual(normalize_volumes(title), expected)
        self.assertEqual(volume_title('番外篇', 8), '第08卷 番外篇')
        self.assertEqual(volume_title('第6.5卷', 8), '第06.5卷')

    def test_metadata_imported_and_exported_with_original_style(self):
        self.assertEqual(self.settings['series'], '雨夜来信')
        self.assertEqual(self.settings['series_index'], '1')
        self.assertIn('合成示例', self.settings['description'])
        self.settings.update(title='新书名 第二卷',author='新作者',description='本卷简介第一段。\n第二段 & 特殊字符。',series='新系列',series_index='2',subjects=['校园','青春','校园'],description_source='手动编辑')
        output = Epub(build_epub(self.book, self.donor, self.settings))
        self.assertEqual(output.title,'新书名 第02卷')
        self.assertEqual(output.author,'新作者')
        self.assertEqual(output.metadata['description'],self.settings['description'])
        self.assertEqual(output.metadata['subjects'],['校园','青春'])
        self.assertEqual(output.metadata['series'],'新系列')
        self.assertEqual(output.metadata['series_index'],'2')
        intro=output.read('OEBPS/Text/v01_Intro.xhtml').decode()
        self.assertIn('新书名 第02卷',intro)
        self.assertIn('新作者',intro)
        self.assertIn('第二段 &amp; 特殊字符。',intro)
        self.assertIn('校园 青春',intro)
        source=Epub(self.data)
        self.assertEqual(output.read('OEBPS/Styles/style.css'),source.read('OEBPS/Styles/style.css'))
        for item in source.names:
            if '/Text/' in item and 'Intro' not in item and '/v01_v' not in item:
                self.assertEqual(output.read(item),source.read(item))

    def test_clear_metadata_removes_stale_tags_series_and_description(self):
        self.settings.update(series='',series_index='',description='',subjects=[])
        output=Epub(build_epub(self.book,self.donor,self.settings))
        self.assertEqual(output.metadata['series'],'')
        self.assertEqual(output.metadata['description'],'')
        self.assertEqual(output.metadata['subjects'],[])
        self.assertNotIn('这是本地合并工具'.encode(),output.read('OEBPS/Text/v01_Intro.xhtml'))

    def test_legacy_project_without_new_fields_retains_source_metadata(self):
        for key in ['description','series','series_index','subjects','description_source']:
            self.settings.pop(key,None)
        output=Epub(build_epub(self.book,self.donor,self.settings))
        self.assertEqual(output.metadata,self.book.metadata)

    def test_invalid_metadata_rejected(self):
        for value in [{'subjects':'校园'},{'subjects':[None]},{'series_index':'第一卷'},{'description':{}}]:
            with self.subTest(value=value),self.assertRaises(BookError):
                build_epub(self.book,self.donor,{**self.settings,**value})

    def test_generic_epub_retains_metadata_too(self):
        data=build_epub(self.book,self.donor,{**self.settings,'description':'全书简介','subjects':['校园']})
        book=parse_text(data,'test.epub')
        book.source_epub=None
        settings=initial_settings(book,self.donor)
        for image in settings['images']: image['role']='ignore'
        output=Epub(build_epub(book,self.donor,settings))
        self.assertEqual(output.metadata['description'],'全书简介')
        self.assertEqual(output.metadata['series'],'雨夜来信')
        self.assertEqual(output.metadata['subjects'],['校园'])


class SourceEncodingTests(unittest.TestCase):
    def test_real_gb18030_response_with_default_detail_call(self):
        import requests
        upstream=wenku.load_upstream()
        fetcher=wenku.source_fetcher(upstream,'requests')
        fetcher.delay=(0,0)
        response=requests.Response()
        response.status_code=200
        response.url='https://www.wenku8.net/book/1213.htm'
        response._content=('<meta charset="gbk"><title>测试青春小说 - 作者 - 文库</title><div id="content"><table><tr><td>小说作者：测试作者</td><td>作品Tags：校园 青春 恋爱</td></tr></table>内容简介：<br/>全书简介𠀀<br/>阅读<br/>小说目录</div>').encode('gb18030')
        with patch.object(fetcher.session,'get',return_value=response):
            detail=upstream.parse_detail(fetcher.get(response.url))
        self.assertEqual(detail['title'],'测试青春小说')
        self.assertEqual(detail['author'],'测试作者')
        self.assertEqual(detail['tags'],'校园 青春 恋爱')
        self.assertEqual(detail['description'],'全书简介𠀀')
        fetcher.close()

    def test_lost_characters_do_not_silently_generate(self):
        upstream=wenku.load_upstream()
        fetcher=wenku.source_fetcher(upstream,'requests')
        with patch.object(upstream.Fetcher,'get',return_value='<title>测试�乱码</title>'):
            with self.assertRaisesRegex(BookError,'解码失败'):
                fetcher.get('https://www.wenku8.net/book/1213.htm')
        fetcher.close()

    def test_explicit_volume_synopsis_has_priority_and_number_survives(self):
        class Fixture:
            def get(self,url,validate=None,encoding='gb18030'):
                if '/book/' in url:
                    return '<title>测试小说 - 文库</title><div id="content">小说作者：测试作者<br/>作品Tags：校园 青春<br/>内容简介：<br/>全书简介<br/>阅读<br/>小说目录</div>'
                if url.endswith('index.htm'):
                    return '<td class="vcss">第6.5卷 番外</td><td class="ccss"><a href="1.htm">本卷简介</a></td><td class="ccss"><a href="2.htm">第一章</a></td>'
                if url.endswith('/1.htm'):
                    return '<div id="content">这是该卷单独的简介。</div>'
                return '<div id="content">正文内容。</div>'
            def get_bytes(self,url):
                return '  本卷简介  \n    这是该卷单独的简介。\n  第一章  \n    正文内容。'.encode('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            result=wenku.generate_no_images(129,1,fetcher=Fixture(),output_root=Path(folder)/'text')
            output=Epub(Path(result['file']).read_bytes())
            self.assertEqual(output.title,'测试小说 第06.5卷 番外')
            self.assertIn('第06.5卷',result['filename'])
            self.assertEqual(output.metadata['description'],'这是该卷单独的简介。')
            self.assertEqual(output.metadata['description_source'],'本卷简介')
            self.assertEqual(output.metadata['series_index'],'6.5')
            self.assertEqual(output.metadata['subjects'],['校园','青春'])
            self.assertNotIn(b'<h1',output.read('OEBPS/Text/v01_v1.xhtml'))
            self.assertIn('第06.5卷'.encode(),output.read('OEBPS/Text/v01_Intro.xhtml'))
