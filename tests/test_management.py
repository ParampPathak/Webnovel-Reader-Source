"""Disposable ID-scoped deletion, real raster parsing and playback status regressions."""
import fcntl
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from tests import test_platform as helpers
from tests.fixtures import epub_bytes, change_entry
from library_store import connect
from platform_store import worker_state
from cover_art import validate_image, store_cover, fetch_cover


def artwork(kind='PNG'):
    output=io.BytesIO()
    image=Image.new('RGB',(120,180),'navy');image.save(output,format=kind)
    return output.getvalue()


class ManagementTests(unittest.TestCase):
    setUp=helpers.PlatformTests.setUp
    upload=helpers.PlatformTests.upload
    book=helpers.PlatformTests.book
    worker=helpers.PlatformTests.worker
    drain=helpers.PlatformTests.drain
    post=helpers.PlatformTests.post

    def test_andrew_fallback_and_saved_default_survive_factory_reload(self):
        voices=self.client.get('/api/edge/voices').json
        self.assertTrue(any(v['ShortName']=='en-US-AndrewNeural' and v['DisplayName']=='Andrew — English (US)' for v in voices['items']))
        r=self.client.put('/api/edge/preferences',json={'voice':'en-US-AndrewNeural','rate':25});self.assertEqual(r.status_code,200)
        book,_=self.book();self.assertEqual(self.client.get('/api/books/'+book+'/listening').json['voice'],'en-US-AndrewNeural')
        from app import create_app
        client=create_app(self.root/'data').test_client()
        self.assertEqual(client.get('/api/edge/preferences').json['rate'],25)

    def test_stopped_worker_is_reported_even_with_fresh_heartbeat(self):
        root=self.root/'data';(root/'.platform-heartbeat').touch()
        with (root/'.platform-worker.lock').open('w') as lock:
            self.assertFalse(worker_state(root)['worker_active'])
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertTrue(worker_state(root)['worker_active'])
        self.assertFalse(worker_state(root)['worker_active'])
        book,sections=self.book()
        state=self.client.get('/api/books/'+book+'/audio',query_string={'section_id':sections[0]['id']}).json
        self.assertFalse(state['worker_active']);self.assertIn('stopped',state['worker_message'])

    def test_default_delete_is_permanent_and_retains_confirmation(self):
        book,_=self.book()
        self.assertEqual(self.client.delete('/api/books/'+book,json={}).status_code,400)
        result=self.client.delete('/api/books/'+book,json={'confirmation':'A & B'})
        self.assertEqual(result.status_code,202);self.assertTrue(result.json['permanent'])
        worker,_=self.worker();self.drain(worker)
        self.assertEqual(self.client.get('/api/books/'+book).status_code,404)

    def test_library_removal_preserves_files_and_can_restore_by_id(self):
        book,sections=self.book();root=self.root/'data'
        with connect(root/'webnovel.db') as c: original=Path(c.execute('SELECT original_path FROM books WHERE id=?',(book,)).fetchone()[0])
        result=self.client.delete('/api/books/'+book,json={'mode':'remove','confirmation':'A & B'})
        self.assertEqual(result.status_code,200);self.assertTrue(original.is_file())
        self.assertNotIn(book,[b['id'] for b in self.client.get('/api/books').json['items']])
        self.assertIn(book,[b['id'] for b in self.client.get('/api/books?archived=1').json['items']])
        self.assertTrue(self.client.get('/api/books/'+book).json['hidden'])
        self.assertEqual(self.post('/api/books/'+book+'/restore',{}).status_code,200)
        self.assertIn(book,[b['id'] for b in self.client.get('/api/books').json['items']])

    def test_permanent_delete_cancels_jobs_and_preserves_same_title_book(self):
        book,sections=self.book()
        payload=change_entry(epub_bytes(),'OPS/chapter0.xhtml','<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Distinct same-title book</p></body></html>')
        other=self.upload(payload).json['id'];self.assertNotEqual(book,other)
        root=self.root/'data';provider_job=self.post('/api/books/'+book+'/audio/jobs',{'section_id':sections[0]['id']}).json['id']
        with connect(root/'webnovel.db') as c: original=Path(c.execute('SELECT original_path FROM books WHERE id=?',(book,)).fetchone()[0]);other_file=Path(c.execute('SELECT original_path FROM books WHERE id=?',(other,)).fetchone()[0])
        r=self.client.delete('/api/books/'+book,json={'mode':'permanent','confirmation':'A & B'});self.assertEqual(r.status_code,202)
        self.assertEqual(self.client.get('/api/jobs/'+provider_job).json['status'],'cancelled')
        worker,_=self.worker();self.drain(worker)
        self.assertFalse(original.exists());self.assertTrue(other_file.exists());self.assertEqual(self.client.get('/api/books/'+other).status_code,200)
        self.assertEqual(self.client.get('/api/books/'+book).status_code,404)
        with connect(root/'webnovel.db') as c:self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_shared_custom_cover_and_unrelated_audio_are_preserved(self):
        book,_=self.book();other=self.upload(change_entry(epub_bytes(),'OPS/chapter0.xhtml','<html><body>Other</body></html>')).json['id']
        root=self.root/'data';relative=store_cover(root,book,artwork())
        with connect(root/'webnovel.db') as c:
            with c:c.execute('UPDATE books SET cover_path=? WHERE id=?',(relative,other))
        unrelated=root/'audio'/'unrelated.mp3';unrelated.parent.mkdir(exist_ok=True);unrelated.write_bytes(b'preserve')
        self.client.delete('/api/books/'+book,json={'mode':'permanent','confirmation':'A & B'})
        worker,_=self.worker();self.drain(worker)
        self.assertTrue((root/relative).is_file());self.assertEqual(unrelated.read_bytes(),b'preserve')

    def test_interrupted_file_cleanup_retries_without_orphaned_metadata(self):
        book,_=self.book();root=self.root/'data'
        with connect(root/'webnovel.db') as c:target=Path(c.execute('SELECT original_path FROM books WHERE id=?',(book,)).fetchone()[0])
        result=self.client.delete('/api/books/'+book,json={'mode':'permanent','confirmation':'A & B'}).json
        worker,_=self.worker();original_unlink=Path.unlink
        def fail_once(path,*args,**kwargs):
            if path==target:raise OSError('Simulated storage failure')
            return original_unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_once):worker.step()
        self.assertEqual(self.client.get('/api/books/'+book).status_code,200)
        with connect(root/'webnovel.db') as c:
            with c:c.execute('UPDATE platform_items SET next_attempt=0 WHERE job_id=?',(result['job_id'],))
        self.drain(worker);self.assertEqual(self.client.get('/api/books/'+book).status_code,404)

    def test_imported_webnovel_permanent_delete_uses_id_and_finishes(self):
        url='https://public.example/novel';book=self.post('/api/imports',{'url':url}).json['book_id']
        worker,_=self.worker()
        with patch('scraper.browse_page',return_value={'title':'Disposable webnovel','links':[{'url':url+'/chapter-1','title':'Chapter One'}],'pages':[]}):self.drain(worker)
        self.post('/api/books/'+book+'/import/jobs',{'first':1,'last':1})
        with patch('scraper.scrape_chapter',return_value={'chapter_title':'Chapter One','novel_title':'Disposable webnovel','content':'Test text','next_url':None}):self.drain(worker)
        title=self.client.get('/api/books/'+book).json['title']
        self.assertEqual(self.client.delete('/api/books/'+book,json={'mode':'permanent','confirmation':title}).status_code,202)
        self.drain(worker);self.assertEqual(self.client.get('/api/books/'+book).status_code,404)

    def test_cover_formats_validation_manual_remove_and_factory_persistence(self):
        book,_=self.book();base='/api/books/'+book
        for kind,mime in [('PNG','image/png'),('JPEG','image/jpeg'),('WEBP','image/webp')]:
            payload=artwork(kind);self.assertEqual(validate_image(payload)[1],mime)
            r=self.client.post(base+'/cover',data={'file':(io.BytesIO(payload),'cover')});self.assertEqual(r.status_code,200)
            response=self.client.get(base+'/cover');self.assertEqual(response.content_type,mime);self.assertEqual(response.data,payload);response.close()
        from app import create_app
        client=create_app(self.root/'data').test_client();response=client.get(base+'/cover');self.assertEqual(response.content_type,'image/webp');response.close()
        r=self.client.post(base+'/cover',data={'file':(io.BytesIO(b'\x89PNG\r\n\x1a\n<script>bad</script>'),'bad.png')});self.assertEqual(r.status_code,400)
        self.assertEqual(self.client.delete(base+'/cover').status_code,200);self.assertIsNone(self.client.get(base).json['cover_url'])

    def test_export_preserves_custom_webp_cover_media_type_and_reopens(self):
        import zipfile
        from epub_creator import export_epub
        from epub_library import parse_epub
        book,_=self.book();root=self.root/'data'
        store_cover(root,book,artwork('WEBP'))
        destination=self.root/'custom-cover.epub'
        export_epub(root/'webnovel.db',book,destination,0,2,lambda:None)
        with zipfile.ZipFile(destination) as archive:
            self.assertIn('media-type="image/webp"',archive.read('EPUB/package.opf').decode())
            self.assertEqual(archive.read('EPUB/cover.webp'),artwork('WEBP'))
        parsed=parse_epub(destination,'reopened')
        self.assertEqual(parsed['cover'],'EPUB/cover.webp')
        self.assertEqual(parsed['section_count'],3)

    def test_embedded_webp_cover_is_extracted_and_public_url_rules_apply(self):
        import zipfile
        payload=epub_bytes()
        with zipfile.ZipFile(io.BytesIO(payload)) as z:opf=z.read('OPS/book.opf').decode().replace('image/png','image/webp')
        payload=change_entry(change_entry(payload,'OPS/book.opf',opf),'OPS/images/cover.png',artwork('WEBP'))
        book=self.upload(payload).json['id'];response=self.client.get('/api/books/'+book+'/cover');self.assertEqual(response.content_type,'image/webp');response.close()
        self.assertRaises(ValueError,fetch_cover,self.root/'data',book,'http://127.0.0.1/cover.png')
        with patch('safe_http.public_url'),patch('scraper._check_robots'),patch('safe_http.fetch',return_value=SimpleNamespace(content=artwork(),headers={'Content-Type':'image/png'})):
            relative=fetch_cover(self.root/'data',book,'https://public.example/cover.png');self.assertTrue((self.root/'data'/relative).exists())

    def test_wrong_confirmation_cannot_remove_a_book(self):
        book,_=self.book();self.assertEqual(self.client.delete('/api/books/'+book,json={'mode':'permanent','confirmation':'Another title'}).status_code,400)
        self.assertIn(book,[r['id'] for r in self.client.get('/api/books').json['items']])

    def test_removed_book_rejects_new_artwork_before_any_file_is_written(self):
        book,_=self.book();root=self.root/'data';before=set((root/'covers').rglob('*')) if (root/'covers').exists() else set()
        self.client.delete('/api/books/'+book,json={'mode':'remove','confirmation':'A & B'})
        self.assertRaises(ValueError,store_cover,root,book,artwork('JPEG'))
        after=set((root/'covers').rglob('*')) if (root/'covers').exists() else set();self.assertEqual(before,after)

    def test_epub2_guide_cover_page_fallback(self):
        import zipfile
        from epub_library import parse_epub
        payload=epub_bytes(epub2=True)
        with zipfile.ZipFile(io.BytesIO(payload)) as z:opf=z.read('OPS/book.opf').decode().replace('<meta name="cover" content="cover"/>','').replace(' properties="cover-image"','').replace('</package>','<guide><reference type="cover" href="chapter0.xhtml"/></guide></package>')
        payload=change_entry(payload,'OPS/book.opf',opf);path=self.root/'guide.epub';path.write_bytes(payload)
        self.assertEqual(parse_epub(path,'guide',stream=True)['cover'],'OPS/images/cover.png')

    def test_bookmarks_are_scoped_persistent_and_removed_on_purge(self):
        book,sections=self.book();other=self.upload(epub_bytes(2)).json['id']
        r=self.client.post('/api/books/'+book+'/bookmarks',json={'section_id':sections[0]['id'],'fraction':.4});self.assertEqual(r.status_code,201)
        self.assertEqual(self.client.get('/api/books/'+book+'/bookmarks').json['items'][0]['fraction'],.4)
        self.assertEqual(self.client.post('/api/books/'+other+'/bookmarks',json={'section_id':sections[0]['id'],'fraction':.4}).status_code,400)
        self.client.delete('/api/books/'+other+'/bookmarks',json={'id':r.json['id']})
        self.assertEqual(len(self.client.get('/api/books/'+book+'/bookmarks').json['items']),1)
        title=self.client.get('/api/books/'+book).json['title'];self.client.delete('/api/books/'+book,json={'mode':'permanent','confirmation':title});worker,_=self.worker();self.drain(worker)
        with connect(self.db) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM book_bookmarks WHERE book_id=?',(book,)).fetchone()[0],0)

    def test_reader_layout_and_fulltext_search_are_bounded(self):
        book,sections=self.book();base='/api/books/'+book
        payload={'section_id':sections[0]['id'],'fraction':.2,'version':0,'preferences':{'mode':'pages','margin':18,'paragraphSpacing':1.4,'alignment':'justify'}}
        self.assertEqual(self.client.put(base+'/progress',json=payload).status_code,200)
        payload['preferences']['margin']=0;self.assertEqual(self.client.put(base+'/progress',json=payload).status_code,400)
        with connect(self.db) as conn:
            with conn:conn.execute('UPDATE book_sections SET text_content=? WHERE id=?',('A hidden searchable phrase.',sections[0]['id']))
        r=self.client.get(base+'/search?q=searchable&limit=1');self.assertEqual(r.json['items'][0]['id'],sections[0]['id'])
        self.assertEqual(len(r.json['items']),1);self.assertEqual(self.client.get(base+'/search?q=x').json['items'],[])

    def test_source_jpg_alias_is_validated_and_stored_as_jpeg(self):
        book,_=self.book()
        with patch('safe_http.public_url'),patch('scraper._check_robots'),patch('safe_http.fetch',return_value=SimpleNamespace(content=artwork('JPEG'),headers={'Content-Type':'image/jpg'})):
            path=fetch_cover(self.root/'data',book,'https://public.example/cover.jpg')
        self.assertTrue(path.endswith('.jpg'));r=self.client.get('/api/books/'+book+'/cover');self.addCleanup(r.close);self.assertEqual(r.mimetype,'image/jpeg')

    def test_paginated_bookmark_anchor_and_idempotent_initialization(self):
        book,sections=self.book();base='/api/books/'+book
        r=self.client.post(base+'/bookmarks',json={'section_id':sections[0]['id'],'fraction':.4,'anchor':'text:321'})
        self.assertEqual(r.status_code,201);self.assertEqual(self.client.get(base+'/bookmarks').json['items'][0]['anchor'],'text:321')
        self.assertEqual(self.client.post(base+'/bookmarks',json={'section_id':sections[0]['id'],'fraction':.4,'anchor':'x'*513}).status_code,400)
        from app import create_app
        with connect(self.db) as c:before=[tuple(x) for x in c.execute('SELECT id,title FROM books ORDER BY id')]
        create_app(self.root/'data');create_app(self.root/'data')
        with connect(self.db) as c:self.assertEqual(before,[tuple(x) for x in c.execute('SELECT id,title FROM books ORDER BY id')])

    def test_sparse_chapter_progress_uses_canonical_rank_not_source_number(self):
        book,sections=self.book()
        with connect(self.db) as c:
            with c:
                for i,section in enumerate(sections):c.execute('UPDATE book_sections SET position=? WHERE id=?',(200+i*5,section['id']))
        self.client.put('/api/books/'+book+'/progress',json={'section_id':sections[0]['id'],'fraction':1,'version':0})
        item=next(x for x in self.client.get('/api/books').json['items'] if x['id']==book)
        self.assertEqual(item['reading_position'],0);self.assertEqual(item['section_count'],3)

    def test_audio_resume_destination_cannot_override_protected_manual_reading(self):
        book,sections=self.book();base='/api/books/'+book
        self.client.put(base+'/progress',json={'section_id':sections[0]['id'],'fraction':.5,'anchor':'text:12','version':0})
        with connect(self.db) as c:
            with c:c.execute("INSERT INTO listening_progress(book_id,section_id,asset_id,seconds,voice,updated_at) VALUES(?,?,?,25,?,'2100-01-01 00:00:00')",(book,sections[1]['id'],'saved','en-US-AndrewNeural'))
        self.assertEqual(self.client.get(base).json['resume_audio']['section_id'],sections[1]['id'])
        self.client.put(base+'/progress',json={'section_id':sections[2]['id'],'fraction':.2,'anchor':'text:32','version':1,'preferences':{'followAlongSuspended':True}})
        self.assertIsNone(self.client.get(base).json['resume_audio']);self.assertEqual(self.client.get(base).json['progress']['anchor'],'text:32')
