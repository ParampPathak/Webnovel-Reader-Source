"""Book details, audio/player, durable jobs, importer, and export HTTP API."""
import json
import math
import os
import time
import uuid
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint,current_app,jsonify,render_template,request,send_file
from audio_providers import DEFAULT_VOICE,FALLBACK_VOICES,EdgeProvider,cache_key,settings,chapter_text_chunks,speech_error_message
from library_api import db_path,page_args,same_origin_writes
from library_store import connect
from platform_store import control,job_payload,schedule,worker_state
from safe_http import public_url
from audio_cache import protect_audio_response

platform=Blueprint('platform',__name__)
platform.before_request(same_origin_writes)


@platform.route('/api/compatibility')
def compatibility():
    return jsonify(api_contract=1,minimum_native_contract=1,release='20261025-source1',
                   capabilities=['separate-reading-listening','reader-text-start','library-persisted-content','source-config-revisions','source-replacement-previews'])


@platform.route('/api/chapter-updates/health')
def chapter_update_health():
    scheduler=current_app.extensions.get('chapter_updates_scheduler')
    with connect(db_path()) as conn:
        enabled=conn.execute('SELECT COUNT(*) FROM chapter_updates WHERE enabled=1').fetchone()[0]
    return jsonify(version='20261022-buffer1',scheduler_running=bool(scheduler and scheduler.is_alive()),enabled_books=enabled)


@platform.route('/api/books/<book_id>/chapter-updates', methods=['GET','PUT','POST'])
def chapter_update_settings(book_id):
    from chapter_updates import status, configure, request_check, SourceConflict
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
    if request.method == 'PUT':
        try:result=configure(db_path(),book_id,body())
        except SourceConflict as e:return jsonify(error=str(e)),409
    elif request.method == 'POST':
        result=request_check(db_path(),book_id,body())
    else:result=status(db_path(),book_id)
    scheduler=current_app.extensions.get('chapter_updates_scheduler')
    result['scheduler_running']=bool(scheduler and scheduler.is_alive())
    return jsonify(result),202 if request.method=='POST' else 200


@platform.route('/api/books/<book_id>/chapter-updates/preview', methods=['POST'])
def chapter_source_preview(book_id):
    from source_replacement import preview
    from chapter_updates import SourceConflict
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
    try:
        return jsonify(preview(db_path(),book_id,body()))
    except SourceConflict as exc:
        return jsonify(error=str(exc)),409


def body():
    value=request.get_json(silent=True)
    if not isinstance(value,dict): raise ValueError('Expected a JSON object.')
    return value


def book_exists(conn,book_id):
    if not conn.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone():
        raise ValueError('Book not found.')
    if conn.execute('SELECT 1 FROM book_visibility WHERE book_id=? AND hidden=1',(book_id,)).fetchone():
        raise ValueError('This book was removed from the library. Restore it before starting new work.')


def chapter_range(conn,book_id,data,table='book_sections'):
    max_position=conn.execute(f'SELECT MAX(position) FROM {table} WHERE book_id=?',(book_id,)).fetchone()[0]
    if max_position is None: raise ValueError('This book has no chapters yet.')
    first,last=data.get('first',1),data.get('last',max_position+1)
    if type(first) is not int or type(last) is not int or not 1<=first<=last<=max_position+1:
        raise ValueError('Enter a valid inclusive chapter range, starting at 1.')
    if not conn.execute(f'SELECT 1 FROM {table} WHERE book_id=? AND position BETWEEN ? AND ? LIMIT 1',(book_id,first-1,last-1)).fetchone():
        raise ValueError('No chapters are available in this range.')
    return first-1,last-1


@platform.errorhandler(ValueError)
def invalid(exc): return jsonify(error=str(exc)),400


@platform.route('/books/<book_id>')
def details_page(book_id):
    with connect(db_path()) as conn:
        row=conn.execute('SELECT id,title FROM books WHERE id=?',(book_id,)).fetchone()
    if not row: return 'Book not found',404
    return render_template('book.html',book=dict(row))


@platform.route('/importer')
@platform.route('/jobs')
@platform.route('/settings')
def dashboard(): return render_template('dashboard.html',page=request.path.strip('/'))


def speech_preferences(data):
    with connect(db_path()) as conn:
        row=conn.execute("SELECT value FROM platform_settings WHERE key='tts_preferences'").fetchone()
    default=json.loads(row[0]) if row else settings()
    return settings(data.get('voice',default['voice']),data.get('rate',default['rate']))


@platform.route('/api/edge/voices')
def voices():
    with connect(db_path()) as conn:
        cached=conn.execute("SELECT value FROM platform_settings WHERE key='edge_voices'").fetchone()
    result=json.loads(cached[0]) if cached else {'items':FALLBACK_VOICES,'updated':None,'source':'fallback'}
    result['online_required']=True
    result['items']=list({r['ShortName']:r for r in FALLBACK_VOICES+result['items']}.values())
    for item in result['items']:
        if item['ShortName']=='en-US-AndrewNeural': item['DisplayName']='Andrew — English (US)'
    with connect(db_path()) as conn:
        saved=conn.execute("SELECT value FROM platform_settings WHERE key='tts_preferences'").fetchone()
    result['default_voice']=json.loads(saved[0])['voice'] if saved else DEFAULT_VOICE
    result['default_rate']=json.loads(saved[0])['rate'] if saved else 0
    return jsonify(result)


@platform.route('/api/edge/preferences',methods=['GET','PUT'])
def edge_preferences():
    import math
    with connect(db_path()) as conn:
        if request.method=='PUT':conn.execute('BEGIN IMMEDIATE')
        row=conn.execute("SELECT value FROM platform_settings WHERE key='tts_preferences'").fetchone()
        previous=json.loads(row[0]) if row else settings()
        data=body() if request.method=='PUT' else {}
        value=settings(data.get('voice',previous['voice']),data.get('rate',previous['rate']))
        for key,default,low,high in [('playback_rate',1,.25,4),('volume',1,0,1)]:
            number=data.get(key,previous.get(key,default))
            if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not low<=number<=high:
                raise ValueError('Invalid global '+key+'.')
            value[key]=number
        if request.method=='PUT':
            conn.execute("INSERT OR REPLACE INTO platform_settings(key,value) VALUES('tts_preferences',?)",(json.dumps(value),));conn.commit()
    return jsonify(value)


@platform.route('/api/edge/voices/refresh',methods=['POST'])
def refresh_voices():
    # Explicit bounded network action; regular library loads never contact Edge.
    try:
        items=EdgeProvider().voices()
    except Exception as exc:
        return jsonify(error='Could not refresh Edge voices. Check the installed package and internet access.',detail=str(exc)[:300]),503
    value={'items':[{'ShortName':r['ShortName'],'Locale':r['Locale'],'Gender':r['Gender']} for r in items],
           'updated':time.time(),'source':'live'}
    with connect(db_path()) as conn:
        with conn: conn.execute("INSERT OR REPLACE INTO platform_settings(key,value) VALUES('edge_voices',?)",(json.dumps(value),))
    return jsonify(value)


@platform.route('/api/books/<book_id>/audio/jobs',methods=['POST'])
def audio_job(book_id):
    data=body();config=speech_preferences(data)
    section_id=data.get('section_id')
    priority=-10 if data.get('prefetch') is True else 100
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
        if section_id is not None:
            if type(section_id) is not int: raise ValueError('Invalid chapter.')
            row=conn.execute('SELECT id FROM book_sections WHERE book_id=? AND id=?',(book_id,section_id)).fetchone()
            if not row: raise ValueError('Chapter does not belong to this book.')
            existing=conn.execute('''SELECT j.id FROM platform_jobs j JOIN platform_items i ON i.job_id=j.id
                WHERE j.book_id=? AND j.kind='audio' AND j.config=? AND i.item_key=?
                AND i.status IN ('pending','running') AND j.status IN ('queued','running') LIMIT 1''',
                (book_id,json.dumps(config),str(section_id))).fetchone()
            if existing:
                with conn: conn.execute('UPDATE platform_items SET priority=MAX(priority,?),requested_at=CASE WHEN ? >=100 THEN ? ELSE requested_at END WHERE job_id=? AND item_key=?',(priority,priority,time.time(),existing[0],str(section_id)))
                from platform_store import notify_worker
                notify_worker(db_path())
                return jsonify(id=existing[0],created=False),200
            rows=[row]
        else:
            first,last=chapter_range(conn,book_id,data)
            rows=conn.execute('SELECT id FROM book_sections WHERE book_id=? AND position BETWEEN ? AND ? ORDER BY position',(book_id,first,last))
        identifier=schedule(db_path(),'audio',book_id,config,((r['id'],{'section_id':r['id']}) for r in rows),priority if section_id is not None else 0)
    return jsonify(id=identifier,created=True,**worker_state(current_app.config['DATA_DIR'])),202


def local_audio_path(value):
    root=Path(current_app.config['DATA_DIR']).resolve()
    path=Path(value or '')
    if str(path).startswith('/data/'):
        path=root/str(path)[6:]
    if not path.is_absolute(): path=root/path
    resolved=path.resolve()
    # Legacy migrated paths may refer to original storage in copied-data tests;
    # only serve data owned by this configured instance.
    if not resolved.is_relative_to(root): return None
    return resolved if resolved.is_file() else None


@platform.route('/api/books/<book_id>/audio')
def audio_status(book_id):
    try: section_id=int(request.args.get('section_id','0'));rate=int(request.args.get('rate','0'))
    except ValueError: raise ValueError('Invalid chapter or synthesis speed.')
    provider=request.args.get('provider','edge')
    with connect(db_path()) as conn:
        section=conn.execute('SELECT id,text_content FROM book_sections WHERE book_id=? AND id=?',(book_id,section_id)).fetchone()
        if not section: return jsonify(error='Chapter not found.'),404
        if provider=='legacy':
            # Prefer the legacy worker's current ready path; the preservation
            # registry remains a fallback for older retained assets.
            row=conn.execute('''SELECT c.id,c.audio_path,c.voice_used voice FROM chapters c
                    JOIN book_sections s ON s.legacy_chapter_id=c.id WHERE s.id=? AND c.status='ready' ''',(section_id,)).fetchone()
            if not row or not local_audio_path(row['audio_path']):
                row=conn.execute('SELECT * FROM library_audio_assets WHERE section_id=?',(section_id,)).fetchone()
            if row and local_audio_path(row['audio_path']):
                return jsonify(status='ready',asset_id='legacy-'+str(section_id),url=f'/api/audio/legacy-{section_id}',voice=row['voice'],provider='legacy')
            return jsonify(status='missing',provider='legacy')
        if provider!='edge': raise ValueError('Unknown audio provider.')
        config=speech_preferences({**({'voice':request.args['voice']} if 'voice' in request.args else {}),**({'rate':rate} if 'rate' in request.args else {})})
        key,_=cache_key(book_id,section_id,section['text_content'],config)
        row=conn.execute('SELECT * FROM audio_variants WHERE id=?',(key,)).fetchone()
        segments=[]
        for chunk in conn.execute('SELECT position,path,duration FROM audio_chunks WHERE asset_id=? ORDER BY position',(key,)):
            if chunk['position']!=len(segments) or not chunk['duration'] or not local_audio_path(chunk['path']): break
            segments.append({'index':chunk['position'],'duration':chunk['duration'],'url':f'/api/audio/{key}/segments/{chunk["position"]}'})
        if row and row['status']=='ready' and local_audio_path(row['path']):
            return jsonify(status='ready',asset_id=key,url='/api/audio/'+key,provider='edge',voice=config['voice'],segments=segments,complete=True,timeline_complete=len(segments)==row['chunk_count'],**worker_state(current_app.config['DATA_DIR']))
        chunks=conn.execute('SELECT COUNT(*) FROM audio_chunks WHERE asset_id=?',(key,)).fetchone()[0]
        job=conn.execute('''SELECT j.id,j.status,i.error,i.status item_status,i.next_attempt FROM platform_jobs j JOIN platform_items i ON i.job_id=j.id
                           WHERE j.book_id=? AND j.kind='audio' AND j.config=? AND i.item_key=?
                           ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1''',(book_id,json.dumps(config),str(section_id))).fetchone()
    return jsonify(status='streaming' if segments else job['status'] if job and job['status']!='completed' else 'missing',segments=segments,complete=False,generation_status=job['status'] if job else 'missing',job_id=job['id'] if job else None,
                   completed_chunks=chunks,error=speech_error_message(job['error']) if job else '',asset_id=key,
                   item_status=job['item_status'] if job else None,retry_at=job['next_attempt'] if job else None,
                   **worker_state(current_app.config['DATA_DIR']))


@platform.route('/api/audio/<asset_id>/segments/<int:position>')
@protect_audio_response
def audio_segment(asset_id,position):
    with connect(db_path()) as conn:
        row=conn.execute('SELECT c.path,c.duration FROM audio_chunks c JOIN audio_variants a ON a.id=c.asset_id WHERE c.asset_id=? AND c.position=?',(asset_id,position)).fetchone()
    path=local_audio_path(row['path']) if row and row['duration'] else None
    if not path: return jsonify(error='Audio segment is not published yet.'),404
    response=send_file(path,conditional=True,mimetype='audio/mpeg')
    response.headers['Cache-Control']='private, max-age=86400'
    return response


@platform.route('/api/audio/<asset_id>/location/<int:position>')
def narration_location(asset_id,position):
    """Reconstruct existing persisted chunk boundaries; never synthesize audio."""
    with connect(db_path()) as conn:
        row=conn.execute('''SELECT a.book_id,a.section_id,a.content_hash,a.first_chunk_bytes,a.chunk_plan,s.text_content,c.path,c.checksum
            FROM audio_variants a JOIN book_sections s ON s.id=a.section_id
            JOIN audio_chunks c ON c.asset_id=a.id AND c.position=? AND c.duration>0 WHERE a.id=?''',(position,asset_id)).fetchone()
    if not row: return jsonify(error='Published narration segment not found.'),404
    if hashlib.sha256(row['text_content'].encode('utf-8')).hexdigest()!=row['content_hash']:
        return jsonify(error='Book text changed; this cached audio cannot be highlighted accurately.'),409
    offset=0;normalized_offset=0;previous_space=True
    for index,text in enumerate(chapter_text_chunks(row['text_content'],row['first_chunk_bytes'],json.loads(row['chunk_plan']) if row['chunk_plan'] else None)):
        normalized=re.sub(r'\s+',' ',text)
        if previous_space:normalized=normalized.lstrip()
        if index==position:
            from narration_timing import load
            cues=load(local_audio_path(row['path']),row['checksum'],text)
            return jsonify(normalized_offset=normalized_offset+(1 if normalized.startswith(' ') else 0),book_id=row['book_id'],section_id=row['section_id'],segment_index=index,
                quote=re.sub(r'\s+',' ',text).strip(),fraction=offset/max(1,len(row['text_content'])),
                anchor=f'narration:{asset_id}:{index}',precision='word' if cues else 'chunk',cues=cues)
        normalized_offset+=len(normalized.encode('utf-16-le'))//2;previous_space=normalized.endswith(' ') if normalized else previous_space
        offset+=len(text)
    return jsonify(error='Cached segment has no matching text boundary.'),409



@platform.route('/api/audio/<asset_id>/start')
def narration_start(asset_id):
    """Resolve a visible-text offset against the existing immutable chunk plan."""
    try: requested=int(request.args.get('offset',''))
    except ValueError: raise ValueError('Invalid reading offset.')
    with connect(db_path()) as conn:
        row=conn.execute('SELECT a.*,s.text_content FROM audio_variants a JOIN book_sections s ON s.id=a.section_id WHERE a.id=?',(asset_id,)).fetchone()
        if not row: return jsonify(error='Narration plan is not available yet.'),404
        published={c['position']:dict(c) for c in conn.execute('SELECT position,duration,path,checksum FROM audio_chunks WHERE asset_id=? ORDER BY position',(asset_id,)) if c['duration'] and local_audio_path(c['path'])}
        durations={i:c['duration'] for i,c in published.items()}
    if hashlib.sha256(row['text_content'].encode('utf-8')).hexdigest()!=row['content_hash']:
        return jsonify(error='Book text changed; the cached narration position cannot be resolved.'),409
    total=len(re.sub(r'\s+',' ',row['text_content']).strip().encode('utf-16-le'))//2
    if not 0<=requested<=total: raise ValueError('Reading offset is outside this chapter.')
    offset=0;previous_space=True
    chunks=chapter_text_chunks(row['text_content'],row['first_chunk_bytes'],json.loads(row['chunk_plan']) if row['chunk_plan'] else None)
    for index,text in enumerate(chunks):
        normalized=re.sub(r'\s+',' ',text)
        if previous_space:normalized=normalized.lstrip()
        end=offset+len(normalized.encode('utf-16-le'))//2
        if requested<end or end>=total:
            available=index in durations and all(i in durations for i in range(index))
            from narration_timing import load
            chunk=published.get(index)
            cues=load(local_audio_path(chunk['path']),chunk['checksum'],text) if chunk else []
            cue_base=offset+(1 if normalized.startswith(' ') else 0)
            preceding=[cue for cue in cues if cue_base+cue['offset']<=requested]
            cue=preceding[-1] if preceding else None
            seconds=cue['at'] if cue else 0
            return jsonify(book_id=row['book_id'],section_id=row['section_id'],segment_index=index,segment_seconds=seconds,
                seconds=sum(durations.get(i,0) for i in range(index))+seconds,available=available,precision='word' if cue else 'chunk',normalized_offset=cue_base+cue['offset'] if cue else offset,
                requested_offset=requested,anchor=f'narration:{asset_id}:{index}')
        offset=end;previous_space=normalized.endswith(' ') if normalized else previous_space
    return jsonify(error='Chapter has no speakable narration position.'),409

@platform.route('/api/audio/<asset_id>')
@protect_audio_response
def audio_file(asset_id):
    with connect(db_path()) as conn:
        if asset_id.startswith('legacy-'):
            try: section_id=int(asset_id[7:])
            except ValueError: return jsonify(error='Audio not found.'),404
            row=conn.execute('''SELECT c.audio_path path FROM chapters c JOIN book_sections s ON s.legacy_chapter_id=c.id
                                    WHERE s.id=? AND c.status='ready' ''',(section_id,)).fetchone()
            if not row or not local_audio_path(row['path']):
                row=conn.execute('SELECT audio_path path FROM library_audio_assets WHERE section_id=?',(section_id,)).fetchone()
        else:
            row=conn.execute("SELECT path FROM audio_variants WHERE id=? AND status='ready'",(asset_id,)).fetchone()
    path=local_audio_path(row['path']) if row else None
    if not path: return jsonify(error='Audio is not available.'),404
    response=send_file(path,conditional=True,mimetype='audio/mpeg' if path.suffix=='.mp3' else 'audio/wav')
    response.headers['Cache-Control']='private, max-age=86400'
    return response


def checkpoint_fields(data):
    names=('session_id','session_generation','event_sequence','checkpoint_intent','playback_state','text_anchor','normalized_offset')
    if not any(name in data for name in names) and 'claim_session' not in data:return None
    session=data.get('session_id');generation=data.get('session_generation');sequence=data.get('event_sequence')
    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',session):raise ValueError('Invalid playback session.')
    if any(type(n) is not int or not 1<=n<=9007199254740991 for n in (generation,sequence)):
        raise ValueError('Invalid playback checkpoint ordering.')
    intent=data.get('checkpoint_intent');state=data.get('playback_state');anchor=data.get('text_anchor','');offset=data.get('normalized_offset')
    if intent not in ('playback','seek','chapter-transition','reader-start','restore','pause','stop','foreground'):
        raise ValueError('Invalid listening navigation intent.')
    if state not in ('playing','paused','buffering','recovering','interrupted','error','completed'):
        raise ValueError('Invalid playback checkpoint state.')
    if not isinstance(anchor,str) or len(anchor)>180 or not re.fullmatch(r'(?:text:\d+|narration:[A-Za-z0-9_-]+:\d+(?::word:\d+)?)?',anchor):
        raise ValueError('Invalid listening text anchor.')
    if offset is not None and (type(offset) is not int or not 0<=offset<=1000000000):raise ValueError('Invalid listening text offset.')
    if type(data.get('claim_session',False)) is not bool or not isinstance(data.get('expected_session_id',''),str):
        raise ValueError('Invalid playback session handoff.')
    return dict(session_id=session,session_generation=generation,event_sequence=sequence,
                checkpoint_intent=intent,playback_state=state,text_anchor=anchor,normalized_offset=offset)


@platform.route('/api/books/<book_id>/listening',methods=['GET','PUT'])
def listening(book_id):
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
        previous=conn.execute('SELECT * FROM listening_progress WHERE book_id=?',(book_id,)).fetchone()
        if request.method=='GET':
            default=conn.execute("SELECT value FROM platform_settings WHERE key='tts_preferences'").fetchone()
            config=json.loads(default[0]) if default else settings()
            return jsonify(dict(previous) if previous else {'version':0,'section_id':None,'seconds':0,'playback_rate':1,'voice':config['voice'],'synthesis_rate':config['rate']})
        data=body();section_id=data.get('section_id');asset_id=data.get('asset_id');seconds=data.get('seconds');speed=data.get('playback_rate',1)
        version=data.get('version')
        if type(section_id) is not int or not isinstance(asset_id,str) or len(asset_id)>100 or type(version) is not int:
            raise ValueError('Invalid listening chapter, asset, or version.')
        if any(type(x) not in (float,int) or not math.isfinite(x) for x in (seconds,speed)) or not 0<=seconds<=86400 or not .5<=speed<=3:
            raise ValueError('Invalid listening position or playback speed.')
        if asset_id.startswith('legacy-'):
            if data.get('voice')!='legacy' or data.get('synthesis_rate',0)!=0:
                raise ValueError('Preserved audio uses its original synthesis settings.')
            config={'voice':'legacy','rate':0}
        else:
            config=settings(data.get('voice',DEFAULT_VOICE),data.get('synthesis_rate',0))
        if not conn.execute('SELECT 1 FROM book_sections WHERE id=? AND book_id=?',(section_id,book_id)).fetchone():
            raise ValueError('Chapter does not belong to this book.')
        if asset_id.startswith('legacy-'):
            if asset_id!='legacy-'+str(section_id): raise ValueError('Audio does not belong to this chapter.')
        elif not conn.execute('SELECT 1 FROM audio_variants WHERE id=? AND section_id=? AND book_id=? AND voice=? AND settings=?',(asset_id,section_id,book_id,config['voice'],json.dumps(config,sort_keys=True))).fetchone():
            raise ValueError('Audio does not belong to this chapter.')
        # No chunk cursor is different from a valid exact cursor at chunk zero.
        # Native complete-file timing must still resume correctly if metadata is
        # subsequently filled by the existing worker.
        segment_index=data.get('segment_index');segment_seconds=data.get('segment_seconds')
        has_cursor='segment_index' in data or 'segment_seconds' in data
        if has_cursor and (type(segment_index) is not int or not 0<=segment_index<=100000 or type(segment_seconds) not in (int,float) or not math.isfinite(segment_seconds) or not 0<=segment_seconds<=86400):
            raise ValueError('Invalid progressive listening position.')
        if 'segment_index' in data and not conn.execute('SELECT 1 FROM audio_chunks WHERE asset_id=? AND position=? AND duration>0',(asset_id,segment_index)).fetchone():
            raise ValueError('Listening segment is not published.')
        checkpoint=checkpoint_fields(data)
        conn.execute('BEGIN IMMEDIATE')
        latest=conn.execute('SELECT * FROM listening_progress WHERE book_id=?',(book_id,)).fetchone()
        current=latest['version'] if latest else 0
        owner=latest['session_id'] if latest else '';generation=latest['session_generation'] if latest else 0
        def rejected(code):
            conn.rollback()
            return jsonify(error='Listening position changed in another player. Reload to resume.',code=code,version=current),409
        if checkpoint:
            same=owner==checkpoint['session_id'] and generation==checkpoint['session_generation']
            if same and latest['checkpoint_revision']==current and checkpoint['event_sequence']==latest['event_sequence']:
                # A lost acknowledgement can retry this exact immutable event.
                fields={**checkpoint,'section_id':section_id,'asset_id':asset_id,'seconds':seconds,
                        'playback_rate':speed,'voice':config['voice'],'synthesis_rate':config['rate'],
                        'segment_index':segment_index,'segment_seconds':segment_seconds}
                if all(latest[key]==value for key,value in fields.items()):
                    conn.rollback();return jsonify(version=current,session_id=owner,session_generation=generation,event_sequence=latest['event_sequence'])
                return rejected('stale_checkpoint')
            if not same:
                if not data.get('claim_session') or data.get('expected_session_id','')!=owner or checkpoint['session_generation']!=generation+1:
                    return rejected('session_conflict')
            elif checkpoint['event_sequence']<=latest['event_sequence']:
                return rejected('stale_checkpoint')
        elif owner:
            return rejected('session_conflict')
        if version!=current:
            return rejected('version_conflict')
        conn.execute('''INSERT INTO listening_progress(book_id,section_id,asset_id,seconds,playback_rate,voice,synthesis_rate,version)
                        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
                        section_id=excluded.section_id,asset_id=excluded.asset_id,seconds=excluded.seconds,
                        playback_rate=excluded.playback_rate,voice=excluded.voice,synthesis_rate=excluded.synthesis_rate,
                        version=excluded.version,updated_at=CURRENT_TIMESTAMP''',
                     (book_id,section_id,asset_id,seconds,speed,config['voice'],config['rate'],current+1))
        conn.execute('UPDATE listening_progress SET segment_index=?,segment_seconds=? WHERE book_id=?',(segment_index,segment_seconds,book_id))
        if checkpoint:
            conn.execute('''UPDATE listening_progress SET session_id=?,session_generation=?,event_sequence=?,
                checkpoint_intent=?,playback_state=?,text_anchor=?,normalized_offset=?,checkpoint_revision=? WHERE book_id=?''',
                tuple(checkpoint[name] for name in ('session_id','session_generation','event_sequence','checkpoint_intent','playback_state','text_anchor','normalized_offset'))+(current+1,book_id))
        conn.commit()
    return jsonify(version=current+1,**({key:checkpoint[key] for key in ('session_id','session_generation','event_sequence')} if checkpoint else {}))


@platform.route('/api/jobs')
def jobs():
    args=page_args()
    if args is None: raise ValueError('Invalid pagination.')
    offset,limit=args;book_id=request.args.get('book_id')
    where='WHERE book_id=?' if book_id else '';params=(book_id,) if book_id else ()
    with connect(db_path()) as conn:
        total=conn.execute('SELECT COUNT(*) FROM platform_jobs '+where,params).fetchone()[0]
        identifiers=conn.execute('SELECT id FROM platform_jobs '+where+' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?',params+(limit,offset)).fetchall()
        result=[job_payload(conn,r[0]) for r in identifiers]
    return jsonify(items=result,total=total,**worker_state(current_app.config['DATA_DIR']))


@platform.route('/api/jobs/<identifier>',methods=['GET','POST'])
def job(identifier):
    if request.method=='POST': control(db_path(),identifier,body().get('action'))
    with connect(db_path()) as conn: result=job_payload(conn,identifier)
    if not result: return jsonify(error='Job not found.'),404
    return jsonify(**result,**worker_state(current_app.config['DATA_DIR']))


@platform.route('/api/jobs/<identifier>/items')
def job_items(identifier):
    args=page_args()
    if args is None: raise ValueError('Invalid pagination.')
    offset,limit=args
    with connect(db_path()) as conn:
        total=conn.execute('SELECT COUNT(*) FROM platform_items WHERE job_id=?',(identifier,)).fetchone()[0]
        rows=conn.execute('''SELECT i.id,i.item_key,i.position,i.status,i.attempts,i.error,c.title FROM platform_items i JOIN platform_jobs j ON j.id=i.job_id LEFT JOIN import_chapters c ON c.book_id=j.book_id AND c.url=i.item_key WHERE i.job_id=? ORDER BY i.position LIMIT ? OFFSET ?''',(identifier,limit,offset)).fetchall()
    return jsonify(items=[dict(r) for r in rows],total=total)


@platform.route('/api/imports',methods=['POST'])
def new_import():
    from source_adapters import canonical_source
    data=body();url=canonical_source(data.get('url'))
    parsed=urlsplit(url)
    title=data.get('title') or parsed.hostname
    if not isinstance(title,str) or len(title)>300: raise ValueError('Invalid book title.')
    identifier=str(uuid.uuid5(uuid.NAMESPACE_URL,'webnovel:'+url))
    with connect(db_path()) as conn:
        with conn:
            existing=conn.execute('SELECT book_id FROM import_sources WHERE url=?',(url,)).fetchone()
            if existing: identifier=existing[0]
            conn.execute("INSERT OR IGNORE INTO books(id,title,source_kind,source_key) VALUES(?,?,'webnovel',?)",(identifier,title,'webnovel:'+url))
            conn.execute('INSERT OR IGNORE INTO import_sources(book_id,url) VALUES(?,?)',(identifier,url))
            from chapter_updates import ensure
            ensure(conn,identifier)
    with connect(db_path()) as conn:
        active=conn.execute("SELECT id FROM platform_jobs WHERE book_id=? AND kind='discover' AND status IN ('queued','running','paused') ORDER BY created_at DESC LIMIT 1",(identifier,)).fetchone()
        cached=conn.execute("SELECT id,result FROM platform_jobs WHERE book_id=? AND kind='discover' AND status='completed' ORDER BY created_at DESC,rowid DESC LIMIT 1",(identifier,)).fetchone()
        author=conn.execute('SELECT author FROM books WHERE id=?',(identifier,)).fetchone()[0]
        # Repair legacy incomplete extraction once, only when this source is
        # explicitly opened. Keep its completed selection snapshot throughout.
        if cached and not json.loads(cached['result']).get('metadata_ready') and (author or '').strip().lower() in {'','unknown','unknown author','author unavailable'}:
            cached=None
    if active or cached: return jsonify(book_id=identifier,job_id=(active or cached)[0],created=False),200
    job_id=schedule(db_path(),'discover',identifier,{},[(url,{'url':url})])
    return jsonify(book_id=identifier,job_id=job_id),202


@platform.route('/api/books/<book_id>/import/refresh',methods=['POST'])
def refresh_import(book_id):
    with connect(db_path()) as conn:
        source=conn.execute('SELECT url FROM import_sources WHERE book_id=?',(book_id,)).fetchone()
        active=conn.execute("SELECT id FROM platform_jobs WHERE book_id=? AND kind='discover' AND status IN ('queued','running','paused') ORDER BY created_at DESC,rowid DESC LIMIT 1",(book_id,)).fetchone()
    if active: return jsonify(id=active[0],created=False),200
    if not source: raise ValueError('This book has no web import source.')
    identifier=schedule(db_path(),'discover',book_id,{},[(source[0],{'url':source[0]})])
    return jsonify(id=identifier),202


@platform.route('/api/books/<book_id>/import/chapters')
def import_chapters(book_id):
    args=page_args()
    if args is None: raise ValueError('Invalid pagination.')
    offset,limit=args
    search=request.args.get('q','')[:300]
    def item(row):
        from source_adapters import chapter_identity
        return {**dict(row),**chapter_identity(row['url'],row['title']),'catalog_position':row['position']}
    with connect(db_path()) as conn:
        live=request.args.get('job')
        if live:
            if not conn.execute("SELECT 1 FROM platform_jobs WHERE id=? AND book_id=? AND kind='discover'",(live,book_id)).fetchone():raise ValueError('Discovery does not belong to this book.')
            total=conn.execute('SELECT COUNT(*) FROM discovery_links WHERE job_id=?',(live,)).fetchone()[0]
            rows=conn.execute('SELECT c.*,c.position local_position,l.position FROM discovery_links l JOIN import_chapters c ON c.book_id=? AND c.url=l.url WHERE l.job_id=? ORDER BY l.position LIMIT ? OFFSET ?',(book_id,live,limit,offset)).fetchall()
            return jsonify(items=[{**item(r),'position':offset+i,'catalog_position':offset+i} for i,r in enumerate(rows)],total=total,counts={})
        total=conn.execute('SELECT COUNT(*) FROM import_current WHERE book_id=? AND instr(lower(title),lower(?))>0',(book_id,search)).fetchone()[0]
        rows=conn.execute('SELECT v.*,c.position local_position FROM import_current v JOIN import_chapters c ON c.book_id=v.book_id AND c.url=v.url WHERE v.book_id=? AND instr(lower(v.title),lower(?))>0 ORDER BY v.position LIMIT ? OFFSET ?',(book_id,search,limit,offset)).fetchall()
        counts={r['status']:r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM import_current WHERE book_id=? GROUP BY status',(book_id,))}
    return jsonify(items=[item(r) for r in rows],total=total,counts=counts)


@platform.route('/api/books/<book_id>/import/summary')
def import_summary(book_id):
    with connect(db_path()) as conn:
        source=conn.execute('SELECT url FROM import_sources WHERE book_id=?',(book_id,)).fetchone()
        if not source: raise ValueError('This book has no web import source.')
        count,imported=conn.execute('SELECT COUNT(*),COUNT(section_id) FROM import_current WHERE book_id=?',(book_id,)).fetchone()
        historical=conn.execute('SELECT COUNT(*) FROM import_chapters WHERE book_id=?',(book_id,)).fetchone()[0]
        first=conn.execute('SELECT title FROM import_current WHERE book_id=? ORDER BY position LIMIT 1',(book_id,)).fetchone()
        last=conn.execute('SELECT title FROM import_current WHERE book_id=? ORDER BY position DESC LIMIT 1',(book_id,)).fetchone()
        catalog=conn.execute("SELECT d.job_id,j.updated_at FROM discovery_catalogs d JOIN platform_jobs j ON j.id=d.job_id WHERE d.book_id=? AND j.status='completed' ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1",(book_id,)).fetchone()
        jobs={}
        for kind in ['discover','import','export']:
            row=conn.execute('SELECT id FROM platform_jobs WHERE book_id=? AND kind=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(book_id,kind)).fetchone()
            jobs[kind]=job_payload(conn,row[0]) if row else None
        latest=jobs['discover']
        live_count=conn.execute('SELECT COUNT(*) FROM discovery_links WHERE job_id=?',(latest['id'],)).fetchone()[0] if latest else 0
    return jsonify(source=source[0],discovered=count,live_discovered=live_count,metadata_ready=bool(latest and (latest['result'].get('metadata_ready') or latest['status']=='completed')),imported=imported,historical_retained=historical-count,first_title=first[0] if first else '',last_title=last[0] if last else '',discovery_complete=bool(catalog or (latest and latest['status']=='completed')),catalog_job_id=catalog['job_id'] if catalog else None,catalog_completed_at=catalog['updated_at'] if catalog else None,jobs=jobs,**worker_state(current_app.config['DATA_DIR']))


@platform.route('/api/books/<book_id>/import/jobs',methods=['POST'])
def import_job(book_id):
    data=body()
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
        mode=data.get('mode','range')
        if mode not in {'all','first','range','new'}: raise ValueError('Unknown chapter selection.')
        if mode=='first':
            n=data.get('count')
            if type(n) is not int or n<1: raise ValueError('Enter a positive chapter count.')
            data={**data,'first':1,'last':n}
        elif mode in {'all','new'}: data={**data,'first':1};data.pop('last',None)
        first,last=chapter_range(conn,book_id,data,'import_current')
        selected=conn.execute('SELECT COUNT(*),COUNT(section_id) FROM import_current WHERE book_id=? AND position BETWEEN ? AND ?',(book_id,first,last)).fetchone()
        active=conn.execute("SELECT id FROM platform_jobs WHERE book_id=? AND kind='import' AND status IN ('queued','running','paused') ORDER BY created_at DESC,rowid DESC LIMIT 1",(book_id,)).fetchone()
        if active: return jsonify(id=active[0],created=False,message='Resume or cancel the existing import before starting another range.'),200
        rows=conn.execute('SELECT url FROM import_current WHERE book_id=? AND position BETWEEN ? AND ? AND section_id IS NULL ORDER BY position',(book_id,first,last))
        config={'first':first,'last':last,'selected_count':selected[0] if mode!='new' else selected[0]-selected[1],'skipped':selected[1] if mode!='new' else 0}
        identifier=schedule(db_path(),'import',book_id,config,((r[0],{'url':r[0]}) for r in rows))
    with connect(db_path()) as conn:
        with conn: conn.execute("UPDATE platform_jobs SET status='completed' WHERE id=? AND NOT EXISTS(SELECT 1 FROM platform_items WHERE job_id=?)",(identifier,identifier))
    return jsonify(id=identifier,created=True),202


@platform.route('/api/books/<book_id>/exports',methods=['POST','GET'])
def exports(book_id):
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
        if request.method=='GET':
            rows=conn.execute('SELECT id,first_position,last_position,created_at FROM epub_exports WHERE book_id=? ORDER BY created_at DESC LIMIT 100',(book_id,)).fetchall()
            return jsonify(items=[{**dict(r),'download_url':'/api/exports/'+r['id']} for r in rows])
        data=body()
        catalog=data.get('catalog',False)
        first,last=chapter_range(conn,book_id,data,'import_current' if catalog else 'book_sections')
        catalog_job=None
        if catalog:
            row=conn.execute("SELECT d.job_id FROM discovery_catalogs d JOIN platform_jobs j ON j.id=d.job_id WHERE d.book_id=? AND j.status='completed' ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1",(book_id,)).fetchone()
            catalog_job=row[0] if row else None
    identifier=schedule(db_path(),'export',book_id,{'first':first,'last':last,'catalog_job':catalog_job},[('export',{})])
    return jsonify(id=identifier),202


@platform.route('/api/exports/<identifier>')
def export_file(identifier):
    with connect(db_path()) as conn:
        row=conn.execute('SELECT e.path,b.title FROM epub_exports e JOIN books b ON b.id=e.book_id WHERE e.id=?',(identifier,)).fetchone()
    if not row or not Path(row['path']).is_file(): return jsonify(error='Export not found.'),404
    return send_file(row['path'],as_attachment=True,download_name=row['title']+'.epub',mimetype='application/epub+zip')


@platform.route('/api/books/<book_id>/metadata',methods=['PATCH'])
def metadata(book_id):
    data=body();allowed={'title':300,'author':300,'description':10000,'language':30}
    if not data or set(data)-set(allowed): raise ValueError('Unknown or empty metadata fields.')
    for key,value in data.items():
        if not isinstance(value,str) or len(value)>allowed[key] or (key=='title' and not value.strip()): raise ValueError('Invalid '+key+'.')
    try:
        for value in data.values(): value.encode('utf-8')
    except UnicodeError: raise ValueError('Metadata contains invalid Unicode.')
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
        with conn:
            conn.execute('UPDATE books SET '+','.join(key+'=?' for key in data)+' WHERE id=?',tuple(data.values())+(book_id,))
            conn.executemany('INSERT OR IGNORE INTO import_metadata_overrides(book_id,field) VALUES(?,?)',((book_id,key) for key in data))
    return jsonify(saved=True)


@platform.route('/api/reader/appearance',methods=['GET','PUT'])
def reader_appearance():
    themes={'dark','gray','lightgray','light','sepia'}
    book_id=request.args.get('book')
    with connect(db_path()) as conn:
        row=conn.execute("SELECT value FROM platform_settings WHERE key='reader_theme'").fetchone()
        theme=row[0] if row and row[0] in themes else 'dark'
        if request.method=='PUT':
            data=body()
            if book_id or set(data)!={'theme'} or not isinstance(data['theme'],str) or data['theme'] not in themes:raise ValueError('Invalid global reading theme.')
            with conn:conn.execute("INSERT OR REPLACE INTO platform_settings(key,value) VALUES('reader_theme',?)",(data['theme'],))
            theme=data['theme']
        inherited=True
        if book_id:
            if not conn.execute('SELECT 1 FROM books WHERE id=?',(book_id,)).fetchone():raise ValueError('Book not found.')
            choice=conn.execute('SELECT inherit FROM reader_theme_choices WHERE book_id=?',(book_id,)).fetchone()
            progress=conn.execute('SELECT preferences FROM reading_progress WHERE book_id=?',(book_id,)).fetchone()
            inherited=bool(choice[0]) if choice else not (progress and json.loads(progress[0]).get('theme'))
    return jsonify(theme=theme,inherit=inherited)


@platform.route('/api/books/<book_id>/cover',methods=['POST','DELETE'])
def replace_cover(book_id):
    with connect(db_path()) as conn: book_exists(conn,book_id)
    if request.method=='DELETE':
        with connect(db_path()) as conn:
            with conn: conn.execute('UPDATE books SET cover_path=NULL WHERE id=?',(book_id,))
        return jsonify(saved=True)
    file=request.files.get('file')
    if not file: raise ValueError('Choose a JPEG, PNG or WebP cover.')
    from cover_art import store_cover,MAX_BYTES
    store_cover(current_app.config['DATA_DIR'],book_id,file.stream.read(MAX_BYTES+1))
    return jsonify(saved=True)


@platform.route('/api/books/<book_id>/cover/refresh',methods=['POST'])
def refresh_book_cover(book_id):
    data=body()
    with connect(db_path()) as conn: book_exists(conn,book_id)
    identifier=schedule(db_path(),'cover',book_id,{'force':data.get('force') is True},[('cover',{})],50)
    return jsonify(id=identifier,**worker_state(current_app.config['DATA_DIR'])),202


@platform.route('/api/books/<book_id>',methods=['DELETE'])
def delete_book(book_id):
    from book_management import remove_book
    data=body()
    result=remove_book(current_app.config['DATA_DIR'],book_id,data.get('mode','permanent'),data.get('confirmation'))
    return jsonify(**result,**worker_state(current_app.config['DATA_DIR'])),202 if result['job_id'] else 200


@platform.route('/api/books/<book_id>/restore',methods=['POST'])
def restore_book(book_id):
    with connect(db_path()) as conn:
        if conn.execute("SELECT 1 FROM book_deletions WHERE book_id=? AND status!='completed'",(book_id,)).fetchone():
            raise ValueError('Permanent deletion is pending. Restore is unavailable.')
        with conn: conn.execute('UPDATE book_visibility SET hidden=0 WHERE book_id=?',(book_id,))
    return jsonify(restored=True)


@platform.route('/api/books/<book_id>/favorite',methods=['PUT'])
def favorite(book_id):
    value=body().get('favorite')
    if type(value) is not bool: raise ValueError('Favorite must be true or false.')
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
        with conn: conn.execute('INSERT INTO book_preferences(book_id,favorite) VALUES(?,?) ON CONFLICT(book_id) DO UPDATE SET favorite=excluded.favorite',(book_id,int(value)))
    return jsonify(favorite=value)

@platform.route('/api/books/<book_id>/preferences')
def preferences(book_id):
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
        row=conn.execute('SELECT favorite,opened_at FROM book_preferences WHERE book_id=?',(book_id,)).fetchone()
    return jsonify(dict(row) if row else {'favorite':False,'opened_at':None})


@platform.route('/api/books/<book_id>/opened',methods=['POST'])
def opened(book_id):
    with connect(db_path()) as conn:
        book_exists(conn,book_id)
        with conn: conn.execute('INSERT INTO book_preferences(book_id) VALUES(?) ON CONFLICT(book_id) DO UPDATE SET opened_at=CURRENT_TIMESTAMP',(book_id,))
    return jsonify(saved=True)

@platform.route('/api/books/<book_id>/audiobook-exports',methods=['POST'])
def audiobook_export_job(book_id):
    data=body()
    provider=data.get('provider','edge')
    if provider=='legacy': audio={'provider':'legacy'}
    elif provider=='edge': audio=speech_preferences(data)
    else: raise ValueError('Unknown audio provider.')
    with connect(db_path()) as conn:
        book_exists(conn,book_id);first,last=chapter_range(conn,book_id,data)
    identifier=schedule(db_path(),'audiobook-export',book_id,{'audio':audio,'first':first,'last':last},[('archive',{})])
    return jsonify(id=identifier),202


@platform.route('/api/audiobook-exports/<identifier>')
def audiobook_export_file(identifier):
    with connect(db_path()) as conn:
        row=conn.execute('SELECT e.path,b.title FROM audiobook_exports e JOIN books b ON b.id=e.book_id WHERE e.id=?',(identifier,)).fetchone()
    if not row or not Path(row['path']).is_file(): return jsonify(error='Audiobook export not found.'),404
    return send_file(row['path'],as_attachment=True,download_name=row['title']+' - audiobook.zip',mimetype='application/zip')

@platform.route('/api/books/<book_id>/import/schedule',methods=['POST'])
def discovery_schedule(book_id):
    interval=body().get('interval')
    if type(interval) is not int or (interval!=0 and not 3600<=interval<=604800):
        raise ValueError('Use zero to disable or a refresh interval from one hour to seven days.')
    with connect(db_path()) as conn:
        with conn:
            row=conn.execute('UPDATE import_sources SET refresh_interval=?,next_refresh=? WHERE book_id=?',(interval,time.time()+interval,book_id))
            if not row.rowcount: raise ValueError('This book has no import source.')
    return jsonify(interval=interval)

@platform.route('/api/platform/status')
def platform_status():
    import importlib.util
    import shutil
    return jsonify(**worker_state(current_app.config['DATA_DIR']),
                   edge_package_installed=importlib.util.find_spec('edge_tts') is not None,
                   ffmpeg_available=shutil.which('ffmpeg') is not None,online_required=True,
                   concurrency=1)
