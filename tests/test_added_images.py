import base64
import copy
import hashlib
import io
import json
import struct
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from unittest.mock import patch

from epub_merge.added_images import with_added_images
from epub_merge.books import BookError, Epub, parse_text, parse_donor
from epub_merge.demo import mojimoon_demo_files, demo_files
from epub_merge.export import initial_settings, build_epub, build_report, validate
from epub_merge.locations import chapter_start, chapter_end, page_start
from epub_merge.server import make_server


def png():
    def chunk(kind, data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',24,32,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\x23\x68\x57'*24)*32))+chunk(b'IEND',b'')


class AddedImageTests(unittest.TestCase):
    def setUp(self):
        text,donor=mojimoon_demo_files()
        self.book=parse_text(text,'base.epub')
        self.donor=parse_donor(donor,'donor.epub')
        self.settings=initial_settings(self.book,self.donor)
        for image in self.settings['images']:image.update(role='ignore',confirmed=True)
        self.data=png()
        self.attachment={'id':'local_test','filename':'新插图.png','data':base64.b64encode(self.data).decode(),'sha256':hashlib.sha256(self.data).hexdigest()}
        self.settings['added_images']=[self.attachment]
        self.settings['image_order'].append(self.attachment['id'])
        self.settings['images'].append({'id':'local_test','role':'body','after_id':self.book.paragraphs[2].id,'confirmed':True})

    def test_export_local_image_preserves_bytes_layout_and_original_session(self):
        before=copy.deepcopy(self.donor.public())
        result=Epub(build_epub(self.book,self.donor,self.settings))
        self.assertEqual(result.read('OEBPS/Images/em_local_test.png'),self.data)
        path=self.book.anchors[self.book.paragraphs[2].id]['path']
        content=result.read(path)
        self.assertLess(content.index(self.book.paragraphs[2].text.encode()),content.index(b'em_local_test'))
        self.assertLess(content.index(b'em_local_test'),content.index(self.book.paragraphs[3].text.encode()))
        self.assertEqual(self.donor.public(),before)
        original=Epub(self.book.source_epub)
        for name in original.names:
            if name.endswith('.css'):self.assertEqual(original.read(name),result.read(name))
        self.assertIn('本地图片：新插图.png',build_report(self.book,self.donor,self.settings))

    def test_project_embeds_original_file_and_loads_without_external_asset(self):
        restored=json.loads(json.dumps(self.settings))
        combined=with_added_images(self.donor,restored)
        self.assertEqual(combined.illustrations[-1].data,self.data)
        self.assertEqual(combined.fingerprint,self.donor.fingerprint)
        self.assertEqual(combined.illustrations[-1].public()['local'],True)
        self.assertNotIn('data',combined.illustrations[-1].public())
        validate(self.book,self.donor,restored)
        build_epub(self.book,self.donor,restored)

    def test_local_cover_front_and_title_page(self):
        for role in ['cover','front','body']:
            with self.subTest(role=role):
                self.settings['images'][-1].update(role=role,after_id=page_start(self.book.pages[0]['id']))
                result=Epub(build_epub(self.book,self.donor,self.settings))
                self.assertEqual(result.read('OEBPS/Images/em_local_test.png'),self.data)
                if role=='cover':self.assertEqual(result.cover_path,'OEBPS/Images/em_local_test.png')
                if role=='body':self.assertIn(b'em_local_test',result.read(self.book.pages[0]['path']))

    def test_same_anchor_keeps_original_then_added_image_order(self):
        self.settings['images'][2].update(role='body',after_id=self.book.paragraphs[2].id)
        result=Epub(build_epub(self.book,self.donor,self.settings))
        data=result.read(self.book.anchors[self.book.paragraphs[2].id]['path'])
        self.assertLess(data.index(b'em_i0003'),data.index(b'em_local_test'))

    def test_new_blank_paragraph_can_anchor_local_image(self):
        self.settings['text_edits']=[{'op':'add','id':'u_blank','chapter_id':self.book.chapters[0].id,'after_id':None,'text':''}]
        self.settings['images'][-1]['after_id']='u_blank'
        result=Epub(build_epub(self.book,self.donor,self.settings))
        data=result.read(self.book.anchors[self.book.paragraphs[0].id]['path'])
        self.assertLess(data.index(b'<p><br/></p>'),data.index(b'em_local_test'))
        self.assertLess(data.index(b'em_local_test'),data.index(self.book.paragraphs[0].text.encode()))

    def test_generic_export_supports_local_images(self):
        text,_=demo_files()
        book=parse_text(text,'plain.txt')
        settings=initial_settings(book,self.donor)
        for image in settings['images']:image.update(role='ignore',confirmed=True)
        settings['added_images']=[self.attachment]
        settings['image_order'].append(self.attachment['id'])
        settings['images'].append({'id':'local_test','role':'body','after_id':chapter_start(book.chapters[0].id),'confirmed':True})
        result=Epub(build_epub(book,self.donor,settings))
        self.assertEqual(result.read('OEBPS/images/local_test.png'),self.data)
        self.assertIn(b'local_test.png',result.read('OEBPS/text/'+book.chapters[0].id+'-001.xhtml'))

    def test_missing_decision_and_unconfirmed_attachment_are_rejected(self):
        self.settings['images'][-1]['confirmed']=False
        with self.assertRaises(BookError):build_epub(self.book,self.donor,self.settings)
        self.settings['images'].pop()
        with self.assertRaises(BookError):build_epub(self.book,self.donor,self.settings)

    def test_bad_attachments_fail_cleanly(self):
        invalid=[None,{},[None],[{**self.attachment,'id':'i0001'}],[{**self.attachment,'id':[]}],
                 [self.attachment,self.attachment], [{**self.attachment,'data':'!!!'}],
                 [{**self.attachment,'sha256':'wrong'}],[{**self.attachment,'filename':None}],
                 [{**self.attachment,'data':'','sha256':hashlib.sha256(b'').hexdigest()}]]
        for value in invalid:
            with self.subTest(value=str(value)[:80]),self.assertRaises(BookError):
                with_added_images(self.donor,{'added_images':value})
        for data in [b'not an image',b'<svg xmlns="http://www.w3.org/2000/svg"/>']:
            with self.subTest(data=data),self.assertRaises(BookError):
                with_added_images(self.donor,{'added_images':[{**self.attachment,'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}]})

    def test_limits_and_legacy_projects(self):
        with patch('epub_merge.added_images.MAX_ADDED',len(self.data)-1),self.assertRaises(BookError):
            with_added_images(self.donor,self.settings)
        with patch('epub_merge.added_images.MAX_IMAGE',len(self.data)-1),self.assertRaises(BookError):
            with_added_images(self.donor,self.settings)
        with patch('epub_merge.added_images.MAX_IMAGES',len(self.donor.illustrations)),self.assertRaises(BookError):
            with_added_images(self.donor,self.settings)
        self.assertIs(with_added_images(self.donor,{}),self.donor)

    def test_preview_endpoint_is_read_only_and_reports_corrupt_data(self):
        server=make_server(0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            token=server.state.add(self.book,self.donor)
            url='http://127.0.0.1:%d/api/images/preview'%server.server_address[1]
            def request(attachments):
                return urllib.request.urlopen(urllib.request.Request(url,json.dumps({'session':token,'added_images':attachments}).encode(),{'Content-Type':'application/json'}))
            with request([self.attachment]) as response:
                preview=json.load(response)
            self.assertEqual(len(preview['illustrations']),len(self.donor.illustrations)+1)
            self.assertEqual(len(server.state.get(token)[1].illustrations),len(self.donor.illustrations))
            with self.assertRaises(urllib.error.HTTPError) as error:
                request([{**self.attachment,'sha256':'wrong'}])
            self.assertEqual(error.exception.code,400)
            error.exception.close()
        finally:
            server.shutdown();server.server_close();worker.join()


if __name__ == '__main__':unittest.main()
