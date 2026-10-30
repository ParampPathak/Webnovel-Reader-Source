"""Isolated platform integration; Edge/source network calls are injected mocks."""
import asyncio
import hashlib
import io
import json
import socket
import sys
import tempfile
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from audio_providers import EdgeProvider,cache_key,settings,text_chunks,chapter_text_chunks
from epub_library import parse_epub
from library_store import connect
from platform_store import claim,control,finish_item,recover,schedule
from platform_worker import Interrupted,PlatformWorker
from safe_http import public_url
from tests import test_http
RUNTIME_AVAILABLE=test_http.RUNTIME_AVAILABLE


def mock_assemble(parts,destination):
    with Path(destination).open('wb') as output:
        for part in parts: output.write(Path(part).read_bytes())


class MockSpeech:
    def __init__(self): self.calls=[];self.failures=0
    def synthesize(self,text,config,destination):
        self.calls.append((text,dict(config)))
        if self.failures:
            self.failures-=1;raise OSError('Mock service throttled')
        Path(destination).write_bytes(b'MOCK-MP3:'+text.encode())


class ProviderTests(unittest.TestCase):
    def test_ramped_chunks_preserve_every_sentence_and_restart_boundaries(self):
        text='A short sentence with Unicode café. '*400
        plan=[200,400,600]
        chunks=list(chapter_text_chunks(text,200,plan))
        self.assertEqual(''.join(chunks),text.strip())
        for piece,budget in zip(chunks,plan):self.assertLessEqual(len(piece.encode()),budget)
        self.assertEqual(chunks,list(chapter_text_chunks(text,200,plan)))

    def test_chunking_preserves_utf8_text_and_budgets(self):
        text=('Hello & <story>! 世界。\nLongword'+'字'*80+'\n')*30
        chunks=list(text_chunks(text,200))
        self.assertEqual(''.join(chunks),text)
        self.assertTrue(all(len(c.encode())<=200 for c in chunks))
        self.assertGreater(len(chunks),20)
        self.assertRaises(ValueError,lambda:list(text_chunks(text,1)))

    def test_short_opening_segment_is_bounded_and_legacy_boundaries_unchanged(self):
        text='A short sentence with Unicode café. '*200
        short=list(chapter_text_chunks(text,200,[200,400,600]));self.assertLessEqual(len(short[0].encode()),600);self.assertEqual(''.join(short),text.strip())
        self.assertEqual(list(chapter_text_chunks(text,3000)),list(text_chunks(text)))

    def test_variant_key_changes_with_voice_speed_and_content(self):
        a,_=cache_key('book',1,'hello',settings())
        for book,section,text,config in [('other',1,'hello',settings()),('book',2,'hello',settings()),('book',1,'changed',settings()),('book',1,'hello',settings('en-GB-SoniaNeural')),('book',1,'hello',settings(rate=25))]:
            self.assertNotEqual(a,cache_key(book,section,text,config)[0])
        for invalid in [{'voice':'../escape'},{'rate':True},{'rate':101}]: self.assertRaises(ValueError,settings,**invalid)

    def test_network_synthesis_can_be_interrupted_without_publishing_partial_audio(self):
        cancelled=[]
        class Communicate:
            def __init__(self,*args,**kwargs):pass
            async def save(self,path,metadata=None):
                try:await asyncio.sleep(60)
                finally:cancelled.append(True)
        def interrupted():raise Interrupted('new Play request')
        with tempfile.TemporaryDirectory() as root,patch.dict(sys.modules,{'edge_tts':types.SimpleNamespace(Communicate=Communicate)}),patch('audio_providers.configure_edge_endpoint'):
            destination=Path(root)/'audio.part'
            with self.assertRaises(Interrupted):EdgeProvider().synthesize('Hello',settings(),destination,checkpoint=interrupted)
            self.assertFalse(destination.exists());self.assertTrue(cancelled)

    def test_edge_package_contract_using_mock_communicator(self):
        calls=[]
        class Communicate:
            def __init__(self,text,voice,**kw): calls.append((text,voice,kw))
            async def save(self,path,metadata=None): Path(path).write_bytes(b'mocked audio')
        async def list_voices(): return [{'ShortName':'en-US-AriaNeural'}]
        with tempfile.TemporaryDirectory() as root,patch.dict(sys.modules,{'edge_tts':types.SimpleNamespace(Communicate=Communicate,list_voices=list_voices)}),patch('audio_providers.configure_edge_endpoint'):
            provider=EdgeProvider();provider.synthesize('hello',settings(rate=-25),Path(root)/'part.mp3')
            self.assertEqual(calls[0][2]['rate'],'-25%')
            self.assertEqual(provider.voices()[0]['ShortName'],'en-US-AriaNeural')


class UrlTests(unittest.TestCase):
    def test_private_urls_credentials_ports_and_mixed_dns_rejected(self):
        for url in ['file:///etc/passwd','http://127.0.0.1','http://[::1]','http://169.254.169.254','http://10.1.2.3','http://localhost','https://a.internal','https://u:p@example.com','https://example.com:8080','http://224.0.0.1']:
            self.assertRaises(ValueError,public_url,url,False)
        addresses=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('192.168.1.4',443))]
        with patch('safe_http.socket.getaddrinfo',return_value=addresses): self.assertRaises(ValueError,public_url,'https://example.com')

    def test_robots_restrictions_are_respected(self):
        from scraper import _check_robots,_robots_cache
        _robots_cache.clear()
        response=types.SimpleNamespace(text='User-agent: *\nDisallow: /private\nCrawl-delay: 2')
        with patch('safe_http.fetch',return_value=response):
            self.assertRaises(ValueError,_check_robots,'https://public.example/private/chapter')
            self.assertEqual(_check_robots('https://public.example/chapter/1'),2)
        _robots_cache.clear()

    def test_public_socket_is_pinned_and_page_budget_is_enforced(self):
        from safe_http import fetch
        import email.message
        headers=email.message.Message();headers['Content-Type']='text/html; charset=utf-8'
        class Response:
            status=200
            def __init__(self):self.stream=io.BytesIO(b'<html>small</html>');self.headers=headers
            def getheader(self,key):return None
            def getheaders(self):return list(headers.items())
            def read(self,size):return self.stream.read(size)
        class Connection:
            def __init__(self,*a,**k):self.response=Response()
            def request(self,*a,**k):self._create_connection(('public.example',443),25)
            def getresponse(self):return self.response
            def close(self):self.response.stream.close()
        addresses=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]
        with patch('safe_http.socket.getaddrinfo',return_value=addresses),patch('safe_http.http.client.HTTPSConnection',Connection),patch('safe_http.socket.create_connection') as connector:
            self.assertEqual(fetch('https://public.example/').text,'<html>small</html>')
            self.assertEqual(connector.call_args.args[0],('8.8.8.8',443))
            self.assertRaises(ValueError,fetch,'https://public.example/',max_bytes=4)

    def test_pinned_transport_checks_redirect_before_connecting(self):
        from safe_http import fetch
        class Response:
            status=302
            def getheader(self,key): return 'http://127.0.0.1/secret' if key=='Location' else None
        connection=types.SimpleNamespace(request=lambda *a,**k:None,getresponse=lambda:Response(),close=lambda:None)
        addresses=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]
        with patch('safe_http.socket.getaddrinfo',return_value=addresses),patch('safe_http.http.client.HTTPSConnection',return_value=connection) as ctor:
            self.assertRaises(ValueError,fetch,'https://public.example/')
            self.assertEqual(ctor.call_count,1)
            self.assertTrue(callable(connection._create_connection))


@unittest.skipUnless(RUNTIME_AVAILABLE,'Existing Flask dependencies required')
class PlatformTests(unittest.TestCase):
    def setUp(self):
        test_http.HttpTests.setUp(self)
        # Worker behavior tests use synthetic speech/data. Shared-runner I/O
        # pressure is not their subject; ResourceGuard has deterministic tests.
        guard=patch('platform_worker.ResourceGuard',return_value=types.SimpleNamespace(check=lambda:None))
        guard.start();self.addCleanup(guard.stop)
    upload=test_http.HttpTests.upload

    def book(self):
        result=self.upload();self.assertEqual(result.status_code,201)
        identifier=result.json['id']
        sections=self.client.get('/api/books/'+identifier+'/sections').json['items']
        return identifier,sections

    def post(self,url,value): return self.client.post(url,json=value)

    def worker(self):
        provider=MockSpeech();return PlatformWorker(self.root/'data',provider,assembler=mock_assemble,duration_probe=lambda p:10.0),provider

    def drain(self,worker,budget=100):
        for _ in range(budget):
            if not worker.step(): return
        self.fail('Worker did not drain within bounded fixture budget')

    def test_inflight_generation_checkpoint_yields_to_a_new_play_request(self):
        book,sections=self.book();worker,_=self.worker();base='/api/books/'+book
        self.post(base+'/audio/jobs',{'section_id':sections[0]['id']});old=claim(self.db)
        self.post(base+'/audio/jobs',{'section_id':sections[1]['id']})
        self.assertRaises(Interrupted,worker.checkpoint,old)

    def test_latest_play_request_preempts_older_requested_chapter(self):
        book,sections=self.book();worker,provider=self.worker();base='/api/books/'+book
        self.post(base+'/audio/jobs',{'section_id':sections[0]['id']})
        self.post(base+'/audio/jobs',{'section_id':sections[2]['id']})
        worker.step();self.assertIn('Chapter 3',provider.calls[0][0])

    def test_persistent_queue_notifies_local_worker_without_extra_polling(self):
        import socket
        from platform_store import notify_worker
        path=self.root/'data/.platform-wake.sock'
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as receiver:
            receiver.bind(str(path));receiver.settimeout(1)
            notify_worker(self.db);self.assertEqual(receiver.recv(100),b'work')
        path.unlink()
        notify_worker(self.db) # A stopped worker does not reject queued jobs.

    def test_global_preferences_merge_validate_and_persist_across_app_restart(self):
        p='/api/edge/preferences'
        self.assertEqual(self.client.get(p).json['voice'],'en-US-AndrewNeural')
        r=self.client.put(p,json={'voice':'en-US-AndrewNeural','rate':20,'playback_rate':2,'volume':.4});self.assertEqual(r.status_code,200)
        r=self.client.put(p,json={'playback_rate':1.5});self.assertEqual(r.json['volume'],.4);self.assertEqual(r.json['rate'],20)
        book,chapters=self.book()
        job=self.post('/api/books/'+book+'/audio/jobs',{'section_id':chapters[0]['id']}).json['id']
        config=self.client.get('/api/jobs/'+job).json['config'];self.assertEqual(config['rate'],20);self.assertNotIn('volume',config)
        from app import create_app
        restarted=create_app(self.root/'data').test_client();self.assertEqual(restarted.get(p).json['playback_rate'],1.5)
        for value in [True,-1,10]:self.assertEqual(self.client.put(p,json={'playback_rate':value}).status_code,400)

    def test_stale_requested_audio_cannot_starve_import_and_new_play_still_wins(self):
        import time
        book,sections=self.book()
        stale=schedule(self.db,'audio',book,{},[('old',{'section_id':sections[0]['id']})],priority=100)
        with connect(self.db) as conn:
            with conn:conn.execute('UPDATE platform_items SET requested_at=? WHERE job_id=?',(time.time()-121,stale))
        imported=schedule(self.db,'import',book,{},[('url',{'url':'https://example.org/chapter'})])
        self.assertEqual(claim(self.db)['job_id'],imported)
        fresh=schedule(self.db,'audio',book,{},[('new',{'section_id':sections[1]['id']})],priority=100)
        self.assertEqual(claim(self.db)['job_id'],fresh)

    def test_import_is_not_starved_by_existing_low_priority_audio_prefetch(self):
        book,sections=self.book()
        old=schedule(self.db,'audio',book,{},[('prefetch',{'section_id':sections[0]['id']})],priority=10)
        imported=schedule(self.db,'import',book,{},[('download',{})])
        selected=claim(self.db)
        self.assertEqual(selected['job_id'],imported)

    def test_requested_job_priority_beats_promoted_prefetch_item(self):
        book,sections=self.book();worker,provider=self.worker();base='/api/books/'+book
        self.post(base+'/audio/jobs',{'section_id':sections[0]['id'],'prefetch':True})
        self.post(base+'/audio/jobs',{'section_id':sections[0]['id'],'prefetch':True})
        self.post(base+'/audio/jobs',{'section_id':sections[2]['id']})
        worker.step();self.assertIn('Chapter 3',provider.calls[0][0])

    def test_progressive_segments_published_before_chapter_assembly_and_saved_cursor(self):
        book,sections=self.book();sid=sections[0]['id'];base='/api/books/'+book
        with connect(self.db) as conn:
            with conn:conn.execute('UPDATE book_sections SET text_content=? WHERE id=?',('A sentence for speech. '*500,sid))
        worker,provider=self.worker()
        job=self.post(base+'/audio/jobs',{'section_id':sid}).json['id'];worker.step()
        state=self.client.get(base+'/audio?section_id='+str(sid)).json
        self.assertEqual(state['status'],'streaming');self.assertFalse(state['complete']);self.assertEqual(len(state['segments']),1)
        segment=self.client.get(state['segments'][0]['url'],headers={'Range':'bytes=0-7'});self.addCleanup(segment.close)
        self.assertEqual(segment.status_code,206);self.assertEqual(segment.mimetype,'audio/mpeg')
        unavailable=self.client.get('/api/audio/'+state['asset_id']);self.assertEqual(unavailable.status_code,404)
        self.assertEqual(self.client.get('/api/jobs/'+job).json['status'],'running')
        progress={'section_id':sid,'asset_id':state['asset_id'],'seconds':3,'voice':'en-US-AndrewNeural','synthesis_rate':0,'version':0,'segment_index':0,'segment_seconds':3}
        self.assertEqual(self.client.put(base+'/listening',json=progress).status_code,200)
        saved=self.client.get(base+'/listening').json;self.assertEqual(saved['segment_seconds'],3)
        progress.update(version=1,segment_index=99);self.assertEqual(self.client.put(base+'/listening',json=progress).status_code,400)
        self.drain(worker);final=self.client.get(base+'/audio?section_id='+str(sid)).json;self.assertEqual(final['status'],'ready');self.assertTrue(final['complete'])
        calls=len(provider.calls);self.post(base+'/audio/jobs',{'section_id':sid});self.drain(worker);self.assertEqual(len(provider.calls),calls)

    def test_audio_generation_cache_variants_ranges_and_download(self):
        book,sections=self.book();base='/api/books/'+book
        worker,provider=self.worker()
        job=self.post(base+'/audio/jobs',{'first':1,'last':3}).json['id']
        self.drain(worker)
        status=self.client.get('/api/jobs/'+job).json
        self.assertEqual(status['status'],'completed');self.assertEqual(status['counts']['done'],3)
        calls=len(provider.calls)
        audio=self.client.get(base+'/audio?section_id='+str(sections[0]['id'])).json
        self.assertEqual(audio['status'],'ready')
        response=self.client.get(audio['url'],headers={'Range':'bytes=0-7'});self.addCleanup(response.close)
        self.assertEqual(response.status_code,206);self.assertEqual(response.data,b'MOCK-MP3')
        self.post(base+'/audio/jobs',{'first':1,'last':3});self.drain(worker)
        self.assertEqual(len(provider.calls),calls)
        self.post(base+'/audio/jobs',{'section_id':sections[0]['id'],'voice':'en-GB-SoniaNeural','rate':25});self.drain(worker)
        self.assertEqual(len(provider.calls),calls+1)
        with connect(self.db) as conn: self.assertEqual(conn.execute("SELECT COUNT(*) FROM audio_variants WHERE status='ready'").fetchone()[0],4)

    def test_long_audio_resume_pause_cancel_and_priority(self):
        book,sections=self.book();base='/api/books/'+book
        with connect(self.db) as conn:
            with conn:conn.execute('UPDATE book_sections SET text_content=? WHERE id=?',('A long sentence. '*800,sections[0]['id']))
        worker,provider=self.worker()
        identifier=self.post(base+'/audio/jobs',{'first':1,'last':3}).json['id']
        worker.step();self.assertEqual(len(provider.calls),1)
        control(self.db,identifier,'pause');self.assertFalse(worker.step())
        urgent=self.post(base+'/audio/jobs',{'section_id':sections[2]['id'],'voice':'en-GB-SoniaNeural'}).json['id']
        worker.step();self.assertEqual(provider.calls[-1][1]['voice'],'en-GB-SoniaNeural')
        control(self.db,identifier,'resume')
        recover(self.db);worker=PlatformWorker(self.root/'data',provider,assembler=mock_assemble,duration_probe=lambda p:10.0)
        self.drain(worker)
        self.assertEqual(self.client.get('/api/jobs/'+identifier).json['status'],'completed')
        count=sum(1 for text,config in provider.calls if text.startswith('A long sentence.'))
        self.assertEqual(count,len(list(chapter_text_chunks('A long sentence. '*800,200,[200,400,600]))))
        cancelled=self.post(base+'/audio/jobs',{'section_id':sections[0]['id'],'rate':50}).json['id']
        control(self.db,cancelled,'cancel');self.assertFalse(worker.step())
        self.assertEqual(self.client.get('/api/jobs/'+cancelled).json['status'],'cancelled')

    def test_existing_denied_audio_job_returns_short_error_without_service_url(self):
        book,sections=self.book();base='/api/books/'+book
        identifier=self.post(base+'/audio/jobs',{'section_id':sections[0]['id']}).json['id']
        for _ in range(4):
            item=claim(self.db)
            finish_item(self.db,item,RuntimeError("403, message='Invalid response status', url=wss://speech.test/?token=secret"))
            with connect(self.db) as conn:
                with conn:conn.execute('UPDATE platform_items SET next_attempt=0 WHERE job_id=?',(identifier,))
        audio=self.client.get(base+'/audio?section_id='+str(sections[0]['id'])).json
        self.assertEqual(audio['status'],'failed')
        self.assertIn('HTTP 403',audio['error'])
        self.assertNotIn('secret',audio['error']);self.assertNotIn('wss:',audio['error'])

    def test_failed_audio_retry_and_crash_recovery(self):
        book,sections=self.book();worker,provider=self.worker();provider.failures=4
        identifier=self.post('/api/books/'+book+'/audio/jobs',{'section_id':sections[0]['id']}).json['id']
        for _ in range(4):
            worker.step()
            with connect(self.db) as conn:
                with conn:conn.execute('UPDATE platform_items SET next_attempt=0 WHERE job_id=?',(identifier,))
        self.assertEqual(self.client.get('/api/jobs/'+identifier).json['status'],'failed')
        control(self.db,identifier,'retry')
        item=claim(self.db);self.assertEqual(item['job_id'],identifier)
        recover(self.db);self.drain(worker)
        self.assertEqual(self.client.get('/api/jobs/'+identifier).json['status'],'completed')

    def test_mid_generation_pause_preserves_completed_chunks(self):
        book,sections=self.book();identifier=self.post('/api/books/'+book+'/audio/jobs',{'section_id':sections[0]['id']}).json['id']
        worker,provider=self.worker()
        original=provider.synthesize
        def pause(text,config,path): original(text,config,path);control(self.db,identifier,'pause')
        provider.synthesize=pause
        self.assertRaises(Interrupted,worker.step)
        self.assertEqual(self.client.get('/api/jobs/'+identifier).json['status'],'paused')
        provider.synthesize=original;control(self.db,identifier,'resume');self.drain(worker)
        self.assertEqual(len(provider.calls),1)
        self.assertEqual(self.client.get('/api/jobs/'+identifier).json['status'],'completed')

    def test_listening_progress_persistence_conflicts_and_validation(self):
        book,sections=self.book();worker,_=self.worker();base='/api/books/'+book
        self.post(base+'/audio/jobs',{'section_id':sections[0]['id']});self.drain(worker)
        audio=self.client.get(base+'/audio?section_id='+str(sections[0]['id'])).json
        data={'section_id':sections[0]['id'],'asset_id':audio['asset_id'],'seconds':34.5,'playback_rate':1.5,'version':0}
        self.assertEqual(self.client.put(base+'/listening',json=data).status_code,200)
        self.assertEqual(self.client.put(base+'/listening',json=data).status_code,409)
        from app import create_app
        restarted=create_app(self.root/'data').test_client()
        self.assertEqual(restarted.get(base+'/listening').json['seconds'],34.5)
        for update in [{'seconds':float('nan')},{'playback_rate':9},{'asset_id':'not-my-asset'},{'section_id':-1}]:
            self.assertEqual(self.client.put(base+'/listening',json={**data,**update,'version':1}).status_code,400)

    def test_duplicate_requested_job_and_no_auto_generation_on_upload(self):
        book,sections=self.book();self.assertEqual(self.client.get('/api/jobs').json['total'],0)
        data={'section_id':sections[0]['id']}
        first=self.post('/api/books/'+book+'/audio/jobs',data).json
        second=self.post('/api/books/'+book+'/audio/jobs',data).json
        self.assertEqual(first['id'],second['id']);self.assertFalse(second['created'])

    def test_daily_discovery_schedule_persists_and_avoids_duplicate_jobs(self):
        result=self.post('/api/imports',{'url':'https://public.example/book'}).json;book=result['book_id'];base='/api/books/'+book
        worker,_=self.worker()
        with patch('scraper.browse_page',return_value={'links':[{'url':'https://public.example/book/chapter-1','title':'Chapter 1'}],'pages':[]}):self.drain(worker)
        self.assertEqual(self.post(base+'/import/schedule',{'interval':86400}).status_code,200)
        self.assertEqual(self.client.get(base).json['refresh_interval'],86400)
        with connect(self.db) as conn:
            with conn:conn.execute('UPDATE import_sources SET next_refresh=0 WHERE book_id=?',(book,))
        worker.last_refresh_check=0;worker.enqueue_refresh();worker.last_refresh_check=0;worker.enqueue_refresh()
        with connect(self.db) as conn:self.assertEqual(conn.execute("SELECT COUNT(*) FROM platform_jobs WHERE book_id=? AND status='queued'",(book,)).fetchone()[0],1)
        self.assertEqual(self.post(base+'/import/schedule',{'interval':1}).status_code,400)

    def test_copied_epub_original_paths_resolve_without_production_access(self):
        book,sections=self.book();base='/api/books/'+book
        with connect(self.db) as conn:
            with conn:
                row=conn.execute('SELECT file_hash FROM books WHERE id=?',(book,)).fetchone()
                conn.execute('UPDATE books SET original_path=? WHERE id=?',('/data/books/'+row[0]+'.epub',book))
        original=self.client.get(base+'/original');self.addCleanup(original.close)
        self.assertEqual(original.status_code,200)
        cover=self.client.get(base+'/cover');self.addCleanup(cover.close)
        self.assertEqual(cover.status_code,200)
        worker,_=self.worker();identifier=self.post(base+'/exports',{}).json['id'];self.drain(worker)
        self.assertEqual(self.client.get('/api/jobs/'+identifier).json['status'],'completed')

    def test_worker_lock_prevents_duplicate_recovery_and_processing(self):
        import fcntl
        worker,_=self.worker()
        with (self.root/'data/.platform-worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertRaises(RuntimeError,worker.run)

    def test_partial_custom_import_range_keeps_original_ordinals_and_rejects_empty_audio(self):
        result=self.post('/api/imports',{'url':'https://public.example/range'}).json;book=result['book_id'];base='/api/books/'+book
        worker,_=self.worker()
        links=[{'title':f'Chapter {i}','url':f'https://public.example/chapter/{i}'} for i in range(1,4)]
        with patch('scraper.browse_page',return_value={'links':links,'pages':[]}):self.drain(worker)
        self.post(base+'/import/jobs',{'first':2,'last':2})
        with patch('scraper.scrape_chapter',return_value={'content':'Chapter two paragraph','chapter_title':'Chapter 2'}):self.drain(worker)
        detail=self.client.get(base).json
        self.assertEqual(detail['section_count'],1);self.assertEqual(detail['position_count'],3)
        self.assertEqual(self.client.get(base+'/navigation').json['total'],1)
        self.assertEqual(self.client.get(base+'/sections').json['items'][0]['position'],1)
        self.assertEqual(self.post(base+'/audio/jobs',{'first':1,'last':1}).status_code,400)

    def test_source_aliases_share_one_book_and_discovery_job(self):
        first=self.post('/api/imports',{'url':'https://www.freewebnovel.com/novel/example/chapter-99'}).json
        second=self.post('/api/imports',{'url':'http://freewebnovel.com/novel/example?tracking=1'}).json
        self.assertEqual(first['book_id'],second['book_id']);self.assertEqual(first['job_id'],second['job_id'])
        from source_adapters import canonical_source
        self.assertEqual(canonical_source('https://royalroad.com/fiction/123/title/chapter/456/text'),'https://www.royalroad.com/fiction/123')
        self.assertEqual(canonical_source('https://public.example/book?volume=2#toc'),'https://public.example/book?volume=2')

    def test_long_chapter_does_not_rehash_all_cached_audio_after_every_chunk(self):
        from preservation import digest
        book,sections=self.book();text='A longer paragraph. '*900
        with connect(self.db) as conn:
            with conn:conn.execute('UPDATE book_sections SET text_content=? WHERE id=?',(text,sections[0]['id']))
        worker,provider=self.worker()
        self.post('/api/books/'+book+'/audio/jobs',{'section_id':sections[0]['id']})
        with patch('preservation.digest',wraps=digest) as hashes:self.drain(worker)
        chunks=len(list(chapter_text_chunks(text,200,[200,400,600])))
        self.assertEqual(len(provider.calls),chunks)
        self.assertEqual(hashes.call_count,chunks)

    def test_metadata_cover_favorites_and_pages(self):
        book,_=self.book();base='/api/books/'+book
        self.assertEqual(self.client.patch(base+'/metadata',json={'title':'Unicode 世界 & <book>','author':'A Writer','description':'New paragraph\nNext'}).status_code,200)
        self.assertEqual(self.client.put(base+'/favorite',json={'favorite':True}).status_code,200)
        self.assertEqual(self.client.get('/api/books?favorite=1&sort=title').json['total'],1)
        self.assertEqual(self.client.get(base+'/preferences').json['favorite'],1)
        from PIL import Image
        artwork=io.BytesIO();Image.new('RGB',(32,48),'navy').save(artwork,format='PNG');artwork.seek(0)
        response=self.client.post(base+'/cover',data={'file':(artwork,'cover.png')})
        response.request.environ['wsgi.input'].close()
        self.assertEqual(response.status_code,200)
        cover=self.client.get(base+'/cover');self.addCleanup(cover.close);self.assertEqual(cover.mimetype,'image/png')
        for route in ('/','/library','/books/'+book,'/reader/'+book,'/importer','/jobs','/settings'):
            self.assertEqual(self.client.get(route).status_code,200)
        self.assertEqual(self.client.post(base+'/audio/jobs',json={},headers={'Origin':'https://evil.example'}).status_code,403)

    def test_requested_chapter_preempts_existing_whole_book_job(self):
        book,sections=self.book();base='/api/books/'+book
        worker,provider=self.worker()
        queued=self.post(base+'/audio/jobs',{'first':1,'last':3}).json['id']
        requested=self.post(base+'/audio/jobs',{'section_id':sections[2]['id']}).json['id']
        self.assertEqual(queued,requested)
        worker.step()
        self.assertIn('Chapter 3',provider.calls[0][0])

    def test_audio_archive_contains_ordered_tracks_metadata_and_cover(self):
        book,sections=self.book();base='/api/books/'+book;worker,_=self.worker()
        self.post(base+'/audio/jobs',{});self.drain(worker)
        identifier=self.post(base+'/audiobook-exports',{}).json['id'];self.drain(worker)
        job=self.client.get('/api/jobs/'+identifier).json
        self.assertEqual(job['status'],'completed')
        response=self.client.get(job['result']['download_url']);self.addCleanup(response.close)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertIsNone(archive.testzip())
            tracks=sorted(name for name in archive.namelist() if name.endswith('.mp3'))
            self.assertEqual(len(tracks),3)
            self.assertTrue(tracks[0].split('/')[-1].startswith('000001'))
            self.assertTrue(any(name.endswith('metadata.opf') for name in archive.namelist()))
            self.assertTrue(any(name.endswith('cover.png') for name in archive.namelist()))
            self.assertFalse(any('..' in name.split('/') for name in archive.namelist()))

    def test_discovery_adapter_has_no_500_link_truncation(self):
        from scraper import browse_page
        markup='<html><title>Large listing</title><body>'+''.join(f'<a href="/novel/book/chapter-{i}">Chapter {i}</a>' for i in range(3001,0,-1))+'</body></html>'
        response=types.SimpleNamespace(text=markup,headers={'Content-Type':'text/html'},url='https://freewebnovel.com/novel/book')
        with patch('scraper._fetch',return_value=response): result=browse_page(response.url)
        self.assertEqual(len(result['links']),3001)
        self.assertTrue(result['links'][0]['url'].endswith('chapter-1'))
        self.assertTrue(result['links'][-1]['url'].endswith('chapter-3001'))

    def test_legacy_batch_and_follow_are_durable_and_untruncated(self):
        worker,_=self.worker()
        urls=[f'https://public.example/chapter/{i}' for i in range(1,102)]
        batch=self.post('/api/add-batch',{'urls':urls})
        self.assertEqual(batch.status_code,202)
        self.assertEqual(self.client.get('/api/jobs/'+batch.json['job_id']).json['total'],101)
        follow=self.post('/api/add-and-follow',{'url':'https://public.example/other/1','count':1501})
        self.assertEqual(follow.status_code,202);self.assertEqual(follow.json['requested'],1501)
        from app import scrape_chapter
        def fixture(url):
            number=int(url.rsplit('/',1)[1]);prefix=url.rsplit('/',1)[0]
            return {'novel_title':'Fixture','chapter_title':'Chapter '+str(number),'content':'A chapter paragraph. '*10,'next_url':prefix+'/'+str(number+1) if number<3 else None}
        with patch('app.scrape_chapter',side_effect=fixture):
            # Batch job is older; process it first, then follow next links.
            self.drain(worker,110)
        job=self.client.get('/api/jobs/'+follow.json['job_id']).json
        self.assertEqual(job['counts']['done'],3)
        self.assertIn('No next chapter',job['result']['stopped_reason'])
        self.assertEqual(self.client.post('/api/add-batch',json={'urls':urls},headers={'Origin':'https://evil.example'}).status_code,403)

    def test_generated_epub_reopens_unicode_metadata_and_range(self):
        book,sections=self.book();base='/api/books/'+book;worker,_=self.worker()
        self.client.patch(base+'/metadata',json={'title':'A & 世界','author':'Author <One>'})
        identifier=self.post(base+'/exports',{'first':2,'last':3}).json['id'];self.drain(worker)
        job=self.client.get('/api/jobs/'+identifier).json;self.assertEqual(job['status'],'completed')
        response=self.client.get(job['result']['download_url']);self.addCleanup(response.close)
        path=self.root/'roundtrip.epub';path.write_bytes(response.data)
        parsed=parse_epub(path,'reopened')
        self.assertEqual(parsed['metadata']['title'],'A & 世界');self.assertEqual(parsed['metadata']['author'],'Author <One>')
        self.assertEqual(len(parsed['sections']),2)
        self.assertIn('<h1',parsed['sections'][0]['html'])
        self.assertIn('<img',parsed['sections'][0]['html'])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.infolist()[0].filename,'mimetype')
            self.assertEqual(archive.infolist()[0].compress_type,zipfile.ZIP_STORED)
            import xml.etree.ElementTree as ET
            for name in archive.namelist():
                if name.endswith(('.xhtml','.opf','.xml')): ET.fromstring(archive.read(name))

    def test_resumable_3001_discovery_1501_import_and_export(self):
        count=3001;url='https://public.example/novel'
        result=self.post('/api/imports',{'url':url,'title':'Large fixture'}).json;book=result['book_id'];base='/api/books/'+book
        links=[{'title':f'Chapter {i+1}','url':f'https://public.example/chapter/{i+1}'} for i in range(count)]
        worker,_=self.worker()
        with patch('scraper.browse_page',return_value={'links':links,'pages':[]}),patch('scraper.scrape_chapter',side_effect=lambda url:{'chapter_title':'Chapter '+url.rsplit('/',1)[1],'content':'Unicode 世界 & <words>\n'+'A paragraph. '*12}):
            self.drain(worker)
            discovered=self.client.get(base+'/import/chapters?offset=3000&limit=1').json
            self.assertEqual(discovered['total'],3001);self.assertEqual(discovered['items'][0]['position'],3000)
            identifier=self.post(base+'/import/jobs',{'first':1,'last':1501}).json['id']
            for _ in range(5):worker.step()
            item=claim(self.db);self.assertIsNotNone(item);recover(self.db)
            worker=PlatformWorker(self.root/'data',MockSpeech(),assembler=mock_assemble,duration_probe=lambda p:10.0);self.drain(worker,1600)
            self.assertEqual(self.client.get('/api/jobs/'+identifier).json['counts']['done'],1501)
            self.assertEqual(self.client.get(base).json['section_count'],1501)
            repeat=self.post(base+'/import/jobs',{'first':1,'last':1501}).json['id']
            self.assertEqual(self.client.get('/api/jobs/'+repeat).json['status'],'completed')
            self.post(base+'/import/refresh',{});self.drain(worker)
            self.assertEqual(self.client.get(base+'/import/chapters').json['total'],3001)
        identifier=self.post(base+'/exports',{}).json['id'];self.drain(worker)
        response=self.client.get('/api/exports/'+identifier);self.addCleanup(response.close)
        path=self.root/'large-export.epub';path.write_bytes(response.data)
        parsed=parse_epub(path,'large-export',stream=True)
        self.assertEqual(parsed['section_count'],1501)
        self.assertEqual(sum(1 for _ in parsed['sections']),1501)

    def test_import_failure_retry_and_source_url_rejection(self):
        for url in ('http://127.0.0.1/private','file:///etc/passwd'):
            self.assertEqual(self.post('/api/imports',{'url':url}).status_code,400)
        result=self.post('/api/imports',{'url':'https://public.example/book'}).json;book=result['book_id'];base='/api/books/'+book
        worker,_=self.worker()
        with patch('scraper.browse_page',return_value={'links':[{'url':'https://public.example/chapter/1','title':'First'}],'pages':[]}):self.drain(worker)
        identifier=self.post(base+'/import/jobs',{}).json['id']
        with patch('scraper.scrape_chapter',side_effect=OSError('Source unavailable')):
            worker.step()
        self.assertEqual(self.client.get(base+'/import/chapters').json['items'][0]['status'],'failed')
        control(self.db,identifier,'pause');self.assertFalse(worker.step());control(self.db,identifier,'resume')
        with connect(self.db) as conn:
            with conn:conn.execute('UPDATE platform_items SET next_attempt=0')
        with patch('scraper.scrape_chapter',return_value={'content':'paragraph text','chapter_title':'First'}):self.drain(worker)
        self.assertEqual(self.client.get(base).json['section_count'],1)

@unittest.skipUnless(RUNTIME_AVAILABLE,'Existing Flask dependencies required')
class NarrationTests(unittest.TestCase):
    setUp=test_http.HttpTests.setUp
    upload=test_http.HttpTests.upload
    book=PlatformTests.book
    worker=PlatformTests.worker
    post=PlatformTests.post

    def test_cached_chunk_location_uses_persisted_plan_without_synthesis(self):
        book,sections=self.book();sid=sections[0]['id'];text='A clear sentence. Unicode café 世界. '*90
        with connect(self.db) as conn:
            with conn:conn.execute('UPDATE book_sections SET text_content=? WHERE id=?',(text,sid))
        worker,provider=self.worker();base='/api/books/'+book
        self.post(base+'/audio/jobs',{'section_id':sid});worker.step()
        status=self.client.get(base+'/audio?section_id='+str(sid)).json;asset=status['asset_id'];calls=len(provider.calls)
        result=self.client.get('/api/audio/'+asset+'/location/0')
        self.assertEqual(result.status_code,200);self.assertEqual(result.json['precision'],'chunk')
        with connect(self.db) as conn:
            row=conn.execute('SELECT first_chunk_bytes,chunk_plan FROM audio_variants WHERE id=?',(asset,)).fetchone()
        import re
        expected=next(chapter_text_chunks(text,row[0],json.loads(row[1]) if row[1] else None))
        self.assertEqual(result.json['quote'],re.sub(r'\s+',' ',expected).strip());self.assertEqual(result.json['section_id'],sid)
        self.assertEqual(len(provider.calls),calls)
        self.assertEqual(self.client.get('/api/audio/'+asset+'/location/999').status_code,404)
        with connect(self.db) as conn:
            with conn:conn.execute('UPDATE book_sections SET text_content=? WHERE id=?',('Changed text',sid))
        self.assertEqual(self.client.get('/api/audio/'+asset+'/location/0').status_code,409)

    def test_follow_preference_and_stable_chunk_anchor_preserve_reader_version_checks(self):
        book,sections=self.book();base='/api/books/'+book+'/progress'
        payload={'section_id':sections[0]['id'],'fraction':.4,'anchor':'narration:cached:0','preferences':{'followAlong':True},'version':0}
        self.assertEqual(self.client.put(base,json=payload).status_code,200)
        self.assertEqual(self.client.get(base).json['preferences']['followAlong'],True)
        self.assertEqual(self.client.put(base,json=payload).status_code,409)
        payload.update(version=1,preferences={'followAlong':'true'})
        self.assertEqual(self.client.put(base,json=payload).status_code,400)

    def test_canonical_neighbors_ignore_titles_and_missing_chapter_numbers(self):
        book,sections=self.book()
        with connect(self.db) as conn:
            with conn:
                for item,position in zip(sections,[43,45,47]):
                    conn.execute('UPDATE book_sections SET title=?,position=? WHERE id=?',('Same title',position,item['id']))
        for index,item in enumerate(sections):
            result=self.client.get('/api/books/'+book+'/sections/'+str(item['id'])).json
            self.assertEqual(result['next_id'],sections[index+1]['id'] if index<2 else None)

    def test_follow_flags_are_separate_from_rollback_reader_preferences(self):
        from library_store import migrate
        book,sections=self.book();base='/api/books/'+book+'/progress'
        payload={'section_id':sections[0]['id'],'fraction':.4,'anchor':'','preferences':{'theme':'sepia','followAlong':True,'followAlongSuspended':True},'version':0}
        self.assertEqual(self.client.put(base,json=payload).status_code,200)
        with connect(self.db) as conn:
            row=conn.execute('SELECT preferences,version FROM reading_progress WHERE book_id=?',(book,)).fetchone()
            self.assertEqual(json.loads(row['preferences']),{'theme':'sepia'})
            self.assertEqual(tuple(conn.execute('SELECT enabled,suspended FROM reading_follow WHERE book_id=?',(book,)).fetchone()),(1,1))
            with conn:
                conn.execute('DELETE FROM reading_follow WHERE book_id=?',(book,))
                conn.execute('UPDATE reading_progress SET preferences=? WHERE book_id=?',(json.dumps(payload['preferences']),book))
        migrate(self.db);migrate(self.db)
        result=self.client.get(base).json
        self.assertEqual(result['version'],1);self.assertTrue(result['preferences']['followAlongSuspended'])
        with connect(self.db) as conn:
            self.assertEqual(json.loads(conn.execute('SELECT preferences FROM reading_progress WHERE book_id=?',(book,)).fetchone()[0]),{'theme':'sepia'})
            with conn:
                conn.execute("INSERT INTO books(id,title,source_kind) VALUES('empty-book','Empty','epub')")
                conn.execute("INSERT INTO reading_follow(book_id,enabled) VALUES('empty-book',1)")
                conn.execute("DELETE FROM books WHERE id='empty-book'")
            self.assertIsNone(conn.execute("SELECT * FROM reading_follow WHERE book_id='empty-book'").fetchone())
