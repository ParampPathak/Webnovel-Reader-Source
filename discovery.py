"""Shared public catalog discovery for imports, source previews and updates.

Provider parsing and transport live in scraper/source_adapters, never in clients.
"""
import time
from contextlib import contextmanager
from contextvars import ContextVar
from source_adapters import canonical_source, chapter_identity
from safe_http import public_url

deadline = ContextVar('catalog_deadline', default=None)


def remaining(default=60):
    end = deadline.get()
    value = min(default, end-time.monotonic()) if end is not None else default
    if value <= 0:
        raise TimeoutError('Chapter discovery timed out; retry the new source.')
    return value


@contextmanager
def budget(seconds=60):
    token = deadline.set(time.monotonic()+seconds)
    try:
        yield
    finally:
        deadline.reset(token)


def discover_page(url):
    from scraper import browse_page
    # Keep pagination queries; the provider parser validates their scope.
    public_url(url, resolve=False)
    page = browse_page(url)
    unique = {}
    for item in page['links']:
        public_url(item['url'], resolve=False)
        key = tuple(chapter_identity(item['url'], item['title'])[k]
                    for k in ('source_host', 'source_chapter_id'))
        unique.setdefault(key, item)
    return {**page, 'links': list(unique.values())}


def discover_catalog(url, max_pages=100, max_links=10000):
    """Complete, bounded evidence; never publish a partial catalog as verified."""
    source = canonical_source(url)
    pending, visited, links, metadata = [source], set(), {}, None
    with budget():
        while pending:
            remaining()
            target = pending.pop(0)
            if target in visited:
                continue
            if len(visited) >= max_pages:
                raise ValueError('Chapter catalog exceeds the preview page budget; no source was changed.')
            page = discover_page(target)
            if canonical_source(page['url']) != source:
                raise ValueError('The new source redirected to a different book.')
            metadata = metadata or page
            visited.add(target)
            for item in page['links']:
                identity = chapter_identity(item['url'], item['title'])
                links.setdefault((identity['source_host'], identity['source_chapter_id']), item)
            if len(links) > max_links:
                raise ValueError('Chapter catalog exceeds the preview chapter budget.')
            for following in page.get('pages', []):
                if canonical_source(following) != source:
                    raise ValueError('Chapter pagination left the requested book.')
                if following not in visited and following not in pending:
                    pending.append(following)
    if not links:
        raise ValueError('The new source returned no public chapter links. Its index may be empty, stubbed, or changed.')
    return {**metadata, 'source_url': source, 'links': list(links.values()), 'pages_checked': len(visited)}
