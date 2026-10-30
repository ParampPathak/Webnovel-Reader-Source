"""Bounded raster artwork validation, content caching and public-only retrieval."""
import hashlib
import io
import os
import tempfile
import warnings
import zipfile
from pathlib import Path
from library_store import connect
from storage_paths import original_path

MAX_BYTES = 5 * 1024 * 1024


def validate_image(payload):
    from PIL import Image
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError('Cover must be a JPEG, PNG or WebP image under 5 MiB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                kind = image.format
                if kind not in {'JPEG', 'PNG', 'WEBP'} or image.width * image.height > 20_000_000:
                    raise ValueError('Unsupported or oversized cover image.')
                image.verify()
        return {'JPEG': ('.jpg', 'image/jpeg'), 'PNG': ('.png', 'image/png'), 'WEBP': ('.webp', 'image/webp')}[kind]
    except Exception as exc:
        raise ValueError('Invalid JPEG, PNG or WebP cover image.') from exc


def store_cover(root, book_id, payload):
    from book_management import book_lock
    with book_lock(root,book_id):
        with connect(Path(root)/'webnovel.db') as conn:
            if not conn.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone() or conn.execute('SELECT 1 FROM book_visibility WHERE book_id=? AND hidden=1',(book_id,)).fetchone():
                raise ValueError('Book was removed. Restore it before changing artwork.')
        return _store_cover(root,book_id,payload)


def _store_cover(root, book_id, payload):
    root = Path(root)
    suffix, _ = validate_image(payload)
    relative = Path('covers') / book_id / (hashlib.sha256(payload).hexdigest() + suffix)
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        try:
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    with connect(root / 'webnovel.db') as conn:
        with conn:
            if not conn.execute('SELECT 1 FROM books WHERE id=?', (book_id,)).fetchone():
                raise ValueError('Book not found.')
            conn.execute('UPDATE books SET cover_path=? WHERE id=?', (relative.as_posix(), book_id))
    return relative.as_posix()


def fetch_cover(root, book_id, url):
    from safe_http import fetch, public_url
    from scraper import HEADERS, _check_robots
    public_url(url)
    _check_robots(url)
    response = fetch(url, headers=HEADERS, max_bytes=MAX_BYTES, redirect_check=_check_robots)
    media = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
    if media not in {'image/jpeg', 'image/jpg', 'image/pjpeg', 'image/png', 'image/webp', 'application/octet-stream'}:
        raise ValueError('Source cover did not return supported raster artwork.')
    return store_cover(root, book_id, response.content)


def refresh_cover(root, book_id, force=False):
    root = Path(root)
    with connect(root / 'webnovel.db') as conn:
        row = conn.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()
        if not row:
            raise ValueError('Book not found.')
        book = dict(row)
        source = conn.execute('SELECT url FROM import_sources WHERE book_id=?', (book_id,)).fetchone()
        # Legacy books may predate source registration. Use their first chapter.
        if not source:
            source = conn.execute('SELECT c.source_url FROM chapters c JOIN book_sections s ON s.legacy_chapter_id=c.id WHERE s.book_id=? ORDER BY s.position LIMIT 1', (book_id,)).fetchone()
    if book['cover_path'] and not force:
        return {'saved': True, 'cached': True}
    if book['source_kind'] == 'epub':
        from epub_library import parse_epub
        original = original_path(root, book)
        parsed = parse_epub(original, book_id, stream=True)
        if not parsed['cover']:
            raise ValueError('This EPUB does not contain an identifiable raster cover.')
        with zipfile.ZipFile(original) as archive:
            item = archive.getinfo(parsed['cover'])
            if item.file_size > MAX_BYTES:
                raise ValueError('Embedded cover exceeds 5 MiB.')
            store_cover(root, book_id, archive.read(item))
    else:
        if not source:
            raise ValueError('No original source URL is available. Upload a custom cover.')
        from source_adapters import canonical_source
        from discovery import discover_page as browse_page
        page = browse_page(canonical_source(source[0]))
        if not page.get('cover_url'):
            raise ValueError('The source does not expose a supported cover image.')
        fetch_cover(root, book_id, page['cover_url'])
    return {'saved': True, 'cached': False}
