"""Library HTTP layer; storage and EPUB logic remain independently testable."""
import hashlib
import io
import json
import math
import os
import sqlite3
import tempfile
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from epub_library import InvalidEpub, SAFE_MEDIA, iter_sections, parse_epub
from library_store import connect, import_book, save_progress, reading_metrics, reading_percentage, readable_section_sql
from preservation import digest
from storage_paths import original_path as resolve_original

library = Blueprint('library', __name__)


def db_path():
    return Path(current_app.config['DATA_DIR']) / 'webnovel.db'


def page_args():
    try:
        offset = int(request.args.get('offset',0))
        limit = int(request.args.get('limit',100))
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError()
    except ValueError:
        return None
    return offset,limit


def progress_payload(conn, book_id):
    row = conn.execute('SELECT * FROM reading_progress WHERE book_id=?',(book_id,)).fetchone()
    if not row:
        return {'version':0,'section_id':None,'fraction':0,'anchor':'','preferences':{}}
    result = dict(row)
    result['preferences'] = json.loads(result['preferences'])
    follow=conn.execute('SELECT enabled,suspended FROM reading_follow WHERE book_id=?',(book_id,)).fetchone()
    if follow:result['preferences'].update(followAlong=bool(follow['enabled']),followAlongSuspended=bool(follow['suspended']))
    return result


@library.before_request
def same_origin_writes():
    if request.method in {'POST','PUT','DELETE','PATCH'}:
        origin = request.headers.get('Origin')
        if request.headers.get('Sec-Fetch-Site') == 'cross-site' or (origin and urlsplit(origin).netloc != request.host):
            return jsonify(error='Cross-origin writes are not allowed.'),403


@library.app_errorhandler(RequestEntityTooLarge)
def too_large(exc):
    return jsonify(error='Upload exceeds the configured 256 MiB request limit.'),413


@library.route('/')
@library.route('/library')
def home():
    return render_template('shell.html' if request.path=='/' else 'library.html')


@library.route('/reader/<book_id>')
def reader(book_id):
    with connect(db_path()) as conn:
        book = conn.execute('SELECT id,title FROM books WHERE id=?',(book_id,)).fetchone()
    if not book:
        return 'Book not found',404
    return render_template('reader.html', book=dict(book))


@library.route('/api/books')
def books():
    args = page_args()
    if args is None:
        return jsonify(error='Invalid pagination.'),400
    offset,limit = args
    search = request.args.get('q','')[:300]
    # All sorting/filter expressions are selected from fixed SQL, never input SQL.
    sort=request.args.get('sort','added')
    ordering={'added':'b.created_at DESC,b.id','title':'lower(b.title),b.id',
              'recent':'COALESCE(bp.opened_at,p.updated_at,l.updated_at,b.created_at) DESC,b.id'}
    if sort not in ordering:
        return jsonify(error='Invalid sort.'),400
    conditions=['instr(lower(b.title || " " || b.author),lower(?)) > 0',
                'COALESCE(v.hidden,0)='+('1' if request.args.get('archived')=='1' else '0')]
    # Discovery creates a recoverable book record before any chapter download.
    # A cover/catalog alone is not a readable library book, for either client.
    readable="EXISTS(SELECT 1 FROM book_sections real WHERE real.book_id=b.id AND "+readable_section_sql("real")+")"
    conditions.append(('NOT ' if request.args.get('pending')=='1' else '')+readable)
    params=[search]
    if request.args.get('favorite')=='1': conditions.append('bp.favorite=1')
    kind=request.args.get('kind','')
    if kind:
        if kind not in {'epub','webnovel'}: return jsonify(error='Invalid book type.'),400
        conditions.append('b.source_kind=?');params.append(kind)
    with connect(db_path()) as conn:
        joins=' FROM books b LEFT JOIN reading_progress p ON p.book_id=b.id LEFT JOIN listening_progress l ON l.book_id=b.id LEFT JOIN book_preferences bp ON bp.book_id=b.id LEFT JOIN book_visibility v ON v.book_id=b.id '
        where='WHERE '+' AND '.join(conditions)
        total=conn.execute('SELECT COUNT(*)'+joins+where,params).fetchone()[0]
        rows=conn.execute('SELECT b.id,b.title,b.author,b.language,b.source_kind,b.cover_path,b.created_at, '
                          '(SELECT COUNT(*) FROM book_sections s WHERE s.book_id=b.id) section_count, (SELECT COALESCE(MAX(id),0) FROM book_sections s WHERE s.book_id=b.id) content_tail, '
                          "(SELECT COUNT(*) FROM book_sections real WHERE real.book_id=b.id AND "+readable_section_sql("real")+") imported_section_count, "
                          "(SELECT COUNT(*) FROM book_sections s WHERE s.book_id=b.id AND "+readable_section_sql("s")+" AND s.position<(SELECT position FROM book_sections cur WHERE cur.id=p.section_id)) reading_position, "
                          'p.section_id,p.fraction,p.updated_at,l.section_id listening_section_id,l.seconds listening_seconds, '
                          "(SELECT status FROM platform_jobs j WHERE j.book_id=b.id AND j.kind='import' ORDER BY created_at DESC,rowid DESC LIMIT 1) import_job_status, "
                          "(SELECT status FROM platform_jobs j WHERE j.book_id=b.id AND j.kind='discover' ORDER BY created_at DESC,rowid DESC LIMIT 1) discovery_job_status, "
                          'COALESCE(bp.favorite,0) favorite,COALESCE(v.hidden,0) hidden,b.original_path IS NOT NULL has_original, '
                          '(SELECT id FROM epub_exports e WHERE e.book_id=b.id ORDER BY created_at DESC LIMIT 1) export_id'+joins+where+' ORDER BY '+ordering[sort]+' LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item['total_section_count']=item['section_count'];item['section_count']=item['readable_section_count']=item['imported_section_count']
        item['content_revision']=str(item['readable_section_count'])+':'+str(item.pop('content_tail'))
        item['reading_percentage']=reading_percentage(item['reading_position'] if item['section_id'] else None,item['fraction'] or 0,item['readable_section_count'])
        imported=item['imported_section_count']>0
        stage=item.get('import_job_status')
        item['import_state']=('partially_imported' if stage in ('failed','cancelled','running','queued','paused') else 'imported') if imported else 'import_failed' if stage=='failed' or item.get('discovery_job_status')=='failed' else 'importing_chapters' if stage in ('running','queued','paused') else 'completed_zero_chapters' if stage=='completed' else 'pending_import' if item.get('discovery_job_status')=='completed' else 'discovered_metadata'
        item['cover_url'] = f'/api/books/{item["id"]}/cover' if item.pop('cover_path') else None
        item['download_url']=f'/api/books/{item["id"]}/original' if item.pop('has_original') else '/api/exports/'+item['export_id'] if item['export_id'] else None
        items.append(item)
    return jsonify(items=items,total=total,offset=offset,limit=limit)


@library.route('/api/books/upload', methods=['POST'])
def upload():
    file = request.files.get('file')
    if file is None or not file.filename:
        return jsonify(error='Choose an EPUB file.'),400
    directory = Path(current_app.config['DATA_DIR'])/'books'
    directory.mkdir(parents=True,exist_ok=True)
    temporary = None
    published = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory,suffix='.upload',delete=False) as stream:
            temporary = Path(stream.name)
            hasher = hashlib.sha256()
            for block in iter(lambda:file.stream.read(1024*1024),b''):
                stream.write(block)
                hasher.update(block)
        file_hash = hasher.hexdigest()
        with connect(db_path()) as conn:
            existing = conn.execute('SELECT id FROM books WHERE file_hash=?',(file_hash,)).fetchone()
        if existing:
            with connect(db_path()) as conn:
                if conn.execute("SELECT 1 FROM book_deletions WHERE book_id=? AND status!='completed'",(existing['id'],)).fetchone():
                    return jsonify(error='Permanent deletion is pending for this EPUB.'),409
                with conn: conn.execute('UPDATE book_visibility SET hidden=0 WHERE book_id=?',(existing['id'],))
            return jsonify(id=existing['id'],created=False),200
        book_id = str(uuid.uuid5(uuid.NAMESPACE_URL,'epub:sha256:'+file_hash))
        parsed = parse_epub(temporary,book_id,stream=True)
        original = directory/(file_hash+'.epub')
        if not original.exists():
            # Atomic publication; filename is derived solely from validated content.
            os.replace(temporary,original)
            published = original
            temporary = None
        elif digest(original) != file_hash:
            return jsonify(error='Stored EPUB checksum mismatch; existing file was preserved.'),409
        parsed['sections'] = iter_sections(parsed,original)
        identifier,created = import_book(db_path(),parsed,original,file_hash)
        if parsed.get('cover'):
            try:
                from cover_art import refresh_cover
                refresh_cover(current_app.config['DATA_DIR'],identifier,force=True)
            except (ValueError,ImportError,OSError):
                current_app.logger.info('Embedded cover remains available from the original EPUB.')
        return jsonify(id=identifier,created=created),201 if created else 200
    except InvalidEpub as exc:
        # Streaming can discover invalid text after publication. Remove only a
        # file created by this failed request, never any pre-existing original.
        if published is not None:
            with connect(db_path()) as conn:
                registered = conn.execute('SELECT 1 FROM books WHERE file_hash=?',(file_hash,)).fetchone()
            if not registered:
                try:
                    published.unlink(missing_ok=True)
                except OSError:
                    current_app.logger.warning('Invalid new upload could not be cleaned up')
        return jsonify(error=str(exc)),400
    except (OSError,sqlite3.Error):
        current_app.logger.exception('EPUB storage failed; original files were preserved')
        return jsonify(error='Could not store this EPUB. Check free space and storage availability.'),507
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@library.route('/api/books/<book_id>')
def book_detail(book_id):
    with connect(db_path()) as conn:
        row = conn.execute('SELECT * FROM books WHERE id=?',(book_id,)).fetchone()
        if not row:
            return jsonify(error='Book not found.'),404
        result = dict(row)
        result['hidden']=bool(conn.execute('SELECT 1 FROM book_visibility WHERE book_id=? AND hidden=1',(book_id,)).fetchone())
        result.pop('original_path')
        result.pop('file_hash')
        result['legacy_audio_count']=conn.execute('SELECT COUNT(*) FROM library_audio_assets a JOIN book_sections s ON s.id=a.section_id WHERE s.book_id=?',(book_id,)).fetchone()[0]
        result['section_count'] = conn.execute('SELECT COUNT(*) FROM book_sections WHERE book_id=?',(book_id,)).fetchone()[0]
        first = conn.execute('SELECT id FROM book_sections WHERE book_id=? AND '+readable_section_sql()+' ORDER BY position LIMIT 1',(book_id,)).fetchone()
        result['first_section_id'] = first['id'] if first else None
        result['last_position']=conn.execute('SELECT MAX(position) FROM book_sections WHERE book_id=?',(book_id,)).fetchone()[0]
        discovered=conn.execute('SELECT MAX(position) FROM import_chapters WHERE book_id=?',(book_id,)).fetchone()[0]
        result['position_count']=max(result['last_position'] if result['last_position'] is not None else -1,discovered if discovered is not None else -1)+1
        source=conn.execute('SELECT url,refresh_interval FROM import_sources WHERE book_id=?',(book_id,)).fetchone()
        result['source_url']=source[0] if source else None
        result['refresh_interval']=source[1] if source else 0
        result['progress'] = progress_payload(conn,book_id)
        result['total_section_count']=result['section_count']
        result.update(reading_metrics(conn,book_id,result['progress'].get('section_id'),result['progress'].get('fraction',0)))
        result['section_count']=result['readable_section_count']
        result['content_revision']=str(result['readable_section_count'])+':'+str(conn.execute('SELECT COALESCE(MAX(id),0) FROM book_sections WHERE book_id=?',(book_id,)).fetchone()[0])
        listening=conn.execute('SELECT * FROM listening_progress WHERE book_id=?',(book_id,)).fetchone()
        result['resume_audio']=dict(listening) if listening and not result['progress']['preferences'].get('followAlongSuspended',False) and (not result['progress'].get('section_id') or listening['updated_at']>result['progress'].get('updated_at','')) else None
        if result['resume_audio'] and listening['asset_id']:
            elapsed=listening['seconds'];index=0
            for chunk in conn.execute('SELECT position,duration FROM audio_chunks WHERE asset_id=? ORDER BY position',(listening['asset_id'],)):
                if not chunk['duration']:continue
                index=chunk['position']
                if elapsed<chunk['duration']:break
                elapsed-=chunk['duration']
            result['resume_audio']['segment_index']=index
        result['cover_url'] = f'/api/books/{book_id}/cover' if result.pop('cover_path') else None
    return jsonify(result)


@library.route('/api/books/<book_id>/sections')
def sections(book_id):
    args = page_args()
    if args is None:
        return jsonify(error='Invalid pagination.'),400
    offset,limit = args
    search = request.args.get('q','')[:300]
    with connect(db_path()) as conn:
        if not conn.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone():
            return jsonify(error='Book not found.'),404
        total = conn.execute(f'SELECT COUNT(*) FROM book_sections WHERE book_id=? AND {readable_section_sql()} AND instr(lower(title),lower(?))>0',(book_id,search)).fetchone()[0]
        around=request.args.get('around')
        if around is not None:
            if len(around)>18 or not around.isascii() or not around.isdigit() or int(around)<=0:
                return jsonify(error='Invalid current chapter.'),400
            current=conn.execute('SELECT position FROM book_sections WHERE book_id=? AND id=?',(book_id,int(around))).fetchone()
            if current is None:
                return jsonify(error='Chapter not found in this book.'),404
            rank=conn.execute('SELECT COUNT(*) FROM book_sections WHERE book_id=? AND position<? AND '+readable_section_sql()+' AND instr(lower(title),lower(?))>0',(book_id,current['position'],search)).fetchone()[0]
            offset=rank//limit*limit
        rows = conn.execute(f'''SELECT id,position,title,href,linear,legacy_chapter_id FROM book_sections
                               WHERE book_id=? AND {readable_section_sql()} AND instr(lower(title),lower(?))>0 ORDER BY position LIMIT ? OFFSET ?''',
                            (book_id,search,limit,offset)).fetchall()
    return jsonify(items=[dict(r) for r in rows],total=total,offset=offset,limit=limit)


@library.route('/api/books/<book_id>/navigation')
def navigation(book_id):
    args = page_args()
    if args is None:
        return jsonify(error='Invalid pagination.'),400
    offset,limit = args
    with connect(db_path()) as conn:
        if not conn.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone():
            return jsonify(error='Book not found.'),404
        total = conn.execute('SELECT COUNT(*) FROM book_navigation WHERE book_id=?',(book_id,)).fetchone()[0]
        if total:
            rows = conn.execute("SELECT n.title,n.href,n.depth,(SELECT s.id FROM book_sections s WHERE s.book_id=n.book_id AND s.href=CASE WHEN instr(n.href,'#')>0 THEN substr(n.href,1,instr(n.href,'#')-1) ELSE n.href END ORDER BY s.position LIMIT 1) id FROM book_navigation n WHERE n.book_id=? ORDER BY n.position LIMIT ? OFFSET ?",
                                (book_id,limit,offset)).fetchall()
        else:
            response=sections(book_id)
            if isinstance(response,tuple):return response
            payload=response.get_json();payload['reading_order']=True
            return jsonify(payload)
    items=[dict(r) for r in rows]
    for item in items:item['anchor']=item['href'].partition('#')[2]
    return jsonify(items=items,total=total,offset=offset,limit=limit,reading_order=False)


@library.route('/api/books/<book_id>/resolve')
def resolve_section(book_id):
    target = request.args.get('href','')
    href,_,anchor = target.partition('#')
    with connect(db_path()) as conn:
        row = conn.execute('SELECT id FROM book_sections WHERE book_id=? AND href=?',(book_id,href)).fetchone()
    if not row:
        return jsonify(error='Reading destination not found.'),404
    return jsonify(id=row['id'],anchor=anchor)


@library.route('/api/books/<book_id>/sections/<int:section_id>')
def section(book_id,section_id):
    with connect(db_path()) as conn:
        row = conn.execute('SELECT * FROM book_sections WHERE book_id=? AND id=?',(book_id,section_id)).fetchone()
        if not row:
            return jsonify(error='Section not found.'),404
        result = dict(row)
        result.pop('text_content')
        result.update(reading_metrics(conn,book_id,section_id))
        for direction,comparison,order in [('previous','<','DESC'),('next','>','ASC')]:
            neighbor = conn.execute(f'SELECT id FROM book_sections WHERE book_id=? AND {readable_section_sql()} AND position {comparison} ? ORDER BY position {order} LIMIT 1',
                                    (book_id,row['position'])).fetchone()
            result[direction+'_id'] = neighbor['id'] if neighbor else None
    return jsonify(result)


@library.route('/api/books/<book_id>/progress',methods=['GET','PUT'])
def progress(book_id):
    with connect(db_path()) as conn:
        if not conn.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone():
            return jsonify(error='Book not found.'),404
        if request.method=='GET':
            return jsonify(progress_payload(conn,book_id))
    data = request.get_json(silent=True)
    try:
        if not isinstance(data,dict):
            raise ValueError('Expected JSON progress.')
        section_id,version = data.get('section_id'),data.get('version')
        fraction = data.get('fraction')
        anchor,prefs = data.get('anchor',''),data.get('preferences',{})
        if type(section_id) is not int or type(version) is not int or version<0:
            raise ValueError('Invalid section or version.')
        if type(fraction) not in (int,float) or not math.isfinite(fraction) or not 0<=fraction<=1:
            raise ValueError('Invalid reading position.')
        if not isinstance(anchor,str) or len(anchor)>512 or not isinstance(prefs,dict):
            raise ValueError('Invalid locator or preferences.')
        allowed = {'theme','font','fontSize','lineHeight','width','paragraphSpacing','margin','alignment','mode','followAlong','followAlongSuspended'}
        if any(key in prefs and type(prefs[key]) is not bool for key in ['followAlong','followAlongSuspended']):
            raise ValueError('Invalid Follow Along preference.')
        if set(prefs)-allowed:
            raise ValueError('Unknown reader preference.')
        if prefs.get('theme','dark') not in {'gray','dark','lightgray','light','sepia'} or prefs.get('font','serif') not in {'serif','sans','mono'}:
            raise ValueError('Invalid reader theme or font.')
        if prefs.get('alignment','left') not in {'left','justify'} or prefs.get('mode','scroll') not in {'scroll','pages'}:
            raise ValueError('Invalid reading mode or alignment.')
        for key,low,high in [('paragraphSpacing',.5,2),('margin',12,60),('fontSize',14,32),('lineHeight',1.2,2.4),('width',480,1000)]:
            value = prefs.get(key,{'fontSize':20,'lineHeight':1.8,'width':760,'paragraphSpacing':1.1,'margin':24}[key])
            if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:
                raise ValueError('Invalid reader layout.')
        inherit=data.get('inherit_theme')
        if inherit is not None and type(inherit) is not bool:raise ValueError('Invalid theme inheritance.')
        updated = save_progress(db_path(),book_id,section_id,fraction,anchor,prefs,version,inherit_theme=inherit)
    except ValueError as exc:
        return jsonify(error=str(exc)),400
    if updated is None:
        with connect(db_path()) as conn:
            latest = progress_payload(conn,book_id)
        return jsonify(error='Reading progress changed in another reader. Reload to resume it.',progress=latest),409
    return jsonify(version=updated)


def resource(book_id,path=None,cover=False):
    with connect(db_path()) as conn:
        book = conn.execute('SELECT original_path,file_hash,cover_path FROM books WHERE id=?',(book_id,)).fetchone()
        if not book:
            return jsonify(error='Book not found.'),404
        if cover:
            path = book['cover_path']
        entry = conn.execute('SELECT media_type FROM book_resources WHERE book_id=? AND path=?',(book_id,path)).fetchone()
    if cover and path and path.startswith('covers/'):
        stored = (Path(current_app.config['DATA_DIR'])/path).resolve()
        root = (Path(current_app.config['DATA_DIR'])/'covers').resolve()
        if stored.is_relative_to(root) and stored.is_file():
            response = send_file(stored,mimetype={'.png':'image/png','.jpg':'image/jpeg','.webp':'image/webp'}.get(stored.suffix,'application/octet-stream'))
            response.headers['X-Content-Type-Options']='nosniff'
            return response
    stored_original=resolve_original(current_app.config['DATA_DIR'],book)
    if not entry or entry['media_type'] not in SAFE_MEDIA or not stored_original:
        return jsonify(error='Resource not available.'),404
    try:
        with zipfile.ZipFile(stored_original) as archive:
            payload = archive.read(path)
    except (OSError,KeyError,zipfile.BadZipFile):
        return jsonify(error='Stored resource is unavailable.'),404
    response = send_file(io.BytesIO(payload),mimetype=entry['media_type'],conditional=True)
    response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'private, max-age=86400'
    return response


@library.route('/api/books/<book_id>/cover')
def cover(book_id):
    return resource(book_id,cover=True)


@library.route('/api/books/<book_id>/resources/<path:path>')
def book_resource(book_id,path):
    return resource(book_id,path)


@library.route('/api/books/<book_id>/original')
def original(book_id):
    with connect(db_path()) as conn:
        book = conn.execute('SELECT title,original_path,file_hash FROM books WHERE id=?',(book_id,)).fetchone()
    stored_original=resolve_original(current_app.config['DATA_DIR'],book) if book else None
    if not stored_original:
        return jsonify(error='Original EPUB not available.'),404
    return send_file(stored_original,as_attachment=True,download_name=book['title']+'.epub',mimetype='application/epub+zip')


@library.route('/api/books/<book_id>/bookmarks', methods=['GET','POST','DELETE'])
def bookmarks(book_id):
    with connect(db_path()) as conn:
        if not conn.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone():
            return jsonify(error='Book not found.'),404
        if request.method=='GET':
            return jsonify(items=[dict(r) for r in conn.execute('SELECT * FROM book_bookmarks WHERE book_id=? ORDER BY created_at DESC,id DESC LIMIT 200',(book_id,))])
        data=request.get_json(silent=True) or {}
        if request.method=='DELETE':
            with conn: conn.execute('DELETE FROM book_bookmarks WHERE book_id=? AND id=?',(book_id,data.get('id')))
            return jsonify(deleted=True)
        section_id=data.get('section_id');fraction=data.get('fraction');anchor=data.get('anchor','')
        if not isinstance(anchor,str) or len(anchor)>512:return jsonify(error='Invalid bookmark anchor.'),400
        if type(section_id) is not int or type(fraction) not in (int,float) or not math.isfinite(fraction) or not 0<=fraction<=1:
            return jsonify(error='Invalid bookmark location.'),400
        section=conn.execute('SELECT title FROM book_sections WHERE book_id=? AND id=?',(book_id,section_id)).fetchone()
        if not section: return jsonify(error='Chapter does not belong to this book.'),400
        with conn: cursor=conn.execute('INSERT INTO book_bookmarks(book_id,section_id,fraction,title,anchor) VALUES(?,?,?,?,?)',(book_id,section_id,fraction,section['title'],anchor))
        return jsonify(id=cursor.lastrowid),201

@library.route('/api/books/<book_id>/search')
def search_book(book_id):
    args=page_args()
    if args is None:return jsonify(error='Invalid pagination.'),400
    offset,limit=args;q=request.args.get('q','').strip()[:200]
    if len(q)<2:return jsonify(items=[],total=0,offset=offset,limit=limit)
    with connect(db_path()) as conn:
        if not conn.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone():return jsonify(error='Book not found.'),404
        where=' FROM book_sections WHERE book_id=? AND instr(lower(title || " " || COALESCE(text_content,"")),lower(?))>0'
        total=conn.execute('SELECT COUNT(*)'+where,(book_id,q)).fetchone()[0]
        rows=conn.execute('SELECT id,position,title'+where+' ORDER BY position LIMIT ? OFFSET ?',(book_id,q,limit,offset)).fetchall()
    return jsonify(items=[dict(r) for r in rows],total=total,offset=offset,limit=limit)
