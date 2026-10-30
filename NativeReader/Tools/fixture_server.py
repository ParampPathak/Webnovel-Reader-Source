"""Isolated real Flask fixture. No production connection, TTS, or production data.
Synthesized tones verify native decoding/queues, not speech quality.
"""
from pathlib import Path
import io, sys, tempfile, json, hashlib, wave, math, struct, os, zipfile
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,os.environ.get('NATIVE_READER_WEB_SOURCE',str(ROOT if (ROOT/'app.py').exists() else ROOT/'Webnovel-Reader')))
os.environ['AUDIO_CACHE_SCHEDULER']='0'
os.environ['CHAPTER_UPDATE_SCHEDULER']='0'
from app import create_app
from tests.fixtures import epub_bytes
from library_store import connect
from audio_providers import cache_key, settings, chapter_text_chunks
from narration_timing import publish
from flask import jsonify,request
import time,threading
faults={'outage_until':0,'delay_next_ack':0,'delayed':0,'rejected':0,'writes':[],'rules':[],'rule_hits':[]}
fault_lock=threading.Lock();appended_sections=[]
fixture=tempfile.TemporaryDirectory(prefix='native-reader-fixture-')
root=Path(fixture.name)
from unittest.mock import patch
with patch('audio_cache.start_scheduler'), patch('chapter_updates.start_scheduler'):
    app=create_app(root/'data')
@app.before_request
def fixture_network_fault():
    if not request.path.startswith('/fixture/'):
        chosen=None
        with fault_lock:
            for rule in faults['rules']:
                if rule.get('path')==request.path and rule.get('remaining',0)>0:
                    rule['remaining']-=1;chosen=dict(rule);faults['rule_hits'].append({'path':request.path,'at':time.time(),'status':rule.get('status')});break
        if chosen:
            if chosen.get('delay'):time.sleep(min(10,float(chosen['delay'])))
            if 'json' in chosen:return jsonify(chosen['json']),int(chosen.get('status',200))
            if chosen.get('status'):return jsonify(error='Synthetic targeted transport fault'),int(chosen['status'])
    if not request.path.startswith('/fixture/') and time.time()<faults['outage_until']:
        with fault_lock:faults['rejected']+=1
        return jsonify(error='Isolated fixture server unavailable.'),503
@app.after_request
def fixture_ack_fault(response):
    if request.path.endswith('/listening') and request.method=='PUT':
        with fault_lock:
            body=request.get_json(silent=True) or {}
            faults['writes'].append({'at':time.time(),'section_id':body.get('section_id'),'event_sequence':body.get('event_sequence'),'status':response.status_code})
            faults['writes']=faults['writes'][-200:]
            delay=faults['delay_next_ack'] if response.status_code==200 else 0
            if delay:faults['delay_next_ack']=0;faults['delayed']+=1
        if delay:time.sleep(delay)
    return response
@app.route('/fixture/control',methods=['POST'])
def control_faults():
    body=request.get_json() or {}
    with fault_lock:
        if 'outage_seconds' in body:faults['outage_until']=time.time()+max(0,min(30,float(body['outage_seconds'])))
        if 'delay_next_ack' in body:faults['delay_next_ack']=max(0,min(10,float(body['delay_next_ack'])))
        if 'rules' in body:faults['rules']=body['rules'][:12]
    if body.get('takeover_book'):
        with connect(db) as c,c:
            c.execute("UPDATE listening_progress SET version=version+1,checkpoint_revision=version+1,session_id='fixture-other-player',session_generation=session_generation+1,event_sequence=1 WHERE book_id=?",(body['takeover_book'],))
    return jsonify(ok=True)
@app.route('/fixture/status')
def fixture_status():
    selected=request.args.get('book_id',book)
    with connect(db) as c:
        r=c.execute('SELECT * FROM listening_progress WHERE book_id=?',(selected,)).fetchone()
        selected_sections=[x['id'] for x in c.execute('SELECT id FROM book_sections WHERE book_id=? ORDER BY position',(selected,))]
    with fault_lock:return jsonify(faults=faults,progress=dict(r) if r else {},book_id=selected,sections=selected_sections)
@app.route('/fixture/info')
def info():return jsonify(book_id=book,sections=[r['id'] for r in sections],storage=str(root),books=fixture_books)
@app.route('/fixture/append',methods=['POST'])
def append_fixture_chapter():
    selected=(request.get_json() or {}).get('book_id')
    if selected not in {b['book_id'] for b in fixture_books}:return jsonify(error='Synthetic book required'),400
    with connect(db) as c,c:
        old=c.execute('SELECT * FROM book_sections WHERE book_id=? ORDER BY position DESC LIMIT 1',(selected,)).fetchone()
        variant=c.execute("SELECT * FROM audio_variants WHERE section_id=? AND status='ready' ORDER BY rowid LIMIT 1",(old['id'],)).fetchone()
        if not variant:return jsonify(error='Ready synthetic audio required'),400
        section=c.execute('INSERT INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES(?,?,?,?,?,?)',(selected,old['position']+1,'Appended synthetic chapter','fixture-append-'+str(old['position']+1),old['content_html'],old['text_content'])).lastrowid
        config=json.loads(variant['settings']);key,digest=cache_key(selected,section,old['text_content'],config)
        folder=root/'data/audio/segments'/key;folder.mkdir(parents=True,exist_ok=True)
        for f in Path(variant['path']).parent.iterdir():
            if f.is_file() and not (folder/f.name).exists():os.link(f,folder/f.name)
        c.execute('INSERT INTO audio_variants(id,book_id,section_id,provider,voice,settings,content_hash,path,status,chunk_count,first_chunk_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,selected,section,'edge',variant['voice'],variant['settings'],digest,str(folder/Path(variant['path']).name),'ready',variant['chunk_count'],variant['first_chunk_bytes']))
        for chunk in c.execute('SELECT * FROM audio_chunks WHERE asset_id=? ORDER BY position',(variant['id'],)).fetchall():
            c.execute('INSERT INTO audio_chunks(asset_id,position,path,checksum,duration) VALUES(?,?,?,?,?)',(key,chunk['position'],str(folder/Path(chunk['path']).name),chunk['checksum'],chunk['duration']))
    appended_sections.append(section)
    return jsonify(section_id=section)
@app.route('/fixture/reset',methods=['POST'])
def reset():
    with connect(db) as conn,conn:
        conn.execute('DELETE FROM listening_progress');conn.execute('DELETE FROM reading_progress')
        for sid in appended_sections:
            conn.execute('DELETE FROM audio_chunks WHERE asset_id IN (SELECT id FROM audio_variants WHERE section_id=?)',(sid,))
            conn.execute('DELETE FROM audio_variants WHERE section_id=?',(sid,));conn.execute('DELETE FROM book_sections WHERE id=?',(sid,))
        appended_sections.clear()
    with fault_lock:faults.update(outage_until=0,delay_next_ack=0,delayed=0,rejected=0,writes=[],rules=[],rule_hits=[])
    return jsonify(ok=True)
client=app.test_client()
db=root/'data/webnovel.db';config=settings();fixture_books=[]
reading_count=max(42,min(3000,int(os.environ.get('NATIVE_FIXTURE_READING_CHAPTERS','42'))))
for label,count,first in [('Native verification · Chapters 150–157',8,150),('Native verification B · Chapters 160–167',8,160),(f'Native verification C · Chapters 120–{119+reading_count}',reading_count,120)][:int(os.environ.get('NATIVE_FIXTURE_BOOKS','3'))]:
    raw=epub_bytes(count,paragraph_bytes=180);out=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source,zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            data=source.read(entry)
            if entry.filename=='OPS/book.opf':data=data.replace(b'A &amp; B',label.encode()).replace(b'>fixture<',('>'+label+'<').encode())
            if reading_count>42 and entry.filename=='OPS/chapter500.xhtml':data=data.replace(b'</body>',b'<p>'+b'A long synthetic page for navigation. '*250+b'</p></body>')
            target.writestr(entry,data)
    book=client.post('/api/books/upload',data={'file':(io.BytesIO(out.getvalue()),'native.epub')},content_type='multipart/form-data').json['id']
    with connect(db) as conn,conn:
        conn.execute('UPDATE books SET title=? WHERE id=?',(label,book))
        sections=conn.execute('SELECT * FROM book_sections WHERE book_id=? ORDER BY position',(book,)).fetchall()
        for row in sections:
            conn.execute('UPDATE book_sections SET title=? WHERE id=?',(f'Chapter {first+row["position"]}',row['id']))
            if reading_count>42 and row['position']>=42 and row['position']<count-2:continue
            text=row['text_content']; key,content_hash=cache_key(book,row['id'],text,config)
            chunks=list(chapter_text_chunks(text,90))
            folder=root/'data/audio/segments'/key;folder.mkdir(parents=True)
            for index,text in enumerate(chunks):
                path=folder/f'{index:06}.wav'
                duration=float(os.environ.get("NATIVE_FIXTURE_TAIL_SECONDS" if row['position']>=6 else "NATIVE_FIXTURE_CHUNK_SECONDS",os.environ.get("NATIVE_FIXTURE_CHUNK_SECONDS","1.2")))
                with wave.open(str(path),'wb') as out:
                    out.setparams((1,2,16000,0,'NONE','not compressed'))
                    out.writeframes(b''.join(struct.pack('<h',int(900*math.sin(2*math.pi*(220+index*80)*i/16000))) for i in range(int(duration*16000))))
                events=path.with_suffix('.events');clean=' '.join(text.split());words=clean.split();cursor=0
                with events.open('w') as f:
                    for wi,word in enumerate(words):
                        f.write(json.dumps({'type':'WordBoundary','offset':int(wi*duration/max(1,len(words))*10000000),'duration':int(duration/max(1,len(words))*10000000),'text':word})+'\n')
                publish(path,text,events)
            # Complete file is used only by the normal browser fixture, native uses chunks.
            final=folder/'complete.wav'
            with wave.open(str(final),'wb') as out:
                out.setparams((1,2,16000,0,'NONE','not compressed'))
                for path in sorted(folder.glob('[0-9]*.wav')):
                    with wave.open(str(path),'rb') as source:out.writeframes(source.readframes(source.getnframes()))
            conn.execute('INSERT INTO audio_variants(id,book_id,section_id,provider,voice,settings,content_hash,path,status,chunk_count,first_chunk_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,book,row['id'],'edge',config['voice'],json.dumps(config,sort_keys=True),content_hash,str(final),'ready',len(chunks),90))
            for index in range(len(chunks)):
                path=folder/f'{index:06}.wav'
                conn.execute('INSERT INTO audio_chunks(asset_id,position,path,checksum,duration) VALUES(?,?,?,?,?)',(key,index,str(path),hashlib.sha256(path.read_bytes()).hexdigest(),duration))
    fixture_books.append({'book_id':book,'sections':[x['id'] for x in sections],'title':label})
book=fixture_books[0]['book_id']
with connect(db) as conn:sections=conn.execute('SELECT * FROM book_sections WHERE book_id=? ORDER BY position',(book,)).fetchall()
print(json.dumps({'book_id':book,'sections':[r['id'] for r in sections],'storage':str(root)}),flush=True)
app.run(host=os.environ.get('NATIVE_FIXTURE_BIND','127.0.0.1'),port=int(os.environ.get('NATIVE_FIXTURE_PORT','18765')),threaded=True,use_reloader=False)
