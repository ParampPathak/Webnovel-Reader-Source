"""Synthetic catalog fixtures only: no downloaded books or user databases."""
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_http,test_platform
from library_store import connect
from platform_store import control
from source_adapters import canonical_source,chapter_identity
from scraper import browse_page


class AdapterTests(unittest.TestCase):
    def page(self,url,html):
        response=SimpleNamespace(url=url,text=html,headers={'Content-Type':'text/html'})
        with patch('scraper._fetch',return_value=response):return browse_page(url)

    def test_royalroad_identity_scoped_metadata_and_duplicates(self):
        url='https://www.royalroad.com/fiction/42/story'
        page=self.page(url,'''<div class="fic-title"><h1>Fixture</h1><a href="/profile/1">Writer</a></div><div class="cover-art-container"><img data-type="cover" src="/cover42.jpg"></div><div class="description"><div class="hidden-content">Description</div></div><table id="chapters"><tr><td><a href="/fiction/42/story/chapter/900/interlude">Interlude</a><a href="/fiction/42/story/chapter/900/interlude">Yesterday</a></td></tr><tr><td><a href="/fiction/99/other/chapter/1">Other book</a></td></tr></table><div class="pagination"><a href="?reviews=2">Next reviews</a></div>''')
        self.assertEqual((page['title'],page['author'],page['description']),('Fixture','Writer','Description'))
        self.assertEqual(page['cover_url'],'https://www.royalroad.com/cover42.jpg');self.assertEqual(len(page['links']),1);self.assertEqual(page['pages'],[])
        self.assertEqual(canonical_source(url),'https://www.royalroad.com/fiction/42')

    def test_novelfull_next_page_only_and_same_book_filter(self):
        html='''<h3 class="title">Fixture</h3><div class="info"><a href="/author/Writer">Writer</a></div><div class="book"><img src="/cover.webp"></div><div class="desc-text">Description</div><ul class="list-chapter"><a href="/fixture/chapter-1354.html">Chapter 1354</a><a href="/fixture/chapter-1354.html">Duplicate</a><a href="/other/chapter-1.html">Other</a></ul><ul class="pagination"><li class="next"><a href="?page=2">Next</a></li><li class="last"><a href="?page=123">Last</a></li></ul>'''
        page=self.page('https://novelfull.com/fixture.html',html)
        self.assertEqual(page['author'],'Writer');self.assertEqual(len(page['links']),1);self.assertEqual(page['pages'],['https://novelfull.com/fixture.html?page=2'])
        self.assertEqual(canonical_source('https://www.novelfull.com/fixture/chapter-1354.html'),'https://novelfull.com/fixture.html')
        for target in ['?page=1','https://other.example/fixture.html?page=2','/other.html?page=2']:
            with self.assertRaises(ValueError):self.page('https://novelfull.com/fixture.html',html.replace('?page=2',target))

    def test_source_numbers_are_not_positions(self):
        for title,expected in [('Chapter 1850 — Title','1850'),('Chapter 1.5 — Bonus','1.5'),('Interlude',None),('Prologue',None),('Chapter 1850 — Renamed','1850')]:
            result=chapter_identity('https://www.royalroad.com/fiction/42/story/chapter/900/title',title)
            self.assertEqual(result['source_number'],expected);self.assertEqual(result['source_chapter_id'],'900')

    def test_redirect_cannot_assign_a_different_novel_to_the_requested_identity(self):
        response=SimpleNamespace(url='https://www.royalroad.com/fiction/99/other',text='<h1>Other</h1>',headers={})
        with patch('scraper._fetch',return_value=response),self.assertRaisesRegex(ValueError,'different novel'):
            browse_page('https://www.royalroad.com/fiction/42/story')

    def test_policy_blocked_and_unverified_sites_fail_before_network(self):
        for host in ['www.wuxiaworld.com','novelfire.net','ranobes.net','www.scribblehub.com']:
            with self.assertRaisesRegex(ValueError,'not supported'):canonical_source('https://'+host+'/novel/1')
        for url in ['http://127.0.0.1/a','http://169.254.169.254/a','file:///etc/passwd']:
            with self.assertRaises(ValueError):canonical_source(url)


@unittest.skipUnless(test_http.RUNTIME_AVAILABLE,'Runtime packages required')
class CatalogTests(unittest.TestCase):
    setUp=test_http.HttpTests.setUp
    worker=test_platform.PlatformTests.worker
    drain=test_platform.PlatformTests.drain
    post=test_platform.PlatformTests.post
    upload=test_http.HttpTests.upload
    book=test_platform.PlatformTests.book

    def create(self):return self.post('/api/imports',{'url':'https://public.example/fixture'}).json
    def test_concurrent_discovery_is_single_job_and_completed_catalog_is_reused(self):
        def submit(_):
            with self.app.test_client() as client:return client.post('/api/imports',json={'url':'https://public.example/fixture'}).json
        with ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(submit,range(8)))
        self.assertEqual(len({r['job_id'] for r in rows}),1)
        worker,_=self.worker()
        with patch('scraper.browse_page',return_value={'links':[{'url':'https://public.example/chapter/1','title':'Interlude'}],'pages':[]}):self.drain(worker)
        self.assertEqual(self.create()['job_id'],rows[0]['job_id'])
        refreshed=self.post('/api/books/'+rows[0]['book_id']+'/import/refresh',{}).json['id'];self.assertNotEqual(refreshed,rows[0]['job_id'])

    def test_metadata_never_clobbers_manual_values_even_placeholder_or_blank(self):
        r=self.create();base='/api/books/'+r['book_id'];worker,_=self.worker()
        self.assertEqual(self.client.patch(base+'/metadata',json={'author':'Unknown Author','description':''}).status_code,200)
        page={'title':'Verified','author':'Real Author','description':'Real description','links':[{'url':'https://public.example/chapter/1','title':'Chapter 1354'}],'pages':[]}
        with patch('scraper.browse_page',return_value=page):self.drain(worker)
        book=self.client.get(base).json
        self.assertEqual((book['author'],book['description']),('Unknown Author',''));self.assertEqual(book['title'],'Verified')
        self.assertTrue(self.client.get(base+'/import/summary').json['metadata_ready'])

    def test_legacy_unknown_author_is_filled_only_by_verified_nonempty_metadata(self):
        r=self.create();base='/api/books/'+r['book_id'];worker,_=self.worker()
        with connect(self.db) as conn:
            with conn:conn.execute("UPDATE books SET author='Unknown Author' WHERE id=?",(r['book_id'],))
        with patch('scraper.browse_page',return_value={'title':'Title','author':'Writer','description':'Description','links':[{'url':'https://public.example/chapter/1','title':'Bonus'}],'pages':[]}):self.drain(worker)
        self.assertEqual(self.client.get(base).json['author'],'Writer')

    def test_reopening_legacy_missing_metadata_repairs_once_without_losing_cached_catalog(self):
        r=self.create();base='/api/books/'+r['book_id'];worker,_=self.worker()
        with patch('scraper.browse_page',return_value={'links':[{'url':'https://public.example/chapter/1','title':'Interlude'}],'pages':[]}):self.drain(worker)
        with connect(self.db) as conn:
            with conn:conn.execute("UPDATE platform_jobs SET result='{}' WHERE id=?",(r['job_id'],))
        repaired=self.create();self.assertNotEqual(repaired['job_id'],r['job_id']);self.assertTrue(self.client.get(base+'/import/summary').json['discovery_complete'])
        self.assertEqual(self.create()['job_id'],repaired['job_id'])
        with patch('scraper.browse_page',return_value={'author':'Writer','links':[{'url':'https://public.example/chapter/1','title':'Interlude'}],'pages':[]}):self.drain(worker)
        self.assertEqual(self.create()['job_id'],repaired['job_id']);self.assertEqual(self.client.get(base).json['author'],'Writer')

    def test_renamed_royalroad_slug_retains_imported_section_and_catalog_position(self):
        r=self.post('/api/imports',{'url':'https://www.royalroad.com/fiction/42'}).json;base='/api/books/'+r['book_id'];worker,_=self.worker()
        old='https://www.royalroad.com/fiction/42/story/chapter/900/old-title'
        with patch('scraper.browse_page',return_value={'links':[{'url':old,'title':'Chapter 1850 — Old'}],'pages':[]}):self.drain(worker)
        self.post(base+'/import/jobs',{'mode':'first','count':1})
        with patch('scraper.scrape_chapter',return_value={'chapter_title':'Chapter 1850 — Old','content':'Original imported text. '*20}):self.drain(worker)
        section=self.client.get(base+'/sections').json['items'][0]['id']
        self.post(base+'/import/refresh',{})
        with patch('scraper.browse_page',return_value={'links':[{'url':old.replace('old-title','new-title'),'title':'Chapter 1850 — New'}],'pages':[]}):self.drain(worker)
        rows=self.client.get(base+'/import/chapters').json['items'];self.assertEqual(len(rows),1);self.assertEqual(rows[0]['section_id'],section);self.assertEqual(rows[0]['url'],old);self.assertEqual(rows[0]['local_position'],0);self.assertEqual(rows[0]['source_chapter_id'],'900');self.assertIn('New',rows[0]['title'])
        with connect(self.db) as conn:self.assertIn('Original imported text',conn.execute('SELECT text_content FROM book_sections WHERE id=?',(section,)).fetchone()[0])

    def test_large_paginated_catalog_progress_partial_retry_and_stable_positions(self):
        r=self.create();base='/api/books/'+r['book_id'];worker,_=self.worker()
        def page(url):
            index=int(url.rsplit('=',1)[1]) if '?page=' in url else 0
            links=[{'url':f'https://public.example/chapter/{n}','title':('Interlude' if n==5 else f'Chapter {1354+n*2}.5 — Fixture')} for n in range(index*400,(index+1)*400)]
            return dict(title='Fixture',author='Writer',links=links+links[:2],pages=[f'https://public.example/fixture?page={index+1}'] if index<2 else ['https://public.example/fixture'])
        with patch('scraper.browse_page',side_effect=page):
            worker.step();s=self.client.get(base+'/import/summary').json;self.assertEqual(s['live_discovered'],400);self.assertFalse(s['discovery_complete'])
            self.drain(worker)
        s=self.client.get(base+'/import/summary').json;self.assertEqual(s['discovered'],1200);self.assertTrue(s['discovery_complete'])
        rows=self.client.get(base+'/import/chapters?offset=1100&limit=100').json;self.assertEqual(len(rows['items']),100);self.assertEqual(rows['items'][0]['local_position'],1100)
        with connect(self.db) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM book_sections').fetchone()[0],0);self.assertEqual(conn.execute('SELECT COUNT(*) FROM audio_variants').fetchone()[0],0)
        job=self.post(base+'/import/refresh',{}).json['id']
        with patch('scraper.browse_page',return_value=dict(links=[{'url':'https://public.example/chapter/5','title':'Interlude renamed'}],pages=['https://public.example/broken'])):worker.step()
        with patch('scraper.browse_page',side_effect=ValueError('Access restricted')):
            for _ in range(4):
                with connect(self.db) as conn:
                    with conn:conn.execute('UPDATE platform_items SET next_attempt=0 WHERE job_id=?',(job,))
                worker.step()
        s=self.client.get(base+'/import/summary').json;self.assertEqual(s['discovered'],1200);self.assertEqual(s['live_discovered'],1);self.assertEqual(s['jobs']['discover']['status'],'failed')
        live=self.client.get(base+'/import/chapters?job='+job).json['items'][0];self.assertEqual(live['local_position'],5);self.assertEqual(live['catalog_position'],0)

    def test_global_default_and_per_book_theme_inheritance_preserve_progress(self):
        book,sections=self.book();base='/api/books/'+book
        self.assertEqual(self.client.get('/api/reader/appearance?book='+book).json,{'theme':'dark','inherit':True})
        payload=dict(section_id=sections[1]['id'],fraction=.4,anchor='text:10',preferences={'theme':'dark'},version=0,inherit_theme=True)
        self.assertEqual(self.client.put(base+'/progress',json=payload).status_code,200)
        old=self.client.get(base+'/progress').json
        self.assertEqual(self.client.put('/api/reader/appearance',json={'theme':'lightgray'}).status_code,200)
        self.assertEqual(self.client.get('/api/reader/appearance?book='+book).json,{'theme':'lightgray','inherit':True});self.assertEqual(self.client.get(base+'/progress').json,old)
        payload.update(version=1,inherit_theme=False,preferences={'theme':'sepia'});self.client.put(base+'/progress',json=payload)
        self.assertFalse(self.client.get('/api/reader/appearance?book='+book).json['inherit'])
        payload.update(version=1,inherit_theme=True);self.assertEqual(self.client.put(base+'/progress',json=payload).status_code,409)
        self.assertFalse(self.client.get('/api/reader/appearance?book='+book).json['inherit'])
        self.assertEqual(self.client.put('/api/reader/appearance',json={'theme':'bogus'}).status_code,400)
        self.assertEqual(self.client.put('/api/reader/appearance',json={'theme':[]}).status_code,400)
        self.assertEqual(self.client.put('/api/reader/appearance?book=missing',json={'theme':'sepia'}).status_code,400)
        self.assertEqual(self.client.get('/api/reader/appearance').json['theme'],'lightgray')
