import json
import re
import time
import threading
from urllib.robotparser import RobotFileParser
from urllib.parse import urljoin, urlparse, parse_qs, urlencode

from bs4 import BeautifulSoup

HEADERS = {'User-Agent':'WebnovelAudio/1.0 (self-hosted personal EPUB importer)',
           'Accept':'text/html,application/xhtml+xml','Accept-Language':'en-US,en;q=0.9'}
_robots_cache = {}


def _check_robots(url):
    from source_adapters import source_policy
    source_policy(url)
    from safe_http import fetch
    parsed=urlparse(url);key=parsed.scheme+'://'+parsed.netloc
    cached=_robots_cache.get(key)
    if cached and time.monotonic()-cached[0]<900:
        rules=cached[1]
    else:
        rules=RobotFileParser()
        try:
            response=fetch(key+'/robots.txt',HEADERS,max_bytes=256*1024)
            rules.parse(response.text.splitlines())
        except ValueError as exc:
            if 'HTTP 404' in str(exc) or 'HTTP 410' in str(exc): rules.parse([])
            elif 'HTTP 401' in str(exc) or 'HTTP 403' in str(exc): rules.parse(['User-agent: *','Disallow: /'])
            else: raise ValueError('Could not verify source robots.txt restrictions: '+str(exc)) from exc
        if len(_robots_cache)>=128: _robots_cache.pop(next(iter(_robots_cache)))
        _robots_cache[key]=(time.monotonic(),rules)
    if not rules.can_fetch('WebnovelAudio',url):
        raise ValueError('This source disallows this URL in robots.txt; importing is not permitted.')
    return rules.crawl_delay('WebnovelAudio') or rules.crawl_delay('*') or 1


_fetch_lock = threading.Lock()
_last_fetch_by_host = {}

# Many independent "webnovel aggregator" sites (FreeWebNovel and its many clones,
# NovelBin-family sites, etc.) reuse a handful of template CMSes. Covering their
# common container class/id names here, in addition to the generic scoring
# fallback below, meaningfully raises the odds a random site "just works".
CONTENT_SELECTORS = [
    ".chapter-content",
    "#chapter-content",
    "#chapter_content",
    ".chapter_content",
    ".chapter-body",
    ".chapter_body",
    ".chapter-text",
    ".chapter_text",
    ".chapter-inner",
    ".chapter-c",
    ".cha-content",
    ".cha-words",
    ".reading-content",
    ".entry-content",
    ".post-content",
    ".article-content",
    ".text-content",
    "#chr-content",
    "#chapterText",
    "#article",
    ".article",
    ".txt",
    "#htmlContent",
    ".chr-c",
    "#chp_raw",
    ".chp_raw",
    "#storytext",
    ".storytext",
    "#chapter-container",
    ".text-left",
    ".par",
    "#arrticle",
    ".chr-c",
    ".content-text",
    ".box-body",
    ".panel-body",
    ".read-content",
    ".story",
    ".novel-content",
    "#content",
    ".content",
    "article",
    "main",
]

# Signatures of a JS/bot-challenge interstitial (Cloudflare, etc). Static requests
# can never get past these; detecting them lets us give an honest error instead of
# silently returning junk or timing out.
ANTIBOT_MARKERS = (
    "checking your browser",
    "cf-browser-verification",
    "just a moment...",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "ddos protection by",
    "verifying you are human",
    "cf-chl-",
)

JUNK_RE = re.compile(
    r"(?:comment|footer|header|nav|menu|sidebar|share|social|advert|ads?|banner|promo|"
    r"cookie|modal|popup|related|recommend|breadcrumb|pagination|toolbar|widget|login)",
    re.I,
)

NEXT_TEXT_RE = re.compile(
    r"^\s*(?:next(?:\s+(?:chapter|chap|episode|part))?|(?:chapter|chap|episode|part)\s+next)\s*[›»→>]*\s*$",
    re.I,
)

NOVEL_WORD_RE = re.compile(r"\b(?:chapter|chap\.?|episode|part)\s*\d+\b", re.I)


def _fetch(url):
    from safe_http import fetch
    from discovery import remaining
    spacing=max(1,_check_robots(url))
    if spacing>60: raise ValueError('Source requests a crawl delay above 60 seconds; automatic importing is disabled for this source.')
    host = urlparse(url).netloc.lower()
    # All adapters share polite same-host spacing and the SSRF-safe transport.
    if not _fetch_lock.acquire(timeout=remaining(10)):
        raise TimeoutError('Source request queue is busy. Retry shortly.')
    try:
        delay = spacing-(time.monotonic()-_last_fetch_by_host.get(host,0))
        if delay>0:
            if delay>=remaining():raise TimeoutError('Source crawl delay exceeds the remaining discovery budget.')
            time.sleep(delay)
        _last_fetch_by_host[host] = time.monotonic()
    finally:_fetch_lock.release()
    return fetch(url,HEADERS,redirect_check=_check_robots)


def scrape_chapter(url):
    resp = _fetch(url)

    content_type = resp.headers.get("Content-Type", "").lower()
    if content_type and "html" not in content_type and "xhtml" not in content_type:
        raise ValueError("That URL did not return an HTML chapter page.")

    if len(resp.text.strip()) < 200:
        raise ValueError("The site returned an unusually small page; it may require JavaScript or block automated requests.")

    sniff = resp.text[:4000].lower()
    if any(marker in sniff for marker in ANTIBOT_MARKERS):
        raise ValueError(
            "This site is showing a bot-protection challenge (e.g. Cloudflare) instead of the chapter. "
            "Static scraping can't get past that kind of JavaScript challenge, so this site isn't supported."
        )

    soup = BeautifulSoup(resp.text, "html.parser")
    final_url = resp.url or url
    # HTTP status errors are rejected by safe_http. Also reject common soft-404
    # templates returned with HTTP200, without matching chapter titles about404.
    error_heading=re.compile(r'(?:error\s*)?(?:404\s*[-:–—]?\s*(?:page\s*)?not found|403\s*[-:–—]?\s*forbidden|(?:page|chapter) (?:not found|unavailable)|access denied)',re.I)
    for heading in soup.select('h1, .error-title'):
        if error_heading.fullmatch(heading.get_text(' ',strip=True)):
            raise ValueError('Source returned an unavailable/error page with HTTP200: '+heading.get_text(' ',strip=True))

    host = urlparse(final_url).netloc.lower().removeprefix("www.")
    if "royalroad.com" in host:
        data = _scrape_royalroad(soup, final_url)
    elif host == "freewebnovel.com":
        data = _scrape_freewebnovel(soup, final_url)
    else:
        data = _scrape_generic(soup, final_url)

    content = (data.get("content") or "").strip()
    if len(content) < 120:
        raise ValueError(
            "Could not reliably find the chapter text. The site may render chapters with JavaScript, require a login, or use anti-bot protection."
        )
    data["content"] = content
    return data


def _freewebnovel_next_fallback(url):
    parsed = urlparse(url)
    match = re.search(r"^(.*?/novel/[^/]+)/chapter-(\d+)(?:/)?$", parsed.path, re.I)
    if not match:
        return None
    next_path = f"{match.group(1)}/chapter-{int(match.group(2)) + 1}"
    return parsed._replace(path=next_path, query="", fragment="").geturl()


def _scrape_freewebnovel(soup, url):
    """FreeWebNovel has a stable .txt/#htmlContent reader template.

    Treat it explicitly instead of relying on generic scoring. This also makes
    Follow Next robust when the site's Next button markup changes, because its
    chapter URLs are sequential (/chapter-1, /chapter-2, ...).
    """
    container = soup.select_one(".txt, #htmlContent, .chapter-content")
    if not container:
        raise ValueError("Could not find FreeWebNovel's chapter text container.")

    chapter_title = _first_text(
        container.find("h4"),
        soup.select_one("h4"),
        soup.select_one(".chapter-title"),
    )
    if not chapter_title:
        doc = _document_title(soup) or ""
        m = re.search(r"(?:^| - )(Chapter\s+\d+[^|]*?)(?:\s*\|\s*Free Web Novel|$)", doc, re.I)
        chapter_title = m.group(1).strip() if m else doc

    novel_title = None
    # The reader page exposes the book link in its breadcrumb/title area.
    for tag in soup.select("a[href*='/novel/']"):
        href = tag.get("href", "")
        text = _clean_text(tag.get_text(" ", strip=True))
        if text and not re.search(r"/chapter-\d+", href, re.I) and not NOVEL_WORD_RE.search(text):
            novel_title = text
            break
    if not novel_title:
        for tag in soup.find_all(["h1", "h2"], limit=8):
            text = _clean_text(tag.get_text(" ", strip=True))
            if text and text.lower() != (chapter_title or "").lower() and not NOVEL_WORD_RE.search(text):
                novel_title = text
                break
    if not novel_title:
        doc = _document_title(soup) or ""
        novel_title = re.split(r"\s+-\s+Chapter\s+\d+", doc, maxsplit=1, flags=re.I)[0].strip()

    # Work on a clone so stripping headings/ads does not disturb navigation.
    body = BeautifulSoup(str(container), "html.parser")
    for tag in body.select("script, style, .ads, .advertisement, h4"):
        tag.decompose()
    content = _extract_text(body)
    next_url = _find_next_url(soup, url) or _freewebnovel_next_fallback(url)

    return {
        "novel_title": _clean_title(novel_title or "FreeWebNovel"),
        "chapter_title": _clean_title(chapter_title or "Unknown Chapter"),
        "content": content,
        "next_url": next_url,
    }


def _scrape_royalroad(soup, url):
    content_div = soup.find("div", class_="chapter-content")
    if not content_div:
        raise ValueError("Could not find Royal Road chapter content; the page structure may have changed.")

    content = _extract_text(content_div)
    chapter_title = _first_text(
        soup.select_one("h1.font-white"),
        soup.select_one(".fic-header h1"),
        soup.find("h1"),
    ) or _document_title(soup) or "Unknown Chapter"

    novel_title = _royalroad_novel_title(soup, chapter_title)
    next_url = _find_next_url(soup, url)

    return {
        "novel_title": novel_title,
        "chapter_title": chapter_title,
        "content": content,
        "next_url": next_url,
    }


def _royalroad_novel_title(soup, chapter_title):
    # Royal Road usually exposes the fiction title in the breadcrumb / fiction link.
    for selector in (
        ".breadcrumb a[href*='/fiction/']",
        "a[href*='/fiction/'][class*='font']",
        ".fic-header h2",
    ):
        matches = soup.select(selector)
        for tag in reversed(matches):
            text = _clean_text(tag.get_text(" ", strip=True))
            if text and text.lower() != chapter_title.lower() and "royal road" not in text.lower():
                return text

    title = _document_title(soup)
    if title:
        title = re.sub(r"\s*\|\s*Royal Road\s*$", "", title, flags=re.I)
        parts = [p.strip() for p in re.split(r"\s+-\s+", title) if p.strip()]
        if len(parts) >= 2:
            return parts[-1]
    return "Royal Road Novel"


def _og_novel_meta(soup):
    """Several webnovel-aggregator CMSes (FreeWebNovel and its many clones,
    among others) expose custom og:novel:* meta tags with clean, reliable
    novel/chapter names — much more reliable than guessing from the DOM."""
    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop})
        return _clean_text(tag.get("content", "")) if tag else None

    return {
        "novel_name": meta("og:novel:novel_name"),
        "chapter_name": meta("og:novel:chapter_name"),
    }


def _scrape_generic(soup, url):
    # Capture navigation and metadata before stripping page furniture; many sites
    # place the Next button in a nav/pagination element and series data in JSON-LD.
    next_url = _find_next_url(soup, url)
    og_novel = _og_novel_meta(soup)
    provisional_title = og_novel["chapter_name"] or _chapter_title(soup, soup.find("body") or soup)
    novel_title = og_novel["novel_name"] or _novel_title(soup, url, provisional_title)

    _remove_junk(soup)
    container = _find_content_container(soup)
    if not container:
        raise ValueError("Could not find a readable chapter body on that page.")

    content = _extract_text(container)
    chapter_title = og_novel["chapter_name"] or _chapter_title(soup, container) or provisional_title

    return {
        "novel_title": _clean_title(novel_title),
        "chapter_title": _clean_title(chapter_title),
        "content": content,
        "next_url": next_url,
    }


def _remove_junk(soup):
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe", "form"]):
        tag.decompose()

    # Remove obvious page furniture while leaving chapter navigation available long
    # enough for _find_next_url to inspect the original soup attributes/text.
    for tag in list(soup.find_all(["aside", "footer"])):
        tag.decompose()

    for tag in list(soup.find_all(True)):
        if getattr(tag, "attrs", None) is None:
            continue
        classes = " ".join(tag.get("class", []))
        ident = tag.get("id", "")
        marker = f"{classes} {ident}".strip()
        if marker and JUNK_RE.search(marker):
            # Do not remove elements that look like the actual chapter container.
            if not re.search(r"chapter[-_ ]?(?:content|body|text)|reading[-_ ]?content", marker, re.I):
                try:
                    tag.decompose()
                except Exception:
                    pass


def _find_content_container(soup):
    for selector in CONTENT_SELECTORS:
        try:
            matches = soup.select(selector)
        except Exception:
            matches = []
        viable = [tag for tag in matches if _text_score(tag) >= 120]
        if viable:
            return max(viable, key=_text_score)

    candidates = []
    for tag in soup.find_all(["article", "main", "section", "div"]):
        score = _text_score(tag)
        if score >= 250:
            candidates.append((score, tag))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]

    body = soup.find("body")
    return body if body and _text_score(body) >= 120 else None


def _text_score(tag):
    if not tag:
        return 0
    text = _clean_text(tag.get_text(" ", strip=True))
    if not text:
        return 0
    paragraphs = tag.find_all("p")
    paragraph_chars = sum(len(_clean_text(p.get_text(" ", strip=True))) for p in paragraphs)
    links = tag.find_all("a")
    link_chars = sum(len(_clean_text(a.get_text(" ", strip=True))) for a in links)
    punctuation = sum(text.count(ch) for ch in ".!?…")
    return len(text) + paragraph_chars * 1.8 + punctuation * 12 - link_chars * 1.5


def _extract_text(container):
    # Prefer paragraph-like blocks. If the site uses bare divs/br tags, fall back to
    # line-aware text extraction instead of returning an empty chapter.
    blocks = []
    for tag in container.find_all(["p", "blockquote"], recursive=True):
        text = _clean_text(tag.get_text(" ", strip=True))
        if text and len(text) > 1:
            blocks.append(text)

    if blocks and sum(len(x) for x in blocks) >= 120:
        return "\n".join(_dedupe_lines(blocks))

    raw = container.get_text("\n", strip=True)
    lines = [_clean_text(line) for line in raw.splitlines()]
    lines = [line for line in lines if len(line) > 1]
    return "\n".join(_dedupe_lines(lines))


def _chapter_title(soup, container):
    for selector in (
        "meta[property='og:title']",
        "meta[name='twitter:title']",
        "h1.chapter-title",
        ".chapter-title h1",
        ".chapter-title",
        "article h1",
        "main h1",
        "h1",
        "h2.chapter-title",
    ):
        tag = soup.select_one(selector)
        text = _meta_or_text(tag)
        if text:
            return _clean_title(text)

    # Sometimes the chapter heading is inside the chosen content container.
    for tag in container.find_all(["h1", "h2", "h3"], limit=3):
        text = _clean_text(tag.get_text(" ", strip=True))
        if text:
            return _clean_title(text)

    return _clean_title(_document_title(soup) or "Unknown Chapter")


def _novel_title(soup, url, chapter_title):
    # JSON-LD often contains the series/work name even when the visible markup is
    # highly site-specific.
    jsonld_title = _novel_from_jsonld(soup)
    if jsonld_title:
        return jsonld_title

    # Breadcrumbs are one of the most reliable generic signals on web-novel sites.
    breadcrumbs = []
    for selector in (
        "nav[aria-label*='breadcrumb' i] a",
        ".breadcrumb a",
        ".breadcrumbs a",
        "[class*='breadcrumb'] a",
    ):
        for tag in soup.select(selector):
            text = _clean_text(tag.get_text(" ", strip=True))
            if text and text.lower() not in {"home", "novels", "novel", "books", "chapters"}:
                breadcrumbs.append(text)
    for text in reversed(breadcrumbs):
        if text.lower() != chapter_title.lower() and not NOVEL_WORD_RE.search(text):
            return text

    for selector in (
        "meta[property='og:site_name']",
        "meta[name='application-name']",
        "a[rel='up']",
        ".novel-title",
        ".book-title",
        ".fiction-title",
        "h1.novel-title",
    ):
        text = _meta_or_text(soup.select_one(selector))
        if text and text.lower() != chapter_title.lower():
            # Site name is a weak signal; prefer it only after breadcrumbs/JSON-LD.
            if selector.startswith("meta"):
                site_name = text
            else:
                return _clean_title(text)

    title = _document_title(soup)
    if title:
        # Common patterns: "Chapter 12 - Novel Name - Site" or
        # "Novel Name - Chapter 12 | Site".
        parts = [p.strip() for p in re.split(r"\s*(?:\||–|—| - )\s*", title) if p.strip()]
        non_chapter = [p for p in parts if p.lower() != chapter_title.lower() and not NOVEL_WORD_RE.search(p)]
        if non_chapter:
            # If a known site name was found, avoid returning it when another part exists.
            if "site_name" in locals() and len(non_chapter) > 1:
                filtered = [p for p in non_chapter if p.lower() != site_name.lower()]
                if filtered:
                    return _clean_title(filtered[0])
            return _clean_title(non_chapter[0])

    if "site_name" in locals() and site_name:
        return _clean_title(site_name)

    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host or "Unknown Novel"


def _novel_from_jsonld(soup):
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _walk_json(data):
            if not isinstance(obj, dict):
                continue
            parent = obj.get("isPartOf") or obj.get("partOfSeries")
            if isinstance(parent, dict):
                name = parent.get("name")
                if name:
                    return _clean_title(str(name))
            if isinstance(parent, str) and parent.strip():
                return _clean_title(parent)
    return None


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _find_next_url(soup, url):
    # Standards-based relation first. This covers many static sites.
    for tag in soup.find_all(["a", "link"]):
        rel = tag.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        if any(str(item).lower() == "next" for item in rel):
            href = tag.get("href")
            if href:
                candidate = _safe_next(urljoin(url, href), url)
                if candidate:
                    return candidate

    # Common web-novel navigation selectors.
    selectors = [
        "a#next_chap", "a#next-chapter", "a#nextChapter",
        "a.next-chapter", "a.next_chapter", "a.nextchap", "a.chapter-next",
        ".next-chapter a", ".next_chapter a", ".nav-next a", ".chapter-nav .next a",
        ".chapter-navigation .next a", ".pagination .next a", "a[aria-label*='next' i]",
        "a[title*='next' i]",
    ]
    for selector in selectors:
        for tag in soup.select(selector):
            href = tag.get("href")
            if href:
                candidate = _safe_next(urljoin(url, href), url)
                if candidate:
                    return candidate

    # Text/class/id heuristic, including icon-only buttons whose class contains next.
    scored = []
    for tag in soup.find_all("a", href=True):
        href = tag.get("href")
        text = _clean_text(tag.get_text(" ", strip=True))
        marker = " ".join([
            " ".join(tag.get("class", [])),
            tag.get("id", ""),
            tag.get("title", ""),
            tag.get("aria-label", ""),
        ]).strip()

        score = 0
        if NEXT_TEXT_RE.match(text or ""):
            score += 100
        elif re.search(r"\bnext\b", text, re.I):
            score += 55
        if re.search(r"(?:^|[-_\s])next(?:$|[-_\s])|nextchap|nextchapter", marker, re.I):
            score += 70
        if text in {"›", "»", "→", ">"}:
            score += 20
        if re.search(r"prev|previous", marker + " " + text, re.I):
            score -= 120

        candidate = _safe_next(urljoin(url, href), url)
        if score > 0 and candidate:
            scored.append((score, candidate))

    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]
    return None


def _safe_next(candidate, current):
    try:
        parsed = urlparse(candidate)
        current_parsed = urlparse(current)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if candidate.rstrip("/") == current.rstrip("/"):
        return None
    # Avoid accidentally following a different domain through an ad/login link.
    if parsed.netloc.lower() != current_parsed.netloc.lower():
        return None
    return candidate


def _document_title(soup):
    tag = soup.find("title")
    return _clean_text(tag.get_text(" ", strip=True)) if tag else None


def _meta_or_text(tag):
    if not tag:
        return None
    if tag.name == "meta":
        return _clean_text(tag.get("content", ""))
    return _clean_text(tag.get_text(" ", strip=True))


def _first_text(*tags):
    for tag in tags:
        text = _meta_or_text(tag)
        if text:
            return text
    return None


def _clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def _clean_title(value):
    value = _clean_text(value)
    value = re.sub(r"\s*(?:\||[-–—])\s*(?:read online|free online|novel updates?)\s*$", "", value, flags=re.I)
    return value[:300] or "Unknown Chapter"


def _dedupe_lines(lines):
    result = []
    previous = None
    for line in lines:
        if line != previous:
            result.append(line)
        previous = line
    return result


def browse_page(url):
    """Return a lightweight, server-side chapter-link browser for a public novel page."""
    original_url = url
    parsed_input = urlparse(url)
    input_host = parsed_input.netloc.lower().removeprefix("www.")
    # On FreeWebNovel, a chapter page only exposes Prev/Next. Its book page
    # exposes the complete Chapter List, so jump there automatically when the
    # user pastes a chapter URL.
    if input_host == "freewebnovel.com":
        match = re.search(r"^(.*?/novel/[^/]+)/chapter-\d+(?:/)?$", parsed_input.path, re.I)
        if match:
            url = parsed_input._replace(path=match.group(1), query="", fragment="").geturl()
    resp = _fetch(url)
    content_type = resp.headers.get("Content-Type", "").lower()
    ajax_catalog = None
    if input_host == 'freewebnovel.com' and parse_qs(urlparse(url).query).get('ajax') == ['chapters']:
        try:
            ajax_catalog = json.loads(resp.text)
            requested = int(parse_qs(urlparse(url).query)['page'][0])
            if ajax_catalog.get('code') != 200 or int(ajax_catalog['page']) != requested or not isinstance(ajax_catalog.get('html'), str):
                raise ValueError('Invalid chapter-list response.')
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('FreeWebNovel chapter pagination failed: '+str(exc)) from exc
    elif content_type and "html" not in content_type and "xhtml" not in content_type:
        raise ValueError("That URL did not return an HTML page.")

    sniff = resp.text[:4000].lower()
    if any(marker in sniff for marker in ANTIBOT_MARKERS):
        raise ValueError(
            "This site is showing a bot-protection challenge (e.g. Cloudflare) instead of the page, "
            "so its chapter links can't be listed here."
        )

    final_url = resp.url or url
    html = '<ul id="idData" class="ul-list5">'+ajax_catalog['html']+'</ul>' if ajax_catalog else resp.text
    if input_host in {'royalroad.com','novelfull.com'}:
        from source_adapters import canonical_source
        if canonical_source(original_url)!=canonical_source(final_url):
            raise ValueError('The source redirected to a different novel; discovery is incomplete.')
    soup = BeautifulSoup(html, "html.parser")
    title = _document_title(soup) or final_url
    host = urlparse(final_url).netloc.lower()

    candidates = []
    fallback = []
    seen = set()
    # Use the chapter table rather than repeated navigation/recommendation links.
    fiction = re.match(r"/fiction/(\d+)(?:/|$)", urlparse(final_url).path)
    royalroad = host.removeprefix('www.') == 'royalroad.com'
    anchors = soup.select('#chapters a[href]') if royalroad else []
    freewebnovel = host.removeprefix('www.') == 'freewebnovel.com'
    if freewebnovel:
        # Prefer the proven full-list CMS container to duplicate Read First/latest links.
        anchors = soup.select('#idData a[href]') or soup.select('.ul-list5 a[href], .chapter-list a[href], #chapters a[href]')
    for tag in anchors or soup.find_all("a", href=True):
        href = (tag.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(final_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host:
            continue
        if freewebnovel and not re.match(re.escape(urlparse(final_url).path.rstrip('/'))+r'/chapter-\d+/?$', parsed.path):
            continue
        if royalroad and fiction and not re.match(r'/fiction/'+fiction[1]+r'/[^/]+/chapter/\d+(?:/|$)', parsed.path):
            continue
        normalized = absolute.split("#", 1)[0]
        if normalized in seen:
            continue
        seen.add(normalized)

        text = _clean_text(tag.get_text(" ", strip=True)) or tag.get("title") or tag.get("aria-label") or parsed.path.rsplit("/", 1)[-1]
        text = _clean_text(text)[:180]
        if not text:
            continue

        marker = f"{text} {parsed.path} {' '.join(tag.get('class', []))} {tag.get('id', '')}"
        score = 0
        if re.search(r"\bchapter\b|\bchap\.?\b|\bepisode\b|\bpart\b", marker, re.I):
            score += 80
        if re.search(r"/(?:chapter|chap|episode|part)[-/]?\d+", parsed.path, re.I):
            score += 90
        if re.search(r"\b(?:chapter|chap\.?|episode|part)\s*\d+", text, re.I):
            score += 90
        if re.search(r"\bnext\b|\bprevious\b|\bprev\b", text, re.I):
            score += 45
        if "royalroad.com" in host and "/chapter/" in parsed.path:
            score += 120
        if host.endswith("freewebnovel.com") and re.search(r"/novel/[^/]+/chapter-\d+", parsed.path, re.I):
            score += 150
        if re.search(r"login|register|privacy|terms|contact|discord|patreon", marker, re.I):
            score -= 100

        item = {"title": text, "url": normalized, "score": score}
        fallback.append(item)
        if score > 0:
            candidates.append(item)

    # Keep document order for generic sites. FreeWebNovel repeats the newest six
    # chapters before its full chapter list, so sort its chapter URLs numerically
    # to present a clean 1, 2, 3... queue browser.
    pool = candidates if candidates else fallback
    if host.endswith("freewebnovel.com"):
        chapter_items = []
        for item in pool:
            match = re.search(r"/chapter-(\d+)(?:/)?$", urlparse(item["url"]).path, re.I)
            if match:
                chapter_items.append((int(match.group(1)), item))
        if chapter_items:
            chapter_items.sort(key=lambda pair: pair[0])
            pool = [item for _, item in chapter_items]
    chosen = pool
    artwork = soup.select_one('.fiction-cover img[src], .book-img img[src], .book-cover img[src]') or soup.select_one('meta[property="og:image"]')
    cover_url = urljoin(final_url, artwork.get('src') or artwork.get('content') or '') if artwork else None
    pages = []
    for tag in soup.select('a[rel="next"], .pagination a, .pagination-container a'):
        if not tag.get('href'):
            continue
        target = urljoin(final_url, tag['href'])
        parsed = urlparse(target)
        if parsed.netloc.lower() != host or '/chapter/' in target or re.search(r'/chapter-\d+', target):
            continue
        # Fiction review/comment pagination is unrelated to chapter discovery.
        if royalroad and (set(parse_qs(parsed.query)) & {'reviews', 'comments'} or
                          (fiction and not re.match(r'/fiction/'+fiction[1]+r'(?:/|$)', parsed.path))):
            continue
        if target not in pages:
            pages.append(target)
    book_data = {}
    if freewebnovel:
        # Book JSON-LD is authoritative only when it identifies THIS novel.
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                value=json.loads(script.string or script.get_text())
            except (ValueError, TypeError):
                continue
            entries=value if isinstance(value,list) else [value]
            for entry in entries:
                if isinstance(entry,dict) and entry.get('@type')=='Book' and urlparse(entry.get('url','')).path.rstrip('/')==urlparse(final_url).path.rstrip('/'):
                    book_data=entry;break
        config=soup.select_one('#indexListPage')
        if ajax_catalog or config:
            data=ajax_catalog or {'page':config.get('data-current-page','1'),'totalPage':config.get('data-total-page','1'),'pageSize':config.get('data-page-size','40')}
            try:
                current,total,size=int(data['page']),int(data['totalPage']),int(data['pageSize'])
                if current<1 or total<current or size<1:raise ValueError('Invalid page counts')
            except (ValueError,TypeError,KeyError) as exc:
                raise ValueError('FreeWebNovel chapter pagination metadata is invalid: '+str(exc)) from exc
            pages=[]
            if current<total:
                pages=[urlparse(final_url)._replace(query=urlencode({'ajax':'chapters','page':current+1,'pageSize':size}),fragment='').geturl()]
        if not chosen:
            raise ValueError('FreeWebNovel returned no usable chapter list at '+final_url+'. The source may have removed the novel or changed its HTML; discovery is not complete.')
    from source_adapters import catalog_metadata,novelfull_catalog
    scoped=catalog_metadata(soup,final_url)
    if host.removeprefix('www.')=='novelfull.com':
        links,pages=novelfull_catalog(soup,final_url)
        chosen=links
    authors=book_data.get('author',[])
    if not isinstance(authors,list):authors=[authors]
    book_author=', '.join(a.get('name','') if isinstance(a,dict) else str(a) for a in authors)
    if isinstance(book_data.get('image'),str):cover_url=urljoin(final_url,book_data['image'])
    return {
        "title": _clean_title(scoped.get('title') or book_data.get('name') or _meta_or_text(soup.select_one("h1.tit, .fiction-title h1")) or title),
        "author": scoped.get('author') or book_author or _meta_or_text(soup.select_one('meta[name="author"],meta[property="books:author"],.fiction-info .author')) or "",
        "description": scoped.get('description') or book_data.get("description") or _meta_or_text(soup.select_one('meta[name="description"],meta[property="og:description"]')) or "",
        "cover_url": scoped.get('cover_url') or cover_url,
        "url": final_url,
        "host": host,
        "links": [{"title": item["title"], "url": item["url"]} for item in chosen],
        "chapter_links_found": len(candidates),
        "original_url": original_url,
        "pages": pages,
    }
