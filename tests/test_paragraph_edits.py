import copy
import io
import json
import threading
import unittest
import urllib.request
import zipfile

from epub_merge.books import BookError, Epub, FlowParser, parse_text, parse_donor, xml
from epub_merge.demo import mojimoon_demo_files, demo_files
from epub_merge.edits import apply_edits
from epub_merge.export import initial_settings, build_epub, build_report, validate
from epub_merge.locations import chapter_start, chapter_end, START
from epub_merge.server import make_server


class ParagraphEditingTests(unittest.TestCase):
    def setUp(self):
        text, donor = mojimoon_demo_files()
        self.book = parse_text(text, 'base.epub')
        self.donor = parse_donor(donor, 'donor.epub')
        self.settings = initial_settings(self.book, self.donor)
        for image in self.settings['images']:
            image.update(role='ignore', confirmed=True)

    def add(self, id_, after, text='新增段落<&>你好', chapter=None):
        return {'op':'add', 'id':id_, 'chapter_id':chapter or self.book.chapters[0].id, 'after_id':after, 'text':text}

    def body(self, id_, index=2):
        self.settings['images'][index].update(role='body', after_id=id_, confirmed=True)

    def prose(self, epub, path):
        return [b.text for b in FlowParser().read(epub.read(path)) if b.kind == 'text']

    def test_add_edit_delete_export_and_original_immutability(self):
        original = copy.deepcopy(self.book.public())
        first, second, third = self.book.paragraphs[:3]
        self.settings['text_edits'] = [self.add('u_start', None, '首段'),
            {'op':'edit', 'id':first.id, 'text':'修改后<&>中文😀'},
            self.add('u_after', first.id), {'op':'delete','id':second.id}]
        self.body('u_after')
        prepared = apply_edits(self.book, self.settings)
        result = Epub(build_epub(self.book, self.donor, self.settings))
        path = self.book.anchors[first.id]['path']
        data = result.read(path)
        self.assertEqual(self.prose(result, path)[:4], ['首段','修改后<&>中文😀','新增段落<&>你好',third.text])
        self.assertLess(data.index('新增段落'.encode()), data.index(b'em_i0003'))
        self.assertLess(data.index(b'em_i0003'), data.index(third.text.encode()))
        self.assertIn(b'&lt;&amp;&gt;',data)
        for p in [first, third, prepared.paragraphs[0], prepared.paragraphs[2]]:
            a=prepared.anchors[p.id]
            self.assertTrue(Epub(prepared.source_epub).read(a['path'])[a['start']:a['end']].startswith(b'<p'))
        self.assertEqual(self.book.public(), original)
        self.assertEqual(self.book.source_epub, Epub(self.book.source_epub).zip.fp.getvalue())
        original_zip=Epub(self.book.source_epub)
        for name in original_zip.names:
            if name.endswith('.css') or '/Images/' in name:
                self.assertEqual(result.read(name),original_zip.read(name))
        a=self.book.anchors[third.id]
        self.assertIn(original_zip.read(path)[a['start']:a['end']],data)
        for image in self.donor.illustrations:
            if image.id == self.settings['images'][2]['id']:
                self.assertEqual(result.read('OEBPS/Images/em_'+image.id+'.'+image.ext),image.data)
        report=build_report(self.book,self.donor,self.settings)
        self.assertIn('段落修改记录：4 条', report)
        self.assertIn('新增段落&lt;&amp;&gt;你好',report)

    def test_deleted_image_anchor_must_be_reassigned(self):
        p=self.book.paragraphs[2]
        self.settings['text_edits']=[{'op':'delete','id':p.id}]
        self.body(p.id)
        with self.assertRaises(BookError):build_epub(self.book,self.donor,self.settings)
        self.body(chapter_end(p.chapter_id))
        build_epub(self.book,self.donor,self.settings)

    def test_chapter_edges_follow_edits_and_keep_ownership(self):
        chapter=self.book.chapters[0]
        self.settings['text_edits']=[self.add('u_first',None,'最前段'), self.add('u_last',chapter.paragraphs[-1].id,'最后段')]
        self.body(chapter_start(chapter.id))
        self.body(chapter_end(chapter.id),3)
        result=Epub(build_epub(self.book,self.donor,self.settings))
        data=result.read(self.book.anchors[chapter.paragraphs[0].id]['path'])
        self.assertLess(data.index(b'em_i0003'),data.index('最前段'.encode()))
        self.assertLess(data.index('最后段'.encode()),data.index(b'em_i0004'))

    def test_delete_entire_chapter_and_add_back(self):
        chapter=self.book.chapters[0]
        self.settings['text_edits']=[{'op':'delete','id':p.id} for p in chapter.paragraphs]
        self.body(chapter_start(chapter.id))
        self.body(chapter_end(chapter.id),3)
        result=Epub(build_epub(self.book,self.donor,self.settings))
        path=self.book.anchors[chapter.paragraphs[0].id]['path']
        self.assertEqual(self.prose(result,path),[])
        self.assertLess(result.read(path).index(b'em_i0003'),result.read(path).index(b'em_i0004'))
        self.settings['text_edits'].append(self.add('u_return',None,'补回段落'))
        result=Epub(build_epub(self.book,self.donor,self.settings))
        self.assertEqual(self.prose(result,path),['补回段落'])
        self.assertLess(result.read(path).index(b'em_i0003'),result.read(path).index('补回段落'.encode()))
        self.assertIn('本章开头',build_report(self.book,self.donor,self.settings))

    def test_delete_all_text_start_anchor_and_project_roundtrip(self):
        self.settings['text_edits']=[{'op':'delete','id':p.id} for p in self.book.paragraphs]
        self.body(START)
        result=Epub(build_epub(self.book,self.donor,json.loads(json.dumps(self.settings))))
        for chapter in self.book.chapters:
            self.assertEqual(self.prose(result,self.book.anchors[chapter.paragraphs[0].id]['path']),[])
        self.assertIn('正文 0 段',build_report(self.book,self.donor,self.settings))

    def test_generic_export_uses_edited_paragraphs(self):
        text,_=demo_files()
        book=parse_text(text,'source.txt')
        settings=initial_settings(book,self.donor)
        for image in settings['images']: image.update(role='ignore',confirmed=True)
        p=book.paragraphs[0]
        settings['text_edits']=[{'op':'edit','id':p.id,'text':'换过的正文'}, self.add('u_generic',p.id,chapter=p.chapter_id)]
        settings['images'][2].update(role='body',after_id='u_generic',confirmed=True)
        result=Epub(build_epub(book,self.donor,settings))
        alltext=''.join(result.read(path).decode('utf-8') for path in result.names if path.endswith('.xhtml'))
        self.assertIn('换过的正文',alltext)
        self.assertIn('新增段落&lt;&amp;&gt;你好',alltext)
        self.assertNotIn(p.text,alltext)

    def test_same_offset_image_order_after_edits(self):
        p=self.book.chapters[0].paragraphs[-1]
        self.settings['text_edits']=[{'op':'edit','id':p.id,'text':'章末修改'}]
        self.body(p.id);self.body(chapter_end(p.chapter_id),3)
        data=Epub(build_epub(self.book,self.donor,self.settings)).read(self.book.anchors[p.id]['path'])
        self.assertLess(data.index(b'em_i0003'),data.index(b'em_i0004'))

    def test_invalid_operations_are_rejected(self):
        p=self.book.paragraphs[0]
        invalid=[None,{},[None],[{'op':'move','id':p.id}], [{'op':'edit','id':p.id,'text':None}],
            [{'op':'edit','id':p.id,'text':'a'+chr(0)}], [{'op':'edit','id':p.id,'text':'a'+chr(0xd800)}],
            [{'op':'edit','id':p.id,'text':'x'*20001}], [{'op':'delete','id':'missing'}],
            [self.add(p.id,p.id)], [self.add('u_same',p.id),self.add('u_same',p.id)],
            [self.add('u_bad',[])], [self.add('u_bad',p.id,chapter='missing')],
            [self.add('u_bad',self.book.chapters[1].paragraphs[0].id)],
            [{'op':'delete','id':p.id},{'op':'edit','id':p.id,'text':'旧段'}]]
        for edits in invalid:
            with self.subTest(edits=str(edits)[:100]),self.assertRaises(BookError):
                apply_edits(self.book,{'text_edits':edits})

    def test_original_empty_chapter_can_be_edited(self):
        original=Epub(self.book.source_epub)
        first=self.book.chapters[0]
        path=self.book.anchors[first.paragraphs[0].id]['path']
        raw=original.read(path)
        for p in reversed(first.paragraphs):
            a=self.book.anchors[p.id]
            raw=raw[:a['start']]+raw[a['end']:]
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z:
            for info in original.zip.infolist():
                z.writestr(info,raw if info.filename==path else original.read(info.filename))
        book=parse_text(buf.getvalue(),'empty-chapter.epub')
        settings=initial_settings(book,self.donor)
        for image in settings['images']:image.update(role='ignore',confirmed=True)
        settings['text_edits']=[self.add('u_empty',None,'空章补正文',chapter=book.chapters[0].id)]
        settings['images'][2].update(role='body',after_id='u_empty',confirmed=True)
        result=Epub(build_epub(book,self.donor,settings))
        self.assertEqual(self.prose(result,path),['空章补正文'])
        self.assertLess(result.read(path).index('空章补正文'.encode()),result.read(path).index(b'em_i0003'))

    def test_blank_paragraphs_survive_project_and_export_with_image_anchors(self):
        first = self.book.paragraphs[0]
        self.settings['text_edits'] = [
            {'op':'edit','id':first.id,'text':''},
            self.add('u_blank', first.id, ''),
            self.add('u_whitespace', 'u_blank', '  \t\n')]
        restored = json.loads(json.dumps(self.settings))
        preview = apply_edits(self.book, restored, preserve_source=False)
        self.assertEqual([p.text for p in preview.paragraphs[:3]], ['', '', '  \t\n'])
        restored['images'][2].update(role='body',after_id='u_blank',confirmed=True)
        restored['images'][3].update(role='body',after_id='u_whitespace',confirmed=True)
        result = Epub(build_epub(self.book,self.donor,restored))
        path = self.book.anchors[first.id]['path']
        node = xml(result.read(path))
        tags = [n for n in node.iter() if n.tag.endswith('}p')]
        self.assertTrue(all(p.find('{http://www.w3.org/1999/xhtml}br') is not None for p in tags[:3]))
        data = result.read(path)
        self.assertEqual(data.count(b'<p><br/></p>'),3)
        self.assertLess(data.index(b'em_i0003'),data.index(b'em_i0004'))
        self.assertIn(b'<p><br/></p>\n<div class="illust" id="em_i0003"',data)
        self.assertNotIn('空段落'.encode(),data)
        prepared = apply_edits(self.book,restored)
        for p in prepared.paragraphs[:3]:
            anchor=prepared.anchors[p.id]
            self.assertEqual(Epub(prepared.source_epub).read(path)[anchor['start']:anchor['end']],b'<p><br/></p>')

    def test_generic_blank_paragraphs_render_as_blank_lines(self):
        text,_=demo_files()
        book=parse_text(text,'source.txt')
        settings=initial_settings(book,self.donor)
        for image in settings['images']:image.update(role='ignore',confirmed=True)
        first=book.paragraphs[0]
        settings['text_edits']=[{'op':'edit','id':first.id,'text':''},self.add('u_blank',first.id,'',chapter=first.chapter_id)]
        result=Epub(build_epub(book,self.donor,settings))
        data=result.read('OEBPS/text/'+first.chapter_id+'-001.xhtml')
        root=xml(data)
        ps=[n for n in root.iter() if n.tag.endswith('}p')]
        self.assertTrue(all(p.find('{http://www.w3.org/1999/xhtml}br') is not None for p in ps[:2]))
        self.assertTrue(all(p.get('class') is None for p in ps[:2]))

    def test_old_project_without_edits_unchanged(self):
        self.settings.pop('text_edits')
        self.assertIs(apply_edits(self.book,self.settings),self.book)
        validate(self.book,self.donor,self.settings)
        build_epub(self.book,self.donor,self.settings)

    def test_preview_endpoint_keeps_original_session(self):
        server=make_server(0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            token=server.state.add(self.book,self.donor)
            url='http://127.0.0.1:%d/api/text/edit'%server.server_address[1]
            payload={'session':token,'text_edits':[self.add('u_preview',None,'预览正文')]}
            req=urllib.request.Request(url,json.dumps(payload).encode(),{'Content-Type':'application/json'})
            with urllib.request.urlopen(req) as response: preview=json.load(response)
            self.assertEqual(preview['paragraphs'][0]['text'],'预览正文')
            self.assertNotEqual(server.state.get(token)[0].paragraphs[0].text,'预览正文')
        finally:
            server.shutdown();server.server_close();worker.join()


if __name__ == '__main__':unittest.main()
