const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
class Element{
 constructor(){this.value='';this.hidden=true;this.dataset={};this.children=[];this.handlers={};this.listeners={};this.textContent='';this.src='';}
 setAttribute(name,value){this[name]=value;}
 addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);this.handlers[name]=(...args)=>{let result;for(const listener of [...this.listeners[name]])result=listener(...args);return result;};}
 removeEventListener(name,fn){this.listeners[name]=(this.listeners[name]||[]).filter(x=>x!==fn);}
 replaceChildren(...nodes){this.children=nodes;}
 click(){this.onclick?.();}
}
function harness(config={}){
 const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
 const audio=get('bookAudio');audio.paused=true;audio.currentTime=0;audio.duration=120;audio.ended=false;audio.playbackRate=1;audio.readyState=4;
 audio.pause=()=>{audio.paused=true;audio.handlers.pause?.();};
 audio.play=()=>{audio.ended=false;audio.paused=false;audio.handlers.play?.();return config.waitPlay||Promise.resolve();};
 audio.load=()=>{audio.error=null;audio.ended=false;audio.currentTime=0;audio.readyState=4;if(config.resetRateOnLoad)audio.playbackRate=1;if(config.holdNativeLoad){audio.paused=true;audio.readyState=0;return;}queueMicrotask(()=>audio.handlers.loadedmetadata?.());};
 const calls=[],saves=[],timers=[],actions={},progress=new Map(),storage=config.storageMap||new Map(Object.entries(config.storage||{}));let missing=false,remoteConflict=false;let globalPreferences=config.preferences||{voice:'en-US-AriaNeural',rate:0,playback_rate:1.5,volume:1};
 if(config.listeningProgress)progress.set('A',config.listeningProgress);
 const book=id=>({id,title:`Book ${id}`,author:'Author',first_section_id:1,cover_url:null,progress:config.readingProgress});
 const section=id=>config.section?config.section(id):({id,title:`Chapter ${id}`,position:id-1,previous_id:id>1?id-1:null,next_id:id<3?id+1:null});
 const reply=(data,status=200)=>({ok:status<400,status,json:async()=>data});
 const documentEvents={};const document={addEventListener:(name,fn)=>documentEvents[name]=fn,hidden:false,body:{dataset:{}},getElementById:get,querySelectorAll:()=>[],createElement:()=>new Element()};
 if(config.prefetch){document.head={children:[],appendChild(node){this.children.push(node);node.remove=()=>{this.children=this.children.filter(x=>x!==node);};}};}
 const lifecycle={};const window={addEventListener(name,fn){lifecycle[name]=fn;}};
 const context=vm.createContext({document,window,navigator:{mediaSession:{setActionHandler:(name,fn)=>actions[name]=fn}},MediaMetadata:class{constructor(data){Object.assign(this,data);}},localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},history:{replaceState(){},pushState(){}},location:{search:config.search||''},URLSearchParams,AbortController,encodeURIComponent,console,Date:config.Date||Date,
  setTimeout:(fn,delay)=>{const entry={fn,delay,cancelled:false};timers.push(entry);return entry;},clearTimeout:t=>{if(t)t.cancelled=true;},
  fetch:async(path,options={})=>{
   calls.push([path,options.method||'GET',options.body&&JSON.parse(options.body)]);
   if(path==='/api/edge/preferences'){if(options.method==='PUT')globalPreferences={...globalPreferences,...JSON.parse(options.body)};return reply(globalPreferences);}
   if(path==='/api/jobs/job'){const intercepted=await config.beforeJobAction?.(options,reply);return intercepted||reply({status:'queued'});}
   if(path==='/api/edge/voices')return reply({items:[{ShortName:'en-US-AriaNeural',Locale:'en-US'},{ShortName:'en-GB-SoniaNeural',Locale:'en-GB'}]});
   if(/^\/api\/audio\/asset-\d+\/location\/\d+$/.test(path)){if(config.waitLocation)await config.waitLocation;const [,chapter,index]=path.match(/asset-(\d+)\/location\/(\d+)/);return reply(config.narrationLocation||{book_id:'A',section_id:Number(chapter),normalized_offset:Number(index)*100,anchor:'narration:asset-'+chapter+':'+index,fraction:0,quote:'Text'});}
   if(/^\/api\/audio\/asset-\d+\/start\?offset=/.test(path))return reply(config.startMapping?config.startMapping(path):{available:true,segment_index:2,seconds:20,precision:'chunk'});
   const match=path.match(/^\/api\/books\/([^/]+)(.*)$/);if(!match)throw new Error('Unexpected '+path);const [,id,suffix]=match;
   if(!suffix){if(config.waitBook&&id==='A')await config.waitBook;return reply(book(id));}
   if(suffix==='/opened')return reply({saved:true});
   if(suffix==='/progress')return reply({version:1});
   if(suffix==='/listening'){
    const intercepted=await config.beforeListening?.(options,progress,reply);if(intercepted)return intercepted;
    if(options.method==='PUT'){if(remoteConflict)return reply({error:'Listening position changed in another player. Reload to resume.'},409);const data=JSON.parse(options.body);saves.push({...data,book:id});const old=progress.get(id)||{version:0};if(data.version!==old.version)return reply({error:'Listening position changed in another player. Reload to resume.',code:'version_conflict'},409);progress.set(id,{...data,version:old.version+1,checkpoint_revision:old.version+1});return reply({version:old.version+1,session_id:data.session_id,session_generation:data.session_generation,event_sequence:data.event_sequence});}
    return reply(progress.get(id)||{version:0,section_id:2,seconds:37,playback_rate:1.5,voice:'en-US-AriaNeural',synthesis_rate:0});
   }
   if(suffix.startsWith('/sections/'))return reply(section(Number(suffix.split('/').pop())));
   if(suffix==='/audio/jobs'){const intercepted=await config.beforeAudioJob?.(options,reply);return intercepted||reply({id:'job'},202);}
   if(suffix.startsWith('/audio?')){const params=new URLSearchParams(suffix.split('?')[1]);const id=Number(params.get('section_id'));const intercepted=await config.beforeAudio?.(params,id,reply,options);if(intercepted)return intercepted;if(config.audioStatus)return reply(config.audioStatus(params,id));return reply(missing?{status:'missing',completed_chunks:0,worker_active:config.workerActive,worker_message:'Background worker is stopped. Chapter queued.'}:{status:'ready',asset_id:`asset-${id}`,url:`/api/audio/asset-${id}`,worker_active:config.workerActive,can_generate:config.canGenerate});}
   throw new Error('Unexpected '+path);
  }});
 vm.runInContext(fs.readFileSync(process.env.TEST_PLAYER_SOURCE||'static/player.js','utf8'),context);
 const rawEnded=()=>audio.listeners.ended?.at(-1);const emitEnded=async()=>{audio.ended=true;audio.currentTime=audio.duration;return audio.handlers.ended?.();};
 return {document,get,audio,lifecycle,documentEvents,rawEnded,emitEnded,calls,saves,timers,actions,player:window.Player,storage,progress,missing(value){missing=value;},conflict(){remoteConflict=true;}};
}
async function settle(){for(let i=0;i<60;i++)await Promise.resolve();}

test('a delayed manual generation Retry cannot reopen or interrupt a newer listening book',async()=>{
 let release;const h=harness({audioStatus:(params,id)=>id===1?{status:'failed',generation_status:'failed',job_id:'job',segments:[]}:{status:'ready',asset_id:'asset-'+id,url:'/api/audio/asset-'+id,worker_active:false},beforeJobAction:()=>new Promise(resolve=>release=resolve)});
 await h.player.listen('A',1);await settle();const retry=h.get('playPause').onclick();await settle();
 await h.player.listen('B',2);await settle();h.audio.currentTime=19;const session=h.player.metrics().session,source=h.player.metrics().source_revision;
 release();await retry;await settle();assert.equal(h.player.metrics().book_id,'B');assert.equal(h.player.metrics().session,session);assert.equal(h.player.metrics().source_revision,source);assert.equal(h.audio.currentTime,19);assert.equal(h.audio.paused,false);
});

test('Pause and headphones OFF cancel a pending manual generation action without late autoplay',async()=>{
 for(const action of ['ui','off']){
  let release;const h=harness({audioStatus:()=>({status:'failed',generation_status:'failed',job_id:'job',segments:[]}),beforeJobAction:()=>new Promise(resolve=>release=resolve)});
  await h.player.listen('A',1);await settle();const retry=h.get('playPause').onclick();await settle();assert.equal(h.player.metrics().media_state,'recovering');
  if(action==='ui')await h.get('playPause').onclick();else h.player.stopReader();await retry;release();await settle();
  assert.equal(h.player.metrics().want_play,false);assert.equal(h.audio.paused,true);assert.equal(h.timers.filter(t=>!t.cancelled&&t.delay===10000).length,0);
 }
});

test('a manual generation action with no acknowledgement ends in Error and explicit Retry stays in its book',async()=>{
 let release,ready=false;const config={audioStatus:()=>ready?{status:'ready',asset_id:'asset-1',url:'/api/audio/asset-1',worker_active:false}:{status:'failed',generation_status:'failed',job_id:'job',segments:[]},beforeJobAction:()=>new Promise(resolve=>release=resolve)},h=harness(config);
 await h.player.listen('A',1);await settle();const retry=h.get('playPause').onclick();await settle();const session=h.player.metrics().session;
 h.timers.findLast(t=>!t.cancelled&&t.delay===10000).fn();await retry;await settle();assert.equal(h.player.metrics().want_play,false);assert.equal(h.player.metrics().media_state,'error');
 release();await settle();assert.equal(h.player.metrics().session,session);assert.equal(h.audio.paused,true);
 config.beforeJobAction=undefined;ready=true;await h.get('playPause').onclick();await settle();assert.equal(h.player.metrics().book_id,'A');assert.equal(h.audio.paused,false);
});

test('a hung audio-status transport is bounded and retries the same book without switching',async()=>{
 let requests=0;const h=harness({beforeAudio:()=>++requests===1?new Promise(()=>{}):undefined});
 const opening=h.player.listen('A',1);await settle();
 const deadline=h.timers.find(t=>!t.cancelled&&t.delay===10000);assert(deadline,'Audio status must have a cancellable transport deadline');
 deadline.fn();await settle();await opening;
 const retry=h.timers.findLast(t=>!t.cancelled&&String(t.fn).includes('waitForAudio'));assert(retry);
 await retry.fn();await settle();assert.equal(h.player.metrics().book_id,'A');assert.equal(h.player.metrics().section_id,1);assert.equal(h.audio.paused,false);
 assert.equal(h.calls.filter(([p,m,data])=>p.endsWith('/audio/jobs')&&!data?.prefetch).length,0);
});

test('exhausted native seek loading exposes Error and a same-book manual retry, not endless buffering',async()=>{
 const config={holdNativeLoad:true,listeningProgress:{version:0,section_id:1,seconds:37,voice:'en-US-AriaNeural',synthesis_rate:0}},h=harness(config);await h.player.listen('A',1);await settle();
 for(let i=0;i<3;i++){const retry=h.timers.findLast(t=>!t.cancelled&&t.delay===2000);assert(retry);retry.cancelled=true;await retry.fn();await settle();}
 assert.equal(h.player.metrics().want_play,false);assert.equal(h.player.metrics().media_state,'error');assert.match(h.get('playerStatus').textContent,/retry/i);
 config.holdNativeLoad=false;await h.get('playPause').onclick();await settle();assert.equal(h.audio.paused,false);assert.equal(h.player.metrics().section_id,1);assert.equal(h.audio.currentTime,37);
});

test('concurrent same-session audio status requests share one in-flight transport',async()=>{
 let release;const h=harness({beforeAudio:()=>new Promise(resolve=>release=resolve)});
 const first=h.player.listen('A',1);await settle();const second=h.player.listen('A',1);await settle();
 assert.equal(h.calls.filter(([p])=>p.includes('/audio?')).length,1);assert.equal(h.timers.filter(t=>!t.cancelled&&t.delay===10000).length,1);
 release();await first;await second;await settle();assert.equal(h.audio.paused,false);
});

test('pending transport cancellation respects Pause remote sleep and headphones OFF even after a late reply',async()=>{
 for(const action of ['ui','remote','sleep','off']){
  let release,now=100000;class Clock extends Date{static now(){return now;}}
  const h=harness({Date:Clock,beforeAudio:()=>new Promise(resolve=>release=resolve)}),opening=h.player.listen('A',1);await settle();
  if(action==='ui')await h.get('playPause').onclick();if(action==='remote')h.actions.pause();if(action==='off')h.player.stopReader();
  if(action==='sleep'){h.get('sleepTimer').value='5';h.get('sleepTimer').onchange();now+=300001;h.documentEvents.visibilitychange();}
  await opening;release();await settle();assert.equal(h.player.metrics().want_play,false,action);assert.equal(h.audio.paused,true,action);
  assert.equal(h.timers.filter(t=>!t.cancelled&&t.delay===10000).length,0,action);
 }
});

test('a late failed Book A request cannot modify active Book B and A can reopen normally',async()=>{
 let release;const config={beforeAudio:(params,id,reply)=>id===1?new Promise(resolve=>release=()=>resolve(reply({error:'Late old failure'},404))):undefined},h=harness(config);
 const first=h.player.listen('A',1);await settle();await h.player.listen('B',2);await first;await settle();const active=h.audio.src;
 release();await settle();assert.equal(h.player.metrics().book_id,'B');assert.equal(h.audio.src,active);assert.equal(h.player.metrics().media_state,'playing');
 config.beforeAudio=undefined;await h.player.listen('A',1);await settle();assert.equal(h.player.metrics().book_id,'A');assert.equal(h.audio.paused,false);
});

test('three stalled transports stop automatic retries and explicit same-book Retry resets the budget',async()=>{
 const config={beforeAudio:()=>new Promise(()=>{})},h=harness(config);const first=h.player.listen('A',1);await settle();
 for(let attempt=0;attempt<3;attempt++){
  const timeout=h.timers.findLast(t=>!t.cancelled&&t.delay===10000);assert(timeout);timeout.fn();await settle();
  if(attempt<2){const retry=h.timers.findLast(t=>!t.cancelled&&String(t.fn).includes('waitForAudio'));assert(retry);retry.cancelled=true;retry.fn();await settle();}
 }
 await first;assert.equal(h.player.metrics().media_state,'error');assert.equal(h.player.metrics().want_play,false);assert.equal(h.calls.filter(([p])=>p.includes('/audio?')).length,3);
 config.beforeAudio=undefined;await h.get('playPause').onclick();await settle();assert.equal(h.audio.paused,false);assert.equal(h.player.metrics().book_id,'A');
});

test('a slow healthy generation job is observed without cancellation or repeated synthesis requests',async()=>{
 let count=0;const h=harness({audioStatus:()=>({status:'running',generation_status:'running',job_id:'job',item_status:'running',completed_chunks:count++,segments:[],worker_active:true})});
 await h.player.listen('A',1);await settle();
 for(let i=0;i<30;i++){const poll=h.timers.findLast(t=>!t.cancelled&&String(t.fn).includes('waitForAudio'));assert(poll);poll.cancelled=true;await poll.fn();await settle();}
 assert.equal(h.player.metrics().want_play,true);assert.notEqual(h.player.metrics().media_state,'error');
 assert.equal(h.calls.filter(([p,m,data])=>p.endsWith('/audio/jobs')&&!data.prefetch).length,1);
 assert.equal(h.timers.filter(t=>!t.cancelled&&t.delay===10000).length,0);
});

test('a lost job acknowledgement recovers from ready status without duplicate generation',async()=>{
 let ready=false;const h=harness({audioStatus:()=>ready?{status:'ready',asset_id:'asset-1',url:'/api/audio/asset-1',worker_active:false}:{status:'missing',segments:[],worker_active:true},beforeAudioJob:()=>{ready=true;return new Promise(()=>{});}});
 const first=h.player.listen('A',1);await settle();h.timers.findLast(t=>!t.cancelled&&t.delay===10000).fn();await settle();await first;
 await h.timers.findLast(t=>!t.cancelled&&String(t.fn).includes('waitForAudio')).fn();await settle();assert.equal(h.audio.paused,false);
 assert.equal(h.calls.filter(([p,m,data])=>p.endsWith('/audio/jobs')&&!data.prefetch).length,1);
});

test('partial generation failure stops at the last published chunk and manual Retry continues the same book',async()=>{
 let failed=false;const state=()=>({status:'streaming',asset_id:'asset-1',complete:false,generation_status:failed?'failed':'running',job_id:'job',worker_active:true,segments:[{index:0,duration:10,url:'/part/0'}]});
 const config={audioStatus:state},h=harness(config);await h.player.listen('A',1);await settle();failed=true;h.audio.duration=10;await h.emitEnded();await settle();
 assert.equal(h.player.metrics().media_state,'error');assert.equal(h.player.metrics().want_play,false);assert.equal(h.player.metrics().segment_index,0);
 config.audioStatus=()=>({...state(),generation_status:'running',segments:[{index:0,duration:10,url:'/part/0'},{index:1,duration:10,url:'/part/1'}]});
 await h.get('playPause').onclick();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().segment_index,1);assert.equal(h.audio.paused,false);
 assert(h.calls.some(([p,m,data])=>p==='/api/jobs/job'&&data?.action==='retry'));
});

test('failed status polling never stops an already advancing cached source',async()=>{
 let fail=false;const h=harness({audioStatus:()=>{if(fail)throw new Error('Network offline');return{status:'streaming',asset_id:'asset-1',complete:false,worker_active:false,segments:[{index:0,duration:120,url:'/cached/0'}]};}});
 await h.player.listen('A',1);await settle();h.audio.currentTime=17;fail=true;
 for(let i=0;i<3;i++){const poll=h.timers.findLast(t=>!t.cancelled&&String(t.fn).includes('waitForAudio'));assert(poll);poll.cancelled=true;await poll.fn();await settle();assert.equal(h.player.metrics().media_state,'playing','Native cached playback remains authoritative during status transport failures');}
 assert.equal(h.audio.paused,false);assert.equal(h.audio.currentTime,17);assert.equal(h.player.metrics().want_play,true);assert.match(h.get('playerStatus').textContent,/Cached audio continues/);
});

test('completed-source foreground checks do not repeat the current generation request',async()=>{
 const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:false,generation_status:'running',worker_active:true,segments:[{index:0,duration:10,url:'/growing/0'}]})});
 await h.player.listen('A',1);await settle();h.audio.duration=10;await h.emitEnded();await settle();
 const requests=h.calls.filter(([p,m,data])=>p.endsWith('/audio/jobs')&&!data?.prefetch).length;
 for(let i=0;i<10;i++){h.lifecycle.pageshow();await settle();await h.rawEnded()();await settle();}
 assert.equal(h.calls.filter(([p,m,data])=>p.endsWith('/audio/jobs')&&!data?.prefetch).length,requests);assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().segment_index,0);
});

test('a recovered chunk cannot exhaust the independent recovery budget of its next published chunk',async()=>{
 const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:true,worker_active:false,segments:[0,1].map(index=>({index,duration:120,url:'/budget/'+index}))})});
 await h.player.listen('A',1);await settle();
 for(let i=0;i<2;i++){h.audio.error={code:2};h.audio.handlers.error();const retry=h.timers.findLast(t=>!t.cancelled&&t.delay===2000);assert(retry);retry.cancelled=true;retry.fn();await settle();}
 await h.emitEnded();await settle();assert.equal(h.player.metrics().segment_index,1);
 h.audio.error={code:2};h.audio.handlers.error();const retry=h.timers.findLast(t=>!t.cancelled&&t.delay===2000);assert(retry);assert.equal(h.player.metrics().want_play,true);
 retry.fn();await settle();assert.equal(h.audio.paused,false);assert.equal(h.player.metrics().segment_index,1);
});

test('native metadata loading survives foreground health checks without cancelling intended playback',async()=>{
 for(const foreground of [false,true]){
  const config={audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:true,segments:[0,1].map(index=>({index,duration:120,url:'/segment/'+index}))})},h=harness(config);await h.player.listen('A',1);await settle();
  config.holdNativeLoad=true;await h.emitEnded();await settle();
  assert.equal(h.audio.readyState,0);assert.equal(h.audio.paused,true);
  if(foreground){h.document.hidden=true;h.documentEvents.visibilitychange();h.document.hidden=false;h.documentEvents.visibilitychange();}
  else h.timers.findLast(t=>t.delay===1500&&!t.cancelled).fn();
  assert.equal(h.player.metrics().want_play,true,'Internal source loading must not become a user/system pause');
  config.holdNativeLoad=false;h.audio.readyState=4;h.audio.handlers.loadedmetadata();await settle();
  assert.equal(h.audio.paused,false);assert.equal(h.audio.currentTime,0);assert.equal(h.player.metrics().segment_index,1);assert.equal(h.player.metrics().section_id,1);
 }
});

test('explicit pauses and unknown native interruptions stay paused throughout a metadata gap',async()=>{
 for(const action of ['ui','remote','sleep','off','native']){
  let now=100000;class Clock extends Date{static now(){return now;}}
  const config={Date:Clock,audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:true,segments:[0,1].map(index=>({index,duration:120,url:'/segment/'+index}))})},h=harness(config);
  await h.player.listen('A',1);await settle();config.holdNativeLoad=true;await h.emitEnded();await settle();
  if(action==='ui')await h.get('playPause').onclick();
  if(action==='remote')h.actions.pause();
  if(action==='off')h.player.stopReader();
  if(action==='native')h.audio.handlers.pause();
  if(action==='sleep'){h.get('sleepTimer').value='5';h.get('sleepTimer').onchange();now+=300001;h.documentEvents.visibilitychange();}
  assert.equal(h.player.metrics().want_play,false,action);
  config.holdNativeLoad=false;h.audio.readyState=4;h.audio.handlers.loadedmetadata();await settle();h.audio.handlers.canplay();await settle();
  assert.equal(h.audio.paused,true,action);assert.equal(h.player.metrics().want_play,false,action);
 }
});

test('late old source completion cannot pause or replace the current playing chunk',async()=>{
 const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:true,segments:[0,1].map(index=>({index,duration:120,url:'/segment/'+index}))})});
 await h.player.listen('A',1);await settle();const old=h.rawEnded();await h.emitEnded();await settle();h.audio.currentTime=29;await old();await settle();
 assert.equal(h.player.metrics().segment_index,1);assert.equal(h.audio.paused,false);assert.equal(h.audio.currentTime,29);
});

test('bounded pause attribution distinguishes remote and sleep actions without storing URLs or text',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.actions.pause();
 assert(h.player.diagnostics().some(e=>e.event==='pause-requested'&&e.reason==='media-session-pause'));
 for(let i=0;i<250;i++)h.audio.handlers.waiting();const entries=h.player.diagnostics();assert(entries.length<=200);
 for(const entry of entries){assert.equal(typeof entry.hidden,'boolean');assert(!JSON.stringify(entry).includes('/api/'));assert(!('quote' in entry));}
});

test('chunk-only narration is delivered again when buffering becomes playing',async()=>{
 let release;const waitPlay=new Promise(r=>release=r);
 const h=harness({waitPlay,audioStatus:(p,id)=>({status:'streaming',asset_id:'asset-'+id,complete:true,segments:[{index:0,duration:120,url:'/segment/'+id}]})});
 let playing=false;const deliveries=[];
 h.get('workspace').contentWindow={ReaderNarration:{bookId:'A',playback:state=>playing=state.playing,update:location=>deliveries.push({section:location.section_id,playing}),clear(){}}};
 await h.player.listen('A',1);await settle();
 assert(deliveries.some(item=>!item.playing));release();await settle();
 assert(deliveries.some(item=>item.section===1&&item.playing),'A location fetched before playback must be redelivered after playback starts');
});

test('return intent during delayed automatic chapter load survives its response',async()=>{
 let blocked=false,release;const gate=new Promise(r=>release=r);
 const h=harness({beforeListening:async options=>{if(blocked&&options.method!=='PUT')await gate;}});
 await h.player.listen('A',1);await settle();h.player.setFollow('A',false,true);
 blocked=true;const transition=h.emitEnded();await settle();
 h.player.setFollow('A',true,false);release();await transition;await settle();
 assert.equal((await h.player.readerResume('A')).following,true);
});

test('superseded tab cannot erase a newer locally durable session before its network save',async()=>{
 const storageMap=new Map(),old=harness({storageMap});await old.player.listen('A',1);await settle();old.actions.pause();await settle();
 const newer=harness({storageMap,listeningProgress:{...old.progress.get('A')},beforeListening:(o,p,r)=>o.method==='PUT'?r({error:'Offline'},503):null});
 await newer.player.listen('A',3);await settle();newer.audio.currentTime=51;newer.lifecycle.pagehide();await settle();
 const checkpoint=JSON.parse(storageMap.get('webnovelPlaybackCheckpoint:A'));
 old.audio.currentTime=7;old.lifecycle.pagehide();await settle();
 const remaining=JSON.parse(storageMap.get('webnovelPlaybackCheckpoint:A'));assert.equal(remaining.writer.id,checkpoint.writer.id);assert.equal(remaining.seconds,51);assert.equal(remaining.sectionId,3);
 const lateOldAck={...old.progress.get('A')};lateOldAck.version++;lateOldAck.checkpoint_revision=lateOldAck.version;lateOldAck.event_sequence++;
 const reopened=harness({listeningProgress:lateOldAck,storageMap});await reopened.player.listen('A',null,false);await settle();assert.equal(reopened.player.metrics().section_id,3);assert.equal(reopened.player.metrics().seconds,51);
});

test('two offline pending claims with the same server generation retain the latest local owner',async()=>{
 const storageMap=new Map(),base=harness();await base.player.listen('A',1);await settle();base.actions.pause();await settle();
 const config={storageMap,listeningProgress:{...base.progress.get('A')},beforeListening:(o,p,r)=>o.method==='PUT'?r({error:'Offline'},503):null};
 const first=harness(config);await first.player.listen('A',2);await settle();first.audio.currentTime=21;first.lifecycle.pagehide();await settle();
 const second=harness(config);await second.player.listen('A',3);await settle();second.audio.currentTime=51;second.lifecycle.pagehide();await settle();
 const latest=JSON.parse(storageMap.get('webnovelPlaybackCheckpoint:A'));first.audio.currentTime=25;first.lifecycle.pagehide();await settle();
 const kept=JSON.parse(storageMap.get('webnovelPlaybackCheckpoint:A'));assert.equal(kept.writer.id,latest.writer.id);assert.equal(kept.sectionId,3);assert.equal(kept.seconds,51);
});

test('foreground resolves actual time through word cues, not the last delivered word',async()=>{
 const h=harness({audioStatus:()=>({status:'ready',asset_id:'asset-1',url:'/complete',complete:true,segments:[{index:0,duration:120}]}),narrationLocation:{book_id:'A',section_id:1,segment_index:0,normalized_offset:100,anchor:'narration:asset-1:0',quote:'Alpha Beta Gamma',cues:[{at:0,offset:0,quote:'Alpha'},{at:20,offset:6,quote:'Beta'},{at:40,offset:11,quote:'Gamma'}]}});
 await h.player.listen('A',1);await settle();h.audio.currentTime=43;
 const location=await h.player.resolveNarration('A');assert.equal(location.quote,'Gamma');assert.equal(location.normalized_offset,111);assert.equal(location.precision,'word');
});

test('explicit return waits for in-flight genuine chunk metadata and rejects the old chapter',async()=>{
 let release;const config={audioStatus:(p,id)=>({status:'streaming',asset_id:'asset-'+id,complete:true,segments:[{index:0,duration:120,url:'/segment/'+id}]})},h=harness(config);
 await h.player.listen('A',1);await settle();assert.equal(h.player.narration().section_id,1);
 config.waitLocation=new Promise(r=>release=r);await h.player.listen('A',2);await settle();
 assert.equal(h.player.narration(),null);const returned=h.player.resolveNarration('A');release();assert.equal((await returned).section_id,2);
});

test('local ordered checkpoint survives a failed save and same-session server acknowledgement without clock authority',async()=>{
 let fail=false;const h=harness({beforeListening:(o,p,reply)=>o.method==='PUT'&&fail?reply({error:'Offline'},503):null});
 await h.player.listen('A',1);await settle();h.actions.pause();await settle();const server={...h.progress.get('A')};
 fail=true;h.audio.currentTime=54;h.lifecycle.pagehide();await settle();
 const saved=JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A'));assert.equal(saved.seconds,54);assert(saved.writer.sequence>server.event_sequence);
 saved.savedAt=1;const restored=harness({listeningProgress:server,storage:{'webnovelPlaybackCheckpoint:A':JSON.stringify(saved)}});
 await restored.player.listen('A',null,false);await settle();assert.equal(restored.player.metrics().seconds,54);assert(restored.audio.paused);
 assert.equal(JSON.parse(restored.storage.get('webnovelPlaybackCheckpoint:A')).writer.id,saved.writer.id);
});

test('a newer foreign session beats a future-dated local checkpoint',async()=>{
 const local={bookId:'A',sectionId:1,seconds:90,voice:'en-US-AriaNeural',rate:0,savedAt:Date.now()+1e9,writer:{id:'old-session',generation:1,sequence:500,claim:false}};
 const h=harness({listeningProgress:{version:9,section_id:3,seconds:42,voice:'en-US-AriaNeural',synthesis_rate:0,session_id:'new-session',session_generation:2,event_sequence:2,checkpoint_revision:9},storage:{'webnovelPlaybackCheckpoint:A':JSON.stringify(local)}});
 await h.player.listen('A',null,false);await settle();assert.equal(h.player.metrics().section_id,3);assert.equal(h.player.metrics().seconds,42);
});

test('blocked network checkpoint does not stall validated chunk or chapter transitions',async()=>{
 let release;const gate=new Promise(r=>release=r);let block=false;
 const h=harness({beforeListening:async o=>{if(o.method==='PUT'&&block)await gate;},audioStatus:(p,id)=>({status:'streaming',asset_id:'asset-'+id,complete:true,segments:[0,1].map(index=>({index,duration:10,url:'/c/'+id+'/'+index}))})});
 await h.player.listen('A',1);await settle();h.audio.duration=10;block=true;h.audio.currentTime=9;h.lifecycle.pagehide();await settle();
 await h.emitEnded();await settle();assert.equal(h.player.metrics().segment_index,1);
 await h.emitEnded();await settle();assert.equal(h.player.metrics().section_id,2);
 const saved=JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A'));assert.equal(saved.sectionId,2);assert.equal(saved.segmentIndex,0);
 release();await settle();await settle();h.lifecycle.pagehide();await settle();assert.equal(h.progress.get('A').section_id,2);
});

test('failed checkpoint reconnect saves the latest cue, not the older failed request',async()=>{
 let fail=true;const h=harness({beforeListening:(o,p,r)=>o.method==='PUT'&&fail?r({error:'Offline'},503):null});
 await h.player.listen('A',1);await settle();h.audio.currentTime=12;h.lifecycle.pagehide();await settle();
 h.audio.currentTime=62;h.lifecycle.pagehide();await settle();assert.equal(JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A')).seconds,62);
 fail=false;h.document.hidden=false;h.documentEvents.visibilitychange();await settle();assert.equal(h.progress.get('A').seconds,62);
});

test('intentional backwards seeks have newer ordered events and preserve their movement intent',async()=>{
 const h=harness();await h.player.listen('A',2);await settle();h.audio.currentTime=75;h.actions.pause();await settle();const prior=h.progress.get('A').event_sequence;
 await h.player.seek(-15);await settle();assert.equal(h.progress.get('A').seconds,60);assert(h.progress.get('A').event_sequence>prior);assert.equal(h.progress.get('A').checkpoint_intent,'seek');
 await h.player.listen('A',1);await settle();h.actions.pause();await settle();assert.equal(h.progress.get('A').section_id,1);
});

test('automatic chapters retain manual-ahead intent rather than reloading stale follow settings',async()=>{
 const h=harness({readingProgress:{version:0,preferences:{followAlong:true,followAlongSuspended:false}},audioStatus:(p,id)=>({status:'ready',asset_id:'asset-'+id,url:'/c/'+id,segments:[{duration:120}],complete:true})});await h.player.listen('A',1);await settle();
 h.player.setFollow('A',true,true);await h.emitEnded();await settle();const resume=await h.player.readerResume('A');
 assert.equal(resume.following,false);assert.equal(h.player.metrics().section_id,2);
});

test('foreground republishes the current media anchor even when the timing cue did not change',async()=>{
 const h=harness({audioStatus:()=>({status:'ready',asset_id:'asset-1',url:'/complete',complete:true,segments:[{index:0,duration:120,url:'/segment/0'}]})});
 await h.player.listen('A',1);await settle();assert(h.player.narration(),'Fixture must have a genuine mapped cue');const updates=[];
 h.get('workspace').contentWindow={ReaderNarration:{bookId:'A',update:location=>updates.push(location),playback(){}}};
 h.document.hidden=true;h.documentEvents.visibilitychange();await settle();
 updates.length=0;h.document.hidden=false;h.documentEvents.visibilitychange();await settle();
 assert.equal(updates.length,1,'The resumed iframe must receive the live position without waiting for another word/chunk');
 assert.equal(updates[0].section_id,1);
});

test('truncated native EOF cannot skip the rest of a published segment',async()=>{
 const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:true,segments:[0,1].map(index=>({index,duration:120,url:'/segment/'+index}))})});
 await h.player.listen('A',1);await settle();h.audio.currentTime=12;h.audio.handlers.timeupdate();h.audio.duration=20;h.audio.currentTime=20;h.audio.ended=true;
 await h.rawEnded()();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().segment_index,0);assert.match(h.audio.src,/segment\/0/);
});

test('unknown native duration at EOF cannot complete a known-length chunk early',async()=>{
 const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:true,segments:[{index:0,duration:120,url:'/segment/0'}]})});
 await h.player.listen('A',1);await settle();h.audio.duration=NaN;h.audio.currentTime=20;h.audio.ended=true;
 await h.rawEnded()();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().segment_index,0);
});

test('lock-screen Play at premature ended uses the same completion validation',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=20;h.audio.ended=true;h.audio.paused=true;
 await h.actions.play();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().seconds,20);
});

test('a missing segment index stops instead of treating array position as sequence',async()=>{
 const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:true,segments:[{index:0,duration:10,url:'/s/0'},{index:2,duration:10,url:'/s/2'}]})});
 await h.player.listen('A',1);await settle();await h.emitEnded();await settle();assert.equal(h.player.metrics().section_id,1);assert.notEqual(h.audio.src,'/s/2');assert.equal(h.player.metrics().want_play,false);
});

test('automatic chapter advance stops at a missing source catalog position',async()=>{
 const h=harness({section:id=>({id,position:id===1?198:200,title:'Chapter',previous_id:null,next_id:id===1?2:null})});
 await h.player.listen('A',1);await settle();await h.emitEnded();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().want_play,false);assert.match(h.get('playerStatus').textContent,/missing|gap/i);
 // Explicit chapter selection remains permitted across a known import gap.
 await h.player.listen('A',2);await settle();assert.equal(h.player.metrics().section_id,2);
});

for(const rate of [1,2,2.5])test(`long sequence: 100 chapters / 800 chunks at ${rate}x with duplicate/stale events and interruptions`,async()=>{
 const ids=Array.from({length:100},(_,i)=>1000+(i*37)%101),visited=[];
 const h=harness({preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:rate,volume:1},section:id=>{const i=ids.indexOf(id);return {id,position:i,title:'Fixture chapter',previous_id:ids[i-1]||null,next_id:ids[i+1]||null};},audioStatus:(params,id)=>({status:'streaming',asset_id:'asset-'+id,complete:true,worker_active:false,segments:Array.from({length:8},(_,index)=>({index,duration:10,url:'/c/'+id+'/'+index}))})});
 await h.player.listen('A',ids[0]);await settle();h.audio.duration=10;let stale;
 for(let c=0;c<100;c++)for(let index=0;index<8;index++){
  assert.equal(h.player.metrics().section_id,ids[c]);assert.equal(h.player.metrics().segment_index,index);assert.equal(h.audio.src,'/c/'+ids[c]+'/'+index);assert.equal(h.audio.playbackRate,rate);visited.push(h.audio.src);
  if(visited.length%17===0){h.audio.currentTime=4;h.audio.handlers.timeupdate();h.actions.pause();h.document.hidden=true;h.documentEvents.visibilitychange();h.lifecycle.pageshow();assert.equal(h.audio.currentTime,4);assert.equal(h.audio.paused,true);h.document.hidden=false;h.documentEvents.visibilitychange();await h.actions.play();await settle();assert.equal(h.audio.currentTime,4);}
  if(stale){await stale();await settle();assert.equal(h.audio.src,'/c/'+ids[c]+'/'+index);}
  stale=h.rawEnded();h.audio.currentTime=10;h.audio.ended=true;h.audio.paused=true;await Promise.all([stale(),stale(),stale()]);await settle();
 }
 assert.equal(new Set(visited).size,800);assert.equal(h.player.metrics().section_id,ids.at(-1));assert.equal(h.player.metrics().media_state,'completed');assert.equal(h.player.metrics().want_play,false);
 const log=h.player.diagnostics();assert(log.length<=200);assert(!JSON.stringify(log).includes('/c/'));assert(!JSON.stringify(log).includes('Fixture chapter'));
});

test('three decode failures stop at the same confirmed chunk and explicit Play retries it',async()=>{
 const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:true,segments:[{index:0,duration:120,url:'/s/0'},{index:1,duration:120,url:'/s/1'}]})});
 await h.player.listen('A',1);await settle();h.audio.currentTime=14;h.audio.handlers.timeupdate();
 for(let attempt=0;attempt<3;attempt++){h.audio.error={code:3};h.audio.currentTime=0;h.audio.readyState=0;h.audio.handlers.error();if(attempt<2){await h.timers.filter(t=>!t.cancelled&&t.delay===2000).at(-1).fn();await settle();assert.equal(h.audio.src,'/s/0');assert.equal(h.audio.currentTime,14);}}
 assert.equal(h.player.metrics().want_play,false);assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().segment_index,0);assert.equal(h.player.metrics().seconds,14);
 await h.get('playPause').onclick();await settle();assert.equal(h.audio.src,'/s/0');assert.equal(h.audio.currentTime,14);assert.equal(h.audio.paused,false);
});

test('end of a growing chapter waits for its next chunk, never skips to another chapter',async()=>{
 let complete=false,count=1;const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete,worker_active:true,segments:Array.from({length:count},(_,index)=>({index,duration:10,url:'/s/'+index}))})});
 await h.player.listen('A',1);await settle();h.audio.duration=10;await h.emitEnded();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().buffering,true);
 count=3;complete=true;await h.timers.filter(t=>!t.cancelled&&String(t.fn).includes('waitForAudio')).at(-1).fn();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().segment_index,1);assert.equal(h.audio.src,'/s/1');
 await h.emitEnded();await settle();assert.equal(h.audio.src,'/s/2');assert.equal(h.player.metrics().section_id,1);
});

test('a shrinking publication manifest cannot move past missing chunks',async()=>{
 let count=3;const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',complete:false,worker_active:false,segments:Array.from({length:count},(_,index)=>({index,duration:10,url:'/s/'+index}))})});
 await h.player.listen('A',1);await settle();h.audio.duration=10;await h.emitEnded();await settle();assert.equal(h.audio.src,'/s/1');count=1;
 await h.timers.filter(t=>!t.cancelled&&String(t.fn).includes('waitForAudio')).at(-1).fn();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().segment_index,1);assert.equal(h.player.metrics().want_play,false);assert.equal(h.audio.src,'/s/1');
});

test('slider preview never seeks or generates until release, and native updates do not move its thumb',async()=>{
 const segments=[{url:'/s/0',duration:20},{url:'/s/1',duration:35},{url:'/s/2',duration:65}],h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',segments,complete:true,worker_active:false})});
 await h.player.listen('A',1);await settle();await h.get('playPause').onclick();const before=h.player.metrics(),jobs=h.calls.filter(([p,m])=>p.endsWith('/audio/jobs')&&m==='POST').length;
 h.get('audioSeek').onpointerdown();for(const value of ['100','500','750']){h.get('audioSeek').value=value;h.get('audioSeek').oninput();}
 h.audio.handlers.timeupdate();assert.equal(h.get('audioSeek').value,'750');assert.equal(h.player.metrics().seconds,before.seconds);assert.equal(h.calls.filter(([p,m])=>p.endsWith('/audio/jobs')&&m==='POST').length,jobs);
 h.get('audioSeek').onchange();await settle();assert.equal(h.player.metrics().segment_index,2);assert.equal(h.player.metrics().segment_seconds,35);assert.equal(h.audio.paused,true);
});

test('headphones OFF invalidates late ON intent and keeps the media paused',async()=>{
 let release;const gate=new Promise(r=>release=r),h=harness({waitBook:gate});
 const on=h.player.toggleReader('A',{section_id:1,anchor:'text:0',normalized_offset:0,manual_revision:0});await settle();h.player.stopReader();release();await on;await settle();
 assert.equal(h.audio.paused,true);assert.equal(h.player.metrics().want_play,false);assert.equal(h.player.playing(),false);
});

test('cancelled or superseded sleep callbacks cannot pause an active replacement session',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.get('sleepTimer').value='5';h.get('sleepTimer').onchange();const old=h.timers.find(t=>t.delay===300000);
 h.get('sleepTimer').value='10';h.get('sleepTimer').onchange();old.fn();assert.equal(h.audio.paused,false);h.get('sleepTimer').value='0';h.get('sleepTimer').onchange();h.timers.find(t=>t.delay===600000).fn();assert.equal(h.audio.paused,false);
});

test('2.5x persists across chapter selection without regenerating cached recordings',async()=>{
 const h=harness({workerActive:false});await h.player.listen('A',1);await settle();const src=h.audio.src;h.get('playbackSpeed').value='2.5';h.get('playbackSpeed').onchange();await settle();assert.equal(h.audio.playbackRate,2.5);assert.equal(h.audio.src,src);await h.player.listen('A',2);await settle();assert.equal(h.audio.playbackRate,2.5);assert.equal(h.storage.get('webnovelPlaybackRate'),'2.5');assert.equal(h.calls.filter(([p,m])=>p.endsWith('/audio/jobs')&&m==='POST').length,0);
});

test('missing chunk duration metadata uses native complete-file timing, not a fabricated short timeline',async()=>{
 const h=harness({audioStatus:()=>({status:'ready',asset_id:'asset-1',url:'/complete',segments:[{url:'/s/0',duration:10}],timeline_complete:false,complete:true,worker_active:false})});await h.player.toggleReader('A',{section_id:1,anchor:'text:0',normalized_offset:0,manual_revision:0});await settle();assert.equal(h.audio.src,'/complete');h.get('audioSeek').value='750';h.get('audioSeek').oninput();h.get('audioSeek').onchange();await settle();assert.equal(h.audio.currentTime,90);assert.equal(h.player.metrics().seconds,90);
});

test('latest listening checkpoint survives an acknowledged older own save without accepting unrelated versions',async()=>{
 const server={version:4,section_id:1,asset_id:'asset-1',seconds:12,segment_index:1,segment_seconds:2,voice:'en-US-AndrewNeural',synthesis_rate:0},saved={bookId:'A',sectionId:1,assetId:'asset-1',seconds:23,segmentIndex:2,segmentSeconds:3,voice:'en-US-AndrewNeural',rate:0,serverVersion:3,savedAt:Date.now()+1000,pendingWrite:{...server,book_id:'A'}};
 const segments=Array.from({length:5},(_,i)=>({url:'/segment/'+i,duration:10})),config={preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:2},listeningProgress:server,storage:{webnovelPlaybackCheckpoint:JSON.stringify(saved)},audioStatus:()=>({status:'streaming',asset_id:'asset-1',segments,complete:true,worker_active:false})};
 const h=harness(config);await h.player.listen('A',null,false);await settle();assert.equal(h.player.metrics().seconds,23);assert.equal(h.audio.paused,true);
 const other=harness({...config,listeningProgress:{...server,seconds:15}});await other.player.listen('A',null,false);await settle();assert.equal(other.player.metrics().seconds,12); // precise server segment cursor wins
});

test('listening never writes reading progress while no reader is attached, even with persisted follow enabled',async()=>{
 const h=harness({readingProgress:{version:0,preferences:{followAlong:true,followAlongSuspended:false}}});await h.player.listen('A',1);await settle();h.audio.currentTime=12;h.audio.handlers.timeupdate();await settle();
 assert(h.saves.length>0);assert.equal(h.calls.filter(([url,method])=>url.endsWith('/progress')&&method==='PUT').length,0);
});

test('a stalled download with playable data is not buffering; canplay reconciles unpaused playback',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.readyState=4;h.audio.currentTime=17;
 h.audio.handlers.stalled();assert.equal(h.player.metrics().media_state,'playing');
 h.audio.handlers.waiting();assert.equal(h.player.metrics().media_state,'buffering');h.audio.handlers.canplay();await settle();
 assert.equal(h.player.metrics().media_state,'playing');assert.equal(h.audio.currentTime,17);assert.equal(h.audio.src,'/api/audio/asset-1');
});

test('15-second seeks cross published chunks precisely in both directions at 2x',async()=>{
 const segments=Array.from({length:5},(_,i)=>({url:'/segment/'+i,duration:10}));
 const h=harness({preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:2,volume:1},audioStatus:()=>({status:'streaming',asset_id:'asset-1',segments,complete:true,worker_active:false})});
 await h.player.listen('A',1);await settle();h.audio.currentTime=8;
 await h.player.seek(15);await settle();assert.equal(h.audio.src,'/segment/2');assert.equal(h.audio.currentTime,3);assert.equal(h.player.metrics().seconds,23);assert.equal(h.audio.playbackRate,2);
 await h.player.seek(-15);await settle();assert.equal(h.audio.src,'/segment/0');assert.equal(h.audio.currentTime,8);assert.equal(h.player.metrics().seconds,8);
});
test('native transient zero during seeking cannot replace the requested chunk timestamp or listening save',async()=>{
 const segments=Array.from({length:5},(_,i)=>({url:'/segment/'+i,duration:10}));const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',segments,complete:true,worker_active:false})});await h.player.listen('A',1);await settle();h.audio.currentTime=8;await h.player.seek(15);await settle();
 h.audio.seeking=true;h.audio.currentTime=0;h.audio.handlers.timeupdate();await settle();assert.equal(h.player.metrics().seconds,23);assert.equal(h.saves.some(x=>x.seconds===20),false);
 h.audio.currentTime=3;h.audio.seeking=false;h.audio.handlers.seeked?.();await settle();assert.equal(h.player.metrics().seconds,23);
});
test('metadata readiness never clears a positive seek until the native decoded target is confirmed',async()=>{
 const segments=Array.from({length:5},(_,i)=>({url:'/segment/'+i,duration:10}));const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',segments,complete:true,worker_active:false})});await h.player.listen('A',1,false);await settle();h.audio.currentTime=8;
 h.audio.load=()=>{h.audio.readyState=2;h.audio.currentTime=0;queueMicrotask(()=>h.audio.handlers.loadedmetadata());};await h.player.seek(15);await settle();h.audio.currentTime=0;h.audio.handlers.timeupdate();await settle();assert.equal(h.player.metrics().seconds,23);assert.equal(h.audio.paused,true);
 h.audio.readyState=4;h.audio.handlers.loadeddata();await settle();h.audio.handlers.loadeddata();await settle();assert.equal(h.audio.currentTime,3);assert.equal(h.player.metrics().seconds,23);assert.equal(h.audio.paused,true);
});
test('a newer same-chunk seek supersedes an older pending decoder goal',async()=>{
 const segments=Array.from({length:5},(_,i)=>({url:'/segment/'+i,duration:10})),h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',segments,complete:true,worker_active:false})});await h.player.listen('A',1,false);await settle();h.audio.currentTime=8;
 h.audio.load=()=>{h.audio.readyState=2;h.audio.currentTime=0;queueMicrotask(()=>h.audio.handlers.loadedmetadata());};await h.player.seek(15);await settle();await h.player.seek(5);await settle();h.audio.readyState=4;h.audio.handlers.loadeddata();h.audio.handlers.loadeddata();await settle();assert.equal(h.player.metrics().seconds,28);assert.equal(h.audio.currentTime,8);
});
test('a stuck native decoder reloads only the same cached source at its exact timestamp',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=17;h.audio.readyState=2;h.audio.handlers.waiting();const recovery=h.timers.findLast(t=>!t.cancelled&&t.delay===2000);assert(recovery);await recovery.fn();await settle();assert.equal(h.audio.src.split('?')[0],'/api/audio/asset-1');assert(h.audio.src.includes('reader_retry='));assert.equal(h.audio.currentTime,17);assert.equal(h.player.metrics().section_id,1);assert.equal(h.calls.filter(([p,m,b])=>p.endsWith('/audio/jobs')&&m==='POST'&&!b.prefetch).length,0);
});

test('unpublished seek retains target and checkpoint, then loads the correct newly published chunk',async()=>{
 let published=2;const h=harness({audioStatus:()=>({status:'streaming',asset_id:'asset-1',segments:Array.from({length:published},(_,i)=>({url:'/segment/'+i,duration:10})),complete:false,worker_active:false})});
 await h.player.listen('A',1);await settle();h.audio.currentTime=8;await h.player.seek(15);await settle();
 assert.equal(h.player.metrics().pending_seek,23);assert.equal(h.audio.paused,true);assert.equal(JSON.parse(h.storage.get('webnovelPlaybackCheckpoint')).seconds,23);
 published=4;await h.timers.findLast(t=>!t.cancelled&&t.delay===1500).fn();await settle();
 assert.equal(h.player.metrics().pending_seek,null);assert.equal(h.audio.src,'/segment/2');assert.equal(h.audio.currentTime,3);assert.equal(h.audio.paused,false);
});

test('15-second skips clamp at chapter boundaries without generation',async()=>{
 const h=harness({audioStatus:()=>({status:'ready',asset_id:'asset-1',segments:[{url:'/segment/0',duration:10},{url:'/segment/1',duration:10}],complete:true,worker_active:false})});
 await h.player.listen('A',1);await settle();h.audio.duration=20;h.audio.currentTime=18;await h.player.seek(15);await settle();
 assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().seconds,20);
 await h.player.seek(-15);await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.player.metrics().seconds,5);
 assert.equal(h.calls.filter(([path,method])=>path.endsWith('/audio/jobs')&&method==='POST').length,0);
});

test('player resumes server listening position and persists speed/seek',async()=>{
 const h=harness();await h.player.listen('A');await settle();
 assert.equal(h.audio.src,'/api/audio/asset-2');assert.equal(h.audio.currentTime,37);assert.equal(h.audio.playbackRate,1.5);assert.equal(h.audio.paused,false);
 h.get('forward').click();assert.equal(h.audio.currentTime,52);h.get('rewind').click();assert.equal(h.audio.currentTime,37);
 h.get('playbackSpeed').value='2';h.get('playbackSpeed').onchange();await settle();
 assert.equal(h.saves.at(-1).seconds,37);assert.equal(h.saves.at(-1).playback_rate,2);
});

test('workspace navigation retains player and auto-next stays in the same book',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();
 h.player.navigate('/books/A');assert.equal(h.get('workspace').src,'/books/A');assert.equal(h.audio.paused,false);
 h.audio.currentTime=120;await h.emitEnded();await settle();
 assert.equal(h.audio.src,'/api/audio/asset-2');assert.equal(h.get('playingBook').textContent,'Book A');
 assert.ok(h.calls.some(([path,method,data])=>method==='POST'&&path==='/api/books/A/audio/jobs'&&data.prefetch));
 assert.ok(!h.calls.some(([path])=>path.includes('/api/books/B/')));
});

test('missing audio queues requested chapter and waits for incremental generation',async()=>{
 const h=harness();h.missing(true);await h.player.listen('A',3);await settle();
 assert.ok(h.calls.some(([path,method,data])=>path==='/api/books/A/audio/jobs'&&data.section_id===3&&!data.prefetch));
 assert.match(h.get('playerStatus').textContent,/online Edge TTS/);
 h.missing(false);const poll=h.timers.find(t=>t.delay===1500&&!t.cancelled);await poll.fn();await settle();
 assert.equal(h.audio.src,'/api/audio/asset-3');assert.equal(h.audio.paused,false);
});

test('voice change requests a separate cached variant',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();
 h.get('playerVoice').value='en-GB-SoniaNeural';await h.get('playerVoice').onchange();await settle();
 assert.ok(h.calls.some(([path])=>path.includes('voice=en-GB-SoniaNeural')));
});

test('sleep timer stops playback and Media Session actions are registered',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();
 h.get('sleepTimer').value='10';h.get('sleepTimer').onchange();h.timers.find(t=>t.delay===600000).fn();await settle();
 assert.equal(h.audio.paused,true);assert.match(h.get('playerStatus').textContent,/Sleep timer/);
 assert.ok(h.actions.play&&h.actions.seekto&&h.actions.nexttrack);
});

test('listening conflict pauses playback instead of overwriting another device',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.conflict();
 h.audio.currentTime=22;h.audio.handlers.timeupdate();await settle();
 assert.equal(h.audio.paused,true);assert.match(h.get('playerStatus').textContent,/another player/);
});

test('stopped worker gives actionable status rather than fake synthesis progress',async()=>{
 const h=harness({workerActive:false});h.missing(true);await h.player.listen('A',1);await settle();
 assert.match(h.get('playerStatus').textContent,/worker is stopped/i);
 assert.ok(h.calls.some(([path,method])=>path==='/api/books/A/audio/jobs'&&method==='POST'));
 assert.ok(!/Generating/.test(h.get('playerStatus').textContent));
});

test('competing Listen requests keep the last requested book and chapter',async()=>{
 let release;const waitBook=new Promise(resolve=>release=resolve);const h=harness({waitBook});
 const old=h.player.listen('A',1);await settle();await h.player.listen('B',3);release();await old;await settle();
 assert.equal(h.get('playingBook').textContent,'Book B');assert.equal(h.get('playingChapter').textContent,'3. Chapter 3');
 assert.equal(h.audio.src,'/api/audio/asset-3');assert.equal(h.storage.get('webnovelListeningBook'),'B');
});

test('reload restores saved player but a different open reader does not select an unrelated book',async()=>{
 const a=harness({storage:{webnovelListeningBook:'A'}});await settle();assert.equal(a.get('playingBook').textContent,'Book A');assert.equal(a.audio.paused,true);
 const b=harness({search:'?view=%2Freader%2FB',storage:{webnovelListeningBook:'A'}});await settle();
 assert.ok(!b.calls.some(([path])=>path==='/api/books/A'));await b.player.listen('B',1);assert.equal(b.get('playingBook').textContent,'Book B');
});

test('Andrew selection and synthesis rate can be saved as default without replacing audio variants',async()=>{
 const h=harness();await h.player.listen('A',1);h.get('playerVoice').value='en-US-AndrewNeural';await h.get('playerVoice').onchange();await settle();
 h.get('synthesisSpeed').value='25';await h.get('synthesisSpeed').onchange();await settle();await h.get('saveDefaultVoice').onclick();
 const saved=h.calls.filter(([path,method])=>path==='/api/edge/preferences'&&method==='PUT').at(-1);assert.equal(saved[2].voice,'en-US-AndrewNeural');assert.equal(saved[2].rate,25);
 assert.ok(h.calls.some(([path])=>path.includes('voice=en-US-AndrewNeural')));
});

test('a requested chapter pending synthesis survives reload without reverting to the older completed chapter',async()=>{
 const h=harness({preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:1,volume:1},storage:{webnovelListeningBook:'A',webnovelListeningSelection:JSON.stringify({bookId:'A',sectionId:3,voice:'en-US-AndrewNeural',rate:0,pending:true})}});await settle();
 assert.equal(h.get('playingChapter').textContent,'3. Chapter 3');assert.equal(h.get('playerVoice').value,'en-US-AndrewNeural');
});


test('immersive navigation and collapsed player retain audio state and volume control',async()=>{const h=harness();await h.player.listen('A',1);await settle();const src=h.audio.src;h.player.navigate('/reader/A');assert.equal(h.document.body.dataset.reading,'true');h.player.expand();assert.equal(h.get('playerPanel').dataset.mini,'false');assert.equal(h.audio.src,src);h.get('volume').value='.5';h.get('volume').oninput();assert.equal(h.audio.volume,.5);h.player.navigate('/library');assert.equal(h.document.body.dataset.reading,'false');assert.equal(h.audio.src,src);});


test('cached chapters continue across three sections with stopped worker and do not prefetch',async()=>{const h=harness({workerActive:false});await h.player.listen('A',1);await settle();await h.emitEnded();await settle();await h.emitEnded();await settle();assert.equal(h.audio.src,'/api/audio/asset-3');assert.equal(h.get('playingBook').textContent,'Book A');assert.equal(h.audio.paused,false);assert.ok(!h.calls.some(([p,m,d])=>p.endsWith('/audio/jobs')&&d.prefetch));});
test('resource-paused worker does not prefetch cached next chapter',async()=>{const h=harness({workerActive:true,canGenerate:false});await h.player.listen('A',1);await settle();assert.equal(h.audio.paused,false);assert.ok(!h.calls.some(([p,m,d])=>p.endsWith('/audio/jobs')&&d.prefetch));});


test('published segments play before final chapter and continue without restarting current segment',async()=>{
 let state={status:'streaming',asset_id:'stream',complete:false,worker_active:true,segments:[{index:0,duration:10,url:'/segment/0'}]};
 const h=harness({audioStatus:()=>state});await h.player.listen('A',1);await settle();
 assert.equal(h.audio.src,'/segment/0');assert.equal(h.audio.paused,false);
 h.audio.currentTime=4;h.audio.handlers.timeupdate();await settle();assert.ok(h.saves.some(s=>s.seconds===4&&s.segment_index===0));assert.match(h.get('audioTime').textContent,/generating/);
 state={...state,segments:[...state.segments,{index:1,duration:12,url:'/segment/1'}]};
 await h.timers.find(t=>!t.cancelled).fn();await settle();assert.equal(h.audio.src,'/segment/0');
 h.audio.currentTime=10;await h.emitEnded();await settle();assert.equal(h.audio.src,'/segment/1');
 h.audio.currentTime=3;h.audio.handlers.timeupdate();h.get('playbackSpeed').value='2';h.get('playbackSpeed').onchange();await settle();assert.ok(h.saves.some(s=>s.seconds===13&&s.segment_index===1));
});

test('segment end buffers for missing segment and resumes automatically when it arrives',async()=>{
 let state={status:'streaming',asset_id:'stream',complete:false,worker_active:true,segments:[{index:0,duration:10,url:'/segment/0'}]};
 const h=harness({audioStatus:()=>state});await h.player.listen('A',1);await settle();h.audio.currentTime=10;
 await h.emitEnded();await settle();assert.match(h.get('playerStatus').textContent,/Buffering/);assert.equal(h.timers.filter(t=>!t.cancelled&&String(t.fn).includes('waitForAudio')).at(-1).delay,500);
 state={...state,segments:[...state.segments,{index:1,duration:12,url:'/segment/1'}]};await h.timers.filter(t=>!t.cancelled&&String(t.fn).includes('waitForAudio')).at(-1).fn();await settle();assert.equal(h.audio.src,'/segment/1');assert.equal(h.audio.paused,false);
 state={...state,status:'ready',complete:true};await h.timers.filter(t=>!t.cancelled&&String(t.fn).includes('waitForAudio')).at(-1).fn();await settle();
 await h.emitEnded();await settle();assert.match(h.get('playingChapter').textContent,/2\./);
});


test('pausing during segment buffering prevents automatic playback when more audio arrives',async()=>{
 let state={status:'streaming',asset_id:'stream',complete:false,worker_active:true,segments:[{index:0,duration:10,url:'/segment/0'}]};
 const h=harness({audioStatus:()=>state});await h.player.listen('A',1);await settle();h.audio.currentTime=10;await h.emitEnded();await settle();
 await h.get('playPause').onclick();await settle();assert.match(h.get('playerStatus').textContent,/Paused/);
 h.audio.paused=true;state={...state,segments:[...state.segments,{index:1,duration:12,url:'/segment/1'}]};await h.timers.filter(t=>!t.cancelled&&String(t.fn).includes('waitForAudio')).at(-1).fn();await settle();assert.equal(h.audio.src,'/segment/1');assert.equal(h.audio.paused,true);
});


test('2x survives source resets across three chunks and three chapters, pause and reload',async()=>{
 const h=harness({resetRateOnLoad:true,audioStatus:()=>({status:'streaming',asset_id:'segments',complete:true,worker_active:false,segments:[0,1,2].map(index=>({index,duration:10,url:'/segment/'+index}))})});
 await h.player.listen('A',1);await settle();h.get('playbackSpeed').value='2';h.get('playbackSpeed').onchange();await settle();
 for(let chapter=1;chapter<=3;chapter++){
  for(let chunk=0;chunk<3;chunk++){assert.equal(h.audio.playbackRate,2);assert.equal(h.audio.defaultPlaybackRate,2);assert.equal(h.get('playbackSpeed').value,'2');await h.emitEnded();await settle();}
 }
 assert.equal(h.get('playingChapter').textContent,'3. Chapter 3');
 await h.get('playPause').onclick();await settle();assert.equal(h.audio.playbackRate,2);
 const reloaded=harness({resetRateOnLoad:true,preferences:{voice:'en-US-AriaNeural',rate:0,playback_rate:2,volume:1},storage:Object.fromEntries(h.storage)});await reloaded.player.listen('A',3);await settle();assert.equal(reloaded.audio.playbackRate,2);
});


test('global speed voice synthesis rate and volume follow the listener across books',async()=>{
 const h=harness({resetRateOnLoad:true,preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:1,volume:1}});await h.player.listen('A',1);await settle();
 h.get('playbackSpeed').value='2';h.get('playbackSpeed').onchange();h.get('volume').value='.4';h.get('volume').oninput();await h.get('volume').onchange();
 h.get('synthesisSpeed').value='20';await h.get('synthesisSpeed').onchange();await settle();await h.player.listen('B',1);await settle();
 assert.equal(h.audio.playbackRate,2);assert.equal(h.audio.volume,.4);assert.equal(h.get('playerVoice').value,'en-US-AndrewNeural');assert.equal(h.get('synthesisSpeed').value,20);
 h.get('playbackSpeed').value='1.5';h.get('playbackSpeed').onchange();await h.player.listen('A',1);await settle();assert.equal(h.audio.playbackRate,1.5);
 const count=h.calls.filter(([p,m])=>p.endsWith('/audio/jobs')&&m==='POST').length;h.get('playbackSpeed').value='1.75';h.get('playbackSpeed').onchange();await settle();assert.equal(h.calls.filter(([p,m])=>p.endsWith('/audio/jobs')&&m==='POST').length,count);
});


test('expand, reopen and collapse preserve one audio instance, position, 2x, polling and auto-next',async()=>{
 const h=harness({resetRateOnLoad:true,preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:2,volume:.5}});
 await h.player.listen('A',1);await settle();h.player.navigate('/reader/A');assert.equal(h.get('playerPanel').dataset.mini,'true');
 h.audio.currentTime=12;h.get('miniOpen').click();const source=h.audio.src,calls=h.calls.length,timers=h.timers.map(t=>t.cancelled);
 h.get('expandPlayer').click();assert.equal(h.audio.paused,false);assert.equal(h.audio.currentTime,12);assert.equal(h.audio.src,source);assert.equal(h.audio.playbackRate,2);assert.equal(h.get('playerPanel').hidden,false);assert.deepEqual(h.timers.map(t=>t.cancelled),timers);
 await h.player.listen('A',1);await settle();assert.equal(h.calls.length,calls);assert.equal(h.audio.currentTime,12);assert.equal(h.get('playerPanel').dataset.mini,'false');
 h.get('expandPlayer').click();assert.equal(h.get('playerPanel').dataset.mini,'true');assert.equal(h.audio.paused,false);
 h.get('miniPlay').click();await settle();assert.equal(h.audio.paused,true);h.get('miniPlay').click();await settle();assert.equal(h.audio.paused,false);
 h.player.navigate('/reader/A');assert.equal(h.audio.src,source);h.audio.currentTime=120;await h.emitEnded();await settle();assert.equal(h.audio.src,'/api/audio/asset-2');assert.equal(h.audio.playbackRate,2);assert.equal(h.audio.paused,false);
 h.get('audioNext').click();await settle();assert.equal(h.audio.src,'/api/audio/asset-3');h.get('audioPrevious').click();await settle();assert.equal(h.audio.src,'/api/audio/asset-2');assert.equal(h.audio.playbackRate,2);
});

test('minimizing progressive audio keeps polling and subsequent segments alive',async()=>{
 let state={status:'streaming',asset_id:'stream',complete:false,worker_active:true,segments:[{index:0,duration:10,url:'/segment/0'}]};
 const h=harness({audioStatus:()=>state});await h.player.listen('A',1);await settle();h.get('expandPlayer').click();assert.equal(h.audio.paused,false);
 state={...state,segments:[...state.segments,{index:1,duration:12,url:'/segment/1'}]};const timer=h.timers.find(t=>!t.cancelled);assert.ok(timer);await timer.fn();await settle();h.audio.currentTime=10;await h.emitEnded();await settle();assert.equal(h.audio.src,'/segment/1');assert.equal(h.audio.paused,false);assert.equal(h.get('playerPanel').dataset.mini,'true');
});

test('notification-style pause requires an explicit tap and preserves exact position and 2x',async()=>{
 const h=harness({preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:2,volume:1}});await h.player.listen('A',1);await settle();
 h.audio.currentTime=32;const src=h.audio.src;h.audio.pause();assert.equal(h.player.metrics().want_play,false);h.lifecycle.pageshow();h.documentEvents.visibilitychange();await settle();assert.equal(h.audio.paused,true);await h.actions.play();await settle();
 assert.equal(h.audio.paused,false);assert.equal(h.audio.currentTime,32);assert.equal(h.audio.src,src);assert.equal(h.audio.playbackRate,2);
});
test('intentional pause cancels interruption recovery and foreground never resumes it',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=32;h.audio.pause();
 h.actions.pause();h.lifecycle.pageshow();h.documentEvents.visibilitychange();await settle();
 assert.equal(h.audio.paused,true);assert.equal(h.player.metrics().want_play,false);assert.equal(h.audio.currentTime,32);
 assert.ok(h.timers.filter(t=>t.delay===750).every(t=>t.cancelled));
});
test('lock screen Play restores intent and resumes after lock-screen Pause',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=42;h.actions.pause();await settle();
 await h.actions.play();await settle();assert.equal(h.player.metrics().want_play,true);assert.equal(h.audio.paused,false);assert.equal(h.audio.currentTime,42);
});
test('waiting and canplay preserve intent and recover without regenerating',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();const calls=h.calls.length;
 h.audio.handlers.waiting();h.audio.paused=true;await h.audio.handlers.canplay();await settle();
 assert.equal(h.audio.paused,false);assert.equal(h.player.metrics().want_play,true);assert.equal(h.calls.length,calls);
});
test('failed autoplay promise retains intent, reports policy restriction, and explicit Play resumes',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();const play=h.audio.play;h.actions.pause();
 h.audio.play=()=>Promise.reject(Object.assign(new Error('Tap required'),{name:'NotAllowedError'}));await h.actions.play();await settle();
 assert.equal(h.player.metrics().want_play,true);assert.equal(h.player.metrics().media_state,'interrupted');assert.match(h.get('playerStatus').textContent,/tap/);
 h.audio.play=play;await h.actions.play();await settle();assert.equal(h.audio.paused,false);
});
test('stale and duplicate ended callbacks cannot advance a newer source',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();const stale=h.rawEnded();
 await h.emitEnded();await settle();assert.equal(h.player.metrics().section_id,2);
 h.audio.ended=true;await stale();await settle();assert.equal(h.player.metrics().section_id,2);
});
test('pause and long inactivity prevent auto-next even if an ended callback arrives',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=32;h.actions.pause();await settle();
 await h.rawEnded()();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.audio.currentTime,32);assert.equal(h.audio.paused,true);
 const saved=JSON.parse(h.storage.get('webnovelPlaybackCheckpoint'));assert.equal(saved.sectionId,1);assert.equal(saved.seconds,32);assert.equal(saved.wantPlay,false);
});
test('paused checkpoint restores exact chapter and position after reload',async()=>{
 const saved={bookId:'A',sectionId:1,seconds:32,segmentIndex:0,segmentSeconds:32,voice:'en-US-AriaNeural',rate:0,wantPlay:false,savedAt:Date.now()};
 const h=harness({storage:{webnovelListeningBook:'A',webnovelPlaybackCheckpoint:JSON.stringify(saved)}});await settle();
 assert.equal(h.player.metrics().section_id,1);assert.equal(h.audio.currentTime,32);assert.equal(h.audio.paused,true);
});
test('canonical 45 to 46 to 47 stops once despite duplicate titles and unordered IDs',async()=>{
 const ids=[500,42,900];const h=harness({section:id=>{const index=ids.indexOf(id);return {id,position:44+index,title:'Duplicate title',previous_id:ids[index-1]||null,next_id:ids[index+1]||null};}});
 await h.player.listen('A',500);await settle();await h.emitEnded();await settle();assert.equal(h.player.metrics().section_id,42);
 await h.emitEnded();await settle();assert.equal(h.player.metrics().section_id,900);
 await h.emitEnded();await settle();assert.equal(h.player.metrics().section_id,900);assert.equal(h.player.metrics().want_play,false);assert.equal(h.player.metrics().media_state,'completed');
 await h.rawEnded()();await settle();assert.equal(h.player.metrics().section_id,900);
});
test('backward canonical neighbor is rejected instead of using an earlier cached chapter',async()=>{
 const h=harness({section:id=>({id,position:id===46?45:41,title:'Chapter',next_id:42,previous_id:null})});await h.player.listen('A',46);await settle();await h.emitEnded();await settle();
 assert.equal(h.player.metrics().section_id,46);assert.equal(h.player.metrics().want_play,false);assert.match(h.get('playerStatus').textContent,/Invalid chapter order/);
});
test('audio-status network failure retains intent and retries without jumping chapters',async()=>{
 let fail=false;const h=harness({audioStatus:()=>{if(fail)throw new Error('Network down');return {status:'streaming',asset_id:'s',complete:false,segments:[{index:0,duration:10,url:'/s/0'}]};}});
 await h.player.listen('A',1);await settle();fail=true;await h.timers.filter(t=>!t.cancelled&&String(t.fn).includes('waitForAudio')).at(-1).fn();await settle();
 assert.equal(h.player.metrics().want_play,true);assert.equal(h.player.metrics().section_id,1);assert.match(h.get('playerStatus').textContent,/Network down/);
 fail=false;await h.timers.filter(t=>!t.cancelled&&String(t.fn).includes('waitForAudio')).at(-1).fn();await settle();assert.equal(h.audio.src,'/s/0');
});

test('complete cached MP3 tracks actual chunk index from chapter timestamp without source changes',async()=>{
 const h=harness({audioStatus:()=>({status:'ready',asset_id:'assembled',url:'/full.mp3',complete:true,segments:[0,1,2].map(index=>({index,duration:10,url:'/chunk/'+index}))})});
 await h.player.listen('A',1);await settle();h.audio.currentTime=23;h.audio.handlers.timeupdate();await settle();
 assert.equal(h.audio.src,'/full.mp3');assert.equal(h.player.metrics().segment_index,2);assert.equal(h.player.metrics().segment_seconds,3);
 assert.ok(h.calls.some(([path])=>path==='/api/audio/assembled/location/2'));
});
test('latest of two queued cross-segment seeks wins without stale source replacement',async()=>{
 const h=harness({audioStatus:()=>({status:'streaming',asset_id:'s',complete:true,segments:[0,1,2].map(index=>({index,duration:10,url:'/s/'+index}))})});await h.player.listen('A',1);await settle();
 h.get('audioSeek').value='400';h.get('audioSeek').onchange();h.get('audioSeek').value='800';h.get('audioSeek').onchange();await settle();
 assert.equal(h.audio.src,'/s/2');assert.equal(h.audio.currentTime,4);
});
test('per-book progress versions remain isolated when switching between books',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=12;h.actions.pause();await settle();await h.player.listen('B',3);await settle();
 h.audio.currentTime=20;h.actions.pause();await settle();await h.player.listen('A',1);await settle();
 assert.equal(h.player.metrics().section_id,1);assert.equal(h.audio.currentTime,12);assert.ok(!/another player/.test(h.get('playerStatus').textContent));
});

test('a stale local checkpoint version cannot replace newer server listening progress',async()=>{
 const saved={bookId:'A',sectionId:1,seconds:32,voice:'en-US-AriaNeural',rate:0,savedAt:Date.now(),serverVersion:-1};
 const h=harness({storage:{webnovelListeningBook:'A',webnovelPlaybackCheckpoint:JSON.stringify(saved)}});await settle();
 assert.equal(h.player.metrics().section_id,2);assert.equal(h.audio.currentTime,37);
});
test('preserved Piper audio publishes chapter-level narration without guessing timings',async()=>{
 const h=harness({audioStatus:()=>({status:'ready',asset_id:'legacy-1',url:'/legacy/1',provider:'legacy'})});await h.player.listen('A',1);await settle();
 assert.equal(h.player.narration().section_id,1);assert.equal(h.player.narration().precision,'chapter');assert.equal(h.player.narration().quote,'');
});

test('forgetting a book clears intent and stale lifecycle or lock-screen Play cannot revive it',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();const old=h.rawEnded();h.player.forgetBook('A');
 h.lifecycle.pageshow();h.actions.play();await old();await settle();
 assert.equal(h.audio.paused,true);assert.equal(h.player.metrics().want_play,false);assert.equal(h.player.metrics().section_id,undefined);
 assert.equal(h.storage.get('webnovelPlaybackCheckpoint'),undefined);
});

test('unusable media that resets native time recovers the last known position without restarting',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=32;h.audio.handlers.timeupdate();await settle();
 h.audio.error={code:2,message:'Network interrupted'};h.audio.currentTime=0;h.audio.paused=true;h.audio.handlers.error();await settle();
 await h.get('playPause').onclick();await settle();assert.equal(h.audio.currentTime,32);assert.equal(h.audio.paused,false);assert.equal(h.audio.src,'/api/audio/asset-1');
});
test('a saved published segment cursor restores that exact segment and timestamp while generation continues',async()=>{
 const checkpoint={bookId:'A',sectionId:1,assetId:'stream',seconds:13,segmentIndex:1,segmentSeconds:3,voice:'en-US-AriaNeural',rate:0,savedAt:Date.now(),serverVersion:0};
 const h=harness({storage:{webnovelListeningBook:'A',webnovelPlaybackCheckpoint:JSON.stringify(checkpoint)},audioStatus:()=>({status:'streaming',asset_id:'stream',complete:false,segments:[0,1,2].map(index=>({index,duration:10,url:'/stream/'+index}))})});await settle();
 assert.equal(h.audio.src,'/stream/1');assert.equal(h.audio.currentTime,3);assert.equal(h.player.metrics().seconds,13);assert.equal(h.audio.paused,true);
});

test('duplicate metadata callbacks cannot reset an already restored playback timestamp',async()=>{
 const h=harness();await h.player.listen('A');await settle();assert.equal(h.audio.currentTime,37);
 h.audio.handlers.loadedmetadata();await settle();assert.equal(h.audio.currentTime,37);
});

 test('prefetch keeps only the immediate published successor without creating another audio controller',async()=>{
 const h=harness({prefetch:true,audioStatus:()=>({status:'streaming',asset_id:'s',complete:true,segments:[0,1,2].map(index=>({index,duration:10,url:'/s/'+index}))})});
 await h.player.listen('A',1);await settle();assert.equal(h.document.head.children.length,1);assert.equal(h.document.head.children[0].href,'/s/1');
 await h.emitEnded();await settle();assert.equal(h.document.head.children.length,1);assert.equal(h.document.head.children[0].href,'/s/2');assert.equal(h.audio.src,'/s/1');
 await h.emitEnded();await settle();assert.equal(h.document.head.children.length,0);assert.equal(h.audio.src,'/s/2');
 });

test('floating title returns to paused narration without replacing or resuming audio',async()=>{const h=harness({preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:2,volume:1}});await h.player.listen('A',2);await settle();h.audio.currentTime=22;await h.get('playPause').onclick();const src=h.audio.src;h.player.navigate('/library');h.get('readPlaying').click();assert.equal(h.get('workspace').src,'/reader/A?section=2&narration=1');assert.equal(h.get('playerPanel').dataset.mini,'true');assert.equal(h.audio.src,src);assert.equal(h.audio.currentTime,22);assert.equal(h.audio.paused,true);assert.equal(h.audio.playbackRate,2);});
test('headphones resume an existing paused session without synthesis or source replacement',async()=>{const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=27;await h.get('playPause').onclick();const src=h.audio.src,n=h.calls.filter(x=>x[0].endsWith('/audio/jobs')).length;await h.player.listen('A',1);await settle();assert.equal(h.audio.paused,false);assert.equal(h.audio.currentTime,27);assert.equal(h.audio.src,src);assert.equal(h.calls.filter(x=>x[0].endsWith('/audio/jobs')).length,n);assert.equal(h.get('miniPlay').textContent,'Ⅱ');});
test('premature ended cannot skip the remainder of a chunk at 2x',async()=>{const h=harness({preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:2,volume:1}});await h.player.listen('A',1);await settle();h.audio.currentTime=20;h.audio.ended=true;await h.rawEnded()();await settle();assert.equal(h.player.metrics().section_id,1);assert.equal(h.audio.currentTime,20);assert.equal(h.audio.playbackRate,2);assert.equal(h.audio.paused,false);});

test('failed generation stops waiting, retains chapter/rate, and explicit Play retries failed job',async()=>{
 let failed=true;
 const h=harness({preferences:{voice:'en-US-AndrewNeural',rate:0,playback_rate:2,volume:.1},audioStatus:()=>failed?({status:'failed',generation_status:'failed',job_id:'job',error:'Microsoft rejected online speech (HTTP 403). Cached audio still works. Retry later.'}):({status:'ready',complete:true,asset_id:'asset-2',url:'/api/audio/asset-2'})});
 await h.player.listen('A',2,true);await settle();
 assert.equal(h.player.metrics().section_id,2);
 assert.equal(h.audio.paused,true);
 assert.match(h.get('audioTime').textContent,/Audio unavailable/);
 assert.match(h.get('playerStatus').textContent,/HTTP 403/);
 assert.equal(h.timers.filter(t=>!t.cancelled).length,0);
 assert.equal(h.player.metrics().playback_rate,2);
 failed=false;await h.get('playPause').onclick();await settle();
 assert.ok(h.calls.some(([path,method,body])=>path==='/api/jobs/job'&&body?.action==='retry'));
 assert.equal(h.audio.paused,false);assert.equal(h.audio.playbackRate,2);assert.equal(h.player.metrics().section_id,2);
});

test('headphone activation starts at the current reader page instead of saved listening seconds',async()=>{
 const h=harness(),position={section_id:3,anchor:'text:800',normalized_offset:800,manual_revision:2};
 await h.player.toggleReader('A',position);await settle();
 assert.equal(h.player.metrics().section_id,3);assert.equal(h.audio.currentTime,20);assert.equal(h.audio.paused,false);
 assert(h.calls.some(([p])=>p==='/api/audio/asset-3/start?offset=800'));
 assert.equal(h.player.metrics().requested_reading_anchor,'text:800');
 await h.player.toggleReader('A',position);assert.equal(h.audio.paused,true);assert.equal(h.player.playing('A'),false);
});
test('headphones OFF then ON resolves the current displayed anchor even without manual movement',async()=>{
 const h=harness(),position={section_id:3,anchor:'text:800',normalized_offset:800,manual_revision:2};
 h.get('workspace').contentWindow={ReaderNarration:{bookId:'A',page:()=>position,playback(){}}};
 await h.player.toggleReader('A',position);await settle();h.audio.currentTime=24;
 await h.player.toggleReader('A',position);const source=h.player.metrics().source_revision,calls=h.calls.filter(([p])=>p.includes('/start?')).length;
 await h.player.toggleReader('A',position);await settle();
 assert.equal(h.audio.currentTime,20);assert(h.player.metrics().source_revision>source);assert.equal(h.calls.filter(([p])=>p.includes('/start?')).length,calls+1);
});
test('headphones OFF then ON deliberately replaces the old listening cursor with the new manual anchor',async()=>{
 const h=harness(),position={section_id:3,anchor:'text:800',normalized_offset:800,manual_revision:2};
 h.get('workspace').contentWindow={ReaderNarration:{bookId:'A',page:()=>position,playback(){}}};
 await h.player.toggleReader('A',position);await settle();await h.player.toggleReader('A',position);
 const next={...position,anchor:'text:1600',normalized_offset:1600,manual_revision:3};
 const source=h.player.metrics().source_revision,seconds=h.player.metrics().seconds;await h.player.toggleReader('A',next);await settle();assert(h.calls.some(([p])=>p==='/api/audio/asset-3/start?offset=1600'));assert(h.player.metrics().source_revision>source);assert.equal(h.player.metrics().requested_reading_anchor,'text:1600');
});
test('unpublished requested audio can be switched OFF and ON without stale autoplay',async()=>{
 let ready=false;const h=harness({startMapping:()=>({available:ready,segment_index:2,seconds:20})}),position={section_id:3,anchor:'text:800',normalized_offset:800,manual_revision:2};
 await h.player.toggleReader('A',position);await settle();assert.equal(h.audio.paused,true);assert.equal(h.player.playing('A'),false);
 await h.player.toggleReader('A',position);assert.equal(h.player.metrics().want_play,false);
 ready=true;await h.player.toggleReader('A',position);await settle();
 assert.equal(h.audio.currentTime,20);assert.equal(h.audio.paused,false);assert.equal(h.player.metrics().requested_reading_anchor,'text:800');
});

test('headphones can retry a denied playback gesture at the exact selected source without being locked',async()=>{
 const h=harness(),position={section_id:3,anchor:'text:800',normalized_offset:800,manual_revision:2},play=h.audio.play;
 let denied=true;h.audio.play=()=>{if(denied){denied=false;return Promise.reject(Object.assign(new Error('Gesture required'),{name:'NotAllowedError'}));}return play();};
 await h.player.toggleReader('A',position);await settle();assert.equal(h.player.metrics().media_state,'interrupted');assert.equal(h.audio.paused,true);
 const source=h.player.metrics().source_revision,mappings=h.calls.filter(([p])=>p.includes('/start?')).length;
 await h.get('playPause').onclick();await settle();assert.equal(h.audio.paused,false);assert.equal(h.player.playing('A'),true);
 assert.equal(h.player.metrics().source_revision,source);assert.equal(h.calls.filter(([p])=>p.includes('/start?')).length,mappings);
});

test('headphones explicitly retry failed or resume paused generation at the requested page',async()=>{
 for(const failure of ['failed','paused']){
  let failed=true;const h=harness({audioStatus:()=>failed?{status:failure,generation_status:failure,job_id:'job',error:'Mock failure'}:{status:'ready',asset_id:'asset-3',url:'/api/audio/asset-3'}}),position={section_id:3,anchor:'text:800',normalized_offset:800,manual_revision:2};
  await h.player.toggleReader('A',position);await settle();assert.equal(h.audio.paused,true);
  failed=false;await h.player.toggleReader('A',position);await settle();assert.equal(h.audio.paused,false);
  assert(h.calls.some(([p,m,d])=>p==='/api/jobs/job'&&m==='POST'&&d.action===(failure==='paused'?'resume':'retry')));
  assert.equal(h.player.metrics().requested_reading_anchor,'text:800');
 }
});

test('a per-book durable checkpoint survives switching to a different book',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=48;h.audio.handlers.timeupdate();h.actions.pause();await settle();
 await h.player.listen('B',1);await settle();h.audio.currentTime=17;h.actions.pause();await settle();
 const a=JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A')),b=JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:B'));
 assert.equal(a.seconds,48);assert.equal(b.seconds,17);assert.equal(a.playbackRate,1.5);
 await h.player.listen('A',null,false);await settle();assert.equal(h.audio.currentTime,48);assert.equal(h.audio.paused,true);
});
test('paused metadata restoration publishes the correct chunk anchor without Play',async()=>{
 const h=harness({audioStatus:()=>({status:'ready',asset_id:'asset-1',url:'/complete',segments:[{duration:10},{duration:30}],timeline_complete:true,complete:true,worker_active:false})});
 await h.player.listen('A',1,true);await settle();h.actions.pause();await settle();assert.equal(h.audio.paused,true);assert.equal(h.player.narration().section_id,1);
 const updates=[];h.get('workspace').contentWindow={ReaderNarration:{bookId:'A',playback(){},update:loc=>updates.push(loc)}};h.get('workspace').handlers.load();await settle();assert.equal(updates.at(-1).anchor,h.player.narration().anchor);
});
test('restoration retains the saved book voice and speed when global defaults changed',async()=>{
 const h=harness({preferences:{voice:'en-US-AndrewNeural',rate:25,playback_rate:2.5,volume:1},listeningProgress:{version:0,section_id:1,asset_id:'asset-1',seconds:37,voice:'en-US-AriaNeural',synthesis_rate:0,playback_rate:1.5}});
 await h.player.listen('A',null,false);await settle();assert.equal(h.player.metrics().voice,'en-US-AriaNeural');assert.equal(h.player.metrics().seconds,37);assert.equal(h.audio.playbackRate,1.5);assert.equal(h.audio.paused,true);
 assert(h.calls.some(([url])=>url.includes('/audio?')&&url.includes('voice=en-US-AriaNeural')&&url.includes('rate=0')));
});

test('headphones OFF checkpoint prevents a fake listening session after reload',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=42;h.player.stopReader();await settle();
 const checkpoint=JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A'));assert.equal(checkpoint.engaged,false);assert.equal(h.player.narration(),null);assert.equal(h.get('playerPanel').hidden,true);
 const restored=harness({storage:Object.fromEntries(h.storage)});await settle();assert.equal(restored.player.metrics().engaged,false);assert.equal(restored.get('playerPanel').hidden,true);assert.equal(restored.player.narration(),null);assert.equal(restored.audio.paused,true);
});
test('reader mini title opens controls without navigating or resuming paused playback',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.actions.pause();const follow=[];h.get('workspace').contentWindow={ReaderNarration:{bookId:'A',follow:()=>follow.push(true)}};
 const source=h.audio.src,seconds=h.audio.currentTime;h.get('miniOpen').click();assert.equal(follow.length,0);assert.equal(h.get('playerPanel').dataset.mini,'false');assert.equal(h.audio.src,source);assert.equal(h.audio.currentTime,seconds);assert(h.audio.paused);
});
test('pause with headphones ON resumes the preserved cursor after independent manual navigation',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=27;h.actions.pause();const source=h.player.metrics().source_revision;
 h.get('workspace').contentWindow={ReaderNarration:{bookId:'A',position:()=>({section_id:3,anchor:'text:9000',normalized_offset:9000})}};
 await h.get('playPause').onclick();await settle();assert.equal(h.audio.currentTime,27);assert.equal(h.player.metrics().source_revision,source);assert.equal(h.player.metrics().engaged,true);
});

test('real cue offsets use the media clock, retain a paused word, and clear when OFF',async()=>{
 const h=harness({narrationLocation:{book_id:'A',section_id:1,normalized_offset:100,anchor:'narration:asset-1:0',quote:'Hello world',precision:'word',cues:[{at:0,duration:.5,offset:0,quote:'Hello'},{at:1,duration:.5,offset:6,quote:'world'}]},audioStatus:()=>({status:'ready',asset_id:'asset-1',url:'/cached',segments:[{duration:10}],complete:true})});
 await h.player.listen('A',1);await settle();assert.equal(h.player.narration().quote,'Hello');h.audio.currentTime=1.2;h.audio.handlers.timeupdate();await settle();assert.equal(h.player.narration().quote,'world');assert.equal(h.player.narration().normalized_offset,106);h.actions.pause();await settle();assert.equal(h.player.narration().quote,'world');h.lifecycle.pageshow();await settle();assert.equal(h.player.narration().quote,'world');h.player.stopReader();assert.equal(h.player.narration(),null);
});
test('an interrupted pending handoff retains its exact requested reading anchor locally',async()=>{
 const position={section_id:3,anchor:'text:800',normalized_offset:800,manual_revision:2},status=()=>({status:'streaming',asset_id:'asset-3',segments:[{duration:10,url:'/zero'}],complete:false,worker_active:false});
 const h=harness({audioStatus:status,startMapping:()=>({available:false,segment_index:2,seconds:20})});await h.player.toggleReader('A',position);await settle();h.lifecycle.pagehide();await settle();
 const local=JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A'));assert.equal(local.readingStart.anchor,'text:800');
 const restored=harness({storage:Object.fromEntries(h.storage),listeningProgress:{version:local.serverVersion,section_id:3,voice:local.voice,synthesis_rate:0},audioStatus:status,startMapping:()=>({available:false,segment_index:2,seconds:20})});await settle();assert(restored.calls.some(([url])=>url==='/api/audio/asset-3/start?offset=800'));assert(restored.audio.paused);
});
test('Pause uses playback intent during the temporary native pause between chunks',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.paused=true;
 await h.get('playPause').onclick();await settle();assert.equal(h.player.metrics().want_play,false);assert(h.audio.paused);
});
test('Pause during initial chapter loading cancels the pending autoplay request',async()=>{
 let release;const h=harness({waitBook:new Promise(r=>release=r)});const pending=h.player.listen('A',1);await settle();await h.get('playPause').onclick();release();await pending;await settle();assert.equal(h.player.metrics().want_play,false);assert(h.audio.paused);
});

test('a delayed pause event cannot mark an actually playing source interrupted',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=24;
 const source=h.player.metrics().source_revision;h.audio.handlers.pause();await settle();
 assert.equal(h.audio.paused,false);assert.equal(h.player.playing(),true);assert.equal(h.player.metrics().want_play,true);
 assert.equal(h.get('playPause').textContent,'Ⅱ');assert.equal(h.player.metrics().source_revision,source);
});

test('foreground repairs lost playing events and an unresolved native play promise without another play',async()=>{
 const h=harness({waitPlay:new Promise(()=>{})});let plays=0;const play=h.audio.play;h.audio.play=()=>{plays++;return play();};
 await h.player.listen('A',1);await settle();h.audio.currentTime=42;
 h.document.hidden=true;h.documentEvents.visibilitychange();h.document.hidden=false;h.documentEvents.visibilitychange();await settle();
 assert.equal(h.player.playing(),true);assert.equal(h.get('playPause').textContent,'Ⅱ');assert.equal(h.audio.currentTime,42);assert.equal(plays,1);
});

test('foreground restores live following but a redundant visible event preserves manual reading',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.player.setFollow('A',false,true);
 h.documentEvents.visibilitychange();await settle();assert.equal((await h.player.readerResume('A')).following,false);
 h.document.hidden=true;h.documentEvents.visibilitychange();h.document.hidden=false;h.documentEvents.visibilitychange();await settle();
 assert.equal((await h.player.readerResume('A')).following,true);
});

test('foreground native pause is truthful and never resumes or resets paused media',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();let plays=0;h.audio.play=()=>{plays++;return Promise.resolve();};
 h.audio.paused=true;h.audio.currentTime=43;h.document.hidden=true;h.documentEvents.visibilitychange();h.document.hidden=false;h.documentEvents.visibilitychange();await settle();
 assert.equal(h.player.playing(),false);assert.equal(h.get('playPause').textContent,'▶');assert.equal(plays,0);assert.equal(h.audio.currentTime,43);
});

test('foreground refreshes expanded and miniature timelines from native time without timeupdate',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=43;
 h.document.hidden=true;h.documentEvents.visibilitychange();h.document.hidden=false;h.documentEvents.visibilitychange();await settle();
 assert.match(h.get('audioTime').textContent,/0:43/);assert.equal(h.get('audioSeek').value,358);assert.equal(h.get('miniProgress').value,358);
});

test('an old rejected play promise cannot interrupt a source already audibly progressing',async()=>{
 let reject;const h=harness({waitPlay:new Promise((r,j)=>reject=j)});await h.player.listen('A',1);await settle();
 h.audio.handlers.playing();h.audio.currentTime=20;reject(new Error('Late native rejection'));await settle();
 assert.equal(h.player.playing(),true);assert.equal(h.player.metrics().media_state,'playing');
 assert(!h.timers.some(t=>!t.cancelled&&t.delay===1000));
});

test('foreground health repairs the recovered stall caption without restarting media',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=12;h.audio.handlers.playing();
 const health=()=>h.timers.filter(t=>!t.cancelled&&t.delay===1500).at(-1).fn();
 let plays=0;const play=h.audio.play;h.audio.play=()=>{plays++;return play();};
 await new Promise(resolve=>setTimeout(resolve,8150));health();assert.equal(h.player.metrics().media_state,'stalled');
 h.audio.currentTime=13;health();assert.equal(h.player.metrics().media_state,'playing');assert.equal(h.get('playerStatus').textContent,'Playing');assert.equal(plays,0);
});

test('a gesture-blocked native play shows Play in shell and reader controls, not stale Pause intent',async()=>{
 const h=harness(),states=[],play=h.audio.play;
 h.get('workspace').contentWindow={ReaderNarration:{bookId:'A',playback:state=>states.push(state),update(){}}};
 h.audio.play=()=>Promise.reject(Object.assign(new Error('Gesture required'),{name:'NotAllowedError'}));
 await h.player.listen('A',1);await settle();assert.equal(h.audio.paused,true);assert.equal(h.get('playPause').textContent,'▶');assert.equal(h.get('miniPlay').textContent,'▶');assert.equal(states.at(-1).control_pause,false);
 h.audio.play=play;await h.get('playPause').onclick();await settle();assert.equal(h.audio.paused,false);assert.equal(h.get('playPause').textContent,'Ⅱ');assert.equal(states.at(-1).control_pause,true);
});

test('every frozen-clock checkpoint matches the actual Flask state contract while local diagnostics retain stalled',async()=>{
 let now=Date.now();class Clock extends Date{static now(){return now;}}
 const h=harness({Date:Clock});await h.player.listen('A',1);await settle();h.audio.currentTime=12;h.audio.handlers.playing();await settle();
 now+=9000;h.timers.filter(t=>!t.cancelled&&t.delay===1500).at(-1).fn();await settle();
 assert.equal(h.player.metrics().media_state,'stalled');assert.equal(h.audio.currentTime,12);
 const states=[...fs.readFileSync('platform_api.py','utf8').match(/if state not in \(([^\n]+)\):/)[1].matchAll(/'([^']+)'/g)].map(m=>m[1]);
 assert(!states.includes('stalled'),'Do not weaken the existing server validator to accommodate a UI-only state');
 assert(h.saves.length);for(const point of h.saves)assert(states.includes(point.playback_state),'Unsupported wire checkpoint state: '+point.playback_state);
 assert.equal(h.saves.at(-1).playback_state,'buffering');assert.equal(h.saves.at(-1).seconds,12);
 assert.equal(JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A')).state,'stalled');
});

test('foreground intent survives a delayed automatic chapter load, but newer explicit intent cancels it',async()=>{
 for(const action of ['none','manual','follow-off','pause']){
  let blocked=false,release;const gate=new Promise(resolve=>release=resolve);
  const h=harness({beforeListening:async options=>{if(blocked&&options.method!=='PUT')await gate;}});
  await h.player.listen('A',1);await settle();h.player.setFollow('A',true,true,null,'manual-navigation');
  h.document.hidden=true;h.documentEvents.visibilitychange();blocked=true;const transition=h.emitEnded();await settle();
  assert.equal(h.audio.paused,true);h.document.hidden=false;h.documentEvents.visibilitychange();await settle();
  if(action==='manual')h.player.setFollow('A',true,true,null,'manual-navigation');
  if(action==='follow-off')h.player.setFollow('A',false,false,null,'follow-toggle');
  if(action==='pause')h.actions.pause();
  release();await transition;await settle();if(action==='pause')await h.get('playPause').onclick();h.audio.handlers.playing();await settle();
  assert.equal(h.player.metrics().section_id,2);assert.equal((await h.player.readerResume('A')).following,action==='none',action);
 }
});

for(const mode of ['playing','paused','buffering'])test(`X stops ${mode} media, preserves checkpoint and fences stale work until explicit Listen`,async()=>{
 const h=harness(mode==='buffering'?{audioStatus:()=>({status:'streaming',asset_id:'stream',complete:false,worker_active:true,segments:[{index:0,duration:10,url:'/segment/0'}]})}:{});
 await h.player.listen('A',1);await settle();h.audio.currentTime=4;
 if(mode==='paused')h.actions.pause();
 if(mode==='buffering'){h.audio.currentTime=10;h.audio.duration=10;await h.emitEnded();await settle();}
 const ended=h.rawEnded(),position=h.player.metrics().seconds;
 h.get('closePlayer').click();h.get('closePlayer').click();await settle();
 assert.equal(h.audio.paused,true);assert.equal(h.player.metrics().want_play,false);assert.equal(h.player.metrics().engaged,false);assert.equal(h.get('playerPanel').hidden,true);
 const stopped=JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A'));
 assert.equal(stopped.intent,'stop');assert.equal(stopped.seconds,position);assert.equal(stopped.engaged,false);
 await ended?.();await h.actions.play();h.lifecycle.pageshow();h.documentEvents.visibilitychange();h.get('workspace').listeners.load?.forEach?.(f=>f());await settle();
 assert.equal(h.audio.paused,true);assert.equal(h.player.metrics().want_play,false);assert.equal(h.get('playerPanel').hidden,true);
 assert.equal(JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A')).seconds,position);
 await h.player.listen('A');await settle();assert.equal(h.player.metrics().want_play,true);assert.equal(h.player.metrics().engaged,true);if(mode!=='buffering')assert.equal(h.audio.paused,false);
});
test('X cancels a pending chapter open before it can autoplay or replace checkpoint',async()=>{
 let release;const h=harness({waitBook:new Promise(resolve=>release=resolve)});
 const opening=h.player.listen('A',1);await settle();h.get('closePlayer').click();release();await opening;await settle();
 assert.equal(h.audio.paused,true);assert.equal(h.player.metrics().want_play,false);assert.equal(h.player.metrics().engaged,false);assert.equal(h.get('playerPanel').hidden,true);
});
test('X saves locally immediately during a server outage and online retries its final ordered stop',async()=>{
 let offline=false;const h=harness({beforeListening:options=>{if(offline&&options.method==='PUT')throw Error('Offline');}});
 await h.player.listen('A',1);await settle();h.audio.currentTime=19;offline=true;h.get('closePlayer').click();await settle();
 assert.equal(h.audio.paused,true);assert.equal(JSON.parse(h.storage.get('webnovelPlaybackCheckpoint:A')).seconds,19);
 offline=false;h.lifecycle.online();await settle();assert.equal(h.progress.get('A').seconds,19);assert.equal(h.progress.get('A').checkpoint_intent,'stop');assert.equal(h.audio.paused,true);
});
test('a cold page or stale launch URL cannot reopen a stopped listening session',async()=>{
 const h=harness();await h.player.listen('A',1);await settle();h.audio.currentTime=23;h.get('closePlayer').click();await settle();
 for(const search of ['', '?listen=A&section=1']){const cold=harness({storageMap:h.storage,search});await settle();assert.equal(cold.audio.paused,true);assert(!cold.calls.some(([p])=>p==='/api/books/A'));}
});
