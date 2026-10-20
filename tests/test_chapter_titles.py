import copy
import io
import json
import threading
import unittest
import urllib.request
import zipfile

from epub_merge.books import BookError, Epub, parse_text, parse_donor, xml
from epub_merge.demo import mojimoon_demo_files, demo_files
from epub_merge.edits import apply_edits
from epub_merge.export import initial_settings, build_epub, build_report
from epub_merge.locations import chapter_start, chapter_end
from epub_merge.presentation import XHTML, NCX
from epub_merge.server import make_server


class ChapterTitleEditingTests(unittest.TestCase):
    def setUp(self):
        text, donor = mojimoon_demo_files()
        self.book = parse_text(text, 'base.epub')
        self.donor = parse_donor(donor, 'donor.epub')
        self.settings = initial_settings(self.book, self.donor)
        for image in self.settings['images']:
            image.update(role='ignore', confirmed=True)

    def rename(self, title, chapter=None):
        return {'op':'rename_chapter', 'id':chapter or self.book.chapters[0].id, 'text':title}

    def assert_title(self, epub, path, title):
        root = xml(epub.read(path))
        self.assertEqual(''.join(root.find('{%s}head/{%s}title' % (XHTML, XHTML)).itertext()), title)
        heading = root.find('{%s}body/{%s}h1' % (XHTML, XHTML))
        self.assertEqual(''.join(heading.itertext()), title)
        for item in epub.manifest.values():
            if 'nav' in item.get('properties','').split():
                links = [a for a in xml(epub.read(item['path'])).iter('{%s}a' % XHTML)
                         if epub.resolve(item['path'],a.get('href','')) == path]
                self.assertTrue(links)
                self.assertTrue(all(''.join(a.itertext()) == title for a in links))
            elif item.get('media-type') == 'application/x-dtbncx+xml':
                labels = []
                for point in xml(epub.read(item['path'])).iter('{%s}navPoint' % NCX):
                    node = point.find('{%s}content' % NCX)
                    if node is not None and epub.resolve(item['path'], node.get('src','')) == path:
                        labels.append(''.join(point.find('{%s}navLabel/{%s}text' % (NCX, NCX)).itertext()))
                self.assertEqual(labels,[title])

    def test_preview_renames_chapter_and_paragraph_labels_without_mutating_original(self):
        original = copy.deepcopy(self.book.public())
        self.settings['text_edits'] = [self.rename('  新章节 <&> 😀  ')]
        preview = apply_edits(self.book,self.settings,preserve_source=False)
        self.assertEqual(preview.chapters[0].title,'新章节 <&> 😀')
        self.assertTrue(all(p.chapter_title == '新章节 <&> 😀' for p in preview.chapters[0].paragraphs))
        self.assertEqual([p.id for p in preview.paragraphs],[p.id for p in self.book.paragraphs])
        self.assertEqual(self.book.public(),original)
        self.assertIs(preview.source_epub,self.book.source_epub)

    def test_preserved_export_syncs_heading_both_contents_and_offsets(self):
        first = self.book.chapters[0]
        path = self.book.chapter_boundaries[first.id]['path']
        title = '新的长章节标题 <&> 中文😀'
        self.settings['text_edits'] = [self.rename(title)]
        self.settings['images'][2].update(role='body',after_id=chapter_start(first.id),confirmed=True)
        self.settings['images'][3].update(role='body',after_id=first.paragraphs[-1].id,confirmed=True)
        prepared = apply_edits(self.book,self.settings)
        prepared_epub = Epub(prepared.source_epub)
        for p in prepared.paragraphs:
            anchor = prepared.anchors[p.id]
            old = self.book.anchors[p.id]
            self.assertEqual(prepared_epub.read(anchor['path'])[anchor['start']:anchor['end']],
                             Epub(self.book.source_epub).read(old['path'])[old['start']:old['end']])
        restored = json.loads(json.dumps(self.settings))
        result = Epub(build_epub(self.book,self.donor,restored))
        self.assert_title(result,path,title)
        raw = result.read(path)
        self.assertLess(raw.index(b'</h1>'),raw.index(b'em_i0003'))
        self.assertLess(raw.index(b'em_i0003'),raw.index(first.paragraphs[0].text.encode()))
        self.assertLess(raw.index(first.paragraphs[-1].text.encode()),raw.index(b'em_i0004'))
        source = Epub(self.book.source_epub)
        other_path = self.book.chapter_boundaries[self.book.chapters[1].id]['path']
        self.assertEqual(result.read(other_path),source.read(other_path))
        for name in source.names:
            if name.endswith('.css') or '/Images/' in name:
                self.assertEqual(result.read(name),source.read(name))
        self.assertEqual(restored['images'],self.settings['images'])
        report = build_report(self.book,self.donor,restored)
        self.assertIn('章节标题修改记录：1 条',report)
        self.assertIn('段落修改记录：0 条',report)
        self.assertIn('新的长章节标题 &lt;&amp;&gt; 中文😀',report)

    def test_rename_interleaves_with_add_edit_delete_and_blank_paragraphs(self):
        first = self.book.paragraphs[0]
        self.settings['text_edits'] = [
            self.rename('临时标题'),
            {'op':'add','id':'u_blank','chapter_id':first.chapter_id,'after_id':None,'text':''},
            {'op':'edit','id':first.id,'text':'修改后的正文'},
            self.rename('最后标题'),
            {'op':'delete','id':self.book.paragraphs[1].id},
            {'op':'add','id':'u_tail','chapter_id':first.chapter_id,'after_id':first.id,'text':'新增段落'},
        ]
        self.settings['images'][2].update(role='body',after_id='u_blank',confirmed=True)
        prepared = apply_edits(self.book,self.settings)
        self.assertTrue(all(p.chapter_title == '最后标题' for p in prepared.chapters[0].paragraphs))
        output = Epub(build_epub(self.book,self.donor,self.settings))
        path = self.book.chapter_boundaries[first.chapter_id]['path']
        self.assert_title(output,path,'最后标题')
        self.assertIn(b'<p><br/></p>',output.read(path))
        self.assertLess(output.read(path).index(b'em_i0003'),output.read(path).index('修改后的正文'.encode()))

    def test_empty_chapter_can_be_renamed_and_receive_illustrations(self):
        first = self.book.chapters[0]
        self.settings['text_edits'] = [{'op':'delete','id':p.id} for p in first.paragraphs] + [self.rename('空章节的新标题')]
        self.settings['images'][2].update(role='body',after_id=chapter_start(first.id),confirmed=True)
        self.settings['images'][3].update(role='body',after_id=chapter_end(first.id),confirmed=True)
        output = Epub(build_epub(self.book,self.donor,self.settings))
        path = self.book.chapter_boundaries[first.id]['path']
        self.assert_title(output,path,'空章节的新标题')
        self.assertLess(output.read(path).index(b'em_i0003'),output.read(path).index(b'em_i0004'))

    def test_original_empty_chapter_can_be_renamed_and_have_text_added(self):
        first = self.book.chapters[0]
        path = self.book.chapter_boundaries[first.id]['path']
        source = Epub(self.book.source_epub)
        raw = source.read(path)
        for p in reversed(first.paragraphs):
            anchor = self.book.anchors[p.id]
            raw = raw[:anchor['start']]+raw[anchor['end']:]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf,'w') as archive:
            for info in source.zip.infolist():
                archive.writestr(info, raw if info.filename == path else source.read(info.filename))
        book = parse_text(buf.getvalue(),'empty.epub')
        settings = initial_settings(book,self.donor)
        for image in settings['images']:
            image.update(role='ignore',confirmed=True)
        settings['text_edits'] = [self.rename('原本空章节'),
            {'op':'add','id':'u_empty','chapter_id':first.id,'after_id':None,'text':'新正文'}]
        settings['images'][2].update(role='body',after_id='u_empty',confirmed=True)
        output = Epub(build_epub(book,self.donor,settings))
        self.assert_title(output,path,'原本空章节')
        self.assertLess(output.read(path).index('新正文'.encode()),output.read(path).index(b'em_i0003'))

    def test_generic_export_allows_duplicate_chapter_titles(self):
        text,_ = demo_files()
        book = parse_text(text,'source.txt')
        settings = initial_settings(book,self.donor)
        for image in settings['images']:
            image.update(role='ignore',confirmed=True)
        settings['text_edits'] = [self.rename('相同标题',chapter.id) for chapter in book.chapters]
        output = Epub(build_epub(book,self.donor,settings))
        for chapter in book.chapters:
            self.assert_title(output,'OEBPS/text/'+chapter.id+'-001.xhtml','相同标题')

    def test_rename_back_to_original_restores_source_bytes(self):
        self.settings['text_edits'] = [self.rename('临时标题'),self.rename(self.book.chapters[0].title)]
        prepared = apply_edits(self.book,self.settings)
        original,updated = Epub(self.book.source_epub),Epub(prepared.source_epub)
        self.assertTrue(all(original.read(path) == updated.read(path) for path in original.names))

    def test_invalid_titles_and_unknown_chapters_rejected(self):
        for edit in [self.rename(''),self.rename(' \t\n'),self.rename('字'*301),self.rename(None),
                     self.rename('坏'+chr(0)),self.rename('坏'+chr(0xd800)),self.rename('标题','missing'),
                     self.rename('标题',self.book.paragraphs[0].id)]:
            with self.subTest(edit=repr(edit)),self.assertRaises(BookError):
                apply_edits(self.book,{'text_edits':[edit]})

    def test_preview_api_and_export_preserve_original_session(self):
        server = make_server(0)
        worker = threading.Thread(target=server.serve_forever,daemon=True)
        worker.start()
        try:
            token = server.state.add(self.book,self.donor)
            self.settings['text_edits'] = [self.rename('接口修改的标题')]
            base = 'http://127.0.0.1:%d' % server.server_address[1]
            request = urllib.request.Request(base+'/api/text/edit', json.dumps({'session':token,'text_edits':self.settings['text_edits']}).encode(), {'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as response:
                preview = json.load(response)
            self.assertEqual(preview['chapters'][0]['title'],'接口修改的标题')
            self.assertNotEqual(server.state.get(token)[0].chapters[0].title,'接口修改的标题')
            request = urllib.request.Request(base+'/api/export', json.dumps({'session':token,'settings':self.settings}).encode(), {'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as response:
                output = Epub(response.read())
            self.assert_title(output,self.book.chapter_boundaries[self.book.chapters[0].id]['path'],'接口修改的标题')
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
