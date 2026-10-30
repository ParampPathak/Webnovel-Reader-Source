"""Non-destructive source previews and chapter aliases; never fetch the old source."""
import hashlib
import json
import re
import time
import unicodedata
import uuid
from collections import defaultdict
from library_store import connect


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value or '').casefold() if c.isalnum())


def title_key(title):
    title = unicodedata.normalize('NFKC', title).casefold()
    match = re.match(r'^\s*(?:chapter|chap\.?|episode|part)\s+(\d+(?:\.\d+)?)\s*[:.\-–—]?\s*', title)
    return normalized(title[match.end():] if match else title), match[1] if match else None


def sections(c, book):
    return [dict(r) for r in c.execute('SELECT id,position,title,href FROM book_sections WHERE book_id=? ORDER BY position', (book,))]


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def match_catalog(existing, links, provider, key):
    from chapter_updates import chapter_id
    titles, identities = defaultdict(list), {}
    for item in existing:
        body, number = title_key(item['title'])
        if body:
            titles[body].append(item)
        try:
            identities[chapter_id(item['href'], provider, key)] = item
        except ValueError:
            pass
    incoming = defaultdict(int)
    for link in links:
        incoming[title_key(link['title'])[0]] += 1
    matches, used, previous = [], set(), -1
    for link in links:
        identifier = chapter_id(link['url'], provider, key)
        old = identities.get(identifier)
        method = 'stable source identifier'
        if old is None:
            body, number = title_key(link['title'])
            candidates = titles[body] if body else []
            if len(candidates) > 1 and number:
                candidates = [r for r in candidates if title_key(r['title'])[1] == number]
            # Number alone never establishes equivalence. Repeated ambiguous
            # titles remain unmatched rather than guessing a section or offset.
            if len(candidates) == 1 and (incoming[body] == 1 or number):
                old = candidates[0]
            method = 'unique title and ordered sequence'
        if old is None:
            continue
        if old['id'] in used or old['position'] <= previous:
            raise ValueError('New catalog has ambiguous or reordered chapter matches. No chapters were changed.')
        used.add(old['id']); previous = old['position']
        matches.append({'source_chapter_id': identifier, 'url': link['url'],
                        'section_id': old['id'], 'position': old['position'],
                        'title': link['title'], 'method': method})
    return matches


def preview(path, book_id, data):
    from chapter_updates import identity, chapter_id, SourceConflict, source_error
    from discovery import discover_catalog
    source, provider, key = identity(data.get('source_url'))
    with connect(path) as c:
        book = c.execute('SELECT title,author FROM books WHERE id=?', (book_id,)).fetchone()
        if not book:
            raise ValueError('Book not found.')
        row = c.execute('SELECT config_revision FROM chapter_updates WHERE book_id=?', (book_id,)).fetchone()
        revision = row[0] if row else 0
        existing = sections(c, book_id)
    if data.get('config_revision', revision) != revision:
        raise SourceConflict('Source settings changed in another client. Reload and retry.')
    try:
        catalog = discover_catalog(source)
    except Exception as exc:
        raise ValueError(source_error(exc)) from exc
    links = catalog['links']
    # Validate every chapter against this provider/book, not just HTTP 200.
    for link in links:
        chapter_id(link['url'], provider, key)
    warning = ''
    try:
        matches = match_catalog(existing, links, provider, key)
    except ValueError as exc:
        matches = []; warning = str(exc)
    metadata = normalized(book['title']) == normalized(catalog.get('title'))
    if book['author'] and catalog.get('author'):
        metadata = metadata and normalized(book['author']) == normalized(catalog['author'])
    tail = bool(existing and matches and matches[-1]['section_id'] == existing[-1]['id'])
    confident = bool(metadata and tail and (len(matches) >= 3 or len(matches) == len(existing)))
    # If overlap cannot be established, confirmation only sets a future baseline;
    # it never invents mappings or fills historical gaps by chapter number.
    baseline = matches[-1]['source_chapter_id'] if confident else chapter_id(links[-1]['url'], provider, key)
    evidence = {'source_url': source, 'provider': provider, 'key': key,
                'catalog_title': catalog.get('title', ''), 'author': catalog.get('author', ''),
                'available': len(links), 'matches': matches if confident else [],
                'existing_count': len(existing), 'fingerprint': fingerprint(existing),
                'baseline': baseline, 'needs_confirmation': not confident,
                'warning': warning, 'pages_checked': catalog['pages_checked']}
    token = uuid.uuid4().hex
    with connect(path) as c, c:
        current = c.execute('SELECT config_revision FROM chapter_updates WHERE book_id=?', (book_id,)).fetchone()
        if (current[0] if current else 0) != revision or fingerprint(sections(c, book_id)) != evidence['fingerprint']:
            raise SourceConflict('Book/source changed during validation. Review a new preview.')
        c.execute('DELETE FROM source_replacement_previews WHERE created_at<?', (time.time()-900,))
        c.execute('INSERT INTO source_replacement_previews VALUES(?,?,?,?,?,?)',
                  (token, book_id, revision, source, time.time(), json.dumps(evidence)))
    return {'preview_id': token, 'source_url': source, 'title': evidence['catalog_title'],
            'author': evidence['author'], 'available': len(links), 'matched': len(evidence['matches']),
            'unmatched': len(links)-len(evidence['matches']), 'existing_count': len(existing),
            'needs_confirmation': not confident, 'warning': warning,
            'baseline': baseline, 'pages_checked': catalog['pages_checked'],
            'message': ('Verified chapter overlap, including the last imported chapter. Existing content stays unchanged.' if confident else
                        'Overlap cannot be established confidently. Confirm to use this source only for releases after the last chapter currently listed; no historical chapters will be mapped or imported.')}


def apply(c, row, book_id, source, data):
    from chapter_updates import SourceConflict
    record = c.execute('SELECT * FROM source_replacement_previews WHERE id=? AND book_id=?',
                       (data.get('preview_id'), book_id)).fetchone()
    if not record or record['source_url'] != source or time.time()-record['created_at'] > 900:
        raise ValueError('Validate the new source first; its preview is missing or expired.')
    evidence = json.loads(record['evidence'])
    if record['revision'] != row.get('config_revision', 0) or fingerprint(sections(c, book_id)) != evidence['fingerprint']:
        raise SourceConflict('Book/source changed after validation. Review a new preview.')
    if evidence['needs_confirmation'] and data.get('confirm_baseline') is not True:
        raise ValueError('Confirm the future-only baseline shown in the preview before saving this unmatched source.')
    for item in evidence['matches']:
        c.execute('INSERT INTO source_chapter_matches(book_id,source_url,source_chapter_id,url,section_id) VALUES(?,?,?,?,?) ON CONFLICT(book_id,source_url,source_chapter_id) DO UPDATE SET url=excluded.url,section_id=excluded.section_id',
                  (book_id, source, item['source_chapter_id'], item['url'], item['section_id']))
    c.execute('DELETE FROM chapter_update_items WHERE book_id=?', (book_id,))
    c.execute('INSERT INTO chapter_updates(book_id,source_url,provider,source_id) VALUES(?,?,?,?) ON CONFLICT(book_id) DO NOTHING',
              (book_id, source, evidence['provider'], evidence['key']))
    c.execute('UPDATE chapter_updates SET source_url=?,provider=?,source_id=?,source_title=?,checkpoint=?,available=?,last_checked=?,last_success=? WHERE book_id=?',
              (source, evidence['provider'], evidence['key'], evidence['catalog_title'], evidence['baseline'], evidence['available'], time.time(), time.time(), book_id))
    c.execute('DELETE FROM source_replacement_previews WHERE id=?', (record['id'],))
