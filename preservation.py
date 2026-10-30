"""Read-only source snapshots and verified backups. Never imports the application."""
import hashlib
import json
import os
import sqlite3
import stat
import time
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path


def digest(path, check=None, bytes_per_second=0):
    h = hashlib.sha256()
    started = time.monotonic()
    read_bytes = 0
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
            read_bytes += len(block)
            if bytes_per_second:
                time.sleep(max(0,read_bytes/bytes_per_second-(time.monotonic()-started)))
            if check:
                check()
    return h.hexdigest()


def snapshot_database(source, destination, *, allow_live_sidecars=False):
    """SQLite online backup when sidecars exist; otherwise an immutable read-only snapshot.

    A read-only WAL connection can still require creation of shared-memory files.
    Preservation callers never create sidecars in the source directory. The
    application startup may opt into normal online backup of its OWN writable
    database, avoiding a race with its background worker.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or destination.exists():
        raise ValueError('Backup destination must be a new file distinct from the source.')
    before = source.stat()
    wal = source.with_name(source.name + '-wal')
    active_wal = wal.exists() and wal.stat().st_size > 0
    if active_wal or allow_live_sidecars:
        shm = source.with_name(source.name + '-shm')
        if active_wal and not shm.exists() and not allow_live_sidecars:
            raise ValueError('Active WAL without shared memory: use a database-aware backup inside the container.')
        connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    else:
        # immutable prevents any journal/SHM creation; only valid with no active WAL.
        connection = sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(destination)) as target:
            connection.backup(target)
            result = target.execute('PRAGMA integrity_check').fetchone()[0]
            if result != 'ok':
                raise ValueError('Backup integrity check failed: ' + result)
    finally:
        connection.close()
    if not active_wal and not allow_live_sidecars:
        after = source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or (wal.exists() and wal.stat().st_size):
            raise ValueError('Database changed during immutable snapshot; retry with an online backup.')
    return destination


def backup_tree(source, destination, *, bytes_per_second=8*1024*1024, monitor=True):
    """Copy readable files with checksums; explicitly report every inaccessible path.

    Source is never opened for writing. This is a file backup, not an atomic
    filesystem snapshot. Freeze writers or use a storage snapshot for rollout.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or source in destination.parents or destination.exists():
        raise ValueError('Use a new backup directory outside the source tree.')
    destination.mkdir(parents=True)
    from resource_guard import ResourceGuard, ResourcePressure
    guard = ResourceGuard() if monitor else None
    manifest = {'source': str(source), 'created_at': datetime.now(timezone.utc).isoformat(),
                'files': [], 'errors': [], 'excluded_runtime': [], 'complete': False}

    def error(exc):
        manifest['errors'].append({'path': exc.filename, 'error': str(exc)})

    stopped = False
    for root, directories, files in os.walk(source, onerror=error, followlinks=False):
        for name in list(directories):
            path = Path(root) / name
            if path.is_symlink():
                directories.remove(name)
                manifest['errors'].append({'path': str(path), 'error': 'Symlink not followed'})
        for name in files:
            if stopped:
                break
            path = Path(root) / name
            relative = path.relative_to(source)
            if str(relative) in ('webnovel.db-wal', 'webnovel.db-shm'):
                continue
            target = destination / relative
            try:
                if relative == Path('.platform-wake.sock') and stat.S_ISSOCK(path.lstat().st_mode):
                    # This process-local notification socket contains no stored
                    # application state and cannot be restored as a regular file.
                    manifest['excluded_runtime'].append({'path':str(relative),'kind':'unix_socket'})
                    continue
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError('Only regular files can be backed up')
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative == Path('webnovel.db'):
                    snapshot_database(path, target)
                    checksum = digest(target)
                else:
                    before = path.stat()
                    h = hashlib.sha256()
                    started = time.monotonic()
                    copied_bytes = 0
                    with path.open('rb') as src, target.open('xb') as dst:
                        for block in iter(lambda: src.read(1024 * 1024), b''):
                            h.update(block)
                            dst.write(block)
                            copied_bytes += len(block)
                            if bytes_per_second:
                                time.sleep(max(0,copied_bytes/bytes_per_second-(time.monotonic()-started)))
                            if guard:
                                guard.check()
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError('Source changed while being copied')
                    checksum = h.hexdigest()
                    if digest(target,guard.check if guard else None,bytes_per_second) != checksum:
                        raise ValueError('Copy checksum mismatch')
                manifest['files'].append({'path': str(relative), 'bytes': target.stat().st_size,
                                          'sha256': checksum})
            except ResourcePressure as exc:
                manifest['errors'].append({'path': str(relative), 'error': str(exc)})
                stopped = True
            except (OSError, ValueError, sqlite3.Error) as exc:
                manifest['errors'].append({'path': str(relative), 'error': str(exc)})
        if stopped:
            break
    if guard:
        manifest['resource_summary'] = guard.summary()
    manifest['complete'] = not manifest['errors']
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest


def verify_backup(directory, *, bytes_per_second=8*1024*1024, monitor=True):
    from resource_guard import ResourceGuard
    guard = ResourceGuard() if monitor else None
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'manifest.json').read_text())
    failures = list(manifest['errors'])
    for entry in manifest['files']:
        path = (directory / entry['path']).resolve()
        if directory not in path.parents:
            failures.append({'path':entry['path'],'error':'Invalid backup manifest path'})
            continue
        if not path.is_file() or digest(path,guard.check if guard else None,bytes_per_second) != entry['sha256']:
            failures.append({'path': entry['path'], 'error': 'Missing file or checksum mismatch'})
    return failures
