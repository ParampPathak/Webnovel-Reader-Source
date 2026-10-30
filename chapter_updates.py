"""Opt-in, durable, sequential new-release imports. No audio or progress writes."""
import html
import json
import logging
import os
import re
import threading
import time
import ssl
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from library_store import connect
from source_adapters import canonical_source

LOG = logging.getLogger(__name__)
INTERVAL = 86400
MAX_PAGES = 100
MAX_LINKS = 10000
MAX_NEW = 100
MAX_CHECK_SECONDS = 900


class SourceConflict(ValueError):
    """A stale editor must reload the authoritative configuration before saving."""


def source_error(error):
    message=str(error).lower()
    if isinstance(error,TimeoutError) or 'timed out' in message:
        reason='Source request timed out. Check connectivity or retry later.'
    elif isinstance(error,ssl.SSLError):
        reason='The source TLS certificate could not be verified. Check the public site; HTTPS verification remains enabled.'
    elif 'resolve' in message or 'dns' in message:
        reason='Source hostname could not be resolved. Check the URL and DNS, then retry.'
    elif 'robots' in message or 'protection' in message or re.search(r'http (401|403|429|503)',message):
        reason='Source access is blocked or rate limited. Check public availability or choose a supported permitted source.'
    elif 'http 404' in message or 'http 410' in message:
        reason='The new source returned HTTP 404/410. Check its public book URL; the old source is not required.'
    elif 'no public chapter links' in message:
        reason='The new source is reachable but exposes no public chapter links. Its index may be empty, stubbed, or changed.'
    elif 'redirected to a different' in message or 'pagination left' in message:
        reason='The new source redirected or paginated to a different book. Review its canonical public book URL.'
    elif isinstance(error,ValueError):
        reason='Source catalog is empty, incomplete, or its chapter identities changed. Check the saved source and retry.'
    else:reason='Source is temporarily unavailable. Check connectivity and retry later.'
    return reason+' Existing chapters and saved settings are preserved.'


def identity(url):
    if not isinstance(url,str):
        raise ValueError('Paste a public book URL.')
    query=parse_qs(urlsplit(url).query,keep_blank_values=True)
    if query and ((urlsplit(url).hostname or '').removeprefix('www.')!='royalroad.com' or set(query)-{'reviews','review','comments','page'}):
        raise ValueError('Use a public book URL without private or unsupported query parameters.')
    url = canonical_source(url)
    p = urlsplit(url)
    if p.query or p.username or p.password:
        raise ValueError('Use a public book URL without query parameters or credentials.')
    if p.hostname == 'freewebnovel.com' and re.fullmatch(r'/novel/[^/]+', p.path):
        return url, 'freewebnovel', p.path.split('/')[-1]
    if p.hostname == 'www.royalroad.com' and re.fullmatch(r'/fiction/\d+', p.path):
        return url, 'royalroad', p.path.split('/')[-1]
    raise ValueError('Automatic updates support public FreeWebNovel and Royal Road book catalogs.')


def chapter_id(url, provider, key):
    p = urlsplit(url)
    host = (p.hostname or '').removeprefix('www.')
    if p.scheme not in ('https', 'http') or p.query or p.fragment or p.username or p.password:
        raise ValueError('Ambiguous chapter URL; source needs attention.')
    pattern = (r'/novel/' + re.escape(key) + r'/chapter-(\d+)/?') if provider == 'freewebnovel' else (r'/fiction/' + re.escape(key) + r'/[^/]+/chapter/(\d+)(?:/[^/]+)?/?')
    if host != ('freewebnovel.com' if provider == 'freewebnovel' else 'royalroad.com'):
        raise ValueError('Chapter belongs to a different source.')
    match = re.fullmatch(pattern, p.path)
    if not match:
        raise ValueError('Chapter identity is not supported.')
    return match.group(1)


def migrate(path):
    with connect(path) as c, c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('''CREATE TABLE IF NOT EXISTS chapter_updates (
            book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
            source_url TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT NOT NULL,
            source_title TEXT DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,
            interval INTEGER NOT NULL DEFAULT 86400, next_check REAL DEFAULT 0,
            checkpoint TEXT DEFAULT '', last_imported TEXT DEFAULT '',
            last_checked REAL DEFAULT 0, last_success REAL DEFAULT 0,
            status TEXT DEFAULT 'idle', message TEXT DEFAULT '', new_count INTEGER DEFAULT 0,
            available INTEGER, failures INTEGER DEFAULT 0, requested INTEGER DEFAULT 0,
            state TEXT DEFAULT '{}')''')
        c.execute('''CREATE TABLE IF NOT EXISTS chapter_update_items (
            book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            source_id TEXT NOT NULL,url TEXT NOT NULL,title TEXT NOT NULL,ordinal INTEGER NOT NULL,
            done INTEGER DEFAULT 0,PRIMARY KEY(book_id,source_id))''')
        columns={r['name'] for r in c.execute('PRAGMA table_info(chapter_updates)')}
        for name,definition in [('run_started_at','REAL NOT NULL DEFAULT 0'),('discovered_count','INTEGER NOT NULL DEFAULT 0')]:
            if name not in columns:c.execute('ALTER TABLE chapter_updates ADD COLUMN '+name+' '+definition)
        c.execute('CREATE TABLE IF NOT EXISTS chapter_update_events(id INTEGER PRIMARY KEY,book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,config_revision INTEGER NOT NULL,at REAL NOT NULL,event TEXT NOT NULL,details TEXT NOT NULL)')
        if 'config_revision' not in {r['name'] for r in c.execute('PRAGMA table_info(chapter_updates)')}:
            c.execute('ALTER TABLE chapter_updates ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 0')
        c.execute('''CREATE TABLE IF NOT EXISTS source_replacement_previews (
            id TEXT PRIMARY KEY,book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,source_url TEXT NOT NULL,created_at REAL NOT NULL,evidence TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS source_chapter_matches (
            book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            source_url TEXT NOT NULL,source_chapter_id TEXT NOT NULL,url TEXT NOT NULL,
            section_id INTEGER NOT NULL REFERENCES book_sections(id) ON DELETE CASCADE,
            PRIMARY KEY(book_id,source_url,source_chapter_id))''')


def audit(c,row,event,at,**details):
    c.execute('INSERT INTO chapter_update_events(book_id,config_revision,at,event,details) VALUES(?,?,?,?,?)',
              (row['book_id'],row['config_revision'],at,event,json.dumps(details,sort_keys=True)))


def ensure(c, book_id):
    book = c.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
    if not book:
        raise ValueError('Book not found.')
    row = c.execute('SELECT * FROM chapter_updates WHERE book_id=?', (book_id,)).fetchone()
    if row:
        return dict(row)
    source = c.execute('SELECT url FROM import_sources WHERE book_id=?', (book_id,)).fetchone()
    if not source and book['source_kind']=='webnovel':
        # Legacy imports retain original URLs in chapters, not section hrefs.
        origins=set()
        for original in c.execute('SELECT ch.source_url FROM chapters ch JOIN book_sections s ON s.legacy_chapter_id=ch.id WHERE s.book_id=?',(book_id,)):
            try:
                url,provider,key=identity(original[0]);chapter_id(original[0],provider,key);origins.add(url)
            except ValueError:
                pass
        if len(origins)==1:
            url=origins.pop()
            owner=c.execute('SELECT book_id FROM import_sources WHERE url=?',(url,)).fetchone()
            if not owner:
                c.execute('INSERT INTO import_sources(book_id,url) VALUES(?,?)',(book_id,url))
                source={'url':url}
    if not source or book['source_kind'] != 'webnovel':
        return {'book_id': book_id, 'eligible': False, 'enabled': False,
                'message': 'Assign a supported source with matching chapter identities. EPUB files require a verified online chapter source.'}
    try:
        url, provider, key = identity(source['url'])
    except ValueError as e:
        return {'book_id': book_id, 'eligible': False, 'enabled': False, 'message': str(e)}
    tail = c.execute('SELECT url FROM import_current WHERE book_id=? ORDER BY position DESC LIMIT 1', (book_id,)).fetchone()
    if c.execute("SELECT 1 FROM platform_jobs WHERE book_id=? AND kind='discover' AND status IN ('queued','running','paused')",(book_id,)).fetchone():
        tail=None
    checkpoint = chapter_id(tail[0], provider, key) if tail else ''
    c.execute('INSERT INTO chapter_updates(book_id,source_url,provider,source_id,checkpoint,source_title) VALUES(?,?,?,?,?,?)',
              (book_id, url, provider, key, checkpoint, book['title']))
    return dict(c.execute('SELECT * FROM chapter_updates WHERE book_id=?', (book_id,)).fetchone())


def status(path, book_id):
    with connect(path) as c, c:
        c.execute('BEGIN IMMEDIATE')
        row = ensure(c, book_id)
        if row.get('status') == 'checking':
            state=json.loads(row.get('state') or '{}')
            if time.time()-state.get('started_at',row.get('last_checked',0)) > MAX_CHECK_SECONDS:
                c.execute("UPDATE chapter_updates SET status='unavailable',requested=0,failures=failures+1,config_revision=config_revision+1,message=?,state='{}',next_check=? WHERE book_id=?",
                          ('Source check timed out. Existing chapters and saved source are preserved. Retry Check now.',time.time()+3600,book_id))
                row=dict(c.execute('SELECT * FROM chapter_updates WHERE book_id=?',(book_id,)).fetchone())
        row['pending_count']=c.execute('SELECT COUNT(*) FROM chapter_update_items WHERE book_id=? AND done=0',(book_id,)).fetchone()[0]
        row['recent_events']=[dict(r) for r in c.execute('SELECT at,event,details FROM chapter_update_events WHERE book_id=? ORDER BY id DESC LIMIT 12',(book_id,))]
        row.pop('state', None)
        row['eligible'] = bool(row.get('provider'))
        row['scheduler_running'] = os.environ.get('CHAPTER_UPDATES_ENABLED', '1') == '1'
        return row


def baseline(c, row):
    if row.get('checkpoint'):
        return
    tail=c.execute('SELECT url FROM import_current WHERE book_id=? ORDER BY position DESC LIMIT 1',(row['book_id'],)).fetchone()
    if tail:
        key=chapter_id(tail[0],row['provider'],row['source_id'])
        c.execute('UPDATE chapter_updates SET checkpoint=? WHERE book_id=?',(key,row['book_id']))


def configure(path, book_id, data):
    with connect(path) as c, c:
        c.execute('BEGIN IMMEDIATE')
        row = ensure(c, book_id)
        revision=data.get('config_revision')
        if revision is not None and (type(revision) is not int or revision != row.get('config_revision',0)):
            raise SourceConflict('Source settings changed in another client. Reload the saved settings and retry your edit.')
        if 'source_url' in data and data['source_url'] == '':
            # Keep the tombstone: ensure() must not rediscover a removed source
            # from historical chapter URLs when this book is reopened.
            if not row.get('provider') and 'config_revision' not in row:
                c.execute("INSERT INTO chapter_updates(book_id,source_url,provider,source_id) VALUES(?,'','','')",(book_id,))
            c.execute('DELETE FROM import_sources WHERE book_id=?',(book_id,))
            c.execute('DELETE FROM chapter_update_items WHERE book_id=?',(book_id,))
            c.execute("UPDATE chapter_updates SET source_url='',provider='',source_id='',enabled=0,status='idle',message='Online source removed. Imported chapters are preserved.',requested=0,checkpoint='',state='{}',config_revision=config_revision+1 WHERE book_id=?",(book_id,))
            return dict(c.execute('SELECT *,0 eligible FROM chapter_updates WHERE book_id=?',(book_id,)).fetchone())
        if 'source_url' in data:
            url, provider, key = identity(data['source_url'])
            owner=c.execute('SELECT book_id FROM import_sources WHERE url=?',(url,)).fetchone()
            if owner and owner[0]!=book_id:
                raise ValueError('This source already belongs to another imported book. Existing books were preserved.')
            if data.get('preview_id'):
                from source_replacement import apply
                apply(c,row,book_id,url,data)
            else:
                # Reliable source/chapter identity, never a title comparison.
                old = c.execute('SELECT url,position FROM import_chapters WHERE book_id=? UNION SELECT href,position FROM book_sections WHERE book_id=? UNION SELECT ch.source_url,s.position FROM chapters ch JOIN book_sections s ON s.legacy_chapter_id=ch.id WHERE s.book_id=? ORDER BY position', (book_id, book_id,book_id)).fetchall()
                matches = []
                for item in old:
                    try:
                        matches.append(chapter_id(item[0], provider, key))
                    except ValueError:
                        pass
                if not matches:
                    raise ValueError('Validate the new source to preview chapter overlap and save a replacement independently of the old source.')
                owner=c.execute('SELECT book_id FROM import_sources WHERE url=?',(url,)).fetchone()
                if owner and owner[0]!=book_id:
                    raise ValueError('This source already belongs to another imported book. Existing books were preserved.')
                if 'config_revision' not in row:
                    c.execute('INSERT INTO chapter_updates(book_id,source_url,provider,source_id) VALUES(?,?,?,?)', (book_id,url,provider,key))
                else:
                    changed=(provider,key)!=(row.get('provider'),row.get('source_id'))
                    c.execute('UPDATE chapter_updates SET source_url=?,provider=?,source_id=?,checkpoint=CASE WHEN ? THEN ? ELSE checkpoint END WHERE book_id=?', (url,provider,key,changed,matches[-1],book_id))
                    if changed:c.execute('DELETE FROM chapter_update_items WHERE book_id=?',(book_id,))
            c.execute('INSERT INTO import_sources(book_id,url) VALUES(?,?) ON CONFLICT(book_id) DO UPDATE SET url=excluded.url',(book_id,url))
            row = dict(c.execute('SELECT * FROM chapter_updates WHERE book_id=?',(book_id,)).fetchone())
        if not row.get('provider'):
            raise ValueError(row['message'])
        baseline(c,row)
        interval = data.get('interval', row['interval'])
        if type(interval) is not int or not 43200 <= interval <= 604800:
            raise ValueError('Choose an interval from 12 hours to 7 days.')
        enabled = data.get('enabled', bool(row['enabled']))
        if type(enabled) is not bool:
            raise ValueError('Enabled must be true or false.')
        c.execute('UPDATE chapter_updates SET enabled=?,interval=?,next_check=CASE WHEN enabled=0 AND ?=1 THEN ? ELSE next_check END WHERE book_id=?', (enabled,interval,enabled,time.time()+interval,book_id))
        if enabled:
            c.execute('UPDATE import_sources SET refresh_interval=0 WHERE book_id=?',(book_id,))
        # Fence in-flight network callbacks and discard an obsolete catalog walk.
        c.execute("UPDATE chapter_updates SET config_revision=config_revision+1,status='idle',requested=0,state='{}',failures=0,message='Source settings saved. Check now to verify current availability.' WHERE book_id=?",(book_id,))
    return status(path, book_id)


def request_check(path, book_id, limits=None):
    limits=limits or {}
    budget={'max_pages':limits.get('max_pages',MAX_PAGES),'max_new':limits.get('max_new',MAX_NEW)}
    if any(type(value) is not int or not 1<=value<=maximum for value,maximum in [(budget['max_pages'],MAX_PAGES),(budget['max_new'],MAX_NEW)]):
        raise ValueError('Check limits must be positive integers within the server budget.')
    with connect(path) as c, c:
        row = ensure(c, book_id)
        if not row.get('provider'):
            raise ValueError(row['message'])
        baseline(c,row)
        # Coalesce repeated taps; no parallel jobs. Requests have a cooldown.
        if (row['status'] not in ('checking','importing') or row['failures']) and (row['status']=='idle' or time.time()-row['last_checked'] >= 60):
            pending=c.execute('SELECT 1 FROM chapter_update_items WHERE book_id=? AND done=0',(book_id,)).fetchone()
            state=json.loads(row['state']) if pending else {}
            state['budget']=budget
            if not pending:c.execute('UPDATE chapter_updates SET new_count=0,discovered_count=0,run_started_at=? WHERE book_id=?',(time.time(),book_id))
            audit(c,row,'requested',time.time(),pending=bool(pending))
            c.execute("UPDATE chapter_updates SET requested=1,status=?,failures=0,message='Check queued.',state=? WHERE book_id=?", ('importing' if pending else 'idle',json.dumps(state),book_id))
    return status(path, book_id)


class Updater:
    def __init__(self, path, browse=None, scrape=None, check=lambda: None, clock=time.time):
        self.path, self.check, self.clock = path, check, clock
        if browse is None or scrape is None:
            from discovery import discover_page
            from scraper import scrape_chapter
            browse, scrape = browse or discover_page, scrape or scrape_chapter
        self.browse, self.scrape = browse, scrape

    def current(self,c,row):
        latest=c.execute('SELECT config_revision FROM chapter_updates WHERE book_id=?',(row['book_id'],)).fetchone()
        return latest and latest[0]==row['config_revision']

    def step(self):
        """One public request/append per tick, protected across web processes."""
        import fcntl
        with Path(str(self.path)+'.chapter-updates.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            return self._step()

    def _step(self):
        self.check()
        now = self.clock()
        with connect(self.path) as c:
            row = c.execute('''SELECT u.* FROM chapter_updates u WHERE
                NOT EXISTS(SELECT 1 FROM book_visibility v WHERE v.book_id=u.book_id AND v.hidden=1)
                AND (requested=1 OR (status IN ('checking','importing') AND next_check<=?) OR (enabled=1 AND next_check<=?))
                AND NOT EXISTS(SELECT 1 FROM platform_jobs j WHERE j.book_id=u.book_id AND j.kind IN ('discover','import','delete') AND j.status IN ('queued','running','paused'))
                ORDER BY next_check,book_id LIMIT 1''',(now,now)).fetchone()
        if not row:
            return False
        row = dict(row)
        if not row['checkpoint']:
            with connect(self.path) as c,c:
                c.execute('BEGIN IMMEDIATE')
                if not self.current(c,row):return True
                baseline(c,row)
                row=dict(c.execute('SELECT * FROM chapter_updates WHERE book_id=?',(row['book_id'],)).fetchone())
        try:
            with connect(self.path) as c:pending=c.execute('SELECT 1 FROM chapter_update_items WHERE book_id=? AND done=0',(row['book_id'],)).fetchone()
            if row['status'] == 'importing' or pending:
                self._import(row)
            else:
                self._catalog(row)
        except Exception as e:
            # Do not expose remote URLs, tokens, or HTML in browser/log errors.
            LOG.warning('chapter_update_failed book=%s type=%s',row['book_id'],type(e).__name__)
            with connect(self.path) as c, c:
                c.execute('BEGIN IMMEDIATE')
                if not self.current(c,row):return True
                audit(c,row,'retrying' if row['failures']<2 else 'failed',now,message=source_error(e))
                c.execute("UPDATE chapter_updates SET status=?,message=?,failures=failures+1,last_checked=?,requested=0,next_check=? WHERE book_id=?", ('importing' if row['status']=='importing' and row['failures']<2 else 'unavailable',source_error(e),now,now+min(604800,3600*2**min(row['failures'],7)),row['book_id']))
        return True

    def _catalog(self, row):
        state = json.loads(row['state'])
        budget=state.get('budget',{'max_pages':MAX_PAGES,'max_new':MAX_NEW})
        starting=not state.get('pending') or row['status'] not in ('checking','unavailable')
        if not state or row['status'] not in ('checking','unavailable'):
            state = {'pending':[row['source_url']], 'visited':[], 'links':[], 'budget':budget}
        state.setdefault('started_at',time.time())
        if time.time()-state['started_at']>MAX_CHECK_SECONDS:
            raise TimeoutError('Source check deadline exceeded.')
        if not state.get('pending'):
            state = {'pending':[row['source_url']], 'visited':[], 'links':[], 'budget':budget}
        state.setdefault('started_at',time.time())
        if starting:
            with connect(self.path) as c,c:
                if not self.current(c,row):return
                c.execute('UPDATE chapter_updates SET new_count=0,discovered_count=0,run_started_at=? WHERE book_id=?',(self.clock(),row['book_id']))
                audit(c,row,'started',self.clock(),trigger='manual' if row['requested'] else 'scheduled')
        url = state['pending'][0]
        if len(state['visited']) >= budget['max_pages']:
            raise ValueError('Catalog exceeds bounded page budget.')
        page = self.browse(url)
        # The safe importer revalidates DNS/redirects/robots. Enforce book scope too.
        if identity(page.get('url',url).split('?')[0])[1:] != (row['provider'],row['source_id']):
            raise ValueError('Catalog redirected to a different book.')
        seen = {x['id'] for x in state['links']}
        for link in page['links']:
            key = chapter_id(link['url'],row['provider'],row['source_id'])
            if key not in seen:
                state['links'].append({'id':key,'url':link['url'],'title':link['title'][:300]});seen.add(key)
        if not page['links'] or len(state['links']) > MAX_LINKS:
            raise ValueError('Empty or excessive chapter catalog.')
        state['visited'].append(url);state['pending'].pop(0)
        for target in page.get('pages',[]):
            if identity(target.split('?')[0])[1:] != (row['provider'],row['source_id']):
                raise ValueError('Pagination changed books.')
            if target not in state['visited'] and target not in state['pending']:
                state['pending'].append(target)
        self.check()
        with connect(self.path) as c, c:
            c.execute('BEGIN IMMEDIATE')
            if not self.current(c,row):return
            if state['pending']:
                c.execute("UPDATE chapter_updates SET status='checking',message='Checking public chapter catalog…',requested=0,next_check=0,state=?,source_title=? WHERE book_id=?", (json.dumps(state),page.get('title','')[:300],row['book_id']))
                return
            links = state['links'];ids = [x['id'] for x in links]
            checkpoint = row['checkpoint']
            if checkpoint and checkpoint not in ids:
                raise ValueError('Previous tail absent from incomplete catalog.')
            candidates = links[ids.index(checkpoint)+1:] if checkpoint else []
            known = set()
            catalog_positions={}
            for old in c.execute('SELECT url,position FROM import_chapters WHERE book_id=?',(row['book_id'],)):
                try:catalog_positions[chapter_id(old[0],row['provider'],row['source_id'])]=old[1]
                except ValueError:pass
            for old in c.execute('SELECT i.url FROM import_chapters i JOIN book_sections s ON s.id=i.section_id WHERE i.book_id=? AND length(trim(s.text_content))>0 UNION SELECT href FROM book_sections WHERE book_id=? AND length(trim(text_content))>0',(row['book_id'],row['book_id'])):
                try:
                    known.add(chapter_id(old[0],row['provider'],row['source_id']))
                except ValueError:
                    pass
            for matched in c.execute('SELECT m.source_chapter_id,s.position FROM source_chapter_matches m JOIN book_sections s ON s.id=m.section_id WHERE m.book_id=? AND m.source_url=?', (row['book_id'],row['source_url'])):
                known.add(matched[0]);catalog_positions[matched[0]]=matched[1]
            tail_position=catalog_positions.get(checkpoint)
            if tail_position is not None and any(x['id'] in catalog_positions and catalog_positions[x['id']]<=tail_position for x in candidates):
                raise ValueError('Source reordered existing chapters.')
            if row['provider']=='freewebnovel' and checkpoint and any(int(x['id']) <= int(checkpoint) for x in candidates):
                raise ValueError('Ambiguous source order.')
            if row['provider']=='freewebnovel' and any(int(b['id'])<=int(a['id']) for a,b in zip(candidates,candidates[1:])):
                raise ValueError('Source reordered new chapters.')
            discovered_count=len(candidates)
            candidates=[x for x in candidates if x['id'] not in known]
            already_count=discovered_count-len(candidates)
            if len(candidates) > budget['max_new']:
                raise ValueError('New chapters exceed this check budget; manual review required.')
            c.execute('DELETE FROM chapter_update_items WHERE book_id=? AND done=1',(row['book_id'],))
            for n,item in enumerate(candidates):
                c.execute('INSERT OR IGNORE INTO chapter_update_items(book_id,source_id,url,title,ordinal) VALUES(?,?,?,?,?)',(row['book_id'],item['id'],item['url'],item['title'],n))
            c.execute('UPDATE chapter_updates SET discovered_count=? WHERE book_id=?',(discovered_count,row['book_id']))
            audit(c,row,'discovered',self.clock(),candidates=discovered_count,eligible=len(candidates),available=len(ids))
            if already_count:audit(c,row,'already_imported',self.clock(),count=already_count)
            if not checkpoint:audit(c,row,'skipped',self.clock(),reason='Existing source baseline; historical chapters are not auto-imported',count=len(ids))
            pending = c.execute('SELECT COUNT(*) FROM chapter_update_items WHERE book_id=? AND done=0',(row['book_id'],)).fetchone()[0]
            c.execute("UPDATE chapter_updates SET status=?,message=?,available=?,last_checked=?,last_success=?,requested=0,new_count=0,failures=0,state=?,next_check=? WHERE book_id=?", ('importing' if pending else 'current','Importing new chapters…' if pending else 'Up to date.' if checkpoint else 'Catalog baseline saved. Only future releases will be imported.',len(ids),self.clock(),row['last_success'] if pending else self.clock(),json.dumps({'tail':ids[-1]}),0 if pending else self.clock()+row['interval'],row['book_id']))
            audit(c,row,'queued' if pending else 'completed',self.clock(),pending=pending,imported=0)
            if not pending:
                c.execute('UPDATE chapter_updates SET checkpoint=?,state=? WHERE book_id=?',(ids[-1],'{}',row['book_id']))

    def _import(self, row):
        with connect(self.path) as c:
            item = c.execute('SELECT * FROM chapter_update_items WHERE book_id=? AND done=0 ORDER BY ordinal LIMIT 1',(row['book_id'],)).fetchone()
        if item:
            with connect(self.path) as c,c:
                if not self.current(c,row):return
                audit(c,row,'importing',self.clock(),source_id=item['source_id'])
            content = self.scrape(item['url'])
            self.check()
            text = content['content'];title = content.get('chapter_title') or item['title']
            if not text.strip():
                raise ValueError('Empty chapter.')
            body = '<h1>'+html.escape(title)+'</h1>'+''.join('<p>'+html.escape(line)+'</p>' for line in text.splitlines() if line.strip())
            with connect(self.path) as c, c:
                c.execute('BEGIN IMMEDIATE')
                if not self.current(c,row):return
                old = c.execute('SELECT section_id,position FROM import_chapters WHERE book_id=? AND url=?',(row['book_id'],item['url'])).fetchone()
                existing = None;placeholder=None
                if c.execute("SELECT 1 FROM platform_jobs WHERE book_id=? AND kind IN ('discover','import','delete') AND status IN ('queued','running','paused')",(row['book_id'],)).fetchone():
                    return
                for candidate in c.execute('SELECT href,id,position,text_content FROM book_sections WHERE book_id=?',(row['book_id'],)):
                    try:
                        if chapter_id(candidate[0],row['provider'],row['source_id'])==item['source_id']:
                            if (candidate['text_content'] or '').strip():existing=candidate;break
                            placeholder=candidate
                    except ValueError:
                        pass
                if not existing and old and old['section_id']:
                    candidate=c.execute('SELECT id,position,text_content FROM book_sections WHERE book_id=? AND id=?',(row['book_id'],old['section_id'])).fetchone()
                    if candidate and (candidate['text_content'] or '').strip():existing=candidate
                    elif candidate and not placeholder:placeholder=candidate
                added = not existing
                if added:
                    if placeholder:
                        position=placeholder['position'];section=placeholder['id']
                        c.execute('UPDATE book_sections SET title=?,href=?,content_html=?,text_content=? WHERE id=? AND book_id=?',(title,item['url'],body,text,section,row['book_id']))
                    else:
                        position = c.execute('SELECT MAX(p)+1 FROM (SELECT COALESCE(MAX(position),-1) p FROM book_sections WHERE book_id=? UNION ALL SELECT COALESCE(MAX(position),-1) FROM import_chapters WHERE book_id=?)',(row['book_id'],row['book_id'])).fetchone()[0]
                        if old:
                            position=old['position']
                            if c.execute('SELECT 1 FROM book_sections WHERE book_id=? AND position=?',(row['book_id'],position)).fetchone():
                                raise ValueError('Chapter position conflict needs attention.')
                        section = c.execute('INSERT INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES(?,?,?,?,?,?)',(row['book_id'],position,title,item['url'],body,text)).lastrowid
                    c.execute("INSERT INTO import_chapters(book_id,url,position,title,section_id,status) VALUES(?,?,?,?,?,'completed') ON CONFLICT(book_id,url) DO UPDATE SET section_id=excluded.section_id,status='completed'",(row['book_id'],item['url'],position,title,section))
                    catalog=c.execute("SELECT d.job_id FROM discovery_catalogs d JOIN platform_jobs j ON j.id=d.job_id WHERE d.book_id=? AND j.status='completed' ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1",(row['book_id'],)).fetchone()
                    if catalog:
                        tail=c.execute('SELECT COALESCE(MAX(position),-1)+1 FROM discovery_links WHERE job_id=?',(catalog[0],)).fetchone()[0]
                        c.execute('INSERT OR IGNORE INTO discovery_links(job_id,url,position) VALUES(?,?,?)',(catalog[0],item['url'],tail))
                c.execute('UPDATE chapter_update_items SET done=1 WHERE book_id=? AND source_id=?',(row['book_id'],item['source_id']))
                audit(c,row,'imported' if added else 'already_imported',self.clock(),source_id=item['source_id'])
                c.execute('UPDATE chapter_updates SET new_count=new_count+?,last_imported=?,checkpoint=? WHERE book_id=?',(int(added),item['source_id'],item['source_id'],row['book_id']))
            return
        with connect(self.path) as c, c:
            c.execute('BEGIN IMMEDIATE')
            if not self.current(c,row):return
            audit(c,row,'completed',self.clock(),imported=row['new_count'])
            tail = json.loads(row['state']).get('tail',row['checkpoint'])
            c.execute("UPDATE chapter_updates SET status='current',message=?,checkpoint=?,last_success=?,state='{}',next_check=? WHERE book_id=?", (f"{row['new_count']} new chapters imported." if row['new_count'] else 'Up to date.',tail,self.clock(),self.clock()+row['interval'],row['book_id']))


def start_scheduler(application):
    if os.environ.get('CHAPTER_UPDATES_ENABLED','1') != '1':
        return
    path = Path(application.config['DATA_DIR'])/'webnovel.db'
    def run():
        from resource_guard import ResourceGuard
        from shutil import disk_usage
        guard=ResourceGuard()
        def check():
            guard.check()
            if disk_usage(path.parent).free<256*1024*1024:
                from resource_guard import ResourcePressure
                raise ResourcePressure('Chapter updates deferred: insufficient free storage.')
        updater = Updater(path,check=check)
        while True:
            time.sleep(10)
            try:
                updater.step()
            except Exception:
                LOG.warning('Chapter scheduler tick deferred safely',exc_info=False)
    thread = threading.Thread(target=run,name='chapter-updates',daemon=True)
    thread.start();application.extensions['chapter_updates_scheduler'] = thread
