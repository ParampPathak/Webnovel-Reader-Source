"""Single conservative background worker for Edge, imports and EPUB creation."""
from collections import OrderedDict
import hashlib
import html
import json
import os
import signal
import threading
import time
import uuid
from pathlib import Path

from audio_providers import EdgeProvider,assemble_mp3,cache_key,text_chunks,segment_duration,chapter_text_chunks
from library_store import connect
from platform_store import claim,finish_item,recover,PLAY_PRIORITY_TTL
from resource_guard import ResourceGuard,ResourcePressure


class Interrupted(RuntimeError):
    pass


class PlatformWorker:
    def __init__(self,data_dir,provider=None,stop=None,assembler=None,duration_probe=None):
        self.root=Path(data_dir)
        self.database=self.root/'webnovel.db'
        self.provider=provider or EdgeProvider()
        self.assembler=assembler or assemble_mp3
        self.duration_probe=duration_probe or segment_duration
        self.stop=stop or threading.Event()
        self.guard=ResourceGuard()
        self.last_refresh_check=0
        self.verified_chunks=OrderedDict()

    def resource_check(self):
        # External monitoring pauses generation; it must not permanently stop
        # a restart-managed worker. A stale monitor cannot block jobs forever.
        try:
            pressure=json.loads((self.root/'.platform-external-pressure.json').read_text())
            if time.time()-float(pressure['at'])<90:
                raise ResourcePressure(pressure.get('reason','External resource-safety delay.'))
        except (OSError,ValueError,KeyError,TypeError):
            pass
        self.guard.check()

    def checkpoint(self,item):
        if self.stop.is_set():
            raise Interrupted('Worker stopping.')
        self.resource_check()
        import shutil
        if shutil.disk_usage(self.root).free<256*1024*1024:
            raise ResourcePressure('Less than 256 MiB free storage: background work suspended.')
        with connect(self.database) as conn:
            state=conn.execute('SELECT status FROM platform_jobs WHERE id=?',(item['job_id'],)).fetchone()[0]
            if item['kind']=='audio' and conn.execute("SELECT 1 FROM platform_items i JOIN platform_jobs j ON j.id=i.job_id WHERE j.kind='audio' AND j.status IN ('queued','running') AND i.status='pending' AND i.next_attempt<=? AND MAX(i.priority,j.priority)>=100 AND i.requested_at>=? AND i.requested_at>(SELECT requested_at FROM platform_items WHERE id=?) LIMIT 1",(time.time(),time.time()-PLAY_PRIORITY_TTL,item['id'])).fetchone():
                raise Interrupted('Synthesis yielded to a newer Play request; completed segments remain cached.')
            if item['kind'] in {'export','audiobook-export'} and conn.execute("SELECT 1 FROM platform_items i JOIN platform_jobs j ON j.id=i.job_id WHERE j.kind='audio' AND j.status IN ('queued','running') AND i.status='pending' AND i.next_attempt<=? AND MAX(i.priority,j.priority)>=100 AND i.requested_at>=? LIMIT 1",(time.time(),time.time()-PLAY_PRIORITY_TTL)).fetchone():
                raise Interrupted('Export yielded to requested playback; it remains queued.')

        if state not in {'queued','running'}:
            raise Interrupted('Job paused or cancelled.')

    def audio(self,item):
        self.checkpoint(item)
        with connect(self.database) as conn:
            section=conn.execute('SELECT id,text_content FROM book_sections WHERE id=? AND book_id=?',
                                 (item['payload']['section_id'],item['book_id'])).fetchone()
        if not section or not section['text_content'].strip():
            raise ValueError('Chapter has no speakable text.')
        config=item['config'];key,content_hash=cache_key(item['book_id'],section['id'],section['text_content'],config)
        directory=self.root/'audio'/'edge'/item['book_id']/key
        directory.mkdir(parents=True,exist_ok=True)
        final=directory/'chapter.mp3'
        with connect(self.database) as conn:
            asset=conn.execute('SELECT status,path FROM audio_variants WHERE id=?',(key,)).fetchone()
            if asset and asset['status']=='ready' and final.is_file() and final.stat().st_size:
                return True
            with conn:
                conn.execute('''INSERT OR IGNORE INTO audio_variants(id,book_id,section_id,provider,voice,settings,content_hash,first_chunk_bytes,chunk_plan)
                                VALUES(?,?,?,'edge',?,?,?,200,?)''',(key,item['book_id'],section['id'],config['voice'],json.dumps(config,sort_keys=True),content_hash,json.dumps([200,400,600])))
            plan=conn.execute('SELECT first_chunk_bytes,chunk_plan FROM audio_variants WHERE id=?',(key,)).fetchone();first_bytes=plan[0];chunk_plan=json.loads(plan[1]) if plan[1] else None
            known={r['position']:dict(r) for r in conn.execute('SELECT * FROM audio_chunks WHERE asset_id=?',(key,))}
        verified=self.verified_chunks.setdefault(key,{})
        self.verified_chunks.move_to_end(key)
        while len(self.verified_chunks)>3: self.verified_chunks.popitem(last=False)
        total=0
        missing=None
        for number,text in enumerate(chapter_text_chunks(section['text_content'],first_bytes,chunk_plan)):
            self.guard.check()
            total=number+1
            previous=known.get(number)
            if previous and Path(previous['path']).is_file():
                if not previous.get('duration'):
                    duration=self.duration_probe(previous['path'])
                    with connect(self.database) as conn:
                        with conn: conn.execute('UPDATE audio_chunks SET duration=? WHERE asset_id=? AND position=?',(duration,key,number))

                # Rehash once after restart or file changes, not the whole
                # growing chapter on every chunk. Bound verification I/O.
                from preservation import digest
                stat=Path(previous['path']).stat()
                signature=(previous['checksum'],stat.st_size,stat.st_mtime_ns)
                if verified.get(number)==signature:
                    continue
                if digest(previous['path'],check=self.guard.check,bytes_per_second=4*1024*1024)==previous['checksum']:
                    verified[number]=signature
                    continue
            missing=(number,text)
            break
        if missing:
            number,text=missing
            part=directory/f'{number:06}.mp3';temporary=directory/f'{number:06}.part'
            started=time.monotonic()
            if getattr(self.provider,'supports_checkpoint',False):
                def synthesis_checkpoint():
                    (self.root/'.platform-heartbeat').touch()
                    self.checkpoint(item)
                self.provider.synthesize(text,config,temporary,checkpoint=synthesis_checkpoint)
            else:
                self.provider.synthesize(text,config,temporary)
            duration=self.duration_probe(temporary)
            os.replace(temporary,part)
            from preservation import digest
            checksum=digest(part,bytes_per_second=4*1024*1024)
            # Optional cues are tied to the exact published recording. Missing
            # legacy cues never trigger regeneration of an existing cache entry.
            from narration_timing import publish
            events=Path(str(temporary)+'.events')
            try:
                publish(part,text,events)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                print('Optional narration cues unavailable: '+type(exc).__name__,flush=True)
            finally:
                events.unlink(missing_ok=True)
            with connect(self.database) as conn:
                with conn:
                    conn.execute('INSERT OR REPLACE INTO audio_chunks(asset_id,position,path,checksum,duration) VALUES(?,?,?,?,?)',
                                 (key,number,str(part),checksum,duration))
            print(json.dumps({'event':'audio_segment_published','asset':key,'segment':number,'duration':duration,'generation_seconds':round(time.monotonic()-started,3),'at':time.time()}),flush=True)
            stat=part.stat();verified[number]=(checksum,stat.st_size,stat.st_mtime_ns)
            self.checkpoint(item)
            # Yield after every chunk so urgent requested chapters can preempt a
            # whole-book conversion without losing already generated chunks.
            return False
        self.checkpoint(item)
        temporary=directory/'chapter.part'
        self.assembler((directory/f'{number:06}.mp3' for number in range(total)),temporary)
        self.checkpoint(item)
        os.replace(temporary,final)
        with connect(self.database) as conn:
            with conn:
                conn.execute("UPDATE audio_variants SET status='ready',path=?,chunk_count=? WHERE id=?",(str(final),total,key))
        return True

    def discover(self,item):
        from discovery import discover_page
        from safe_http import public_url
        from source_adapters import canonical_source
        def verify_source(conn):
            saved=conn.execute('SELECT url FROM import_sources WHERE book_id=?',(item['book_id'],)).fetchone()
            first=conn.execute('SELECT payload FROM platform_items WHERE job_id=? ORDER BY position,id LIMIT 1',(item['job_id'],)).fetchone()
            original=json.loads(first[0])['url'] if first else item['payload']['url']
            if not saved or canonical_source(saved[0])!=canonical_source(original):
                raise ValueError('The source changed while this discovery was queued. Retry using the saved source.')
        self.checkpoint(item)
        with connect(self.database) as conn:
            verify_source(conn)
        page=discover_page(item['payload']['url'])
        if not page['links'] and not page.get('pages'):
            with connect(self.database) as conn:
                found=conn.execute('SELECT 1 FROM discovery_links WHERE job_id=? LIMIT 1',(item['job_id'],)).fetchone()
            if not found:
                raise ValueError('No chapter links found at '+page.get('url',item['payload']['url'])+'. The source may have changed its chapter list, removed public chapters, or returned an access restriction. Previous completed catalog remains available; inspect the source or retry discovery.')
        self.checkpoint(item)
        with connect(self.database) as conn:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                verify_source(conn)
                current=conn.execute('SELECT title,author,description,cover_path FROM books WHERE id=?',(item['book_id'],)).fetchone()
                from urllib.parse import urlsplit
                protected={r[0] for r in conn.execute('SELECT field FROM import_metadata_overrides WHERE book_id=?',(item['book_id'],))}
                for field in ('title','author','description'):
                    value=(page.get(field) or '').strip()
                    old=(current[field] or '').strip()
                    missing=not old or (field=='title' and old==urlsplit(item['payload']['url']).hostname) or (field=='author' and old.lower() in {'unknown author','author unavailable','unknown'})
                    if field not in protected and missing and value and value.lower() not in {'unknown','unknown author','author unavailable'}:
                        conn.execute('UPDATE books SET '+field+'=? WHERE id=?',(value,item['book_id']))
                position=conn.execute('SELECT COALESCE(MAX(position),-1)+1 FROM import_chapters WHERE book_id=?',(item['book_id'],)).fetchone()[0]
                conn.execute('UPDATE platform_jobs SET result=? WHERE id=?',(json.dumps({'metadata_ready':True,'last_page_chapters':len(page['links']),'last_page_url':page.get('url',item['payload']['url'])}),item['job_id']))
                # Publish membership only when the complete discovery job succeeds.
                # Failed/paused refreshes retain the previous public catalog.
                conn.execute('INSERT OR IGNORE INTO discovery_catalogs(job_id,book_id) VALUES(?,?)',(item['job_id'],item['book_id']))
                visible_position=conn.execute('SELECT COALESCE(MAX(position),-1)+1 FROM discovery_links WHERE job_id=?',(item['job_id'],)).fetchone()[0]
                for link in page['links']:
                    public_url(link['url'],resolve=False)
                    from source_adapters import retained_chapter_url
                    link={**link,'url':retained_chapter_url(conn,item['book_id'],link['url'])}
                    conn.execute('UPDATE import_chapters SET title=? WHERE book_id=? AND url=?',(link['title'],item['book_id'],link['url']))
                    result=conn.execute('INSERT OR IGNORE INTO import_chapters(book_id,url,position,title) VALUES(?,?,?,?)',
                                        (item['book_id'],link['url'],position,link['title']))
                    if result.rowcount: position+=1
                    membership=conn.execute('INSERT OR IGNORE INTO discovery_links(job_id,url,position) VALUES(?,?,?)',(item['job_id'],link['url'],visible_position))
                    if membership.rowcount: visible_position+=1
                for url in page.get('pages',[]):
                    public_url(url,resolve=False)
                    conn.execute('INSERT OR IGNORE INTO platform_items(job_id,item_key,position,payload) VALUES(?,?,?,?)',
                                 (item['job_id'],url,position,json.dumps({'url':url})))
                conn.execute('UPDATE import_sources SET refreshed_at=CURRENT_TIMESTAMP WHERE book_id=?',(item['book_id'],))
        if page.get('cover_url') and not current['cover_path']:
            self.checkpoint(item)
            try:
                from cover_art import fetch_cover
                fetch_cover(self.root,item['book_id'],page['cover_url'])
            except Exception as exc:
                print('Cover retrieval skipped: '+str(exc)[:200],flush=True)
        return True

    def cover(self,item):
        from cover_art import refresh_cover
        self.checkpoint(item)
        refresh_cover(self.root,item['book_id'],bool(item['config'].get('force')))
        self.checkpoint(item)
        return True

    def delete(self,item):
        from book_management import purge_step,book_lock
        with book_lock(self.root,item['book_id']):
            return purge_step(self,item)

    def import_chapter(self,item):
        from scraper import scrape_chapter
        self.checkpoint(item)
        url=item['payload']['url']
        with connect(self.database) as conn:
            chapter=conn.execute('SELECT * FROM import_chapters WHERE book_id=? AND url=?',(item['book_id'],url)).fetchone()
        if not chapter: raise ValueError('Import chapter no longer exists.')
        if chapter['section_id']: return True
        content=scrape_chapter(url)
        self.checkpoint(item)
        text=content['content'];title=content.get('chapter_title') or chapter['title']
        if not isinstance(text,str) or not text.strip():
            raise ValueError('The source returned no chapter text. Retry after checking public availability.')
        body='<h1>'+html.escape(title)+'</h1>'+''.join('<p>'+html.escape(line)+'</p>' for line in text.splitlines() if line.strip())
        with connect(self.database) as conn:
            with conn:
                # Stable discovered position survives retries and partial imports.
                conn.execute('INSERT OR IGNORE INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES(?,?,?,?,?,?)',
                             (item['book_id'],chapter['position'],title,url,body,text))
                section=conn.execute('SELECT id FROM book_sections WHERE book_id=? AND href=?',(item['book_id'],url)).fetchone()
                conn.execute("UPDATE import_chapters SET section_id=?,status='completed',error='' WHERE book_id=? AND url=?",
                             (section[0],item['book_id'],url))
        return True

    def export(self,item):
        from epub_creator import export_epub
        self.checkpoint(item)
        identifier=item['job_id'];directory=self.root/'exports';directory.mkdir(exist_ok=True)
        destination=directory/(identifier+'.epub');temporary=directory/(identifier+'.part')
        count=export_epub(self.database,item['book_id'],temporary,item['config']['first'],item['config']['last'],lambda:self.checkpoint(item),catalog_job=item['config'].get('catalog_job'))
        self.checkpoint(item);os.replace(temporary,destination)
        with connect(self.database) as conn:
            with conn:
                conn.execute('INSERT OR IGNORE INTO epub_exports(id,book_id,path,first_position,last_position) VALUES(?,?,?,?,?)',
                             (identifier,item['book_id'],str(destination),item['config']['first'],item['config']['last']))
                conn.execute('UPDATE platform_jobs SET result=? WHERE id=?',
                             (json.dumps({'export_id':identifier,'sections':count,'download_url':'/api/exports/'+identifier}),identifier))
        # The source book is already in the unified library and reader; an export
        # adds a downloadable revision rather than a duplicate library book.
        return True

    def audiobook_export(self,item):
        from audiobook_export import export_audiobook
        self.checkpoint(item)
        directory=self.root/'exports';directory.mkdir(exist_ok=True)
        temporary=directory/(item['job_id']+'.zip.part');final=directory/(item['job_id']+'.zip')
        count=export_audiobook(self.database,item['book_id'],temporary,item['config']['audio'],item['config']['first'],item['config']['last'],lambda:self.checkpoint(item))
        self.checkpoint(item);os.replace(temporary,final)
        with connect(self.database) as conn:
            with conn:
                conn.execute('INSERT OR IGNORE INTO audiobook_exports(id,book_id,path) VALUES(?,?,?)',(item['job_id'],item['book_id'],str(final)))
                conn.execute('UPDATE platform_jobs SET result=? WHERE id=?',(json.dumps({'chapters':count,'download_url':'/api/audiobook-exports/'+item['job_id']}),item['job_id']))
        return True

    def legacy_chapter(self,item):
        from flask import Flask
        from app import _insert_chapter
        self.checkpoint(item)
        application=Flask('legacy-worker-bridge')
        application.config['DATA_DIR']=str(self.root.resolve())
        with application.app_context(),connect(self.database) as conn:
            result=_insert_chapter(conn,item['payload']['url'],refresh_duplicate_next=False)
        with connect(self.database) as conn:
            with conn:
                section=conn.execute('SELECT book_id FROM book_sections WHERE legacy_chapter_id=?',(result['id'],)).fetchone()
                if section: conn.execute('UPDATE platform_jobs SET book_id=? WHERE id=?',(section[0],item['job_id']))
                if item['kind']=='legacy-follow':
                    count=conn.execute('SELECT COUNT(*) FROM platform_items WHERE job_id=?',(item['job_id'],)).fetchone()[0]
                    target=result.get('next_url')
                    if target and count<item['config']['count']:
                        conn.execute('INSERT OR IGNORE INTO platform_items(job_id,item_key,position,payload) VALUES(?,?,?,?)',
                                     (item['job_id'],target,count,json.dumps({'url':target})))
                    if not target and count<item['config']['count']:
                        conn.execute('UPDATE platform_jobs SET result=? WHERE id=?',
                                     (json.dumps({'stopped_reason':'No next chapter was exposed by the source.','chapters_found':count}),item['job_id']))
        return True

    def enqueue_refresh(self):
        if time.time()-self.last_refresh_check<30: return
        self.last_refresh_check=time.time()
        with connect(self.database) as conn:
            conn.execute('BEGIN IMMEDIATE')
            source=conn.execute("SELECT * FROM import_sources s WHERE refresh_interval>0 AND next_refresh<=? AND NOT EXISTS(SELECT 1 FROM platform_jobs j WHERE j.book_id=s.book_id AND j.kind='discover' AND j.status IN ('queued','running','paused')) ORDER BY next_refresh LIMIT 1",(time.time(),)).fetchone()
            if source:
                identifier=str(uuid.uuid4())
                conn.execute("INSERT INTO platform_jobs(id,kind,book_id,config) VALUES(?,'discover',?,'{}')",(identifier,source['book_id']))
                conn.execute('INSERT INTO platform_items(job_id,item_key,position,payload) VALUES(?,?,0,?)',(identifier,source['url'],json.dumps({'url':source['url']})))
                conn.execute('UPDATE import_sources SET next_refresh=? WHERE book_id=?',(time.time()+source['refresh_interval'],source['book_id']))
            conn.commit()

    def step(self):
        self.resource_check()
        self.enqueue_refresh()
        item=claim(self.database)
        if not item: return False
        try:
            done={'delete':self.delete,'cover':self.cover,'audio':self.audio,'discover':self.discover,'import':self.import_chapter,'export':self.export,'audiobook-export':self.audiobook_export,'legacy-follow':self.legacy_chapter,'legacy-batch':self.legacy_chapter}[item['kind']](item)
            if done:
                finish_item(self.database,item)
            else:
                with connect(self.database) as conn:
                    with conn: conn.execute("UPDATE platform_items SET status='pending' WHERE id=?",(item['id'],))
        except (Interrupted,ResourcePressure):
            with connect(self.database) as conn:
                with conn: conn.execute("UPDATE platform_items SET status='pending' WHERE id=?",(item['id'],))
            raise
        except Exception as exc:
            finish_item(self.database,item,exc)
            if item['kind']=='import':
                with connect(self.database) as conn:
                    with conn:
                        conn.execute("UPDATE import_chapters SET status='failed',error=? WHERE book_id=? AND url=?",(str(exc)[:1000],item['book_id'],item['payload']['url']))
        return True

    def run(self):
        import fcntl
        with (self.root/'.platform-worker.lock').open('a') as lock:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('Another platform worker already owns this storage.')
            import socket,select,stat
            wake_path=self.root/'.platform-wake.sock'
            if wake_path.exists():
                if not stat.S_ISSOCK(wake_path.stat().st_mode):raise RuntimeError('Worker wake path contains an unexpected file.')
                wake_path.unlink()
            with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as wake:
                wake.bind(str(wake_path));wake.setblocking(False)
                try:
                    recover(self.database)
                    while not self.stop.is_set():
                        (self.root/'.platform-heartbeat').touch()
                        try:
                            worked=self.step()
                            (self.root/'.platform-pressure.json').unlink(missing_ok=True)
                            if select.select([wake],[],[],.5 if worked else 2)[0]:wake.recv(4096)
                        except Interrupted:
                            self.stop.wait(.5)
                        except ResourcePressure as exc:
                            (self.root/'.platform-pressure.json').write_text(json.dumps({'reason':str(exc),'at':time.time()}))
                            print(str(exc),flush=True)
                            self.stop.wait(30)
                        except Exception as exc:
                            # Infrastructure/claim failures do not trigger a container
                            # restart loop or repeated startup snapshots.
                            print('Platform worker will retry after storage error: '+str(exc)[:500],flush=True)
                            self.stop.wait(10)
                finally:
                    wake_path.unlink(missing_ok=True)



if __name__=='__main__':
    from app import create_app
    application=create_app()
    stop=threading.Event()
    for signum in (signal.SIGTERM,signal.SIGINT): signal.signal(signum,lambda *_:stop.set())
    PlatformWorker(application.config['DATA_DIR'],stop=stop).run()
