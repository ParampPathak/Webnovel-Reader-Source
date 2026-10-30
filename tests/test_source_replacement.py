"""Safe cross-provider replacements using synthetic metadata and isolated SQLite."""
import json
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from chapter_updates import configure, status, request_check, Updater, identity
from library_store import connect
from source_replacement import preview, match_catalog
from discovery import discover_catalog
from source_adapters import retained_chapter_url
from tests import test_chapter_updates


class ReplacementTests(unittest.TestCase):
    setUp = test_chapter_updates.UpdateTests.setUp
    seed = test_chapter_updates.UpdateTests.seed

    def setup_case(self, removed=True):
        self.old=self.seed()
        self.source='https://www.royalroad.com/fiction/42'
        self.links=[{'url':self.source+'/story/chapter/'+str(900+n)+'/chapter',
                     'title':f'Chapter {n+4}: {title}'} for n,title in enumerate(['Opening','Cold Front','Arrival'])]
        with connect(self.db) as c,c:
            c.execute("UPDATE books SET title='Synthetic Story',author='Fixture Author' WHERE id='novel'")
            for n,title in enumerate(['Opening','Cold Front','Arrival']):
                c.execute('UPDATE book_sections SET title=? WHERE book_id=? AND position=?',(f'Chapter {n+1} - {title}','novel',n))
            sid=c.execute("SELECT id FROM book_sections WHERE book_id='novel' ORDER BY position LIMIT 1").fetchone()[0]
            c.execute("INSERT INTO reading_progress(book_id,section_id,fraction) VALUES('novel',?,0.43)",(sid,))
            c.execute("INSERT INTO listening_progress(book_id,section_id,asset_id,seconds,voice) VALUES('novel',?,'cached',195,'en-US-AndrewNeural')",(sid,))
            c.execute("INSERT INTO book_bookmarks(book_id,section_id,fraction,title) VALUES('novel',?,0.5,'Saved')",(sid,))
            c.execute("INSERT INTO audio_variants(id,book_id,section_id,provider,voice,settings,content_hash,path,status,chunk_count) VALUES('cached','novel',?,'edge','en-US-AndrewNeural','{}','original-hash','/synthetic/cached.mp3','ready',1)",(sid,))
            c.execute("INSERT INTO audio_chunks(asset_id,position,path,checksum) VALUES('cached',0,'/synthetic/chunk.mp3','original-checksum')")
        if removed:configure(self.db,'novel',{'source_url':''})
        self.page={'url':self.source+'/story','title':'Synthetic Story','author':'Fixture Author','links':self.links,'pages':[]}

    def get_preview(self):
        with patch('scraper.browse_page',return_value=self.page) as browse:
            result=preview(self.db,'novel',{'source_url':self.source})
        browse.assert_called_once_with(self.source)
        return result

    def save(self,p,**extra):
        return configure(self.db,'novel',{'source_url':self.source,'preview_id':p['preview_id'],**extra})

    def snapshot(self):
        with connect(self.db) as c:
            return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in
                    ['books','book_sections','audio_variants','audio_chunks','reading_progress','listening_progress','book_bookmarks']}

    def test_removed_old_source_replacement_preserves_all_content_and_progress(self):
        self.setup_case();before=self.snapshot();p=self.get_preview()
        self.assertEqual(p['matched'],3);self.assertFalse(p['needs_confirmation'])
        saved=self.save(p);self.assertEqual(saved['source_url'],self.source)
        self.assertEqual(saved['checkpoint'],'902');self.assertEqual(before,self.snapshot())

    def test_old_404_blocked_unavailable_or_healthy_is_never_fetched(self):
        for old_error in ['HTTP 404','timeout','HTTP 403','healthy']:
            with self.subTest(old_error=old_error):
                if old_error=='HTTP 404':self.setup_case(removed=False)
                p=self.get_preview();self.save(p)
        self.assertEqual(status(self.db,'novel')['provider'],'royalroad')

    def test_unmatched_source_needs_explicit_conservative_future_baseline(self):
        self.setup_case();self.page['title']='Another Story'
        self.page['links']=[{**x,'title':'Chapter '+str(n+1)} for n,x in enumerate(self.links)]
        p=self.get_preview();self.assertTrue(p['needs_confirmation']);before=self.snapshot()
        with self.assertRaisesRegex(ValueError,'Confirm'):self.save(p)
        self.save(p,confirm_baseline=True);self.assertEqual(before,self.snapshot())
        with connect(self.db) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM source_chapter_matches').fetchone()[0],0)

    def test_conflicting_edit_or_expired_preview_cannot_save(self):
        self.setup_case();p=self.get_preview();configure(self.db,'novel',{'source_url':''})
        with self.assertRaisesRegex(ValueError,'changed'):self.save(p)
        p=self.get_preview()
        with patch('source_replacement.time.time',return_value=time.time()+901):
            with self.assertRaisesRegex(ValueError,'expired'):self.save(p)

    def test_invalid_timeout_empty_and_redirected_new_source_leave_configuration_unchanged(self):
        self.setup_case();before=status(self.db,'novel')
        for error,word in [(TimeoutError('secret'),'timed out'),(ValueError('Source returned HTTP 404'),'404'),(ValueError('This site bot-protection challenge'),'blocked')]:
            with patch('scraper.browse_page',side_effect=error):
                with self.assertRaisesRegex(ValueError,word):preview(self.db,'novel',{'source_url':self.source})
        for page in [{**self.page,'links':[]},{**self.page,'url':self.source.replace('42','99')}]:
            with patch('scraper.browse_page',return_value=page):
                with self.assertRaises(ValueError):preview(self.db,'novel',{'source_url':self.source})
        self.assertEqual(status(self.db,'novel'),before)

    def test_aliases_preserve_importer_section_and_audio_identity_after_renamed_slug(self):
        self.setup_case();self.save(self.get_preview())
        with connect(self.db) as c:
            for n,link in enumerate(self.links):
                retained=retained_chapter_url(c,'novel',link['url'].replace('/story/','/renamed/'))
                self.assertEqual(retained,self.old+'/chapter-'+str(n+1))

    def test_updates_fetch_new_source_append_only_new_chapter_without_duplicate(self):
        self.setup_case();self.save(self.get_preview(),enabled=True);before=self.snapshot()
        added={'url':self.source+'/story/chapter/10/later-draft-id','title':'Chapter 8: New Arrival'}
        requests=[];downloads=[]
        def browse(url):requests.append(url);return {**self.page,'links':self.links+[added]}
        def scrape(url):downloads.append(url);return {'content':'Synthetic chapter text','chapter_title':added['title']}
        worker=Updater(self.db,browse,scrape,clock=lambda:time.time()+1000000)
        request_check(self.db,'novel')
        for _ in range(8):
            worker.step()
            if status(self.db,'novel')['status']=='current':break
        self.assertEqual(requests,[self.source]);self.assertEqual(downloads,[added['url']])
        after=self.snapshot();self.assertEqual(after['book_sections'][:3],before['book_sections'])
        for t in ['reading_progress','listening_progress','audio_variants','audio_chunks','book_bookmarks']:self.assertEqual(before[t],after[t])
        request_check(self.db,'novel');worker.step();self.assertEqual(len(self.snapshot()['book_sections']),4)

    def test_native_web_preview_and_save_share_revision_and_persist_after_restart(self):
        from app import create_app
        self.setup_case();native=self.app.test_client();web=self.app.test_client();base='/api/books/novel/chapter-updates'
        with patch('scraper.browse_page',return_value=self.page):
            p=native.post(base+'/preview',json={'source_url':self.source,'config_revision':1})
        self.assertEqual(p.status_code,200)
        r=native.put(base,json={'source_url':self.source,'preview_id':p.json['preview_id'],'config_revision':1})
        self.assertEqual(r.status_code,200);self.assertEqual(web.get(base).json['source_url'],self.source)
        revision=r.json['config_revision'];r=web.put(base,json={'interval':172800,'config_revision':revision})
        self.assertEqual(r.status_code,200);self.assertEqual(native.get(base).json['interval'],172800)
        self.assertEqual(create_app(self.root/'data').test_client().get(base).json['source_url'],self.source)
        self.assertEqual(native.put(base,json={'enabled':True,'config_revision':revision}).status_code,409)

    def test_normal_importer_reuses_aliases_without_duplicate_sections_or_audio(self):
        from platform_worker import PlatformWorker
        from tests import test_platform
        self.setup_case();self.save(self.get_preview());before=self.snapshot()
        response=self.client.post('/api/books/novel/import/refresh',json={})
        self.assertIn(response.status_code,(200,202))
        with patch('platform_worker.ResourceGuard',return_value=SimpleNamespace(check=lambda:None)):
            worker=PlatformWorker(self.root/'data',test_platform.MockSpeech(),assembler=test_platform.mock_assemble,duration_probe=lambda p:10)
        with patch('scraper.browse_page',return_value=self.page):
            for _ in range(8):
                if not worker.step():break
        self.assertEqual(self.snapshot(),before)
        with connect(self.db) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM import_chapters WHERE book_id='novel'").fetchone()[0],3)
            self.assertTrue(all(r[0] for r in c.execute("SELECT section_id FROM import_current WHERE book_id='novel'")))

    def test_old_discovery_cannot_publish_after_source_changes_during_network_request(self):
        from platform_worker import PlatformWorker
        from tests import test_platform
        self.setup_case(removed=False);p=self.get_preview();before=self.snapshot()
        self.client.post('/api/books/novel/import/refresh',json={})
        with patch('platform_worker.ResourceGuard',return_value=SimpleNamespace(check=lambda:None)):
            worker=PlatformWorker(self.root/'data',test_platform.MockSpeech(),assembler=test_platform.mock_assemble,duration_probe=lambda p:10)
        def old_response(url):
            self.assertEqual(url,self.old);self.save(p)
            return {'url':self.old,'links':[{'url':self.old+'/chapter-99','title':'Stale Chapter'}],'pages':[]}
        with patch('scraper.browse_page',side_effect=old_response):worker.step()
        self.assertEqual(before,self.snapshot())
        self.assertEqual(status(self.db,'novel')['source_url'],self.source)
        with connect(self.db) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM import_chapters WHERE url LIKE '%chapter-99'").fetchone())


class SharedDiscoveryTests(unittest.TestCase):
    def test_royalroad_normalization_query_chapter_links_and_duplicate_identity(self):
        self.assertEqual(identity('https://royalroad.com/fiction/42/story?reviews=2')[0],'https://www.royalroad.com/fiction/42')
        url='https://www.royalroad.com/fiction/42'
        html='<div class="fic-title"><h1>Synthetic Story</h1></div><table id="chapters"><a href="/fiction/42/story/chapter/9/old">Chapter 1: Arrival</a><a href="/fiction/42/story/chapter/9/new">Date duplicate</a></table>'
        with patch('scraper._fetch',return_value=SimpleNamespace(text=html,url=url+'/story',headers={})):
            p=discover_catalog(url)
        self.assertEqual(len(p['links']),1);self.assertEqual(p['title'],'Synthetic Story')

    def test_timeout_page_budget_and_other_provider_use_same_parser(self):
        with patch('scraper.browse_page') as browse:
            browse.return_value={'url':'https://freewebnovel.com/novel/test','title':'Test','links':[{'title':'Chapter 1','url':'https://freewebnovel.com/novel/test/chapter-1'}],'pages':['https://freewebnovel.com/novel/test?page=2']}
            with self.assertRaisesRegex(ValueError,'page budget'):discover_catalog('https://freewebnovel.com/novel/test',max_pages=1)
        from discovery import budget,remaining
        with budget(-1):
            with self.assertRaises(TimeoutError):remaining()

    def test_number_alone_and_reordered_titles_do_not_map(self):
        existing=[{'id':i,'position':i,'title':t,'href':'https://freewebnovel.com/novel/test/chapter-'+str(i)} for i,t in enumerate(['Chapter 1: One','Chapter 2: Two','Chapter 3: Three'])]
        links=[{'url':'https://www.royalroad.com/fiction/42/s/chapter/'+str(i)+'/x','title':t} for i,t in enumerate(['Chapter 1','Chapter 2','Chapter 3'])]
        self.assertEqual(match_catalog(existing,links,'royalroad','42'),[])
        links[0]['title']='Chapter 10: Three';links[1]['title']='Chapter 11: One'
        with self.assertRaisesRegex(ValueError,'reordered'):match_catalog(existing,links,'royalroad','42')
