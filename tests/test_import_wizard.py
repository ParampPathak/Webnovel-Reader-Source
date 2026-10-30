"""Bounded persisted importer/worker-state regression, mocked source network only."""
import json,fcntl,unittest
from unittest.mock import patch
from tests import test_platform as helpers
from library_store import connect
from platform_store import worker_state

class ImportWizardTests(unittest.TestCase):
    setUp=helpers.PlatformTests.setUp
    post=helpers.PlatformTests.post
    worker=helpers.PlatformTests.worker
    drain=helpers.PlatformTests.drain
    def exhaust_fixture_retries(self,worker):
        # Advance only this disposable queue's eligibility instead of sleeping
        # through production retry delays. The normal four-attempt cap remains.
        for _ in range(40):
            with connect(worker.database) as c,c:
                c.execute("UPDATE platform_items SET next_attempt=0 WHERE status='pending'")
            if not worker.step():return
        self.fail('Fixture retry budget exceeded')
    def discover_fixture(self,count=60):
        book=self.post('/api/imports',{'url':'https://public.example/wizard'}).json['book_id']
        worker,_=self.worker()
        with patch('scraper.browse_page',return_value={'title':'Wizard Novel','author':'Fixture Author','links':[{'url':f'https://public.example/wizard/{i}','title':f'Chapter {i+1}'} for i in range(count)],'pages':[]}):self.drain(worker)
        return book,worker

    def test_failed_zero_chapter_import_stays_pending_and_retry_promotes_real_content(self):
        book,worker=self.discover_fixture(3)
        self.assertNotIn(book,{b['id'] for b in self.client.get('/api/books').json['items']})
        job=self.post('/api/books/'+book+'/import/jobs',{'mode':'all'}).json['id']
        with patch('scraper.scrape_chapter',side_effect=ValueError('Fixture source unavailable')):self.exhaust_fixture_retries(worker)
        self.assertNotIn(book,{b['id'] for b in self.client.get('/api/books').json['items']})
        self.assertEqual(self.client.get('/api/books/'+book+'/import/summary').json['jobs']['import']['status'],'failed')
        self.post('/api/jobs/'+job,{'action':'retry'})
        with patch('scraper.scrape_chapter',return_value={'content':'Real retry chapter.'}):self.drain(worker)
        self.assertIn(book,{b['id'] for b in self.client.get('/api/books').json['items']})

    def test_empty_download_is_a_failure_and_partial_real_book_is_preserved(self):
        book,worker=self.discover_fixture(3)
        self.post('/api/books/'+book+'/import/jobs',{'mode':'all'})
        def chapter(url):
            if url.endswith('/0'):return {'content':'Only real chapter.'}
            return {'content':' \n\t '}
        with patch('scraper.scrape_chapter',side_effect=chapter):self.exhaust_fixture_retries(worker)
        detail=self.client.get('/api/books/'+book).json
        self.assertEqual(detail['section_count'],1)
        self.assertIn(book,{b['id'] for b in self.client.get('/api/books').json['items']})
        self.assertEqual(self.client.get('/api/books/'+book+'/import/summary').json['jobs']['import']['status'],'failed')

    def test_first_ten_fifty_custom_and_all_counts(self):
        for mode,payload,total in [('first',{'count':10},10),('first',{'count':50},50),('range',{'first':20,'last':40},21),('all',{},60)]:
            book,_=self.discover_fixture()
            job=self.post('/api/books/'+book+'/import/jobs',{'mode':mode,**payload}).json['id']
            state=self.client.get('/api/jobs/'+job).json
            self.assertEqual(state['progress']['selected'],total);self.assertEqual(state['progress']['pending'],total)
            self.post('/api/jobs/'+job,{'action':'cancel'})
        self.assertEqual(self.post('/api/books/'+book+'/import/jobs',{'mode':'first','count':61}).status_code,400)

    def test_progress_skips_duplicates_and_discovery_summary(self):
        book,worker=self.discover_fixture(12)
        summary=self.client.get('/api/books/'+book+'/import/summary').json
        self.assertEqual(summary['discovered'],12);self.assertTrue(summary['discovery_complete']);self.assertEqual(summary['last_title'],'Chapter 12')
        job=self.post('/api/books/'+book+'/import/jobs',{'mode':'first','count':10}).json['id']
        with patch('scraper.scrape_chapter',return_value={'content':'Fixture paragraphs.','chapter_title':'Downloaded'}):self.drain(worker)
        new=self.post('/api/books/'+book+'/import/jobs',{'mode':'all'}).json['id']
        p=self.client.get('/api/jobs/'+new).json['progress']
        self.assertEqual((p['selected'],p['skipped'],p['pending']),(12,10,2))
        duplicate=self.post('/api/books/'+book+'/import/jobs',{'mode':'first','count':10}).json
        self.assertEqual(duplicate['id'],new);self.assertFalse(duplicate['created'])
        self.post('/api/jobs/'+new,{'action':'pause'});self.assertEqual(self.client.get('/api/jobs/'+new).json['pause_reason'],'user')
        self.post('/api/jobs/'+new,{'action':'resume'})
        with patch('scraper.scrape_chapter',return_value={'content':'More text.'}):self.drain(worker)
        all_done=self.post('/api/books/'+book+'/import/jobs',{'mode':'all'}).json['id']
        state=self.client.get('/api/jobs/'+all_done).json;self.assertEqual(state['status'],'completed');self.assertEqual(state['progress']['percentage'],100)

    def test_source_refresh_keeps_manual_title_and_refreshes_link_titles(self):
        book=self.post('/api/imports',{'url':'https://public.example/manual','title':'My title'}).json['book_id']
        worker,_=self.worker()
        page={'title':'Source title','author':'Source author','description':'Source description','links':[{'url':'https://public.example/manual/1','title':'Actual chapter'}],'pages':[]}
        with patch('scraper.browse_page',return_value=page):self.drain(worker)
        detail=self.client.get('/api/books/'+book).json
        self.assertEqual(detail['title'],'My title');self.assertEqual(detail['author'],'Source author')
        summary=self.client.get('/api/books/'+book+'/import/summary').json
        self.assertEqual(summary['jobs']['discover']['result']['last_page_chapters'],1)

    def test_wizard_template_loads_script_in_head_and_status_in_body(self):
        html=self.client.get('/importer').data.decode()
        head,body=html.split('</head>',1)
        self.assertIn('/static/importer.js',head)
        self.assertIn('<body',body);self.assertIn('dashboardStatus',body)
        self.assertIn('chapterSelection',body);self.assertNotIn('dashboardJobs',body)

    def test_resource_safety_distinct_from_user_pause(self):
        root=self.root/'data';(root/'.platform-heartbeat').touch()
        (root/'.platform-pressure.json').write_text(json.dumps({'reason':'Sustained I/O wait'}))
        with (root/'.platform-worker.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            state=worker_state(root);self.assertTrue(state['worker_active']);self.assertTrue(state['resource_paused']);self.assertFalse(state['can_generate']);self.assertIn('Resource-safety',state['worker_message'])
        self.assertFalse(worker_state(root)['worker_active'])


    def test_historical_catalog_dense_ranges_export_and_stable_content(self):
        book,worker=self.discover_fixture(5)
        job=self.post('/api/books/'+book+'/import/jobs',{'mode':'all'}).json['id']
        with patch('scraper.scrape_chapter',return_value={'content':'Original preserved text.'}):self.drain(worker)
        with connect(worker.database) as conn:
            original=[tuple(r) for r in conn.execute('SELECT id,position,text_content FROM book_sections WHERE book_id=? ORDER BY position',(book,))]
        refresh=self.post('/api/books/'+book+'/import/refresh',{}).json['id']
        page={'links':[{'url':f'https://public.example/wizard/{i}','title':f'Visible {i}'} for i in [4,2]],'pages':[]}
        with patch('scraper.browse_page',return_value=page):self.drain(worker)
        summary=self.client.get('/api/books/'+book+'/import/summary').json
        self.assertEqual((summary['discovered'],summary['historical_retained']),(2,3))
        rows=self.client.get('/api/books/'+book+'/import/chapters').json['items']
        self.assertEqual([r['position'] for r in rows],[0,1]);self.assertTrue(rows[0]['url'].endswith('/4'))
        selected=self.post('/api/books/'+book+'/import/jobs',{'mode':'first','count':1}).json['id']
        self.assertEqual(self.client.get('/api/jobs/'+selected).json['progress']['skipped'],1)
        exported=self.post('/api/books/'+book+'/exports',{'first':1,'last':1,'catalog':True}).json['id']
        self.drain(worker)
        import zipfile
        with connect(worker.database) as conn:
            path=conn.execute('SELECT path FROM epub_exports WHERE id=?',(exported,)).fetchone()[0]
            self.assertEqual(original,[tuple(r) for r in conn.execute('SELECT id,position,text_content FROM book_sections WHERE book_id=? ORDER BY position',(book,))])
        with zipfile.ZipFile(path) as archive:
            self.assertIn('Chapter 5',archive.read('EPUB/nav.xhtml').decode())
            self.assertNotIn('Chapter 3',archive.read('EPUB/nav.xhtml').decode())
            self.assertEqual(len([n for n in archive.namelist() if n.startswith('EPUB/chapter-')]),1)
            self.assertIn('Original preserved text.',archive.read('EPUB/chapter-0.xhtml').decode())
        self.assertEqual(self.post('/api/books/'+book+'/import/jobs',{'mode':'first','count':3}).status_code,400)

    def test_partial_and_failed_discovery_never_replace_completed_catalog(self):
        book,worker=self.discover_fixture(5)
        self.post('/api/books/'+book+'/import/refresh',{})
        page={'links':[{'url':'https://public.example/new','title':'New chapter'}],'pages':['https://public.example/next']}
        with patch('scraper.browse_page',return_value=page):worker.step()
        summary=self.client.get('/api/books/'+book+'/import/summary').json
        self.assertEqual(summary['discovered'],5);self.assertTrue(summary['discovery_complete'])
        job=summary['jobs']['discover']['id']
        self.post('/api/jobs/'+job,{'action':'cancel'})
        self.assertEqual(self.client.get('/api/books/'+book+'/import/summary').json['discovered'],5)

    def test_empty_discovery_reports_error_and_preserves_completed_snapshot(self):
        book,worker=self.discover_fixture(5)
        self.post('/api/books/'+book+'/import/refresh',{})
        with patch('scraper.browse_page',return_value={'links':[],'pages':[]}):self.drain(worker)
        summary=self.client.get('/api/books/'+book+'/import/summary').json
        self.assertEqual((summary['discovered'],summary['historical_retained']),(5,0))
        self.assertTrue(summary['discovery_complete'])
        self.assertIn('No chapter links found',summary['jobs']['discover']['error'])

    def test_multipage_catalog_publishes_only_after_all_pages_complete(self):
        book,worker=self.discover_fixture(5)
        self.post('/api/books/'+book+'/import/refresh',{})
        def page(url):
            if url.endswith('/next'):
                return {'links':[{'url':'https://public.example/b','title':'Second'}],'pages':[]}
            return {'links':[{'url':'https://public.example/a','title':'First'}],'pages':['https://public.example/next']}
        with patch('scraper.browse_page',side_effect=page):
            worker.step()
            self.assertEqual(self.client.get('/api/books/'+book+'/import/summary').json['discovered'],5)
            self.drain(worker)
        rows=self.client.get('/api/books/'+book+'/import/chapters').json['items']
        self.assertEqual([(r['position'],r['title']) for r in rows],[(0,'First'),(1,'Second')])

    def test_completed_catalog_remains_usable_while_refresh_queued(self):
        book,worker=self.discover_fixture(12)
        catalog=self.client.get('/api/books/'+book+'/import/summary').json['catalog_job_id']
        self.post('/api/books/'+book+'/import/refresh',{})
        summary=self.client.get('/api/books/'+book+'/import/summary').json
        self.assertEqual(summary['jobs']['discover']['status'],'queued')
        self.assertTrue(summary['discovery_complete']);self.assertEqual(summary['catalog_job_id'],catalog)
        job=self.post('/api/books/'+book+'/import/jobs',{'mode':'first','count':3}).json['id']
        self.assertEqual(self.client.get('/api/jobs/'+job).json['progress']['selected'],3)
