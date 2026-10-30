"""Real Flask test-client coverage. Skips explicitly if runtime packages are absent."""
import importlib.util
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fixtures import epub_bytes, change_entry, write_epub
from library_store import connect

RUNTIME_AVAILABLE = all(importlib.util.find_spec(package) for package in ('flask','bs4','ebooklib','requests'))


@unittest.skipUnless(RUNTIME_AVAILABLE,'Flask, BeautifulSoup, EbookLib and requests are required for HTTP integration tests')
class HttpTests(unittest.TestCase):
    def setUp(self):
        from app import create_app
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = create_app(self.root/'data')
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        self.db = self.root/'data/webnovel.db'

    def test_reading_percentage_uses_readable_rank_and_refreshes_after_append(self):
        book=self.upload().json['id'];base='/api/books/'+book
        with connect(self.db) as c,c:
            rows=c.execute('SELECT id FROM book_sections WHERE book_id=? ORDER BY position',(book,)).fetchall()
            c.execute('UPDATE book_sections SET position=position+100 WHERE book_id=?',(book,))
            c.execute("UPDATE book_sections SET text_content='',content_html='' WHERE id=?",(rows[1][0],))
        self.client.put(base+'/progress',json={'section_id':rows[0][0],'fraction':.5,'anchor':'test','preferences':{},'version':0})
        before=self.client.get(base+'/progress').json
        payload=self.client.get(base).json
        self.assertEqual(payload['readable_section_count'],2)
        self.assertAlmostEqual(payload['reading_percentage'],25)
        with connect(self.db) as c,c:
            c.execute("INSERT INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES(?,200,'New','new','<p>New</p>','New')",(book,))
        latest=self.client.get(base).json
        self.assertAlmostEqual(latest['reading_percentage'],100/6)
        self.assertEqual(before,self.client.get(base+'/progress').json)
        current=self.client.get(base+'/sections/'+str(rows[0][0])).json
        self.assertEqual(current['reading_index'],0)
        self.assertEqual(current['readable_section_count'],3)

    def test_mutable_book_inventory_and_progress_are_not_http_cached(self):
        book=self.upload().json['id'];base='/api/books/'+book
        for path in ['/api/books',base,base+'/progress',base+'/sections',base+'/navigation']:
            response=self.client.get(path)
            self.assertEqual(response.status_code,200)
            self.assertIn('no-store',response.headers.get('Cache-Control',''))

    def test_metadata_placeholders_are_not_readable_chapter_destinations(self):
        book=self.upload().json['id'];base='/api/books/'+book
        with connect(self.db) as c,c:
            sid=c.execute("INSERT INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES(?,99,'Pending','pending','','')",(book,)).lastrowid
        sections=self.client.get(base+'/sections').json
        self.assertNotIn(sid,[r['id'] for r in sections['items']])
        self.assertEqual(sections['total'],self.client.get(base).json['readable_section_count'])

    def test_embedded_contents_resolves_chapter_ids_and_preserves_anchors(self):
        book=self.upload().json['id'];base='/api/books/'+book
        nav=self.client.get(base+'/navigation').json
        self.assertFalse(nav['reading_order']);self.assertEqual(nav['items'][0]['id'],self.client.get(base).json['first_section_id'])
        self.assertEqual(nav['items'][0]['anchor'],'heading')

    def test_image_only_epub_sections_remain_readable_without_counting_blank_placeholders(self):
        raw=change_entry(epub_bytes(),'OPS/chapter1.xhtml','<html xmlns="http://www.w3.org/1999/xhtml"><body><img src="images/cover.png" alt=""/></body></html>')
        book=self.upload(raw).json['id'];base='/api/books/'+book
        self.assertEqual(self.client.get(base).json['readable_section_count'],3)
        self.assertEqual(self.client.get(base+'/sections').json['total'],3)

    def upload(self,payload=None):
        response = self.client.post('/api/books/upload',data={'file':(io.BytesIO(payload or epub_bytes()),'book.epub')},content_type='multipart/form-data')
        response.request.environ['wsgi.input'].close()
        self.addCleanup(response.close)
        return response

    def test_upload_detail_cover_original_and_reader(self):
        payload = epub_bytes()
        result = self.upload(payload)
        result.request.environ['wsgi.input'].close()
        self.addCleanup(result.close)
        self.assertEqual(result.status_code,201)
        book_id = result.json['id']
        details = self.client.get('/api/books/'+book_id).json
        self.assertEqual(details['title'],'A & B')
        self.assertEqual(details['section_count'],3)
        self.assertEqual(self.client.get(details['cover_url']).mimetype,'image/png')
        original = self.client.get('/api/books/'+book_id+'/original')
        self.addCleanup(original.close)
        self.assertEqual(original.data,payload)
        self.assertEqual(self.client.get('/reader/'+book_id).status_code,200)
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertEqual(self.client.get('/audio').status_code,200)
        self.assertEqual(self.upload(payload).json['created'],False)
        self.assertEqual(self.client.get('/api/books').json['total'],1)
        self.assertFalse(self.app.extensions.get('tts_workers'))
        with connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM chapters').fetchone()[0],0)

    def test_invalid_late_section_leaves_no_partial_library_entry(self):
        response = self.upload(change_entry(epub_bytes(),'OPS/chapter1.xhtml',b'\xffinvalid UTF8'))
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.client.get('/api/books').json['total'],0)
        self.assertEqual(list((self.root/'data/books').glob('*.epub')),[])

    def test_all_1501_sections_accessible_with_pagination(self):
        fixture = write_epub(self.root/'large.epub',1501,paragraph_bytes=9000)
        with fixture.open('rb') as stream:
            result = self.client.post('/api/books/upload',data={'file':(stream,'large.epub')},content_type='multipart/form-data')
        result.request.environ['wsgi.input'].close()
        self.addCleanup(result.close)
        self.assertEqual(result.status_code,201)
        book_id = result.json['id']
        prefix = '/api/books/'+book_id
        sections = []
        for offset in range(0,1501,200):
            response = self.client.get(f'{prefix}/sections?offset={offset}&limit=200')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['total'],1501)
            sections.extend(response.json['items'])
        self.assertEqual([s['position'] for s in sections],list(range(1501)))
        final = self.client.get(f'{prefix}/sections/{sections[-1]["id"]}').json
        self.assertEqual(final['title'],'Chapter 1501')
        self.assertIsNone(final['next_id'])
        self.assertEqual(self.client.get(f'{prefix}/navigation?offset=1500').json['items'][0]['title'],'Chapter 1501')

    def test_progress_persisted_across_app_restart_and_conflict(self):
        from app import create_app
        book_id = self.upload().json['id'];prefix = '/api/books/'+book_id
        first = self.client.get(prefix).json['first_section_id']
        payload = {'section_id':first,'fraction':.43,'preferences':{'theme':'sepia','font':'sans','fontSize':24},'version':0}
        self.assertEqual(self.client.put(prefix+'/progress',json=payload).status_code,200)
        self.assertEqual(self.client.put(prefix+'/progress',json=payload).status_code,409)
        restarted = create_app(self.root/'data').test_client()
        progress = restarted.get(prefix+'/progress').json
        self.assertEqual(progress['fraction'],.43)
        self.assertEqual(progress['preferences']['fontSize'],24)
        self.assertEqual(progress['version'],1)
        self.assertTrue(list((self.root/'data-backups').glob('*.db')))

    def test_invalid_progress_and_cross_book_section_rejected(self):
        a = self.upload().json['id'];b = self.upload(epub_bytes(4)).json['id']
        first = self.client.get('/api/books/'+a).json['first_section_id']
        payload = {'section_id':first,'fraction':.5,'version':0}
        self.assertEqual(self.client.put('/api/books/'+b+'/progress',json=payload).status_code,400)
        for invalid in [-1,2,'0.1',True,float('inf')]:
            payload['fraction']=invalid
            self.assertEqual(self.client.put('/api/books/'+a+'/progress',json=payload).status_code,400)
        payload['fraction']=.5;payload['preferences']={'theme':'evil'}
        self.assertEqual(self.client.put('/api/books/'+a+'/progress',json=payload).status_code,400)

    def test_resources_and_fragment_navigation(self):
        book_id = self.upload().json['id'];prefix = '/api/books/'+book_id
        resolution = self.client.get(prefix+'/resolve?href=OPS/chapter1.xhtml%23note').json
        self.assertEqual(resolution['anchor'],'note')
        section = self.client.get(prefix+'/sections/'+str(resolution['id'])).json
        self.assertIn('data-book-target',section['content_html'])
        self.assertEqual(self.client.get(prefix+'/resources/OPS/book.opf').status_code,404)
        image = self.client.get(prefix+'/resources/OPS/images/cover.png')
        self.assertEqual(image.status_code,200)
        self.assertEqual(image.headers['X-Content-Type-Options'],'nosniff')
        self.assertIn('sandbox',image.headers['Content-Security-Policy'])

    def test_malformed_upload_and_request_limit_and_cross_origin(self):
        self.assertEqual(self.upload(b'bad file').status_code,400)
        self.assertEqual(self.client.post('/api/books/upload').status_code,400)
        self.assertEqual(self.client.post('/api/books/upload',headers={'Origin':'https://evil.invalid'},data={}).status_code,403)
        self.app.config['MAX_CONTENT_LENGTH']=100
        self.assertEqual(self.upload().status_code,413)
        self.assertEqual(self.client.get('/api/books?offset=-1').status_code,400)
        self.assertEqual(self.client.get('/api/books?limit=10000').status_code,400)

    def test_legacy_priority_preserves_export_and_book_scoped_neighbors(self):
        from library_store import attach_legacy
        with connect(self.db) as conn:
            for title,chapter in [('Old Novel','first'),('Old Novel','second'),('Other Novel','other')]:
                cursor = conn.execute('''INSERT INTO chapters(novel_title,chapter_title,content,source_url,status,queue_order)
                                         VALUES(?,?,?,?,'ready',?)''',(title,chapter,'A & B','https://example/'+chapter,1 if chapter=='first' else 2 if chapter=='second' else 3))
                attach_legacy(conn,cursor.lastrowid)
            conn.commit()
        chapters = self.client.get('/api/queue').json
        first,second,other = [c['id'] for c in chapters]
        with connect(self.db) as conn:
            conn.execute("UPDATE chapters SET status='pending' WHERE id=?",(second,));conn.commit()
        self.client.post('/api/prioritize/'+str(second))
        self.assertEqual(self.client.get('/api/next-in-queue/'+str(first)).json['next_id'],second)
        self.assertIsNone(self.client.get('/api/next-in-queue/'+str(second)).json['next_id'])
        import zipfile
        exported = self.client.get('/api/epub/Old%20Novel')
        self.addCleanup(exported.close)
        self.assertEqual(exported.status_code,200)
        with zipfile.ZipFile(io.BytesIO(exported.data)) as archive:
            chapter0 = next(n for n in archive.namelist() if n.endswith('chap_0.xhtml'))
            self.assertIn('first',archive.read(chapter0).decode())
            self.assertIn('A &amp; B',archive.read(chapter0).decode())


    def test_existing_audio_paths_resolve_in_copied_storage(self):
        payload = b'ID3' + b'old-audio-content'*40
        audio = self.root/'data/audio/99.mp3'
        audio.write_bytes(payload)
        with connect(self.db) as conn:
            conn.execute("INSERT INTO chapters(id,novel_title,chapter_title,content,status,audio_path,audio_format) VALUES(99,'Novel','Chapter','text','ready','/data/audio/99.mp3','mp3')")
            conn.execute("INSERT INTO audio_segments(chapter_id,segment_index,audio_path,audio_format) VALUES(99,0,'/data/audio/99.mp3','mp3')")
            conn.commit()
        for route in ['/api/audio/99','/api/segment-audio/1']:
            response = self.client.get(route,headers={'Range':'bytes=0-31'})
            self.addCleanup(response.close)
            self.assertEqual(response.status_code,206)
            self.assertEqual(response.data,payload[:32])
        download = self.client.get('/api/download/99')
        self.addCleanup(download.close)
        self.assertEqual(download.data,payload)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT audio_path FROM chapters WHERE id=99').fetchone()[0],'/data/audio/99.mp3')
