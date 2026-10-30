"""Disposable SQLite, Flask API, and injected public-source fixtures."""
import json
import threading
import unittest
from unittest.mock import patch
from chapter_updates import Updater, identity, chapter_id, status, configure, request_check
from library_store import connect
from tests import test_http


class UpdateTests(unittest.TestCase):
    setUp = test_http.HttpTests.setUp

    def test_source_configuration_revision_rejects_stale_client_edit(self):
        self.seed()
        first=self.client.get('/api/books/novel/chapter-updates').json
        revision=first.get('config_revision',0)
        response=self.client.put('/api/books/novel/chapter-updates',json={'enabled':True,'config_revision':revision})
        self.assertEqual(response.status_code,200)
        stale=self.client.put('/api/books/novel/chapter-updates',json={'enabled':False,'config_revision':revision})
        self.assertEqual(stale.status_code,409)
        self.assertTrue(status(self.db,'novel')['enabled'])

    def test_source_removal_is_explicit_persisted_and_recoverable(self):
        self.seed()
        result=self.client.put('/api/books/novel/chapter-updates',json={'source_url':''})
        self.assertEqual(result.status_code,200)
        self.assertFalse(result.json['eligible'])
        self.assertFalse(result.json.get('source_url'))
        with connect(self.db) as c:
            self.assertIsNone(c.execute("SELECT url FROM import_sources WHERE book_id='novel'").fetchone())
        self.assertFalse(status(self.db,'novel')['eligible'])
        self.assertEqual(configure(self.db,'novel',{'source_url':'https://freewebnovel.com/novel/novel'})['source_id'],'novel')

    def test_stale_worker_cannot_write_after_source_settings_change(self):
        source=self.seed();request_check(self.db,'novel');w=self.worker(source,4)
        browse=w.browse
        def edit_while_fetching(url):
            configure(self.db,'novel',{'enabled':False})
            return browse(url)
        w.browse=edit_while_fetching
        w.step()
        s=status(self.db,'novel')
        self.assertNotIn(s['status'],('checking','importing'))
        self.assertEqual(self.rows('chapter_update_items'),[])

    def test_expired_check_exits_checking_and_can_retry(self):
        self.seed()
        with connect(self.db) as c,c:
            c.execute("UPDATE chapter_updates SET status='checking',last_checked=1,next_check=0,state=? WHERE book_id='novel'",(json.dumps({'pending':['https://freewebnovel.com/novel/novel'],'visited':[],'links':[],'started_at':1}),))
        s=status(self.db,'novel')
        self.assertEqual(s['status'],'unavailable')
        self.assertIn('timed out',s['message'].lower())

    def test_status_uses_real_scheduler_state(self):
        self.seed()
        response=self.client.get('/api/books/novel/chapter-updates')
        self.assertFalse(response.json['scheduler_running'])

    def test_two_clients_share_saved_source_across_backend_restart(self):
        from app import create_app
        source=self.seed()
        with connect(self.db) as c,c:c.execute("UPDATE import_sources SET url=? WHERE book_id='novel'",(source.replace('https:','http:'),))
        native=self.app.test_client();web=self.app.test_client()
        revision=native.get('/api/books/novel/chapter-updates').json['config_revision']
        result=native.put('/api/books/novel/chapter-updates',json={'source_url':source,'enabled':True,'config_revision':revision})
        self.assertEqual(result.status_code,200)
        self.assertEqual(web.get('/api/books/novel').json['source_url'],source)
        saved=web.get('/api/books/novel/chapter-updates').json
        self.assertEqual(saved['source_url'],source)
        result=web.put('/api/books/novel/chapter-updates',json={'interval':172800,'config_revision':saved['config_revision']})
        self.assertEqual(result.status_code,200)
        restarted=create_app(self.root/'data').test_client()
        self.assertEqual(restarted.get('/api/books/novel/chapter-updates').json['interval'],172800)
        self.assertEqual(native.get('/api/books/novel/chapter-updates').json['interval'],172800)

    def test_invalid_source_keeps_authoritative_configuration_and_revision(self):
        source=self.seed();before=status(self.db,'novel')
        response=self.client.put('/api/books/novel/chapter-updates',json={'source_url':'https://freewebnovel.com/novel/unrelated','config_revision':before['config_revision']})
        self.assertEqual(response.status_code,400)
        self.assertEqual(status(self.db,'novel'),before)
        self.assertEqual(self.client.get('/api/books/novel').json['source_url'],source)

    def test_manual_paginated_check_continues_before_the_next_scheduled_deadline(self):
        source=self.seed();now=10000000000
        with connect(self.db) as c,c:
            c.execute("UPDATE chapter_updates SET next_check=?,new_count=4,last_checked=0 WHERE book_id='novel'",(now+43200,))
        worker=self.worker(source,4);original=worker.browse
        def pages(url):
            result=original(url)
            if url==source:result['links']=result['links'][:3];result['pages']=[source+'?page=2']
            else:result['links']=result['links'][3:]
            return result
        worker.browse=pages;request_check(self.db,'novel');self.assertTrue(worker.step())
        for _ in range(5):worker.step()
        self.assertEqual(len(self.rows('book_sections')),4)
        self.assertEqual(status(self.db,'novel')['status'],'current')

    def test_new_check_does_not_report_a_previous_runs_import_count(self):
        source=self.seed()
        with connect(self.db) as c,c:c.execute("UPDATE chapter_updates SET new_count=4,last_checked=0 WHERE book_id='novel'")
        request_check(self.db,'novel')
        self.assertEqual(status(self.db,'novel')['new_count'],0)

    def test_disabled_manual_import_recovers_after_worker_restart_without_catalog_rescan(self):
        source=self.seed();w=self.worker(source,4);request_check(self.db,'novel');w.step()
        def failed(url):raise TimeoutError('temporary source outage')
        w.scrape=failed;w.step()
        restarted=self.worker(source,4);restarted.clock=lambda:20000000000
        for _ in range(6):restarted.step()
        self.assertEqual(len(self.rows('book_sections')),4)
        self.assertEqual(status(self.db,'novel')['new_count'],1)
        self.assertFalse(self.fetches,'A pending chapter resumes its durable import queue')

    def test_metadata_only_chapter_is_filled_before_it_can_count_as_imported(self):
        source=self.seed()
        with connect(self.db) as c,c:
            sid=c.execute("INSERT INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES('novel',3,'Metadata',?,'','')",(source+'/chapter-4',)).lastrowid
            c.execute("INSERT INTO import_chapters(book_id,url,position,title,section_id,status) VALUES('novel',?,3,'Metadata',?,'completed')",(source+'/chapter-4',sid))
        result=self.run_check(self.worker(source,4))
        with connect(self.db) as c:
            row=c.execute('SELECT id,text_content FROM book_sections WHERE id=?',(sid,)).fetchone()
            events=[r[0] for r in c.execute("SELECT event FROM chapter_update_events WHERE book_id='novel'")]
        self.assertTrue(row['text_content'].strip());self.assertEqual(row['id'],sid)
        self.assertEqual(result['new_count'],1);self.assertEqual(len(self.rows('book_sections')),4)
        self.assertIn('imported',events);self.assertEqual(events[-1],'completed')

    def test_due_scheduled_path_imports_readable_content_and_records_committed_outcome(self):
        source=self.seed();configure(self.db,'novel',{'enabled':True})
        with connect(self.db) as c,c:c.execute("UPDATE chapter_updates SET next_check=0,new_count=7 WHERE book_id='novel'")
        w=self.worker(source,4)
        w.step();self.assertEqual(status(self.db,'novel')['last_success'],0)
        for _ in range(5):w.step()
        result=status(self.db,'novel');self.assertEqual(result['new_count'],1)
        self.assertEqual(result['pending_count'],0);self.assertEqual(result['status'],'current')
        with connect(self.db) as c:
            events=[dict(r) for r in c.execute("SELECT event,details FROM chapter_update_events WHERE book_id='novel' ORDER BY id")]
            chapter=c.execute("SELECT id,text_content FROM book_sections WHERE book_id='novel' ORDER BY position DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(events[0]['details'])['trigger'],'scheduled')
        self.assertTrue(chapter['text_content'].strip())
        self.assertEqual(self.client.get('/api/books/novel/sections/'+str(chapter['id'])).status_code,200)
        self.assertEqual([e['event'] for e in events],['started','discovered','queued','importing','imported','completed'])
        with connect(self.db) as c,c:c.execute("UPDATE chapter_updates SET next_check=0 WHERE book_id='novel'")
        w.step();self.assertEqual(len(self.rows('book_sections')),4)

    def seed(self, book='novel', count=3, partial=False):
        source='https://freewebnovel.com/novel/'+book
        with connect(self.db) as c, c:
            c.execute("INSERT INTO books(id,title,source_kind) VALUES(?,?,'webnovel')",(book,book))
            c.execute('INSERT INTO import_sources(book_id,url) VALUES(?,?)',(book,source))
            for n in range(1,count+1):
                url=source+'/chapter-'+str(n)
                sid=None
                if not partial or n in (1,2):
                    sid=c.execute('INSERT INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES(?,?,?,?,?,?)',(book,n-1,'Chapter '+str(n),url,'<p>Original</p>','Original')).lastrowid
                c.execute('INSERT INTO import_chapters(book_id,url,position,title,section_id) VALUES(?,?,?,?,?)',(book,url,n-1,'Chapter '+str(n),sid))
        status(self.db,book)
        return source

    def worker(self, source, count=3, fail=False, rename=False):
        self.fetches=[];self.downloads=[]
        def browse(url):
            self.fetches.append(url)
            if fail: raise TimeoutError('private token URL must not leak')
            return {'url':source,'title':'Novel','links':[{'url':source+'/chapter-'+str(n),'title':('Renamed' if rename else 'Chapter ')+str(n)} for n in range(1,count+1)],'pages':[]}
        def scrape(url):
            self.downloads.append(url)
            return {'content':'New safe text\n<script>alert(1)</script>','chapter_title':'New <title>'}
        return Updater(self.db,browse,scrape,clock=lambda:10000000000)

    def run_check(self, worker, book='novel'):
        request_check(self.db,book)
        for _ in range(105):
            if not worker.step():break
            if status(self.db,book)['status']=='current':break
        return status(self.db,book)

    def rows(self, table):
        with connect(self.db) as c:return [tuple(r) for r in c.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    def test_new_import_stores_supported_identity_and_default_off(self):
        response=self.client.post('/api/imports',json={'url':'https://freewebnovel.com/novel/new'})
        book=response.json['book_id'];s=self.client.get('/api/books/'+book+'/chapter-updates').json
        self.assertEqual(s['provider'],'freewebnovel');self.assertEqual(s['source_id'],'new');self.assertEqual(s['enabled'],0)

    def test_reuses_existing_source_and_catalog_tail(self):
        source=self.seed(count=3200,partial=True);s=status(self.db,'novel')
        self.assertEqual(s['source_url'],source);self.assertEqual(s['checkpoint'],'3200');self.assertFalse(s['enabled'])

    def test_enable_disable_interval(self):
        self.seed();self.assertTrue(configure(self.db,'novel',{'enabled':True,'interval':43200})['enabled'])
        self.assertFalse(configure(self.db,'novel',{'enabled':False})['enabled'])
        self.assertRaises(ValueError,configure,self.db,'novel',{'interval':60})

    def test_disabled_scheduler_never_fetches(self):
        source=self.seed();w=self.worker(source);self.assertFalse(w.step());self.assertEqual(self.fetches,[])

    def test_manual_check_works_while_off_and_coalesces(self):
        source=self.seed();request_check(self.db,'novel');request_check(self.db,'novel');w=self.worker(source)
        self.assertTrue(w.step());self.assertFalse(w.step());self.assertEqual(len(self.fetches),1)

    def test_no_new_content_is_unchanged(self):
        source=self.seed();before=self.rows('book_sections');s=self.run_check(self.worker(source))
        self.assertEqual(before,self.rows('book_sections'));self.assertEqual(s['new_count'],0)

    def test_one_new_chapter_is_appended_and_sanitized(self):
        source=self.seed();s=self.run_check(self.worker(source,4));self.assertEqual(s['new_count'],1)
        row=self.rows('book_sections')[-1];self.assertEqual(row[2],3);self.assertIn('&lt;script&gt;',row[5]);self.assertNotIn('<script>',row[5])

    def test_multiple_ordered_chapters_and_stable_ids(self):
        source=self.seed();before=self.rows('book_sections');self.run_check(self.worker(source,6))
        rows=self.rows('book_sections');self.assertEqual(rows[:3],before);self.assertEqual([r[2] for r in rows],[0,1,2,3,4,5])

    def test_duplicate_checks_do_not_duplicate(self):
        source=self.seed();w=self.worker(source,5);self.run_check(w)
        with connect(self.db) as c,c:c.execute("UPDATE chapter_updates SET last_checked=0 WHERE book_id='novel'")
        self.run_check(w);self.assertEqual(len(self.rows('book_sections')),5);self.assertEqual(len(self.downloads),2)

    def test_renamed_titles_keep_original_text_and_identity(self):
        source=self.seed();before=self.rows('book_sections');self.run_check(self.worker(source,3,rename=True));self.assertEqual(before,self.rows('book_sections'))

    def test_partial_history_does_not_backfill_3200(self):
        source=self.seed(count=3200,partial=True);s=self.run_check(self.worker(source,3203))
        self.assertEqual(s['new_count'],3);self.assertEqual(len(self.rows('book_sections')),5);self.assertEqual(len(self.downloads),3);self.assertEqual(s['checkpoint'],'3203')

    def test_partial_import_restart_resumes_exact_items(self):
        source=self.seed();w=self.worker(source,6);request_check(self.db,'novel');w.step();w.step()
        restarted=self.worker(source,6)
        while status(self.db,'novel')['status']!='current':restarted.step()
        self.assertEqual(len(self.rows('book_sections')),6);self.assertEqual(status(self.db,'novel')['new_count'],3)

    def test_server_restart_retains_enabled_schedule(self):
        source=self.seed();configure(self.db,'novel',{'enabled':True});self.worker(source,4).step()
        restarted=self.worker(source,4)
        while status(self.db,'novel')['status']!='current':restarted.step()
        self.assertEqual(len(self.rows('book_sections')),4)

    def test_failed_check_keeps_checkpoint_and_redacts_errors(self):
        source=self.seed();s=self.run_check(self.worker(source,fail=True));self.assertEqual(s['checkpoint'],'3');self.assertEqual(s['status'],'unavailable');self.assertNotIn('token',s['message'])

    def test_incomplete_catalog_preserves_existing_chapters(self):
        source=self.seed();before=self.rows('book_sections');s=self.run_check(self.worker(source,2));self.assertEqual(s['checkpoint'],'3');self.assertEqual(before,self.rows('book_sections'))

    def test_concurrent_checks_import_each_chapter_once(self):
        source=self.seed();w=self.worker(source,5);request_check(self.db,'novel');w.step()
        threads=[threading.Thread(target=w.step) for _ in range(8)]
        for t in threads:t.start()
        for t in threads:t.join()
        while status(self.db,'novel')['status']!='current':w.step()
        self.assertEqual(len(self.rows('book_sections')),5)

    def test_unsupported_and_private_sources_fail(self):
        for url in ['http://127.0.0.1/book','http://169.254.169.254/latest/meta-data','file:///secret','https://example.com/book','https://freewebnovel.com/novel/x?token=secret']:
            with self.subTest(url=url):self.assertRaises(ValueError,identity,url)

    def test_changed_book_redirect_is_rejected(self):
        source=self.seed();w=self.worker(source);w.browse=lambda url:{'url':'https://freewebnovel.com/novel/other','links':[],'pages':[]}
        self.assertEqual(self.run_check(w)['status'],'unavailable')

    def test_source_assignment_requires_stable_chapter_identity(self):
        self.seed();self.assertRaises(ValueError,configure,self.db,'novel',{'source_url':'https://freewebnovel.com/novel/other','enabled':True})
        self.assertEqual(configure(self.db,'novel',{'source_url':'https://freewebnovel.com/novel/novel'})['source_id'],'novel')

    def test_epub_without_source_remains_ineligible(self):
        with connect(self.db) as c,c:c.execute("INSERT INTO books(id,title,source_kind) VALUES('epub','Novel','epub')")
        self.assertFalse(status(self.db,'epub')['eligible']);self.assertRaises(ValueError,request_check,self.db,'epub')

    def test_progress_bookmarks_preferences_and_audio_are_untouched(self):
        source=self.seed()
        with connect(self.db) as c,c:
            sid=c.execute("SELECT id FROM book_sections WHERE book_id='novel' ORDER BY position LIMIT 1").fetchone()[0]
            c.execute("INSERT INTO reading_progress(book_id,section_id,anchor,fraction,preferences) VALUES('novel',?,'text:20',.4,'{}')",(sid,))
            c.execute("INSERT INTO listening_progress(book_id,section_id,asset_id,seconds,voice) VALUES('novel',?,'cached',195,'en-US-AndrewNeural')",(sid,))
            c.execute("INSERT INTO book_bookmarks(book_id,section_id,fraction,title) VALUES('novel',?,.4,'Here')",(sid,))
            c.execute("INSERT INTO book_preferences(book_id,favorite) VALUES('novel',1)")
        tables=['reading_progress','listening_progress','book_bookmarks','book_preferences','audio_variants','audio_chunks','platform_settings']
        before={t:self.rows(t) for t in tables};self.run_check(self.worker(source,5))
        self.assertEqual(before,{t:self.rows(t) for t in tables});self.assertEqual(self.rows('platform_jobs'),[])

    def test_failure_does_not_block_other_enabled_book(self):
        source=self.seed('a');self.seed('b');configure(self.db,'a',{'enabled':True});configure(self.db,'b',{'enabled':True})
        w=self.worker(source);w.browse=lambda url: (_ for _ in ()).throw(TimeoutError()) if '/a' in url else {'url':url,'links':[{'url':url+'/chapter-'+str(n),'title':str(n)} for n in (1,2,3)],'pages':[]}
        w.step();w.step();self.assertEqual(status(self.db,'a')['status'],'unavailable');self.assertEqual(status(self.db,'b')['status'],'current')

    def test_overlap_with_manual_import_is_deferred(self):
        source=self.seed();request_check(self.db,'novel')
        with connect(self.db) as c,c:c.execute("INSERT INTO platform_jobs(id,kind,book_id,config) VALUES('manual','import','novel','{}')")
        self.assertFalse(self.worker(source).step());self.assertEqual(self.fetches,[])

    def test_paginated_checkpoint_survives_restart(self):
        source=self.seed();w=self.worker(source,4);browse=w.browse
        def pages(url):
            page=browse(url)
            if '?' not in url:page['links']=page['links'][:2];page['pages']=[source+'?ajax=chapters&page=2']
            else:page['links']=page['links'][2:]
            return page
        w.browse=pages;request_check(self.db,'novel');w.step();self.assertEqual(status(self.db,'novel')['status'],'checking')
        restarted=Updater(self.db,pages,w.scrape,clock=w.clock)
        while status(self.db,'novel')['status']!='current':restarted.step()
        self.assertEqual(len(self.rows('book_sections')),4)

    def test_no_checkpoint_establishes_baseline_without_backfill(self):
        source=self.seed(count=0)
        self.run_check(self.worker(source,100));self.assertEqual(self.downloads,[]);self.assertEqual(status(self.db,'novel')['checkpoint'],'100')

    def test_large_new_batch_requires_attention(self):
        source=self.seed();s=self.run_check(self.worker(source,200));self.assertEqual(s['status'],'unavailable');self.assertEqual(self.downloads,[])

    def test_reordered_source_is_not_appended(self):
        source=self.seed();w=self.worker(source,4);browse=w.browse
        def reordered(url):
            p=browse(url);p['links']=[p['links'][2],p['links'][0],p['links'][1],p['links'][3]];return p
        w.browse=reordered;self.assertEqual(self.run_check(w)['status'],'unavailable');self.assertEqual(self.downloads,[])

    def test_royalroad_identity_survives_slug_rename(self):
        a='https://www.royalroad.com/fiction/42/old/chapter/123/old-name'
        b='https://www.royalroad.com/fiction/42/new/chapter/123/new-name'
        self.assertEqual(chapter_id(a,'royalroad','42'),chapter_id(b,'royalroad','42'))

    def test_cross_origin_writes_are_rejected(self):
        self.seed();response=self.client.post('/api/books/novel/chapter-updates',json={},headers={'Origin':'http://evil.example'})
        self.assertEqual(response.status_code,403)

    def test_already_discovered_new_release_is_imported_at_its_stable_catalog_position(self):
        source=self.seed()
        with connect(self.db) as c,c:c.execute("INSERT INTO import_chapters(book_id,url,position,title) VALUES('novel',?,3,'New')",(source+'/chapter-4',))
        self.run_check(self.worker(source,4));self.assertEqual(self.rows('book_sections')[-1][2],3)

    def test_manually_imported_new_release_is_not_downloaded_again(self):
        source=self.seed()
        with connect(self.db) as c,c:
            sid=c.execute("INSERT INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES('novel',3,'New',?,'<p>Manual</p>','Manual')",(source+'/chapter-4',)).lastrowid
            c.execute("INSERT INTO import_chapters(book_id,url,position,title,section_id) VALUES('novel',?,3,'New',?)",(source+'/chapter-4',sid))
        self.run_check(self.worker(source,4));self.assertEqual(self.downloads,[]);self.assertEqual(len(self.rows('book_sections')),4)

    def test_import_failure_keeps_durable_items_and_resumes_with_backoff(self):
        source=self.seed();w=self.worker(source,5);request_check(self.db,'novel');w.step();w.step()
        scrape=w.scrape;w.scrape=lambda url: (_ for _ in ()).throw(TimeoutError());w.step();self.assertEqual(status(self.db,'novel')['checkpoint'],'4');self.assertEqual(len(self.rows('book_sections')),4)
        w.scrape=scrape;w.clock=lambda:20000000000;w.step();w.step();self.assertEqual(len(self.rows('book_sections')),5);self.assertEqual(status(self.db,'novel')['new_count'],2)

    def test_small_manual_budget_stops_before_importing_an_excessive_batch(self):
        source=self.seed();request_check(self.db,'novel',{'max_new':1,'max_pages':1});w=self.worker(source,5);w.step()
        self.assertEqual(status(self.db,'novel')['status'],'unavailable');self.assertEqual(self.downloads,[]);self.assertEqual(len(self.rows('book_sections')),3)

    def test_small_page_budget_never_fetches_the_second_catalog_page(self):
        source=self.seed();request_check(self.db,'novel',{'max_pages':1});w=self.worker(source);browse=w.browse
        def paginated(url):
            p=browse(url);p['pages']=[source+'?page=2'];return p
        w.browse=paginated;w.step();w.step();self.assertEqual(len(self.fetches),1);self.assertEqual(status(self.db,'novel')['status'],'unavailable')

    def test_royalroad_draft_id_need_not_increase_when_new_release_is_appended(self):
        self.seed();source='https://www.royalroad.com/fiction/42'
        with connect(self.db) as c,c:
            c.execute("DELETE FROM chapter_updates WHERE book_id='novel'")
            c.execute("UPDATE import_sources SET url=? WHERE book_id='novel'",(source,))
            for n,key in enumerate([50,100,150]):
                url=source+'/book/chapter/'+str(key)+'/title'
                c.execute("UPDATE import_chapters SET url=? WHERE book_id='novel' AND position=?",(url,n))
                c.execute("UPDATE book_sections SET href=? WHERE book_id='novel' AND position=?",(url,n))
        status(self.db,'novel');w=self.worker(source)
        w.browse=lambda url:{'url':source,'title':'Book','links':[{'url':source+'/book/chapter/'+str(key)+'/title','title':str(key)} for key in [50,100,150,144]],'pages':[]}
        self.assertEqual(self.run_check(w)['new_count'],1);self.assertEqual(status(self.db,'novel')['checkpoint'],'144')
