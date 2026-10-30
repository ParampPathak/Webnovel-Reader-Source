"""Optional Audiobookshelf folder ZIP; streaming copies, no external service."""
import json
import re
import time
import zipfile
from pathlib import Path
from audio_providers import cache_key
from epub_creator import xml_text
from library_store import connect
from storage_paths import original_path


def component(value,fallback):
    clean=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',value or '').strip('. ')[:90]
    return clean or fallback


def export_audiobook(database,book_id,destination,config,first,last,checkpoint=lambda:None):
    root=Path(database).parent.resolve()
    with connect(database) as conn:
        book=conn.execute('SELECT * FROM books WHERE id=?',(book_id,)).fetchone()
        if not book: raise ValueError('Book not found.')
        stored_original=original_path(root,book)
        folder=component(book['author'],'Unknown author')+'/'+component(book['title'],'Untitled book')+'/'
        destination=Path(destination);destination.parent.mkdir(exist_ok=True)
        # ZIPs are rebuilt from immutable cached tracks after interruption; a partial
        # entry must never be mistaken for a complete chapter on resume.
        mode='w'
        count=0
        with zipfile.ZipFile(destination,mode,compression=zipfile.ZIP_STORED,allowZip64=True) as archive:
            existing=set(archive.namelist());previous=first-1
            while True:
                chapter=conn.execute('SELECT id,position,title,text_content FROM book_sections WHERE book_id=? AND position>? AND position<=? ORDER BY position LIMIT 1',(book_id,previous,last)).fetchone()
                if not chapter: break
                previous=chapter['position'];checkpoint()
                if config['provider']=='legacy':
                    asset=conn.execute('SELECT audio_path path FROM library_audio_assets WHERE section_id=?',(chapter['id'],)).fetchone()
                    if not asset:
                        asset=conn.execute("SELECT c.audio_path path FROM chapters c JOIN book_sections s ON s.legacy_chapter_id=c.id WHERE s.id=? AND c.status='ready'",(chapter['id'],)).fetchone()
                else:
                    key,_=cache_key(book_id,chapter['id'],chapter['text_content'],config)
                    asset=conn.execute("SELECT path FROM audio_variants WHERE id=? AND status='ready'",(key,)).fetchone()
                if not asset or not asset['path']: raise ValueError(f"Chapter {previous+1} has no cached audio for these settings. Generate it first.")
                path=Path(asset['path'])
                if str(path).startswith('/data/'): path=root/str(path)[6:]
                if not path.is_absolute(): path=root/path
                path=path.resolve()
                if not path.is_relative_to(root) or not path.is_file(): raise ValueError('Cached audio is unavailable.')
                name=folder+f'{previous+1:06} - '+component(chapter['title'],'Chapter')+path.suffix
                if name not in existing:
                    with path.open('rb') as source,archive.open(name,'w',force_zip64=True) as target:
                        while block:=source.read(128*1024):
                            start=time.monotonic();checkpoint();target.write(block)
                            time.sleep(max(0,len(block)/(4*1024*1024)-(time.monotonic()-start)))
                count+=1
            if not count: raise ValueError('The selected range is empty.')
            voice=config.get('voice','Preserved original audio')
            metadata='<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf" version="3.0"><metadata><dc:title>'+xml_text(book['title'])+'</dc:title><dc:creator opf:role="aut">'+xml_text(book['author'])+'</dc:creator><dc:creator opf:role="nrt">'+xml_text(voice)+'</dc:creator><dc:language>'+xml_text(book['language'])+'</dc:language><dc:description>'+xml_text(book['description'])+'</dc:description></metadata></package>'
            for name,value in [('metadata.opf',metadata),('desc.txt',book['description']),('reader.txt',voice),('generation.json',json.dumps({'book_id':book_id,'settings':config,'first_chapter':first+1,'last_chapter':last+1},indent=2))]:
                if folder+name not in existing: archive.writestr(folder+name,value)
            cover=book['cover_path']
            if cover and cover.startswith('covers/'):
                path=root/cover
                if path.is_file() and folder+'cover'+path.suffix not in existing: archive.write(path,folder+'cover'+path.suffix)
            elif cover and stored_original:
                with zipfile.ZipFile(stored_original) as original:
                    name=folder+'cover'+Path(cover).suffix
                    if name not in existing:
                        with original.open(cover) as source,archive.open(name,'w') as target:
                            while block:=source.read(64*1024): checkpoint();target.write(block)
    return count
