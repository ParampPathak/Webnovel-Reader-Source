"""Incremental EPUB 3 export: one chapter in memory, ZIP entries on disk."""
import html
import hashlib
from html.parser import HTMLParser
from urllib.parse import unquote
from epub_library import ReadingHTML,SAFE_MEDIA
import re
import zipfile
from datetime import datetime,timezone
from pathlib import Path
from library_store import connect
from storage_paths import original_path


def xml_text(value):
    return html.escape(re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]',' ',value or ''),quote=True)


class ExportHTML(HTMLParser):
    """Serialize sanitized reader HTML as valid XHTML with local export links."""
    def __init__(self,hrefs,resources):
        super().__init__(convert_charrefs=True)
        self.hrefs,self.resources=hrefs,resources
        self.output,self.stack=[],[]
    def handle_starttag(self,tag,attrs):
        if tag not in ReadingHTML.allowed: return
        attrs=dict(attrs);clean={key:attrs[key] for key in ('id','title','lang','dir','colspan','rowspan','alt') if attrs.get(key)}
        if tag=='a':
            target=attrs.get('data-book-target','')
            path,_,fragment=target.partition('#')
            if path in self.hrefs: clean['href']=self.hrefs[path]+('#'+fragment if fragment else '')
        if tag=='img':
            path=unquote(attrs.get('src','').split('/resources/',1)[-1])
            if path not in self.resources: return
            clean['src']=self.resources[path]
        ending='/>' if tag in ReadingHTML.void else '>'
        self.output.append('<'+tag+''.join(' '+key+'="'+xml_text(value)+'"' for key,value in clean.items())+ending)
        if tag not in ReadingHTML.void: self.stack.append(tag)
    def handle_endtag(self,tag):
        if tag in self.stack:
            while self.stack:
                last=self.stack.pop();self.output.append('</'+last+'>')
                if last==tag: break
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in ReadingHTML.void: self.handle_endtag(tag)
    def handle_data(self,text): self.output.append(xml_text(text))
    def finish(self):
        while self.stack: self.output.append('</'+self.stack.pop()+'>')
        return ''.join(self.output)


def export_epub(database,book_id,destination,first,last,checkpoint=lambda:None,catalog_job=None):
    with connect(database) as conn:
        if catalog_job:
            conn.execute('CREATE TEMP TABLE export_selection(section_id INTEGER PRIMARY KEY,position INTEGER UNIQUE)')
            conn.execute('INSERT INTO export_selection SELECT c.section_id,l.position FROM discovery_links l JOIN import_chapters c ON c.url=l.url AND c.book_id=? WHERE l.job_id=? AND c.section_id IS NOT NULL',(book_id,catalog_job))
            conn.execute('CREATE TEMP VIEW export_sections AS SELECT e.position,s.book_id,s.title,s.href,s.text_content,s.content_html FROM export_selection e JOIN book_sections s ON s.id=e.section_id')
        else:
            conn.execute('CREATE TEMP VIEW export_sections AS SELECT * FROM book_sections')
        book=conn.execute('SELECT * FROM books WHERE id=?',(book_id,)).fetchone()
        if not book:
            raise ValueError('Book not found.')
        stored_original=original_path(Path(database).parent,book)
        count=conn.execute('SELECT COUNT(*) FROM export_sections WHERE book_id=? AND position BETWEEN ? AND ?',(book_id,first,last)).fetchone()[0]
        if not count:
            raise ValueError('The selected range is empty.')
        Path(destination).parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(destination,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=3) as archive:
            archive.writestr('mimetype','application/epub+zip',compress_type=zipfile.ZIP_STORED)
            archive.writestr('META-INF/container.xml','<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            hrefs={row['href']:f"chapter-{row['position']}.xhtml" for row in conn.execute('SELECT href,position FROM export_sections WHERE book_id=? AND position BETWEEN ? AND ?',(book_id,first,last))}
            resources={};media={}
            if stored_original:
                with zipfile.ZipFile(stored_original) as original:
                    for resource in conn.execute('SELECT path,media_type FROM book_resources WHERE book_id=?',(book_id,)):
                        if resource['media_type'] not in SAFE_MEDIA: continue
                        checkpoint()
                        path=resource['path'];target='resources/'+hashlib.sha256(path.encode()).hexdigest()+Path(path).suffix
                        with original.open(path) as source,archive.open('EPUB/'+target,'w') as output:
                            while block:=source.read(64*1024):
                                checkpoint();output.write(block)
                        resources[path]=target;media[target]=resource['media_type']
            cover = None
            cover_path=book['cover_path']
            if cover_path and str(cover_path).startswith('covers/'):
                path=Path(database).parent/cover_path
                if path.is_file():
                    cover=('cover'+path.suffix,{'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}[path.suffix.lower()])
                    archive.write(path,'EPUB/'+cover[0])
            elif stored_original and cover_path:
                resource=conn.execute('SELECT media_type FROM book_resources WHERE book_id=? AND path=?',(book_id,cover_path)).fetchone()
                if resource and resource['media_type'] in {'image/png','image/jpeg','image/webp','image/gif'}:
                    try:
                        with zipfile.ZipFile(stored_original) as original:
                            cover=('cover'+Path(cover_path).suffix,resource['media_type'])
                            with original.open(cover_path) as source,archive.open('EPUB/'+cover[0],'w') as target:
                                import shutil
                                shutil.copyfileobj(source,target,64*1024)
                    except (OSError,KeyError,zipfile.BadZipFile):
                        cover=None
            with archive.open('EPUB/package.opf','w') as opf:
                def write(value): opf.write(value.encode('utf-8'))
                write('<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book">urn:uuid:'+xml_text(book_id)+'</dc:identifier><dc:title>'+xml_text(book['title'])+'</dc:title><dc:creator>'+xml_text(book['author'])+'</dc:creator><dc:language>'+xml_text(book['language'] or 'en')+'</dc:language><dc:description>'+xml_text(book['description'])+'</dc:description><meta property="dcterms:modified">'+datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')+'</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
                if cover: write('<item id="cover" href="'+xml_text(cover[0])+'" media-type="'+cover[1]+'" properties="cover-image"/>')
                for row in conn.execute('SELECT position FROM export_sections WHERE book_id=? AND position BETWEEN ? AND ? ORDER BY position',(book_id,first,last)):
                    write(f'<item id="c{row[0]}" href="chapter-{row[0]}.xhtml" media-type="application/xhtml+xml"/>')
                for index,(target,media_type) in enumerate(media.items()):
                    write('<item id="r'+str(index)+'" href="'+xml_text(target)+'" media-type="'+media_type+'"/>')
                write('</manifest><spine>')
                for row in conn.execute('SELECT position FROM export_sections WHERE book_id=? AND position BETWEEN ? AND ? ORDER BY position',(book_id,first,last)):
                    write(f'<itemref idref="c{row[0]}"/>')
                write('</spine></package>')
            with archive.open('EPUB/nav.xhtml','w') as nav:
                nav.write(('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><h1>Contents</h1><ol>').encode())
                for row in conn.execute('SELECT position,title FROM export_sections WHERE book_id=? AND position BETWEEN ? AND ? ORDER BY position',(book_id,first,last)):
                    nav.write((f'<li><a href="chapter-{row[0]}.xhtml">'+xml_text(row[1])+'</a></li>').encode())
                nav.write(b'</ol></nav></body></html>')
            previous=first-1
            while True:
                # Keyset iteration releases each SQLite read cursor before the
                # checkpoint, allowing pause/cancel writes during large exports.
                row=conn.execute('SELECT position,title,text_content,content_html FROM export_sections WHERE book_id=? AND position>? AND position<=? ORDER BY position LIMIT 1',(book_id,previous,last)).fetchone()
                if not row: break
                previous=row['position']
                checkpoint()
                with archive.open(f'EPUB/chapter-{row[0]}.xhtml','w') as chapter:
                    chapter.write(('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>'+xml_text(row['title'])+'</title></head><body>').encode())
                    if row['content_html']:
                        serializer=ExportHTML(hrefs,resources);serializer.feed(row['content_html']);serializer.close()
                        chapter.write(serializer.finish().encode())
                    else:
                        chapter.write(('<h1>'+xml_text(row['title'])+'</h1>').encode())
                        for paragraph in row['text_content'].splitlines():
                            if paragraph.strip(): chapter.write(('<p>'+xml_text(paragraph)+'</p>').encode())
                    chapter.write(b'</body></html>')
    return count
