"""Additive durable platform storage. Existing book and Piper rows are untouched."""
import json
import time
import uuid
from library_store import connect

SCHEMA = [
'''CREATE TABLE IF NOT EXISTS import_metadata_overrides (book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,field TEXT NOT NULL,PRIMARY KEY(book_id,field))''',
'''CREATE TABLE IF NOT EXISTS reader_theme_choices (book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,inherit INTEGER NOT NULL)''',
'''CREATE TABLE IF NOT EXISTS book_bookmarks (id INTEGER PRIMARY KEY,book_id TEXT NOT NULL REFERENCES books(id),section_id INTEGER NOT NULL REFERENCES book_sections(id),fraction REAL NOT NULL,title TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS book_visibility (book_id TEXT PRIMARY KEY REFERENCES books(id), hidden INTEGER NOT NULL DEFAULT 0)''',
'''CREATE TABLE IF NOT EXISTS book_deletions (id TEXT PRIMARY KEY,book_id TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS deletion_files (deletion_id TEXT NOT NULL REFERENCES book_deletions(id),path TEXT NOT NULL,status TEXT DEFAULT 'pending',PRIMARY KEY(deletion_id,path))''',
'''CREATE TABLE IF NOT EXISTS platform_jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, book_id TEXT REFERENCES books(id),
 status TEXT NOT NULL DEFAULT 'queued', config TEXT NOT NULL, priority INTEGER DEFAULT 0,
 error TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP, result TEXT DEFAULT '{}')''',
'''CREATE INDEX IF NOT EXISTS idx_jobs_book_status ON platform_jobs(book_id,status,created_at)''',
'''CREATE TABLE IF NOT EXISTS platform_items (
 id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES platform_jobs(id),
 item_key TEXT NOT NULL, position INTEGER NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
 status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
 next_attempt REAL DEFAULT 0, priority INTEGER DEFAULT 0, error TEXT DEFAULT '', UNIQUE(job_id,item_key))''',
'''CREATE INDEX IF NOT EXISTS idx_platform_pending ON platform_items(job_id,status,next_attempt,position)''',
'''CREATE TABLE IF NOT EXISTS audio_variants (
 id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
 section_id INTEGER NOT NULL REFERENCES book_sections(id), provider TEXT NOT NULL,
 voice TEXT NOT NULL, settings TEXT NOT NULL, content_hash TEXT NOT NULL,
 path TEXT, status TEXT NOT NULL DEFAULT 'pending', chunk_count INTEGER DEFAULT 0,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE INDEX IF NOT EXISTS idx_audio_section ON audio_variants(book_id,section_id,status)''',
'''CREATE TABLE IF NOT EXISTS audio_chunks (
 asset_id TEXT NOT NULL REFERENCES audio_variants(id), position INTEGER NOT NULL,
 path TEXT NOT NULL, checksum TEXT NOT NULL, PRIMARY KEY(asset_id,position))''',
'''CREATE TABLE IF NOT EXISTS listening_progress (
 book_id TEXT PRIMARY KEY REFERENCES books(id), section_id INTEGER NOT NULL REFERENCES book_sections(id),
 asset_id TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0, playback_rate REAL DEFAULT 1,
 voice TEXT NOT NULL, synthesis_rate INTEGER DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS book_preferences (
 book_id TEXT PRIMARY KEY REFERENCES books(id), favorite INTEGER DEFAULT 0,
 opened_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS import_sources (
 book_id TEXT PRIMARY KEY REFERENCES books(id), url TEXT NOT NULL UNIQUE,
 refreshed_at TEXT DEFAULT CURRENT_TIMESTAMP,refresh_interval INTEGER DEFAULT 0,next_refresh REAL DEFAULT 0)''',
'''CREATE TABLE IF NOT EXISTS import_chapters (
 book_id TEXT NOT NULL REFERENCES books(id), url TEXT NOT NULL, position INTEGER NOT NULL,
 title TEXT NOT NULL, section_id INTEGER REFERENCES book_sections(id),
 status TEXT DEFAULT 'pending', error TEXT DEFAULT '', PRIMARY KEY(book_id,url),
 UNIQUE(book_id,position))''',
'''CREATE TABLE IF NOT EXISTS discovery_catalogs (
 job_id TEXT PRIMARY KEY REFERENCES platform_jobs(id),book_id TEXT NOT NULL REFERENCES books(id))''',
'''CREATE TABLE IF NOT EXISTS discovery_links (
 job_id TEXT NOT NULL REFERENCES discovery_catalogs(job_id),url TEXT NOT NULL,position INTEGER NOT NULL,
 PRIMARY KEY(job_id,url),UNIQUE(job_id,position))''',
'''CREATE VIEW IF NOT EXISTS import_current AS
 WITH latest AS (SELECT d.book_id,d.job_id,ROW_NUMBER() OVER(PARTITION BY d.book_id ORDER BY j.created_at DESC,j.rowid DESC) rank
 FROM discovery_catalogs d JOIN platform_jobs j ON j.id=d.job_id WHERE j.status='completed')
 SELECT c.book_id,c.url,l.position,c.title,c.section_id,c.status,c.error
 FROM latest d JOIN discovery_links l ON l.job_id=d.job_id JOIN import_chapters c ON c.book_id=d.book_id AND c.url=l.url WHERE d.rank=1
 UNION ALL SELECT c.* FROM import_chapters c WHERE NOT EXISTS(SELECT 1 FROM latest d WHERE d.book_id=c.book_id)''',
'''CREATE TABLE IF NOT EXISTS epub_exports (
 id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id), path TEXT NOT NULL,
 first_position INTEGER NOT NULL, last_position INTEGER NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS audiobook_exports (id TEXT PRIMARY KEY,book_id TEXT NOT NULL REFERENCES books(id),path TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
'''CREATE TABLE IF NOT EXISTS platform_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)''',
]


def migrate(path):
    with connect(path) as conn:
        with conn:
            for statement in SCHEMA:
                conn.execute(statement)
            columns={row['name'] for row in conn.execute('PRAGMA table_info(book_bookmarks)')}
            if 'anchor' not in columns:conn.execute("ALTER TABLE book_bookmarks ADD COLUMN anchor TEXT DEFAULT ''")
            columns={row['name'] for row in conn.execute('PRAGMA table_info(platform_items)')}
            if 'requested_at' not in columns: conn.execute('ALTER TABLE platform_items ADD COLUMN requested_at REAL DEFAULT 0')
            if 'priority' not in columns:
                conn.execute('ALTER TABLE platform_items ADD COLUMN priority INTEGER DEFAULT 0')
            columns={row['name'] for row in conn.execute('PRAGMA table_info(import_sources)')}
            for name in ('refresh_interval','next_refresh'):
                if name not in columns: conn.execute('ALTER TABLE import_sources ADD COLUMN '+name+' INTEGER DEFAULT 0')
            for table, additions in [('audio_variants',[('first_chunk_bytes','INTEGER DEFAULT 3000'),('chunk_plan',"TEXT DEFAULT ''")]),('audio_chunks',[('duration','REAL')]),('listening_progress',[('segment_index','INTEGER DEFAULT 0'),('segment_seconds','REAL DEFAULT 0')])]:
                columns={row['name'] for row in conn.execute('PRAGMA table_info('+table+')')}
                for name, declaration in additions:
                    if name not in columns: conn.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+declaration)
            # Neutral provenance for legacy rows: migration never claims an
            # active player or advances a saved position/version/timestamp.
            columns={row['name'] for row in conn.execute('PRAGMA table_info(listening_progress)')}
            for name,declaration in [('session_id',"TEXT NOT NULL DEFAULT ''"),
                    ('session_generation','INTEGER NOT NULL DEFAULT 0'),('event_sequence','INTEGER NOT NULL DEFAULT 0'),
                    ('checkpoint_revision','INTEGER NOT NULL DEFAULT 0'),('checkpoint_intent',"TEXT NOT NULL DEFAULT 'legacy'"),
                    ('playback_state',"TEXT NOT NULL DEFAULT 'paused'"),('text_anchor',"TEXT NOT NULL DEFAULT ''"),
                    ('normalized_offset','INTEGER')]:
                if name not in columns:conn.execute('ALTER TABLE listening_progress ADD COLUMN '+name+' '+declaration)
            # Upgrade existing application-wide defaults without discarding the
            # most recent saved listening speed from before global preferences.
            row=conn.execute("SELECT value FROM platform_settings WHERE key='tts_preferences'").fetchone()
            value=json.loads(row[0]) if row else {'provider':'edge','voice':'en-US-AndrewNeural','rate':0,'format':'mp3','chunk_version':1}
            if 'playback_rate' not in value or 'volume' not in value:
                latest=conn.execute('SELECT playback_rate FROM listening_progress ORDER BY updated_at DESC,rowid DESC LIMIT 1').fetchone()
                value.setdefault('playback_rate',latest[0] if latest else 1)
                value.setdefault('volume',1)
                conn.execute("INSERT OR REPLACE INTO platform_settings(key,value) VALUES('tts_preferences',?)",(json.dumps(value),))
            conn.execute('INSERT OR IGNORE INTO library_migrations(version) VALUES(2)')


def worker_state(root):
    import fcntl
    from pathlib import Path
    root=Path(root)
    try:
        age=max(0,time.time()-(root/'.platform-heartbeat').stat().st_mtime)
        with (root/'.platform-worker.lock').open('r') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                state={'worker_active':age<180,'heartbeat_age_seconds':age,'worker_message':'Background worker is running.' if age<180 else 'Background worker is not responding.'}
                try:
                    pressure=json.loads((root/'.platform-pressure.json').read_text())
                except (OSError,ValueError): pressure=None
                state.update(resource_paused=bool(pressure),can_generate=age<180 and not pressure)
                if pressure: state['worker_message']='Resource-safety delay: '+pressure.get('reason','host pressure')+' Queued jobs are preserved; worker will retry when safe.'
                return state
        return {'worker_active':False,'heartbeat_age_seconds':age,'worker_message':'Background worker is stopped. Jobs will wait until it starts.'}
    except OSError:
        return {'worker_active':False,'heartbeat_age_seconds':None,'worker_message':'Background worker is stopped. Jobs will wait until it starts.'}


def notify_worker(database):
    """Best-effort local wakeup after commit; persistent jobs never depend on it."""
    import socket
    from pathlib import Path
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as channel:
            channel.setblocking(False)
            channel.sendto(b'work',str(Path(database).parent/'.platform-wake.sock'))
    except OSError:
        pass  # Stopped workers recover the same SQLite queue on startup.


def schedule(path,kind,book_id,config,items,priority=0):
    identifier = str(uuid.uuid4())
    with connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if kind=='discover':
            active=conn.execute("SELECT id FROM platform_jobs WHERE book_id=? AND kind='discover' AND status IN ('queued','running','paused') ORDER BY rowid DESC LIMIT 1",(book_id,)).fetchone()
            if active:return active[0]
        with conn:
            conn.execute('INSERT INTO platform_jobs(id,kind,book_id,config,priority) VALUES(?,?,?,?,?)',
                         (identifier,kind,book_id,json.dumps(config),priority))
            conn.executemany('INSERT INTO platform_items(job_id,item_key,position,payload,requested_at) VALUES(?,?,?,?,?)',
                             ((identifier,str(key),i,json.dumps(payload),time.time() if kind=='audio' and priority>=100 else 0) for i,(key,payload) in enumerate(items)))
    notify_worker(path)
    return identifier


def job_payload(conn,identifier):
    row = conn.execute('SELECT * FROM platform_jobs WHERE id=?',(identifier,)).fetchone()
    if not row:
        return None
    result = dict(row)
    if not result['error']:
        retry=conn.execute("SELECT error,next_attempt FROM platform_items WHERE job_id=? AND status IN ('pending','failed') AND error!='' ORDER BY id LIMIT 1",(identifier,)).fetchone()
        if retry:
            result['error']=retry['error']
            result['retry_at']=retry['next_attempt']
    result['config'] = json.loads(result['config'])
    result['result'] = json.loads(result['result'])
    counts = {r['status']:r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM platform_items WHERE job_id=? GROUP BY status',(identifier,))}
    result['counts'] = counts
    result['total'] = sum(counts.values())
    book=conn.execute('SELECT title FROM books WHERE id=?',(row['book_id'],)).fetchone()
    result['book_title']=book[0] if book else 'Removed book'
    current=conn.execute("SELECT i.item_key,i.error,c.title FROM platform_items i LEFT JOIN import_chapters c ON c.book_id=? AND c.url=i.item_key WHERE i.job_id=? AND i.status='running' ORDER BY i.position LIMIT 1",(row['book_id'],identifier)).fetchone()
    result['current_chapter']=current['title'] if current else None
    skipped=result['config'].get('skipped',0) if row['kind']=='import' else 0
    selected=result['config'].get('selected_count',result['total']+skipped)
    result['progress']={'selected':selected,'completed':counts.get('done',0),'processing':counts.get('running',0),'pending':counts.get('pending',0),'failed':counts.get('failed',0),'skipped':skipped,'percentage':round(100*(counts.get('done',0)+skipped)/selected,1) if selected else 100}
    result['pause_reason']='user' if row['status']=='paused' else None
    return result


def control(path,identifier,action):
    with connect(path) as conn:
        with conn:
            row = conn.execute('SELECT status FROM platform_jobs WHERE id=?',(identifier,)).fetchone()
            if not row:
                raise ValueError('Job not found.')
            if action == 'pause':
                if row['status'] not in {'queued','running','paused'}:
                    raise ValueError('Only active jobs can be paused.')
                state = 'paused'
            elif action == 'cancel':
                state = 'cancelled'
            elif action in {'resume','retry'}:
                if row['status'] == 'cancelled':
                    raise ValueError('Cancelled jobs cannot be resumed; create a new job to reuse cached work.')
                state = 'queued'
                conn.execute("UPDATE platform_items SET status='pending',attempts=0,next_attempt=0,error='' WHERE job_id=? AND status='failed'",(identifier,))
            else:
                raise ValueError('Unknown job action.')
            conn.execute('UPDATE platform_jobs SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(state,'',identifier))


def recover(path):
    """Call only after acquiring the exclusive worker lock."""
    with connect(path) as conn:
        with conn:
            conn.execute("UPDATE platform_items SET status='pending' WHERE status='running'")
            conn.execute("UPDATE platform_jobs SET status='queued' WHERE status='running'")


PLAY_PRIORITY_TTL = 120


def claim(path):
    with connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('''SELECT i.*,j.kind,j.book_id,j.config FROM platform_items i
            JOIN platform_jobs j ON j.id=i.job_id WHERE j.status IN ('queued','running')
            AND i.status='pending' AND i.next_attempt<=?
            ORDER BY CASE WHEN j.kind='audio' AND (MAX(i.priority,j.priority)<100 OR i.requested_at<?) THEN -10 ELSE MAX(i.priority,j.priority) END DESC,i.requested_at DESC,j.created_at,i.position,i.id LIMIT 1''',(time.time(),time.time()-PLAY_PRIORITY_TTL)).fetchone()
        if row:
            conn.execute("UPDATE platform_items SET status='running' WHERE id=?",(row['id'],))
            conn.execute("UPDATE platform_jobs SET status='running',updated_at=CURRENT_TIMESTAMP WHERE id=?",(row['job_id'],))
        conn.commit()
        if not row:
            return None
        result = dict(row)
        result['config'] = json.loads(result['config'])
        result['payload'] = json.loads(result['payload'])
        return result


def finish_item(path,item,error=None):
    with connect(path) as conn:
        with conn:
            if error:
                attempts = item['attempts']+1
                state = 'failed' if attempts>=4 else 'pending'
                conn.execute('UPDATE platform_items SET status=?,attempts=?,next_attempt=?,error=? WHERE id=?',
                             (state,attempts,time.time()+min(120,2**attempts*2),str(error)[:1000],item['id']))
            else:
                conn.execute("UPDATE platform_items SET status='done',error='' WHERE id=?",(item['id'],))
            pending = conn.execute("SELECT 1 FROM platform_items WHERE job_id=? AND status IN ('pending','running') LIMIT 1",(item['job_id'],)).fetchone()
            if not pending:
                failed = conn.execute("SELECT 1 FROM platform_items WHERE job_id=? AND status='failed' LIMIT 1",(item['job_id'],)).fetchone()
                conn.execute("UPDATE platform_jobs SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('queued','running')",
                             ('failed' if failed else 'completed',str(error or '')[:1000],item['job_id']))
