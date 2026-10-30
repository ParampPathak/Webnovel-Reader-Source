"""EPUB 2/3 parsing and a script-free, local-resource reading representation."""
import html
import os
import posixpath
import re
import stat
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

MAX_EXPANDED_BYTES = int(os.environ.get("EPUB_MAX_EXPANDED_BYTES", 128 * 1024 * 1024))
MAX_MEMBER_BYTES = int(os.environ.get("EPUB_MAX_MEMBER_BYTES", 32 * 1024 * 1024))
SAFE_MEDIA = {'image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/avif'}


class InvalidEpub(ValueError):
    pass


def xml(data):
    text = decode_markup(data)
    if re.search(r'<!\s*ENTITY', text, re.I):
        raise InvalidEpub('XML entities are not supported.')
    # EPUB generators routinely include HTML/XHTML doctypes. Remove external
    # declarations without loading them; reject all internal subsets/entities.
    text = re.sub(r'<!\s*DOCTYPE\s+[^<>\[\]]+>', '', text, flags=re.I)
    if re.search(r'<!\s*DOCTYPE', text, re.I):
        raise InvalidEpub('XML internal document type subsets are not supported.')
    try:
        return ET.fromstring(text)
    except (ET.ParseError, ValueError) as exc:
        raise InvalidEpub('Malformed EPUB XML.') from exc


def decode_markup(data):
    if data.startswith((b'\xff\xfe',b'\xfe\xff')):
        return data.decode('utf-16')
    if data.startswith(b'\x00<'):
        return data.decode('utf-16-be')
    if data.startswith(b'<\x00'):
        return data.decode('utf-16-le')
    return data.decode('utf-8-sig')


def local_name(tag):
    return tag.rsplit('}', 1)[-1]


def resolve(base, href):
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or parts.query or '\\' in href or '\x00' in href:
        raise InvalidEpub('Only local EPUB resource references are supported.')
    decoded = unquote(parts.path)
    if decoded.startswith('/') or '\\' in decoded or '\x00' in decoded:
        raise InvalidEpub('Invalid EPUB resource path.')
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), decoded)) if decoded else base
    if path == '..' or path.startswith('../') or path.startswith('/'):
        raise InvalidEpub('EPUB resource escapes the archive.')
    return path + ('#' + unquote(parts.fragment) if parts.fragment else '')


def validate_archive(archive):
    seen = set()
    total = 0
    for member in archive.infolist():
        name = member.filename
        if name in seen:
            raise InvalidEpub('Duplicate archive paths are not supported.')
        seen.add(name)
        if '\\' in name or '\x00' in name or name.startswith('/') or any(x == '..' for x in name.split('/')):
            raise InvalidEpub('Unsafe archive path.')
        if stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
            raise InvalidEpub('Encrypted entries and symbolic links are not supported.')
        total += member.file_size
        if member.file_size > MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES:
            raise InvalidEpub('EPUB expanded size exceeds the configured safety budget.')
    if archive.read('mimetype').strip() != b'application/epub+zip':
        raise InvalidEpub('This file is not an EPUB archive.')


class ReadingHTML(HTMLParser):
    allowed = set('p div span h1 h2 h3 h4 h5 h6 blockquote pre code em strong b i u s small sub sup br hr ul ol li dl dt dd table thead tbody tfoot tr td th caption figure figcaption a img section article aside ruby rt rp'.split())
    blocked = {'head', 'script', 'style', 'iframe', 'object', 'embed', 'form', 'button', 'input', 'textarea', 'select', 'svg', 'math', 'audio', 'video'}
    void = {'br', 'hr', 'img'}

    def __init__(self, base, book_id, media):
        super().__init__(convert_charrefs=True)
        self.base, self.book_id, self.media = base, book_id, media
        self.output, self.text, self.suppressed, self.stack = [], [], [], []

    def handle_starttag(self, tag, attrs):
        if self.suppressed:
            if tag in self.blocked and tag not in {'input','embed'}:
                self.suppressed.append(tag)
            return
        if tag in self.blocked:
            # Void controls have no closing tag.
            if tag not in {'input', 'embed'}:
                self.suppressed.append(tag)
            return
        if tag not in self.allowed:
            return
        clean = []
        attrs = dict(attrs)
        for key in ('id', 'title', 'lang', 'dir', 'colspan', 'rowspan'):
            if attrs.get(key):
                clean.append((key, attrs[key][:512]))
        if tag == 'a' and attrs.get('href'):
            try:
                target = resolve(self.base, attrs['href'])
                clean.extend([('href', '#'), ('data-book-target', target)])
            except InvalidEpub:
                pass
        if tag == 'img':
            try:
                target = resolve(self.base, attrs.get('src', '')).split('#')[0]
                if self.media.get(target) not in SAFE_MEDIA:
                    return
                clean.extend([('src', f'/api/books/{self.book_id}/resources/{quote(target, safe="/")}'),
                              ('alt', attrs.get('alt', '')), ('loading', 'lazy')])
            except InvalidEpub:
                return
        self.output.append('<' + tag + ''.join(' ' + key + '="' + html.escape(value, quote=True) + '"' for key,value in clean) + '>')
        if tag not in self.void:
            self.stack.append(tag)
        if tag in {'p','div','br','h1','h2','h3','li'}:
            self.text.append('\n')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.suppressed:
            if tag == self.suppressed[-1]:
                self.suppressed.pop()
            return
        if tag in self.stack:
            while self.stack:
                last = self.stack.pop()
                self.output.append('</' + last + '>')
                if last == tag:
                    break

    def handle_data(self, data):
        if not self.suppressed:
            self.output.append(html.escape(data))
            self.text.append(data)

    def finish(self):
        while self.stack:
            self.output.append('</' + self.stack.pop() + '>')
        return ''.join(self.output), ''.join(self.text).strip()


def parse_epub(path, book_id, *, stream=False):
    try:
        with zipfile.ZipFile(path) as archive:
            validate_archive(archive)
            if 'META-INF/encryption.xml' in archive.namelist():
                raise InvalidEpub('Encrypted EPUB resources are not supported.')
            container = xml(archive.read('META-INF/container.xml'))
            rootfiles = [e for e in container.iter() if local_name(e.tag) == 'rootfile']
            if not rootfiles:
                raise InvalidEpub('Missing package document.')
            opf_path = resolve('', rootfiles[0].get('full-path', ''))
            package = xml(archive.read(opf_path))
            metadata = next((e for e in package if local_name(e.tag)=='metadata'), None)
            if metadata is None:
                raise InvalidEpub('Missing EPUB metadata.')
            def field(name, default=''):
                values = [''.join(e.itertext()).strip() for e in metadata if local_name(e.tag)==name]
                return ', '.join(v for v in values if v) or default
            meta = {'title':field('title','Untitled book'), 'author':field('creator'),
                    'language':field('language','en'), 'description':field('description')}
            manifest = next((e for e in package if local_name(e.tag)=='manifest'), None)
            spine = next((e for e in package if local_name(e.tag)=='spine'), None)
            if manifest is None or spine is None:
                raise InvalidEpub('Missing manifest or reading order.')
            items, media = {}, {}
            archive_names = set(archive.namelist())
            for item in manifest:
                href = resolve(opf_path,item.get('href','')).split('#')[0]
                if href not in archive_names:
                    raise InvalidEpub('Manifest resource is missing: ' + href)
                identifier = item.get('id')
                if not identifier or identifier in items:
                    raise InvalidEpub('Manifest identifiers must be unique.')
                items[identifier] = {'path':href,'type':item.get('media-type',''), 'properties':item.get('properties','').split()}
                media[href] = item.get('media-type','')
            cover_id = next((e.get('content') for e in metadata if local_name(e.tag)=='meta' and e.get('name')=='cover'),None)
            cover = next((i['path'] for key,i in items.items() if ('cover-image' in i['properties'] or key==cover_id) and i['type'] in SAFE_MEDIA),None)
            if not cover:
                reference=next((e.get('href') for e in package.iter() if local_name(e.tag)=='reference' and e.get('type')=='cover'),None)
                if reference:
                    cover_page=resolve(opf_path,reference).split('#')[0]
                    if media.get(cover_page) in SAFE_MEDIA:
                        cover=cover_page
                    elif media.get(cover_page) in {'application/xhtml+xml','text/html'}:
                        page=xml(archive.read(cover_page))
                        for image in page.iter():
                            if local_name(image.tag)=='img' and image.get('src'):
                                candidate=resolve(cover_page,image.get('src')).split('#')[0]
                                if media.get(candidate) in SAFE_MEDIA:
                                    cover=candidate
                                    break
            navigation = []
            nav_item = next((i for i in items.values() if 'nav' in i['properties']),None)
            if nav_item:
                root = xml(archive.read(nav_item['path']))
                toc = next((e for e in root.iter() if local_name(e.tag)=='nav' and 'toc' in e.get('{http://www.idpf.org/2007/ops}type','').split()),None)
                def walk(element,depth=0):
                    for child in element:
                        tag = local_name(child.tag)
                        if tag=='a' and child.get('href'):
                            navigation.append({'title':''.join(child.itertext()).strip(), 'href':resolve(nav_item['path'],child.get('href')), 'depth':max(0,depth-1)})
                        walk(child,depth+(tag=='ol'))
                if toc is not None:
                    walk(toc)
            elif spine.get('toc') in items:
                ncx_item = items[spine.get('toc')]
                root = xml(archive.read(ncx_item['path']))
                def walk_ncx(element,depth=0):
                    for child in element:
                        if local_name(child.tag)=='navPoint':
                            label = next((e for e in child if local_name(e.tag)=='navLabel'),None)
                            content = next((e for e in child if local_name(e.tag)=='content'),None)
                            if label is not None and content is not None:
                                navigation.append({'title':''.join(label.itertext()).strip(), 'href':resolve(ncx_item['path'],content.get('src','')), 'depth':depth})
                            walk_ncx(child,depth+1)
                        elif local_name(child.tag)!='navLabel':
                            walk_ncx(child,depth)
                walk_ncx(root)
            specs = []
            seen_spine = set()
            for ref in spine:
                if local_name(ref.tag)!='itemref':
                    continue
                item = items.get(ref.get('idref'))
                if not item or item['type'] not in {'application/xhtml+xml','text/html'}:
                    raise InvalidEpub('Unsupported or missing EPUB reading section.')
                if item['path'] in seen_spine:
                    raise InvalidEpub('Repeated spine resources are not supported.')
                seen_spine.add(item['path'])
                specs.append({'href':item['path'],'linear':int(ref.get('linear','yes')!='no')})
            if not specs:
                raise InvalidEpub('EPUB has no readable sections.')
            navigation = [n for n in navigation if n['href'].split('#')[0] in seen_spine]
            result = {'id':book_id,'metadata':meta,'cover':cover,'section_specs':specs,
                      'section_count':len(specs),'navigation':navigation,
                      'resources':[{'path':p,'media_type':t} for p,t in media.items()]}
            result['sections'] = iter_sections(result,path)
            if not stream:
                result['sections'] = list(result['sections'])
            return result
    except InvalidEpub:
        raise
    except (KeyError,zipfile.BadZipFile,UnicodeError,ValueError,RuntimeError,NotImplementedError,RecursionError) as exc:
        raise InvalidEpub('Invalid or unsupported EPUB: ' + str(exc)) from exc


def iter_sections(parsed, source):
    """Yield one sanitized section at a time; no whole-book text collection.

    Archive metadata is validated by parse_epub first. A fresh iterator may be
    bound to the published original after the temporary upload is renamed.
    Errors during iteration propagate so the importing transaction rolls back.
    """
    labels = {}
    for item in parsed['navigation']:
        labels.setdefault(item['href'].split('#')[0],item['title'])
    media = {item['path']:item['media_type'] for item in parsed['resources']}
    try:
        with zipfile.ZipFile(source) as archive:
            for position,spec in enumerate(parsed['section_specs']):
                raw = decode_markup(archive.read(spec['href']))
                parser = ReadingHTML(spec['href'],parsed['id'],media)
                parser.feed(raw)
                body,text = parser.finish()
                heading = re.search(r'<h[1-6]\b[^>]*>(.*?)</h[1-6]>',body,re.S)
                title = labels.get(spec['href']) or (html.unescape(re.sub('<[^>]+>','',heading.group(1))).strip() if heading else '') or f'Section {position+1}'
                yield {'position':position,'title':title,'href':spec['href'],
                       'html':body,'text':text,'linear':spec['linear']}
    except InvalidEpub:
        raise
    except (KeyError,zipfile.BadZipFile,UnicodeError,ValueError,RuntimeError,NotImplementedError) as exc:
        raise InvalidEpub('Invalid or unsupported EPUB section: '+str(exc)) from exc
