"""ID-scoped library removal and resumable, conservative permanent cleanup."""
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from library_store import connect
from storage_paths import original_path


@contextmanager
def book_lock(root,book_id):
    import fcntl
    if str(uuid.UUID(book_id))!=book_id:
        raise ValueError('Invalid stable book ID.')
    directory=Path(root)/'.book-locks';directory.mkdir(exist_ok=True)
    with (directory/(book_id+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield


def remove_book(root, book_id, mode, confirmation):
    root = Path(root)
    if mode not in {'remove', 'permanent'}:
        raise ValueError('Choose removal from library or permanent deletion.')
    with connect(root / 'webnovel.db') as conn:
        conn.execute('BEGIN IMMEDIATE')
        book = conn.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
        if not book:
            raise ValueError('Book not found.')
        if confirmation != book['title']:
            raise ValueError('Book title changed. Reload and confirm the intended book.')
        if mode == 'permanent' and conn.execute("SELECT 1 FROM chapters c JOIN book_sections s ON s.legacy_chapter_id=c.id WHERE s.book_id=? AND c.status NOT IN ('ready','failed') LIMIT 1", (book_id,)).fetchone():
            raise ValueError('Piper generation is active for this book. Remove from library, or wait for it to finish before permanent deletion.')
        conn.execute("UPDATE platform_jobs SET status='cancelled',updated_at=CURRENT_TIMESTAMP WHERE book_id=? AND kind!='delete' AND status IN ('queued','running','paused','failed')", (book_id,))
        conn.execute('INSERT INTO book_visibility(book_id,hidden) VALUES(?,1) ON CONFLICT(book_id) DO UPDATE SET hidden=1', (book_id,))
        conn.execute('UPDATE import_sources SET refresh_interval=0,next_refresh=0 WHERE book_id=?', (book_id,))
        job = None
        if mode == 'permanent':
            old = conn.execute("SELECT id FROM book_deletions WHERE book_id=? AND status!='completed'", (book_id,)).fetchone()
            job = old[0] if old else str(uuid.uuid4())
            if not old:
                conn.execute('INSERT INTO book_deletions(id,book_id,title) VALUES(?,?,?)', (job, book_id, book['title']))
                conn.execute("INSERT INTO platform_jobs(id,kind,book_id,config,priority) VALUES(?,'delete',?,'{}',200)", (job, book_id))
                conn.execute("INSERT INTO platform_items(job_id,item_key,position,payload) VALUES(?,'delete',0,'{}')", (job,))
        conn.commit()
    return {'removed': True, 'permanent': mode == 'permanent', 'job_id': job, 'title': book['title']}


def managed_path(root, value):
    root = Path(root).resolve()
    value = str(value or '')
    if value.startswith('/data/'):
        value = value[6:]
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    # Never follow a symlink into another book/storage tree.
    if path.is_symlink():
        raise ValueError('Refusing symlink during book cleanup.')
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('Book file is outside this instance storage; preserved.')
    relative = resolved.relative_to(root)
    if not relative.parts or relative.parts[0] not in {'books', 'covers', 'audio', 'exports', 'audiobook-exports'}:
        raise ValueError('Refusing cleanup of an unmanaged file.')
    return resolved


def shared_file(conn, root, path, book_id):
    relative = str(path.relative_to(root))
    # Preserve references belonging to ANY other book or legacy chapter.
    for row in conn.execute('SELECT id,file_hash,original_path,cover_path FROM books WHERE id!=?', (book_id,)):
        if row['cover_path'] and row['cover_path'].startswith('covers/') and managed_path(root, row['cover_path']) == path:
            return True
        if original_path(root, row) == path:
            return True
    for table, field, condition in [('audio_variants', 'path', 'book_id!=?'), ('epub_exports', 'path', 'book_id!=?'), ('audiobook_exports', 'path', 'book_id!=?')]:
        for row in conn.execute(f'SELECT {field} FROM {table} WHERE {condition}', (book_id,)):
            if row[0] and managed_path(root, row[0]) == path:
                return True
    for row in conn.execute('SELECT a.audio_path FROM library_audio_assets a JOIN book_sections s ON s.id=a.section_id WHERE s.book_id!=?', (book_id,)):
        if row[0] and managed_path(root, row[0]) == path:
            return True
    # Loose legacy audio can exist without a registered library asset.
    for row in conn.execute('SELECT audio_path FROM chapters WHERE id NOT IN (SELECT legacy_chapter_id FROM book_sections WHERE book_id=? AND legacy_chapter_id IS NOT NULL)', (book_id,)):
        if row[0] and managed_path(root, row[0]) == path:
            return True
    for row in conn.execute('SELECT audio_path FROM audio_segments WHERE chapter_id NOT IN (SELECT legacy_chapter_id FROM book_sections WHERE book_id=? AND legacy_chapter_id IS NOT NULL)',(book_id,)):
        if row[0] and managed_path(root,row[0])==path:
            return True
    return False


def purge_step(worker, item):
    """One serial worker owns deletion; interrupted file cleanup resumes safely."""
    root = worker.root.resolve()
    deletion_id, book_id = item['job_id'], item['book_id']
    worker.checkpoint(item)
    with connect(worker.database) as conn:
        audit = conn.execute('SELECT * FROM book_deletions WHERE id=?', (deletion_id,)).fetchone()
        book = conn.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
        if not book:
            return True
        if audit['status'] == 'pending':
            paths = []
            original = original_path(root, book)
            if original:
                paths.append(original)
            if book['cover_path']:
                # EPUB internal resources belong to its original archive.
                if book['cover_path'].startswith('covers/'):
                    paths.append(managed_path(root, book['cover_path']))
            for table, field in [('audio_variants', 'path'), ('epub_exports', 'path'), ('audiobook_exports', 'path')]:
                paths.extend(managed_path(root, r[0]) for r in conn.execute(f'SELECT {field} FROM {table} WHERE book_id=?', (book_id,)) if r[0])
            paths.extend(managed_path(root, r[0]) for r in conn.execute('SELECT a.audio_path FROM library_audio_assets a JOIN book_sections s ON s.id=a.section_id WHERE s.book_id=?', (book_id,)) if r[0])
            paths.extend(managed_path(root, r[0]) for r in conn.execute('SELECT a.audio_path FROM audio_segments a JOIN book_sections s ON s.legacy_chapter_id=a.chapter_id WHERE s.book_id=?', (book_id,)) if r[0])
            # Flat, book-scoped cache directories include chunks and assembly logs.
            for folder in (root / 'audio' / 'edge' / book_id, root / 'covers' / book_id):
                if folder.is_symlink():
                    raise ValueError('Refusing symlinked book directory.')
                if folder.is_dir():
                    for child in folder.iterdir():
                        worker.checkpoint(item)
                        if child.is_dir() and not child.is_symlink():
                            paths.extend(managed_path(root, p) for p in child.iterdir() if p.is_file())
                        elif child.is_file():
                            paths.append(managed_path(root, child))
            with conn:
                for path in paths:
                    if not shared_file(conn, root, path, book_id):
                        conn.execute('INSERT OR IGNORE INTO deletion_files(deletion_id,path) VALUES(?,?)', (deletion_id, str(path)))
                conn.execute("UPDATE book_deletions SET status='prepared' WHERE id=?", (deletion_id,))
        pending = conn.execute("SELECT path FROM deletion_files WHERE deletion_id=? AND status='pending' LIMIT 16", (deletion_id,)).fetchall()
    for row in pending:
        worker.checkpoint(item)
        path = managed_path(root, row['path'])
        with connect(worker.database) as conn:
            # Recheck shared references before each unlink, including concurrent edits.
            if not shared_file(conn, root, path, book_id):
                path.unlink(missing_ok=True)
            with conn:
                conn.execute("UPDATE deletion_files SET status='done' WHERE deletion_id=? AND path=?", (deletion_id, str(path)))
    with connect(worker.database) as conn:
        if conn.execute("SELECT 1 FROM deletion_files WHERE deletion_id=? AND status='pending'", (deletion_id,)).fetchone():
            return False
        with conn:
            conn.execute("UPDATE platform_jobs SET status='cancelled' WHERE book_id=? AND id!=? AND status IN ('queued','running','paused','failed')",(book_id,deletion_id))
            # Historical jobs retain an audit trail but no foreign key to a deleted book.
            conn.execute('DELETE FROM discovery_links WHERE job_id IN (SELECT job_id FROM discovery_catalogs WHERE book_id=?)',(book_id,))
            conn.execute('DELETE FROM discovery_catalogs WHERE book_id=?',(book_id,))
            conn.execute('UPDATE platform_jobs SET book_id=NULL WHERE book_id=?', (book_id,))
            conn.execute('DELETE FROM audio_chunks WHERE asset_id IN (SELECT id FROM audio_variants WHERE book_id=?)', (book_id,))
            for table in ['listening_progress', 'book_bookmarks','reading_progress', 'audio_variants', 'import_chapters', 'import_sources', 'epub_exports', 'audiobook_exports', 'book_preferences', 'book_visibility', 'book_resources', 'book_navigation']:
                conn.execute(f'DELETE FROM {table} WHERE book_id=?', (book_id,))
            conn.execute('DELETE FROM library_audio_assets WHERE section_id IN (SELECT id FROM book_sections WHERE book_id=?)', (book_id,))
            legacy = [r[0] for r in conn.execute('SELECT legacy_chapter_id FROM book_sections WHERE book_id=? AND legacy_chapter_id IS NOT NULL', (book_id,))]
            conn.execute('DELETE FROM book_sections WHERE book_id=?', (book_id,))
            for chapter_id in legacy:
                conn.execute('DELETE FROM audio_segments WHERE chapter_id=?', (chapter_id,))
                conn.execute('DELETE FROM chapters WHERE id=?', (chapter_id,))
            conn.execute('DELETE FROM books WHERE id=?', (book_id,))
            conn.execute("UPDATE book_deletions SET status='completed' WHERE id=?", (deletion_id,))
    return True
