"""Canonical source identities and scoped catalog adapters (no network access)."""
import re
from urllib.parse import urlsplit,urlunsplit
from safe_http import public_url


def source_policy(url):
    host=urlsplit(url).hostname.lower().rstrip('.').removeprefix('www.')
    reasons={'wuxiaworld.com':'Automated collection requires source permission.',
             'novelfire.net':'The source terms prohibit scraping.',
             'ranobes.net':'Public access returned HTTP 503; no verified complete adapter.',
             'scribblehub.com':'The full paginated catalog workflow is not yet verified.'}
    for blocked,reason in reasons.items():
        if host==blocked or host.endswith('.'+blocked):raise ValueError('Source not supported: '+reason)


def canonical_source(url):
    public_url(url,resolve=False)
    parsed=urlsplit(url)
    host=parsed.hostname.lower().removeprefix('www.')
    source_policy(url)
    if host=='novelfull.com':
        match=re.match(r'^/([^/]+?)(?:\.html|/chapter-[^/]+\.html)/?$',parsed.path)
        if not match: raise ValueError('Paste a NovelFull novel or chapter URL.')
        return 'https://novelfull.com/'+match[1]+'.html'
    if host=='freewebnovel.com':
        path=re.sub(r'/chapter-\d+/?$','',parsed.path).rstrip('/')
        return urlunsplit(('https','freewebnovel.com',path,'',''))
    if host=='royalroad.com':
        match=re.match(r'^/fiction/(\d+)(?:/|$)',parsed.path)
        if match: return 'https://www.royalroad.com/fiction/'+match.group(1)
    # Generic URLs may need their query string for discovery; never guess it away.
    return parsed._replace(fragment='').geturl()


def chapter_identity(url,title):
    parsed=urlsplit(url);host=parsed.hostname.lower().removeprefix('www.')
    match=re.search(r'/chapter/(\d+)(?:/|$)',parsed.path) if host=='royalroad.com' else None
    number=re.search(r'\b(?:chapter|chap\.?|episode|part)\s+(\d+(?:\.\d+)?)\b',title,re.I)
    return dict(source_chapter_id=match[1] if match else parsed.path,source_number=number[1] if number else None,source_host=host)


def retained_chapter_url(conn,book_id,url):
    """A renamed RR URL must retain the existing section/cache/progress identity."""
    identity=chapter_identity(url,'')
    source=canonical_source(url)
    try:
        from chapter_updates import identity as source_identity, chapter_id
        _,provider,key=source_identity(source)
        match_id=chapter_id(url,provider,key)
    except ValueError:
        match_id=identity['source_chapter_id']
    matched=conn.execute('SELECT s.href FROM source_chapter_matches m JOIN book_sections s ON s.id=m.section_id WHERE m.book_id=? AND m.source_url=? AND m.source_chapter_id=?',
                         (book_id,source,match_id)).fetchone()
    if matched:return matched[0]
    if identity['source_host']!='royalroad.com' or not identity['source_chapter_id'].isdigit():return url
    chapter=identity['source_chapter_id']
    row=conn.execute("SELECT url FROM import_chapters WHERE book_id=? AND (url LIKE ? OR url LIKE ?) ORDER BY section_id IS NOT NULL DESC,position LIMIT 1",
                     (book_id,'https://www.royalroad.com/%/chapter/'+chapter+'/%','https://www.royalroad.com/%/chapter/'+chapter)).fetchone()
    return row[0] if row else url


def catalog_metadata(soup,url):
    host=urlsplit(url).hostname.lower().removeprefix('www.')
    selectors={
        'royalroad.com':('.fic-title h1, .fiction-title h1','.fic-title a[href^="/profile/"], .fiction-title a[href^="/profile/"]','.cover-art-container img[data-type="cover"], .fiction-cover img','.description .hidden-content, .fiction-description'),
        'novelfull.com':('h3.title','.info a[href^="/author/"]','.book img','.desc-text'),
    }
    if host not in selectors:return {}
    title,author,cover,description=selectors[host]
    text=lambda sel: soup.select_one(sel).get_text(' ',strip=True) if soup.select_one(sel) else ''
    image=soup.select_one(cover)
    from urllib.parse import urljoin
    return dict(title=text(title),author=', '.join(dict.fromkeys(n.get_text(' ',strip=True) for n in soup.select(author))),
                description=text(description),cover_url=urljoin(url,image['src']) if image and image.get('src') else None)


def novelfull_catalog(soup,url):
    """Follow only the next catalog page; never recommendations or chapter text."""
    from urllib.parse import urljoin,parse_qs
    parsed=urlsplit(url);slug=parsed.path.removesuffix('.html').strip('/')
    links=[];seen=set()
    for a in soup.select('.list-chapter a[href]'):
        target=urljoin(url,a['href']);p=urlsplit(target)
        if p.hostname!=parsed.hostname or not p.path.startswith('/'+slug+'/') or target in seen:continue
        seen.add(target);links.append(dict(url=target,title=a.get_text(' ',strip=True)))
    if not links:raise ValueError('NovelFull returned no public chapter list; discovery is incomplete.')
    pages=[];next_link=soup.select_one('.pagination li.next:not(.disabled) a[href]')
    if next_link:
        target=urljoin(url,next_link['href']);p=urlsplit(target)
        current=int(parse_qs(parsed.query).get('page',['1'])[0]);following=int(parse_qs(p.query).get('page',['1'])[0])
        if p.hostname!=parsed.hostname or p.path!=parsed.path or following!=current+1:
            raise ValueError('NovelFull pagination did not advance exactly one catalog page.')
        pages=[target]
    return links,pages
