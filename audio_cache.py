"""Bounded, conservative generated-audio maintenance. Never scan book contents.

The existing workers need no change: their atomic SQLite claims serialize with
eviction, and ANY running job stops eviction. Incomplete assets are preserved.
HTTP readers hold a shared file lock until their response closes. The collector
needs the exclusive lock and a SQLite write transaction before each asset.
"""
import argparse
import hashlib
import json
import logging
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from audio_providers import cache_key
from library_store import connect

GIB = 1024 ** 3
LOG = logging.getLogger(__name__)


def configuration():
    budget = float(os.environ.get('AUDIO_CACHE_LIMIT_GIB', '20'))
    if not 0 < budget <= 100000:
        raise ValueError('AUDIO_CACHE_LIMIT_GIB must be positive and finite.')
    return {'budget': int(budget * GIB), 'max_files': 256, 'max_bytes': 32 * 1024 ** 2,
            'max_seconds': 20, 'recent_seconds': 86400, 'keep_chapters': 10}


@contextmanager
def metadata(root):
    path = Path(root) / 'audio-cache.sqlite'
    if path.is_symlink():
        raise ValueError('Unsafe cache metadata path.')
    db = sqlite3.connect(path, timeout=2)
    db.execute('CREATE TABLE IF NOT EXISTS access (key TEXT PRIMARY KEY, book TEXT, section INTEGER, accessed REAL, hits INTEGER DEFAULT 1)')
    db.execute('CREATE TABLE IF NOT EXISTS eviction (key TEXT PRIMARY KEY, files TEXT NOT NULL)')
    db.commit()
    try:
        with db:
            yield db
    finally:
        db.close()


@contextmanager
def cache_lock(root, exclusive=False):
    import fcntl
    root = Path(root)
    descriptor = os.open(root / '.audio-cache.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        os.close(descriptor)


def touch(root, key, book=None, section=None):
    """Rate-limit recency writes; callers already hold the shared cache lock."""
    with metadata(root) as db:
        previous = db.execute('SELECT accessed FROM access WHERE key=?', (key,)).fetchone()
        now = time.time()
        if not previous or now - previous[0] >= 300:
            db.execute('INSERT INTO access(key,book,section,accessed) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET accessed=excluded.accessed,hits=hits+1,book=COALESCE(excluded.book,access.book),section=COALESCE(excluded.section,access.section)', (key, book, section, now))


def protect_audio_response(view):
    """Lock BEFORE endpoint lookup, until the HTTP body/range closes."""
    from functools import wraps
    from flask import current_app, make_response
    @wraps(view)
    def wrapped(*args, **kwargs):
        root = Path(current_app.config['DATA_DIR'])
        guard = cache_lock(root);guard.__enter__()
        try:
            response = make_response(view(*args, **kwargs))
            if response.status_code in (200, 206, 304):
                key = kwargs.get('asset_id')
                with connect(root / 'webnovel.db') as conn:
                    row = conn.execute('SELECT book_id,section_id FROM audio_variants WHERE id=?', (key,)).fetchone() if key else None
                # Legacy bytes still count and are retained; their access is logged.
                touch(root, key or 'legacy:' + view.__name__ + ':' + str(kwargs), row['book_id'] if row else None, row['section_id'] if row else None)
            # Direct passthrough bypasses Response.call_on_close callbacks.
            response.direct_passthrough = False
            response.call_on_close(lambda: guard.__exit__(None, None, None))
            return response
        except BaseException:
            guard.__exit__(None, None, None)
            raise
    return wrapped


def relative_audio(root, value):
    root = Path(root).absolute()
    path = Path(value or '')
    if str(path).startswith('/data/'):
        path = root / str(path)[6:]
    elif not path.is_absolute():
        path = root / path
    relative = path.relative_to(root)
    if relative.parts[0] != 'audio' or any(p in {'..', '.'} for p in relative.parts):
        raise ValueError('Not an approved audio cache path.')
    return relative


def safe_parent(root, relative):
    """Open each component without following links; unlink by directory FD."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def audio_inventory(root, check=lambda: None):
    total = 0
    files = {}
    errors = []
    base = Path(root) / 'audio'
    if base.is_symlink():
        raise ValueError('Refusing symlinked cache root.')
    for directory, folders, names in os.walk(base, followlinks=False):
        check()
        folders[:] = [name for name in folders if not (Path(directory) / name).is_symlink()]
        for name in names:
            path = Path(directory) / name
            item = path.lstat()
            if stat.S_ISREG(item.st_mode):
                files[str(path.relative_to(root))] = item.st_size
                total += item.st_size
            else:
                errors.append('Non-regular cache file preserved: ' + str(path.relative_to(root)))
            if len(files) > 100000:
                raise ValueError('Inventory bound exceeded; no eviction performed.')
    return total, files, errors


def busy(conn):
    # Also protects running exports/deletions/imports. Never interrupt workers.
    return bool(conn.execute("SELECT 1 FROM platform_items WHERE status='running' LIMIT 1").fetchone() or
                conn.execute("SELECT 1 FROM chapters WHERE status='processing' LIMIT 1").fetchone())


def protected(conn, key, book, section):
    if conn.execute('SELECT 1 FROM listening_progress WHERE book_id=? AND section_id=?', (book, section)).fetchone():
        return 'current listening chapter'
    # Paused/failed jobs may own resumable chunks; preserve them too.
    if conn.execute("SELECT 1 FROM platform_items i JOIN platform_jobs j ON j.id=i.job_id WHERE j.book_id=? AND j.kind='audio' AND i.item_key=? AND j.status IN ('queued','running','paused','failed') AND i.status!='done' LIMIT 1", (book, str(section))).fetchone():
        return 'generation/resumable job'
    return None


def candidate(conn, root, row, inventory):
    """Only complete, keyed Edge assets are proved regenerable by this worker.

    Legacy/Piper, orphan and partial files count toward the budget but are kept:
    their old preservation registry cannot safely be invalidated by eviction.
    Fail safely rather than pretending those bytes can be reclaimed.
    """
    if row['provider'] != 'edge' or row['status'] != 'ready':
        return None
    section = conn.execute('SELECT text_content FROM book_sections WHERE id=? AND book_id=?', (row['section_id'], row['book_id'])).fetchone()
    if not section: raise ValueError('Original chapter unavailable; preserved.')
    key, content_hash = cache_key(row['book_id'], row['section_id'], section[0], json.loads(row['settings']))
    if key != row['id'] or content_hash != row['content_hash']:
        raise ValueError('Asset identity/content mismatch.')
    directory = Path('audio') / 'edge' / row['book_id'] / key
    final = relative_audio(root, row['path'])
    if final != directory / 'chapter.mp3':
        raise ValueError('Noncanonical chapter path.')
    paths = [str(final)]
    checksums = {}
    chunks = conn.execute('SELECT * FROM audio_chunks WHERE asset_id=? ORDER BY position', (key,)).fetchall()
    if len(chunks) != row['chunk_count'] or not chunks:
        raise ValueError('Incomplete chunk registry.')
    for number, chunk in enumerate(chunks):
        expected = directory / f'{number:06}.mp3'
        if chunk['position'] != number or relative_audio(root, chunk['path']) != expected or not chunk['duration']:
            raise ValueError('Invalid published chunk.')
        paths.append(str(expected))
        checksums[str(expected)] = chunk['checksum']
    existing = {p for p in inventory if Path(p).parent == directory}
    # The existing assembler leaves these two diagnostic cache artifacts.
    # Keep them; they are never deletion targets and do not imply partial audio.
    # Cues are tiny, optional recording-bound diagnostics. Retain them like
    # assembler logs, without making all newly timed audio unevictable.
    cues={str(directory / f'{n:06}.timing.json') for n in range(len(chunks))}
    if existing - {str(directory / 'parts.ffconcat'), str(directory / 'assembly.log')} - cues != set(paths):
        raise ValueError('Incomplete or unregistered files in asset; preserved.')
    return paths, checksums


def complete_eviction(conn, key):
    # Preserve immutable chunk plan, voice/settings, section and all user data.
    conn.execute('DELETE FROM audio_chunks WHERE asset_id=?', (key,))
    conn.execute("UPDATE audio_variants SET path=NULL,status='pending',chunk_count=0 WHERE id=?", (key,))


def _cleanup(root, dry_run=True, config=None, check=lambda: None):
    root = Path(root).resolve()
    config = config or configuration()
    report = {'dry_run': dry_run, 'budget_bytes': config['budget'], 'before_bytes': 0, 'after_bytes': 0,
              'removed': [], 'planned': [], 'reclaimed_bytes': 0, 'protected': [], 'errors': [], 'complete': True}
    started = time.monotonic()
    try:
        with cache_lock(root, exclusive=True), metadata(root) as meta:
            total, inventory, errors = audio_inventory(root, check)
            report.update(before_bytes=total, after_bytes=total, errors=errors, inventory_files=len(inventory))
            with connect(root / 'webnovel.db') as conn:
                conn.execute('BEGIN IMMEDIATE')
                if busy(conn):
                    report['protected'].append({'reason': 'active generation/export; entire cache protected'})
                    conn.rollback()
                    return report
                # Durable intent makes a crash between unlink and commit recoverable.
                for key, encoded in meta.execute('SELECT key,files FROM eviction').fetchall():
                    if dry_run:
                        report['errors'].append('Pending eviction requires non-dry recovery: ' + key)
                        continue
                    row = conn.execute('SELECT book_id,section_id FROM audio_variants WHERE id=?', (key,)).fetchone()
                    if row and protected(conn, key, row['book_id'], row['section_id']):
                        report['errors'].append('Pending eviction now protected: ' + key)
                        continue
                    intent = json.loads(encoded)
                    expected_directory = Path('audio') / 'edge' / row['book_id'] / key if row else None
                    if not expected_directory or len(intent)>config['max_files']: raise ValueError('Invalid eviction journal.')
                    for value, signature in intent.items():
                        relative = relative_audio(root, value)
                        if relative.parent != expected_directory or not (relative.name == 'chapter.mp3' or (len(relative.name)==10 and relative.name[:6].isdigit() and relative.suffix=='.mp3')):
                            raise ValueError('Noncanonical recovery path.')
                        fd = safe_parent(root, relative)
                        try:
                            try:
                                item = os.stat(relative.name, dir_fd=fd, follow_symlinks=False)
                                if not stat.S_ISREG(item.st_mode): raise ValueError('Unsafe recovery target.')
                                if [item.st_ino,item.st_mtime_ns,item.st_size] != signature: raise ValueError('Recovery file changed; preserved.')
                                os.unlink(relative.name, dir_fd=fd)
                                report['removed'].append(str(relative))
                                report['reclaimed_bytes'] += item.st_size
                            except FileNotFoundError:
                                pass
                        finally:
                            os.close(fd)
                    complete_eviction(conn, key)
                    conn.commit()
                    meta.execute('DELETE FROM eviction WHERE key=?', (key,));meta.commit()
                    conn.execute('BEGIN IMMEDIATE')
                if report['removed']:
                    total, inventory, _ = audio_inventory(root, check)
                    report['after_bytes'] = total
                if total <= config['budget']:
                    conn.rollback()
                    return report
                access = {r[0]: r for r in meta.execute('SELECT key,book,section,accessed,hits FROM access')}
                recent = {}
                for value in sorted(access.values(), key=lambda r: r[3], reverse=True):
                    if value[1] and value[2]: recent.setdefault(value[1], [])
                    if value[1] and value[2] not in recent[value[1]] and len(recent[value[1]]) < config['keep_chapters']:
                        recent[value[1]].append(value[2])
                rows = conn.execute('SELECT * FROM audio_variants').fetchall()
                # Prefer last ten accessed chapters, then LRU, then lower access count.
                rows = sorted(rows, key=lambda r: (r['section_id'] in recent.get(r['book_id'], []), access.get(r['id'], (0,0,0,0,0))[3], access.get(r['id'], (0,0,0,0,0))[4]))
                selected_files = 0; selected_bytes = 0
                for row in rows:
                    check()
                    if total - selected_bytes <= config['budget']: break
                    if time.monotonic() - started >= config['max_seconds']:
                        report['complete'] = False;break
                    reason = protected(conn, row['id'], row['book_id'], row['section_id'])
                    recency = access.get(row['id'], (0,0,0,0,0))[3]
                    if time.time() - recency < config['recent_seconds']: reason = 'recent access/playback lease'
                    if reason:
                        report['protected'].append({'asset': row['id'], 'reason': reason});continue
                    try:
                        value = candidate(conn, root, row, inventory)
                        if not value:
                            report['protected'].append({'asset': row['id'], 'reason': 'partial/unproved regenerable asset'});continue
                        paths, checksums = value
                        size = sum(inventory[p] for p in paths)
                        if selected_files + len(paths) > config['max_files'] or selected_bytes + size > config['max_bytes']:
                            report['complete'] = False;continue
                        # Open/verify all deletion targets first, not partial success.
                        opened = [];intent = {}
                        try:
                            for name in paths:
                                relative = Path(name); fd = safe_parent(root, relative)
                                opened.append((relative, fd))
                                item = os.stat(relative.name, dir_fd=fd, follow_symlinks=False)
                                if not stat.S_ISREG(item.st_mode) or item.st_size != inventory[name]: raise ValueError('Cache file changed.')
                                intent[name]=[item.st_ino,item.st_mtime_ns,item.st_size]
                                if name in checksums:
                                    file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                                    with os.fdopen(file_fd, 'rb') as source:
                                        digest = hashlib.sha256()
                                        while block := source.read(128 * 1024):
                                            check();digest.update(block)
                                            if time.monotonic()-started > config['max_seconds']: raise TimeoutError('Verification time bound reached.')
                                            if not dry_run: time.sleep(len(block)/(4*1024*1024))
                                    if digest.hexdigest() != checksums[name]: raise ValueError('Chunk checksum mismatch.')
                            report['planned'].extend(paths)
                            if not dry_run:
                                meta.execute('INSERT INTO eviction(key,files) VALUES(?,?)', (row['id'], json.dumps(intent)));meta.commit()
                                for relative, fd in opened:
                                    os.unlink(relative.name, dir_fd=fd)
                                    report['removed'].append(str(relative))
                                    report['reclaimed_bytes'] += inventory[str(relative)]
                                complete_eviction(conn, row['id']);conn.commit()
                                meta.execute('DELETE FROM eviction WHERE key=?', (row['id'],));meta.commit()
                                conn.execute('BEGIN IMMEDIATE')
                            selected_files += len(paths);selected_bytes += size
                        finally:
                            for _, fd in opened: os.close(fd)
                    except (OSError, ValueError) as exc:
                        report['errors'].append(row['id'] + ': ' + str(exc))
                        # An incomplete durable intent must be recovered on next run.
                        if meta.execute('SELECT 1 FROM eviction LIMIT 1').fetchone(): break
                conn.rollback()
                report['after_bytes'] = report['before_bytes'] - report['reclaimed_bytes']
                report['projected_bytes'] = total - selected_bytes
                report['over_budget_bytes'] = max(0, report['projected_bytes'] - config['budget'])
                report['complete'] = report['complete'] and not report['over_budget_bytes'] and not report['errors']
    except BlockingIOError:
        report['protected'].append({'reason': 'active HTTP streaming/download or another cleanup'})
        report['complete'] = False
    return report


def cleanup(root, dry_run=True, config=None, check=lambda: None):
    report = _cleanup(root, dry_run, config, check)
    LOG.warning('audio_cache_maintenance %s', json.dumps(report, sort_keys=True))
    return report


def start_scheduler(application):
    if os.environ.get('AUDIO_CACHE_CLEANUP_ENABLED', '0') != '1': return
    config = configuration()
    root = application.config['DATA_DIR']
    def run():
        # No initial purge: the first maintenance run is at least a day later.
        while True:
            time.sleep(86400)
            try:
                from resource_guard import ResourceGuard
                guard = ResourceGuard()
                cleanup(root, dry_run=os.environ.get('AUDIO_CACHE_DRY_RUN', '1') != '0', config=config, check=guard.check)
            except Exception:
                LOG.exception('Audio-cache maintenance skipped safely')
    thread = threading.Thread(target=run, name='audio-cache-daily', daemon=True)
    thread.start()
    application.extensions['audio_cache_scheduler'] = thread


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', default=os.environ.get('DATA_DIR', '/data'))
    parser.add_argument('--apply', action='store_true', help='Apply one bounded batch; default is dry-run.')
    args = parser.parse_args()
    from resource_guard import ResourceGuard
    guard = ResourceGuard()
    print(json.dumps(cleanup(args.data_dir, dry_run=not args.apply, check=guard.check), indent=2))
