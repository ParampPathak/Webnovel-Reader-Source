"""Additive library schema. No legacy chapters or generated files are rewritten."""
import hashlib
import heapq
import html
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
    finally:
        conn.close()


SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS library_migrations (version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS books (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT DEFAULT '', language TEXT DEFAULT 'en',
        description TEXT DEFAULT '', source_kind TEXT NOT NULL, source_key TEXT UNIQUE,
        original_path TEXT, file_hash TEXT UNIQUE, cover_path TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS book_sections (
        id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id), position INTEGER NOT NULL,
        title TEXT NOT NULL, href TEXT NOT NULL, content_html TEXT NOT NULL, text_content TEXT NOT NULL,
        legacy_chapter_id INTEGER UNIQUE, linear INTEGER NOT NULL DEFAULT 1,
        UNIQUE(book_id, position), UNIQUE(book_id, href))''',
    '''CREATE TABLE IF NOT EXISTS book_navigation (
        id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id), position INTEGER NOT NULL,
        title TEXT NOT NULL, href TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS book_resources (
        book_id TEXT NOT NULL REFERENCES books(id), path TEXT NOT NULL, media_type TEXT NOT NULL,
        PRIMARY KEY(book_id, path))''',
    '''CREATE TABLE IF NOT EXISTS library_audio_assets (
        id INTEGER PRIMARY KEY, section_id INTEGER NOT NULL REFERENCES book_sections(id),
        legacy_chapter_id INTEGER UNIQUE, engine TEXT, voice TEXT, audio_path TEXT,
        audio_format TEXT, content_hash TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS reading_progress (
        book_id TEXT PRIMARY KEY REFERENCES books(id), section_id INTEGER NOT NULL REFERENCES book_sections(id),
        fraction REAL NOT NULL DEFAULT 0, anchor TEXT DEFAULT '', preferences TEXT NOT NULL DEFAULT '{}',
        version INTEGER NOT NULL DEFAULT 1, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE IF NOT EXISTS reading_follow (
        book_id TEXT PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
        enabled INTEGER NOT NULL DEFAULT 0, suspended INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS library_migration_notes (
        source_key TEXT PRIMARY KEY, ordering_basis TEXT NOT NULL, warning TEXT NOT NULL DEFAULT '')''',
    'CREATE INDEX IF NOT EXISTS idx_sections_book_position ON book_sections(book_id,position)',
    'CREATE INDEX IF NOT EXISTS idx_navigation_book_position ON book_navigation(book_id,position)',
    'CREATE INDEX IF NOT EXISTS idx_legacy_audio_section ON library_audio_assets(section_id)',
]


def attach_legacy(conn, chapter_id):
    row = conn.execute('SELECT * FROM chapters WHERE id=?', (chapter_id,)).fetchone()
    if not row:
        return
    if conn.execute('SELECT 1 FROM book_sections WHERE legacy_chapter_id=?', (chapter_id,)).fetchone():
        return
    title = row['novel_title'] or 'Untitled novel'
    key = 'legacy:' + title
    book = conn.execute('SELECT id FROM books WHERE source_key=?', (key,)).fetchone()
    book_id = book['id'] if book else str(uuid.uuid5(uuid.NAMESPACE_URL, key))
    conn.execute('INSERT OR IGNORE INTO books(id,title,source_kind,source_key) VALUES(?,?,?,?)',
                 (book_id, title, 'webnovel', key))
    position = conn.execute('SELECT COALESCE(MAX(position),-1)+1 FROM book_sections WHERE book_id=?', (book_id,)).fetchone()[0]
    content = row['content'] or ''
    chapter_title = row['chapter_title'] or f'Chapter {position+1}'
    body = '<h1>' + html.escape(chapter_title) + '</h1>' + ''.join('<p>' + html.escape(p) + '</p>' for p in content.splitlines() if p.strip())
    cursor = conn.execute('''INSERT INTO book_sections(book_id,position,title,href,content_html,text_content,legacy_chapter_id)
                            VALUES(?,?,?,?,?,?,?)''', (book_id,position,chapter_title,f'legacy-{chapter_id}',body,content,chapter_id))
    keys = row.keys()
    if row['audio_path']:
        conn.execute('''INSERT OR IGNORE INTO library_audio_assets
                        (section_id,legacy_chapter_id,engine,voice,audio_path,audio_format,content_hash)
                        VALUES(?,?,?,?,?,?,?)''',
                     (cursor.lastrowid,chapter_id,row['engine_used'] if 'engine_used' in keys else None,
                      row['voice_used'] if 'voice_used' in keys else None,row['audio_path'],
                      row['audio_format'] if 'audio_format' in keys else 'mp3',hashlib.sha256(content.encode()).hexdigest()))


def legacy_order(conn):
    """Prefer persisted next-link evidence; use insertion IDs for disconnected chains.

    Queue priority is never evidence of book order. Record incomplete/cyclic
    evidence explicitly rather than interpreting arbitrary chapter title numbers.
    """
    groups = {}
    columns={row['name'] for row in conn.execute('PRAGMA table_info(chapters)')}
    selection='id,novel_title,source_url'+(',next_url' if 'next_url' in columns else '')
    # Ordering needs only links/IDs, never all legacy chapter text in memory.
    for row in conn.execute('SELECT '+selection+' FROM chapters ORDER BY id'):
        groups.setdefault(row['novel_title'] or 'Untitled novel',[]).append(row)
    ordered = []
    for title,rows in groups.items():
        by_url = {r['source_url']:r['id'] for r in rows if r['source_url']}
        by_id = {r['id']:r for r in rows}
        indegrees = {r['id']:0 for r in rows}
        edges = {}
        for row in rows:
            target = by_url.get(row['next_url']) if 'next_url' in row.keys() else None
            if target is not None and target != row['id']:
                edges[row['id']] = target
                indegrees[target] += 1
        queue = [identifier for identifier,count in indegrees.items() if count==0]
        heapq.heapify(queue)
        chain = []
        while queue:
            identifier = heapq.heappop(queue)
            chain.append(identifier)
            target = edges.get(identifier)
            if target is not None:
                indegrees[target] -= 1
                if indegrees[target]==0:
                    heapq.heappush(queue,target)
        warning = ''
        if len(chain)!=len(rows):
            chain = sorted(by_id)
            warning = 'Next-link cycle detected; insertion order retained for review.'
        elif len(edges)<len(rows)-1:
            warning = 'Incomplete next-link chain; disconnected sections use insertion order.'
        basis = 'next-links' if edges and not warning else 'next-links with insertion fallback' if edges else 'insertion IDs'
        conn.execute('INSERT OR REPLACE INTO library_migration_notes(source_key,ordering_basis,warning) VALUES(?,?,?)',
                     ('legacy:'+title,basis,warning))
        ordered.extend(chain)
    return ordered


def migrate(path):
    with connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            for statement in SCHEMA:
                conn.execute(statement)
            # Early staging candidates used JSON keys unknown to the rollback
            # reader. Keep its JSON format unchanged; retain the flags separately.
            for row in conn.execute("SELECT book_id,preferences FROM reading_progress WHERE preferences LIKE '%followAlong%'"):
                preferences=json.loads(row['preferences'])
                enabled=preferences.pop('followAlong',False)
                suspended=preferences.pop('followAlongSuspended',False)
                conn.execute('INSERT OR REPLACE INTO reading_follow(book_id,enabled,suspended) VALUES(?,?,?)',
                    (row['book_id'],int(bool(enabled)),int(bool(suspended))))
                conn.execute('UPDATE reading_progress SET preferences=? WHERE book_id=?',
                    (json.dumps(preferences),row['book_id']))
            applied = conn.execute('SELECT 1 FROM library_migrations WHERE version=1').fetchone()
            if not applied:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='chapters'").fetchone()
                if exists:
                    for identifier in legacy_order(conn):
                        attach_legacy(conn, identifier)
                conn.execute('INSERT INTO library_migrations(version) VALUES(1)')
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def import_book(path, parsed, original_path, file_hash):
    with connect(path) as conn:
        with conn:
            existing = conn.execute('SELECT id FROM books WHERE file_hash=?', (file_hash,)).fetchone()
            if existing:
                return existing['id'], False
            meta = parsed['metadata']
            book_id = parsed['id']
            conn.execute('''INSERT INTO books(id,title,author,language,description,source_kind,original_path,file_hash,cover_path)
                            VALUES(?,?,?,?,?,'epub',?,?,?)''',
                         (book_id,meta['title'],meta['author'],meta['language'],meta['description'],str(original_path),file_hash,parsed['cover']))
            conn.executemany('''INSERT INTO book_sections(book_id,position,title,href,content_html,text_content,linear)
                                VALUES(?,?,?,?,?,?,?)''',
                             ((book_id,s['position'],s['title'],s['href'],s['html'],s['text'],s['linear']) for s in parsed['sections']))
            conn.executemany('INSERT INTO book_navigation(book_id,position,title,href,depth) VALUES(?,?,?,?,?)',
                             [(book_id,i,n['title'],n['href'],n['depth']) for i,n in enumerate(parsed['navigation'])])
            conn.executemany('INSERT INTO book_resources(book_id,path,media_type) VALUES(?,?,?)',
                             [(book_id,r['path'],r['media_type']) for r in parsed['resources']])
    return book_id, True


def save_progress(path, book_id, section_id, fraction, anchor, preferences, expected_version, inherit_theme=None):
    preferences=dict(preferences)
    follow={key:preferences.pop(key) for key in ['followAlong','followAlongSuspended'] if key in preferences}
    with connect(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM book_sections WHERE id=? AND book_id=?', (section_id,book_id)).fetchone():
            conn.rollback()
            raise ValueError('Section does not belong to this book')
        previous = conn.execute('SELECT version FROM reading_progress WHERE book_id=?', (book_id,)).fetchone()
        version = previous['version'] if previous else 0
        if version != expected_version:
            conn.rollback()
            return None
        if follow:
            prior=conn.execute('SELECT enabled,suspended FROM reading_follow WHERE book_id=?',(book_id,)).fetchone()
            conn.execute('INSERT OR REPLACE INTO reading_follow(book_id,enabled,suspended) VALUES(?,?,?)',
                (book_id,int(follow.get('followAlong',prior['enabled'] if prior else False)),
                 int(follow.get('followAlongSuspended',prior['suspended'] if prior else False))))
        conn.execute('''INSERT INTO reading_progress(book_id,section_id,fraction,anchor,preferences,version)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
                        section_id=excluded.section_id,fraction=excluded.fraction,anchor=excluded.anchor,
                        preferences=excluded.preferences,version=excluded.version,updated_at=CURRENT_TIMESTAMP''',
                     (book_id,section_id,fraction,anchor,json.dumps(preferences),version+1))
        if inherit_theme is not None:
            conn.execute('INSERT OR REPLACE INTO reader_theme_choices(book_id,inherit) VALUES(?,?)',(book_id,int(inherit_theme)))
        conn.commit()
        return version+1


def readable_section_sql(alias='book_sections'):
    if alias not in {'book_sections','real','s'}:raise ValueError('Unknown section alias')
    return "(length(trim(COALESCE("+alias+".text_content,''),char(9)||char(10)||char(13)||' '))>0 OR (length(trim(COALESCE("+alias+".content_html,'')))>0 AND EXISTS(SELECT 1 FROM books rb WHERE rb.id="+alias+".book_id AND rb.source_kind='epub')))"


def reading_percentage(index, fraction, count):
    """Equal readable-chapter weighting; independent of audio and sparse positions."""
    return min(100.0,max(0.0,100.0*(index+fraction)/count)) if index is not None and count else 0.0


def reading_metrics(conn, book_id, section_id=None, fraction=0):
    readable=readable_section_sql()
    count=conn.execute('SELECT COUNT(*) FROM book_sections WHERE book_id=? AND '+readable,(book_id,)).fetchone()[0]
    current=conn.execute('SELECT position FROM book_sections WHERE book_id=? AND id=? AND '+readable,(book_id,section_id)).fetchone() if section_id else None
    index=conn.execute('SELECT COUNT(*) FROM book_sections WHERE book_id=? AND position<? AND '+readable,(book_id,current[0])).fetchone()[0] if current else None
    return {'readable_section_count':count,'reading_index':index,'reading_percentage':reading_percentage(index,fraction,count)}
