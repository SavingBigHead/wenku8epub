import base64
import copy
import hashlib
import json
import unittest

from epub_merge.books import BookError,Epub,parse_text,parse_donor
from epub_merge.demo import mojimoon_demo_files,demo_files
from epub_merge.export import initial_settings,build_epub,build_report
from epub_merge.image_order import prepare_images
from epub_merge.locations import chapter_start,chapter_end,page_start,START
from test_added_images import png


class ImageOrderTests(unittest.TestCase):
    def setUp(self):
        text,donor=mojimoon_demo_files()
        self.book=parse_text(text,'base.epub');self.donor=parse_donor(donor,'donor.epub')
        self.settings=initial_settings(self.book,self.donor)
        for image in self.settings['images']:image.update(role='ignore',confirmed=True)
        data=png()
        self.settings['added_images']=[{'id':'local_sort','filename':'排序图.png','data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}]
        self.settings['images'].append({'id':'local_sort','role':'body','after_id':self.book.paragraphs[2].id,'confirmed':True})
        self.settings['image_order']=['i0001','i0002','local_sort','i0004','i0003']

    def set_body(self,id_,anchor):
        next(x for x in self.settings['images'] if x['id']==id_).update(role='body',after_id=anchor,confirmed=True)

    def test_mixed_images_follow_saved_order_and_keep_original_source_immutable(self):
        original=copy.deepcopy(self.donor.public())
        for id_ in ['i0003','i0004']:self.set_body(id_,self.book.paragraphs[2].id)
        settings=json.loads(json.dumps(self.settings))
        result=Epub(build_epub(self.book,self.donor,settings))
        data=result.read(self.book.anchors[self.book.paragraphs[2].id]['path'])
        self.assertLess(data.index(b'em_local_sort'),data.index(b'em_i0004'))
        self.assertLess(data.index(b'em_i0004'),data.index(b'em_i0003'))
        report=build_report(self.book,self.donor,settings)
        self.assertLess(report.index('local_sort'),report.index('i0004'))
        self.assertLess(report.index('i0004'),report.index('i0003'))
        self.assertEqual(self.donor.public(),original)
        self.assertEqual(result.read('OEBPS/Images/em_local_sort.png'),png())

    def test_front_order_is_used_in_spine(self):
        for x in self.settings['images']:x.update(role='front',confirmed=True)
        result=Epub(build_epub(self.book,self.donor,self.settings))
        ids=[entry['idref'] if isinstance(entry,dict) else entry for entry in result.spine]
        # Read resource paths in actual EPUB reading order.
        paths=[result.manifest[id_]['path'] for id_ in ids]
        self.assertLess(next(i for i,p in enumerate(paths) if p.endswith('em_local_sort.xhtml')),next(i for i,p in enumerate(paths) if p.endswith('em_i0004.xhtml')))
        self.assertLess(next(i for i,p in enumerate(paths) if p.endswith('em_i0004.xhtml')),next(i for i,p in enumerate(paths) if p.endswith('em_i0003.xhtml')))

    def test_title_page_obeys_custom_order(self):
        anchor=page_start(self.book.pages[0]['id'])
        for id_ in ['i0003','i0004','local_sort']:self.set_body(id_,anchor)
        result=Epub(build_epub(self.book,self.donor,self.settings))
        data=result.read(self.book.pages[0]['path'])
        self.assertLess(data.index(b'em_local_sort'),data.index(b'em_i0004'))
        self.assertLess(data.index(b'em_i0004'),data.index(b'em_i0003'))

    def test_equivalent_last_paragraph_and_chapter_end_order_in_both_layouts(self):
        for generic in [False,True]:
            with self.subTest(generic=generic):
                book=parse_text(demo_files()[0],'generic.txt') if generic else self.book
                chapter=book.chapters[0]
                self.settings['text_fingerprint']=book.fingerprint
                self.settings['layout']='rebuild' if generic else 'mojimoon'
                self.set_body('i0003',chapter.paragraphs[-1].id)
                self.set_body('i0004',chapter_end(chapter.id))
                self.set_body('local_sort',chapter_end(chapter.id))
                result=Epub(build_epub(book,self.donor,self.settings))
                path='OEBPS/text/'+chapter.id+'-001.xhtml' if generic else book.anchors[chapter.paragraphs[-1].id]['path']
                data=result.read(path)
                ids=[b'local_sort',b'i0004',b'i0003']
                self.assertLess(data.index(ids[0]),data.index(ids[1]));self.assertLess(data.index(ids[1]),data.index(ids[2]))

    def test_start_alias_in_preserved_layout(self):
        self.set_body('i0003',START)
        self.set_body('i0004',chapter_start(self.book.chapters[0].id))
        self.set_body('local_sort',chapter_start(self.book.chapters[0].id))
        result=Epub(build_epub(self.book,self.donor,self.settings))
        data=result.read(self.book.anchors[self.book.paragraphs[0].id]['path'])
        self.assertLess(data.index(b'em_local_sort'),data.index(b'em_i0004'))
        self.assertLess(data.index(b'em_i0004'),data.index(b'em_i0003'))

    def test_sorting_never_moves_images_across_chapter_boundary(self):
        self.set_body('i0003',chapter_end(self.book.chapters[0].id))
        self.set_body('i0004',chapter_start(self.book.chapters[1].id))
        self.set_body('local_sort',chapter_end(self.book.chapters[0].id))
        result=Epub(build_epub(self.book,self.donor,self.settings))
        a=result.read(self.book.anchors[self.book.chapters[0].paragraphs[0].id]['path'])
        b=result.read(self.book.anchors[self.book.chapters[1].paragraphs[0].id]['path'])
        self.assertIn(b'em_local_sort',a);self.assertIn(b'em_i0003',a);self.assertNotIn(b'em_i0004',a)
        self.assertIn(b'em_i0004',b);self.assertNotIn(b'em_local_sort',b)

    def test_legacy_project_keeps_original_then_added_order(self):
        self.settings.pop('image_order')
        prepared=prepare_images(self.donor,self.settings)
        self.assertEqual([p.id for p in prepared.illustrations],['i0001','i0002','i0003','i0004','local_sort'])
        build_epub(self.book,self.donor,self.settings)

    def test_bad_image_orders_are_rejected_without_mutation(self):
        original=copy.deepcopy(self.donor.public())
        for value in [None,{},'bad',[],['i0001'],['i0001']*5,['i0001','i0002','i0003','i0004','missing'],[[],1,2,3,4]]:
            with self.subTest(value=value),self.assertRaises(BookError):
                prepare_images(self.donor,{**self.settings,'image_order':value})
        self.assertEqual(original,self.donor.public())


if __name__=='__main__':unittest.main()
