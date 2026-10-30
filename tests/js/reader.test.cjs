const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

class Element {
  constructor() {this.value = ''; this.hidden = true; this.dataset = {}; this.children = []; this.handlers = {}; this.attributes = {};this.style={};this.scrollTop=0;this.clientHeight=400;}
  addEventListener(name, fn) {this.handlers[name] = fn;}
  setAttribute(name, value) {this.attributes[name] = value;}
  append(...children) {this.children.push(...children);}
  replaceChildren() {this.children = [];}
  querySelector() {return new Element();}
  focus() {}
}
function harness(config={}) {
  const elements = new Map();
  const get = id => {if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id);};
  const scroller = {scrollHeight: 2000, clientHeight: 1000, scrollTop: 0};
  const doc = {readyState:'complete',body:{dataset:{}},scrollingElement: scroller, images: [], handlers: {}, getElementById: id => id === 'reader-style' ? get('iframeStyle') : null,
    addEventListener(name, fn) {this.handlers[name] = fn;}};
  if(config.pagination){doc.documentElement={clientWidth:375,clientHeight:1000};scroller.scrollWidth=375*4-24;scroller.scrollLeft=0;}
  const frame = get('readingFrame'); frame.contentDocument = doc;
  Object.defineProperty(frame, 'srcdoc', {set(value) {this.documentSource = value;const token=/data-section-token="(\d+)"/.exec(value)?.[1];doc.body.dataset.sectionId=/data-section-id="(\d+)"/.exec(value)?.[1];if(config.staleFrameEvent){queueMicrotask(()=>{doc.body.dataset.sectionToken='stale';this.onload();queueMicrotask(()=>{doc.body.dataset.sectionToken=token;this.onload();});});}else{doc.body.dataset.sectionToken=token;if(!config.deferFrameLoad)queueMicrotask(() => this.onload());}}});
  const calls = [], saved = [];
  let revision = config.progress?.version||0, failNextSection = false;
  const storage=new Map(Object.entries(config.storage||{}));
  const section = id => ({id, position: id - 1, title: `Section ${id}`, content_html: '<p>Safe reading text</p>', previous_id: id === 1 ? null : id - 1, next_id: id === 3 ? null : id + 1});
  const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
  const document = {body: {dataset: {bookId: 'book'}}, hidden: false, getElementById: get, createElement: () => new Element(), addEventListener() {}};
  const intervals = [],followUpdates=[],highlighted=[],cleared=[],frames=[];let clock=0;
  const readerWindow={addEventListener(){},parent:{Player:{setFollow:(...args)=>followUpdates.push(args),narration:()=>config.narration||null}},NarrationMarkup:{clear:doc=>cleared.push(doc),highlight:(document,location)=>{highlighted.push(location);return {scrollIntoView(){scroller.scrollTop=220;}};}}};
  Object.assign(readerWindow.parent.Player,config.player);
  if(config.pagination)readerWindow.ReaderPagination=config.pagination;
  readerWindow.matchMedia=()=>({matches:!!config.reducedMotion});
  const context = vm.createContext({document, window:readerWindow,...(config.search?{location:{search:config.search},URLSearchParams}:{}),...(config.animation?{performance:{now:()=>clock}}:{}),requestAnimationFrame: fn => config.delayedFrames||config.animation?frames.push(fn):fn(),
    setTimeout: (fn,delay) => {config.timers?.push(fn);config.timerDelays?.push(delay);return 1;}, clearTimeout() {}, setInterval: fn => intervals.push(fn), Promise, encodeURIComponent,
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},fetch: async (url, options = {}) => {
      calls.push([url, options.method || 'GET']);
      if(url.startsWith('/api/reader/appearance'))return response({theme:config.globalTheme||'dark',inherit:!config.progress?.preferences?.theme});
      if (url === '/api/books/book') {if(config.waitBook)await config.waitBook;return response({section_count: 3, first_section_id: 1, source_kind: 'epub', progress: {version: 0, section_id: 1, fraction: .43, anchor: '', preferences: {},...config.progress}});}
      if (options.method === 'PUT') {
        if(config.waitProgress)await config.waitProgress;
        const data = JSON.parse(options.body);
        if (data.version !== revision) return response({error: 'Progress conflict'}, 409);
        saved.push(data); return response({version: ++revision});
      }
      if (/\/sections\/\d+$/.test(url) && failNextSection) {failNextSection = false; return response({error:'Temporary chapter failure'}, 503);}
      if (/\/sections\/\d+$/.test(url)) {const id=Number(url.split('/').pop());if(config.waitSections?.[id])await config.waitSections[id];return response(section(id));}
      if (url.includes('/navigation?')&&config.fallbackContents)return response({total:2000,offset:0,reading_order:true,items:Array.from({length:100},(_,i)=>({id:i+1,position:i,title:'Chapter '+(i+1)}))});
      if (url.includes('/sections?')) return response({total: 3, items: [1, 2, 3].map(id => ({id, position: id - 1, title: `Section ${id}`}))});
      if (url.includes('/resolve?')) {if(config.waitResolve)await config.waitResolve;return response({id: 2, anchor: 'note'});}
      throw new Error(`Unexpected URL: ${url}`);
    }});
  vm.runInContext(fs.readFileSync('static/reader.js', 'utf8'), context);
  return {resize:()=>vm.runInContext('viewportChanged()',context),get, scroller, doc, frame, saved,reader:readerWindow,open:id=>vm.runInContext('openSection('+id+')',context),navigate:href=>vm.runInContext('navigateTarget('+JSON.stringify(href)+')',context),followUpdates,highlighted,cleared, calls, intervals, runFrame(time){clock=time;for(const fn of frames.splice(0))fn(time);},flushFrames(){while(frames.length)frames.shift()();},changeRemoteRevision() {revision += 1;}, failNextChapter() {failNextSection = true;}};
}
async function settle() {for (let i = 0; i < 100; i++) await Promise.resolve();}
test('Listen after manual browsing starts the visible chapter while the audiobook is already active',async()=>{
 const commands=[],h=harness({player:{toggleReader:async(...args)=>commands.push(['start',...args]),stopReader:()=>commands.push(['stop'])}});await settle();
 h.reader.NarrationMarkup.position=()=>({anchor:'text:120',normalized_offset:120});
 h.reader.ReaderNarration.playback({book_id:'book',engaged:true,intent:true,playing:true,following:true});
 h.get('nextSection').onclick();await settle();
 await h.get('dockListen').onclick();
 assert.equal(commands[0]?.[0],'start');assert.equal(commands[0][2].section_id,2);assert.equal(commands[0][2].normalized_offset,120);
});
test('Listen maps the currently visible anchor instead of a stale restored passage',async()=>{
 let visible='text:10';const p=require('../../static/pagination.js'),h=harness({pagination:{...p,anchor:()=>visible,restore(){}}});await settle();
 h.reader.NarrationMarkup.position=(_doc,anchor)=>({anchor,normalized_offset:Number(anchor.split(':')[1])});
 visible='text:300';h.scroller.scrollLeft=375;
 assert.equal(h.reader.ReaderNarration.position().normalized_offset,300);
});
test('an old audiobook callback cannot pull the reader away while Listen from here is preparing',async()=>{
 let release;const gate=new Promise(resolve=>release=resolve),h=harness({player:{startReader:()=>gate}});await settle();
 h.reader.NarrationMarkup.position=()=>({anchor:'text:120',normalized_offset:120});
 h.reader.ReaderNarration.playback({book_id:'book',engaged:true,intent:true,playing:true,following:true});
 h.get('nextSection').onclick();await settle();
 const starting=h.get('dockListen').onclick();await settle();
 await h.reader.ReaderNarration.update({book_id:'book',section_id:1,anchor:'narration:old:1',quote:'Old audio',fraction:0});await settle();
 assert.equal(h.reader.ReaderNarration.page().section_id,2);
 release();await starting;
});
test('a temporary chunk fallback cannot rewind an established word location, but intentional seek can',async()=>{
 const h=harness();await settle();h.reader.ReaderNarration.playback({book_id:'book',engaged:true,playing:true,following:true});
 const word={book_id:'book',section_id:1,segment_index:0,anchor:'narration:asset:0:word:3',quote:'Safe reading text',precision:'word',fraction:.7};await h.reader.ReaderNarration.update(word,true);const count=h.highlighted.length;
 // The chunk metadata says word precision when cues exist, even before a cue is selected.
 const chunk={...word,anchor:'narration:asset:0',precision:'word',fraction:0};await h.reader.ReaderNarration.update(chunk);assert.equal(h.highlighted.length,count);
 h.reader.ReaderNarration.seek();await h.reader.ReaderNarration.update(chunk,true);assert.equal(h.highlighted.length,count+1);
});
test('a word arriving before a queued font restore still invalidates the font geometry exactly once',async()=>{
 let layouts=0;const p=require('../../static/pagination.js'),h=harness({delayedFrames:true,pagination:{...p,anchor:()=> 'text:100',restore(){},reveal(){},layout(){layouts++;}}});await settle();h.flushFrames();
 h.reader.ReaderNarration.playback({book_id:'book',engaged:true,playing:true,following:true});await h.reader.ReaderNarration.update({book_id:'book',section_id:1,anchor:'live:1',quote:'Safe reading text'},true);const count=layouts;
 h.get('fontSize').value=32;h.get('fontSize').handlers.change();await h.reader.ReaderNarration.update({book_id:'book',section_id:1,anchor:'live:2',quote:'Safe reading text'});h.flushFrames();await settle();assert.equal(layouts,count+1);
});

test('a following reflow never restores an obsolete manual anchor before the live narration anchor',async()=>{
 const timers=[],restored=[],actual=require('../../static/pagination.js'),h=harness({timers,pagination:{...actual,anchor:()=> 'text:100',restore(doc,fraction,anchor){restored.push(anchor);},reveal(){}}});await settle();
 h.frame.clientWidth=375;h.frame.clientHeight=500;h.reader.ReaderNarration.playback({book_id:'book',engaged:true,playing:true,following:true});await h.reader.ReaderNarration.update({book_id:'book',section_id:1,anchor:'live:1',quote:'Safe reading text',fraction:0},true);await h.reader.ReaderNarration.follow();const count=restored.length;h.resize();timers.at(-1)();await settle();assert.equal(restored.length,count);
});
test('unchanged valid reader geometry is not measured again on a settings restore',async()=>{
 let layouts=0;const p=require('../../static/pagination.js'),h=harness({pagination:{...p,anchor:()=> 'text:100',restore(){},layout(){layouts++;}}});await settle();const first=layouts;
 h.get('fontSize').value=20;h.get('fontSize').handlers.change();await settle();assert.equal(layouts,first);h.get('fontSize').value=32;h.get('fontSize').handlers.change();await settle();assert.equal(layouts,first+1);
});
test('UI-only reflow does not publish a new reading fraction for the same logical passage',async()=>{
 const timers=[],p=require('../../static/pagination.js'),h=harness({timers,pagination:{...p,anchor:()=> 'text:100',restore(doc){doc.scrollingElement.scrollLeft=375;}}});await settle();const count=h.saved.length;
 h.frame.clientWidth=375;h.frame.clientHeight=500;h.doc.documentElement.clientHeight=500;h.scroller.scrollWidth=1875;h.resize();timers.at(-1)();await settle();h.intervals[0]();await settle();assert.equal(h.saved.length,count);
});
function readerTouch(h,name,x,y){h.doc.handlers[name]({isTrusted:true,touches:name==='touchend'?[]:[{clientX:x,clientY:y}],changedTouches:[{clientX:x,clientY:y}],target:{closest:()=>null},preventDefault(){}});}
test('vertical up reveals and vertical down hides chrome without manual page navigation',async()=>{
 const p=require('../../static/pagination.js'),h=harness({pagination:{...p,anchor:()=> 'text:100',restore(){}}});await settle();h.frame.clientWidth=375;h.frame.clientHeight=1000;h.get('hideChrome').onclick();const before=h.reader.ReaderNarration.page().manual_revision;
 readerTouch(h,'touchstart',180,790);readerTouch(h,'touchmove',183,700);readerTouch(h,'touchend',183,700);assert.equal(h.get('revealChrome').hidden,true);
 readerTouch(h,'touchstart',180,700);readerTouch(h,'touchmove',183,790);readerTouch(h,'touchend',183,790);assert.equal(h.get('revealChrome').hidden,false);assert.equal(h.reader.ReaderNarration.page().manual_revision,before);
});

test('control overlay toggles and mini-player visibility never schedule reader reflow',async()=>{
 const timerDelays=[],h=harness({timerDelays});await settle();const start=timerDelays.length;
 h.get('hideChrome').onclick();h.get('revealChrome').onclick();h.reader.ReaderNarration.playback({book_id:'book',engaged:true,playing:true,intent:true});h.get('readerMiniClose').onclick();h.reader.ReaderNarration.clear();
 assert.equal(timerDelays.slice(start).filter(delay=>delay===80).length,0);assert.equal(h.reader.ReaderNarration.page().manual_revision,0);
});

test('the accessible reveal button remains outside the hidden bottom overlay group',()=>{
 const html=fs.readFileSync('templates/reader.html','utf8'),reveal=html.indexOf('id="revealChrome"'),overlay=html.indexOf('class="reader-bottom-overlay"');
 assert(reveal>=0&&overlay>reveal);assert(html.indexOf('class="reader-audio-slot"')>overlay);assert(html.indexOf('<footer class="reader-footer">')>overlay);
});
test('12px initial horizontal drift does not lock a predominantly vertical gesture or suspend follow',async()=>{
 const p=require('../../static/pagination.js'),h=harness({pagination:{...p,anchor:()=> 'text:100',restore(){}}});await settle();h.frame.clientWidth=375;h.frame.clientHeight=1000;const before=h.reader.ReaderNarration.page().manual_revision;
 readerTouch(h,'touchstart',180,700);readerTouch(h,'touchmove',192,700);assert.equal(h.reader.ReaderNarration.page().dragging,false);readerTouch(h,'touchmove',192,785);readerTouch(h,'touchend',192,785);assert.equal(h.reader.ReaderNarration.page().manual_revision,before);assert.equal(h.scroller.scrollLeft,0);
});
test('vertical movement outside the toolbar band suppresses compatibility page taps',async()=>{
 const p=require('../../static/pagination.js'),h=harness({pagination:{...p,anchor:()=> 'text:100',restore(){}}});await settle();h.frame.clientWidth=375;h.frame.clientHeight=1000;
 readerTouch(h,'touchstart',340,100);readerTouch(h,'touchmove',344,185);readerTouch(h,'touchend',344,185);let suppressed=false;h.doc.handlers.click({isTrusted:true,clientX:344,target:{closest:()=>null},preventDefault(){suppressed=true;},stopImmediatePropagation(){}});assert.equal(suppressed,true);assert.equal(h.scroller.scrollLeft,0);
});

test('viewport observation ignores unchanged dimensions instead of continuously repaginating',async()=>{
 const timers=[],h=harness({timers});await settle();h.frame.clientWidth=375;h.frame.clientHeight=600;
 const before=timers.length;h.resize();assert.equal(timers.length,before+1);h.resize();h.resize();assert.equal(timers.length,before+1);
 h.frame.clientHeight=500;h.resize();assert.equal(timers.length,before+2);
});
test('a control-height reflow retains the logical text anchor rather than the old page number',async()=>{
 const timers=[],restored=[];let visible='text:386';const p=require('../../static/pagination.js');
 const h=harness({timers,pagination:{...p,anchor:()=>visible,restore(doc,fraction,anchor){restored.push(anchor);}}});await settle();
 h.frame.clientWidth=375;h.frame.clientHeight=500;h.doc.documentElement.clientHeight=500;h.scroller.scrollLeft=0;visible='text:0';h.resize();timers.at(-1)();await settle();assert.equal(restored.at(-1),'text:386');
});

test('reader state tolerates a temporarily missing iframe scrolling element',async()=>{
 const h=harness();await settle();h.doc.scrollingElement=null;assert.doesNotThrow(()=>h.reader.ReaderNarration.page());assert.equal(h.reader.ReaderNarration.page().fraction,0);
});

test('superseding an iframe load settles the obsolete navigation promise',async()=>{
 const config={},h=harness(config);await settle();config.deferFrameLoad=true;
 let obsoleteSettled=false;h.open(2).then(()=>obsoleteSettled=true);await settle();
 const latest=h.open(3);await settle();h.frame.onload();await latest;await settle();assert.equal(h.reader.ReaderNarration.page().section_id,3);assert.equal(obsoleteSettled,true);
});

test('live narration waits for reader bootstrap instead of racing its initial iframe load',async()=>{
 let release;const live={book_id:'book',section_id:3,anchor:'narration:live:0',quote:'Safe reading text',fraction:0};
 const h=harness({search:'?narration=1',waitBook:new Promise(r=>release=r),player:{readerResume:async()=>({location:live,following:true})}});
 h.reader.ReaderNarration.playback({book_id:'book',engaged:true,playing:true,following:true,follow_suspended:false});
 h.reader.ReaderNarration.update(live);await settle();assert.equal(h.calls.filter(([url])=>/sections\/\d+$/.test(url)).length,0);
 release();await settle();assert.equal(h.reader.ReaderNarration.page().section_id,3);assert.equal(h.highlighted.at(-1).anchor,live.anchor);
});

test('a blocked reading progress request cannot block returning to live narration',async()=>{
 let release;const config={},h=harness(config);await settle();
 config.waitProgress=new Promise(r=>release=r);h.scroller.scrollTop=600;h.doc.handlers.scroll();h.intervals[0]();await settle();
 config.narration={book_id:'book',section_id:3,anchor:'narration:live:0',quote:'Safe reading text',fraction:0};
 h.reader.ReaderNarration.follow();await settle();
 assert.equal(h.reader.ReaderNarration.page().section_id,3);release();await settle();
});

test('an independent reading save conflict cannot block the authoritative live narration view',async()=>{
 const config={},h=harness(config);await settle();h.changeRemoteRevision();h.scroller.scrollTop=700;h.intervals[0]();await settle();
 config.narration={book_id:'book',section_id:3,anchor:'narration:live:0',quote:'Safe reading text',fraction:0};
 h.reader.ReaderNarration.playback({book_id:'book',engaged:true,playing:true});await h.reader.ReaderNarration.follow();await settle();
 assert.equal(h.reader.ReaderNarration.page().section_id,3);assert.equal(h.highlighted.at(-1).anchor,config.narration.anchor);
});

test('return remains visible while the requested chapter is still loading',async()=>{
 let release;const config={waitSections:{3:new Promise(r=>release=r)}},h=harness(config);await settle();
 config.narration={book_id:'book',section_id:3,anchor:'narration:live:0',quote:'Safe reading text',fraction:0};
 h.reader.ReaderNarration.playback({book_id:'book',engaged:true,playing:false});
 const returned=h.reader.ReaderNarration.follow();await settle();assert.equal(h.get('returnNarration').hidden,false);
 release();await returned;await settle();assert.equal(h.get('returnNarration').hidden,true);
});

test('a pending paused return survives unavailable location until the actual anchor arrives',async()=>{
 const h=harness({player:{resolveNarration:async()=>null}});await settle();h.reader.ReaderNarration.playback({book_id:'book',engaged:true,playing:false});
 await h.reader.ReaderNarration.follow();assert.equal(h.get('returnNarration').hidden,false);
 await h.reader.ReaderNarration.update({book_id:'book',section_id:3,anchor:'narration:live:0',quote:'Safe reading text',fraction:0});await settle();
 assert.equal(h.reader.ReaderNarration.page().section_id,3);assert.equal(h.get('returnNarration').hidden,true);assert.equal(h.get('dockListen').dataset.playing,'false');
});

test('headphones OFF cancels an unresolved return intent and keeps its control hidden',async()=>{
 const h=harness({player:{resolveNarration:async()=>null}});await settle();h.reader.ReaderNarration.playback({book_id:'book',engaged:true,playing:false});
 await h.reader.ReaderNarration.follow();assert.equal(h.get('returnNarration').hidden,false);
 h.reader.ReaderNarration.playback({book_id:'book',engaged:false,playing:false});h.get('fontSize').value=24;h.get('fontSize').handlers.change();await settle();assert.equal(h.get('returnNarration').hidden,true);
});

test('synthetic content gestures cannot suspend active narration following',async()=>{
 const h=harness();await settle();h.get('followNarration').onclick();await settle();const before=h.followUpdates.length;
 h.doc.handlers.wheel({isTrusted:false});h.doc.handlers.pointerdown?.({isTrusted:false,target:h.doc.documentElement});
 assert.equal(h.followUpdates.length,before);h.reader.ReaderNarration.playback({book_id:'book',playing:true,engaged:true});
 await h.reader.ReaderNarration.update({book_id:'book',section_id:3,anchor:'narration:live:0',quote:'Safe reading text',fraction:0});await h.reader.ReaderNarration.follow();await settle();assert.equal(h.reader.ReaderNarration.page().section_id,3);
});

test('following reader remount opens the verified live chapter first without an old manual flash',async()=>{
 const live={book_id:'book',section_id:3,anchor:'narration:live:1',quote:'Safe reading text',fraction:.6};
 const h=harness({search:'?narration=1',player:{readerResume:async()=>({location:live,following:true})}});await settle();
 assert.equal(h.reader.ReaderNarration.page().section_id,3);assert.deepEqual(h.calls.filter(([url])=>/sections\/\d+$/.test(url)).map(([url])=>url),['/api/books/book/sections/3']);
 assert.equal(h.highlighted.at(-1).anchor,live.anchor);assert.equal(h.saved.at(-1).section_id,1);
});

test('paused manually-ahead remount retains manual chapter until explicit narration return',async()=>{
 const live={book_id:'book',section_id:2,anchor:'narration:live:1',quote:'Safe reading text',fraction:.6};
 const h=harness({progress:{section_id:3},player:{readerResume:async()=>({location:live,following:false}),resolveNarration:async()=>live}});await settle();
 assert.equal(h.reader.ReaderNarration.page().section_id,3);await h.reader.ReaderNarration.follow();assert.equal(h.reader.ReaderNarration.page().section_id,2);assert.equal(h.saved.at(-1).section_id,3);
});

test('explicit narration route reconciles active chapter instead of an obsolete route section',async()=>{
 const live={book_id:'book',section_id:3,anchor:'narration:live:1',quote:'Safe reading text',fraction:.6};
 const h=harness({search:'?section=2&narration=1',player:{readerResume:async()=>({location:live,following:false})}});await settle();
 assert.equal(h.reader.ReaderNarration.page().section_id,3);assert(!h.calls.some(([url])=>url.endsWith('/sections/2')));
});

test('explicit return selects the live player anchor, not an older iframe narration snapshot',async()=>{
 const config={},h=harness(config);await settle();
 h.reader.ReaderNarration.playback({book_id:'book',playing:false,state:'paused',engaged:true});
 await h.reader.ReaderNarration.update({book_id:'book',section_id:2,anchor:'narration:old:0',quote:'Safe reading text',fraction:0});
 config.narration={book_id:'book',section_id:3,anchor:'narration:current:2',quote:'Safe reading text',fraction:.5};
 await h.reader.ReaderNarration.follow();await settle();
 assert.equal(h.get('sectionTitle').textContent,'Section 3');
 assert.equal(h.highlighted.at(-1).anchor,'narration:current:2');
 assert.equal(h.saved.at(-1).section_id,1,'Returning to audio preserves the independent manual location');
});

test('a newer narration chapter arriving during an older load is reconciled rather than dropped',async()=>{
 let release;const gate=new Promise(r=>release=r),h=harness({waitSections:{2:gate}});await settle();
 h.reader.ReaderNarration.playback({book_id:'book',playing:true,engaged:true});
 h.get('followNarration').onclick();await settle();
 const old=h.reader.ReaderNarration.update({book_id:'book',section_id:2,anchor:'narration:old:0',quote:'Safe reading text',fraction:0},true);
 await settle();
 await h.reader.ReaderNarration.update({book_id:'book',section_id:3,anchor:'narration:latest:1',quote:'Safe reading text',fraction:.3});
 release();await old;await settle();
 assert.equal(h.get('sectionTitle').textContent,'Section 3');
 assert.equal(h.highlighted.at(-1).anchor,'narration:latest:1');
});

test('headphones stop listening and hide controls while mini pause retains resumable controls',async()=>{
 const h=harness();await settle();let playing=false,seconds=23,starts=0;
 h.reader.NarrationMarkup.position=()=>({anchor:'text:0',normalized_offset:0});
 const publish=()=>h.reader.ReaderNarration.playback({book_id:'book',playing,intent:playing,engaged:starts>0,state:playing?'playing':'paused'});
 h.reader.parent.Player.playing=()=>playing;
 h.reader.parent.Player.toggleReader=async()=>{starts++;playing=!playing;publish();};
 h.reader.parent.Player.togglePlayback=()=>{playing=!playing;publish();};h.reader.parent.Player.stopReader=()=>{playing=false;starts=0;publish();};
 assert.equal(h.get('readerMini').hidden,true);
 await h.get('dockListen').onclick();assert.equal(playing,true);assert.equal(h.get('readerMini').hidden,false);
 h.get('readerMiniPlay').onclick();assert.equal(playing,false);assert.equal(h.get('readerMini').hidden,false);
 await h.get('dockListen').onclick();assert.equal(playing,false);assert.equal(h.get('readerMini').hidden,true);
 await h.get('dockListen').onclick();assert.equal(playing,true);assert.equal(h.get('readerMini').hidden,false);
 const before=h.get('sectionTitle').textContent;h.get('readerMiniClose').onclick();assert.equal(playing,false);assert.equal(h.get('sectionTitle').textContent,before);assert.equal(h.get('readerMini').hidden,true);
 publish();assert.equal(h.get('readerMini').hidden,true);assert.equal(h.get('dockListen').dataset.playing,'false');assert.equal(seconds,23);
});

test('manual vertical scrolling suspends narration follow without stealing native scroll',async()=>{
 const h=harness();await settle();h.get('mode').value='scroll';h.get('mode').handlers.change();h.reader.ReaderNarration.playback({book_id:'book',playing:true});h.get('followNarration').onclick();await settle();let prevented=false;
 h.doc.handlers.touchstart({touches:[{clientX:100,clientY:300}]});h.doc.handlers.touchmove({touches:[{clientX:100,clientY:100}],preventDefault(){prevented=true;}});await settle();assert.equal(prevented,false);assert.equal(h.followUpdates.at(-1)[2],true);
});

test('a delayed publication-link resolution cannot replace a newer chapter selection',async()=>{
 let release;const h=harness({waitResolve:new Promise(r=>release=r)});await settle();h.navigate('chapter2.xhtml');await settle();await h.open(3);release();await settle();assert.equal(h.reader.ReaderNarration.page().section_id,3);assert.equal(h.reader.ReaderNarration.page().ready,true);
});

test('a second finger cancels an in-flight page drag without leaving navigation locked',async()=>{
 const p=require('../../static/pagination.js'),h=harness({pagination:{...p,anchor:()=> 'text:100',restore(){}}});await settle();h.scroller.scrollLeft=0;
 h.doc.handlers.touchstart({touches:[{clientX:300,clientY:100}]});h.doc.handlers.touchmove({touches:[{clientX:200,clientY:100}],preventDefault(){}});assert.equal(h.reader.ReaderNarration.page().dragging,true);
 h.doc.handlers.touchstart({touches:[{clientX:200,clientY:100},{clientX:250,clientY:100}]});assert.equal(h.reader.ReaderNarration.page().dragging,false);assert.equal(h.scroller.scrollLeft,0);h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});assert.equal(h.scroller.scrollLeft,375);
});

test('three out-of-order chapter requests cannot change the newest document or readiness',async()=>{
 let second,third;const h=harness({waitSections:{2:new Promise(r=>second=r),3:new Promise(r=>third=r)}});await settle();
 h.open(2);await settle();h.open(3);await settle();await h.open(1);await settle();second();third();await settle();
 assert.equal(h.reader.ReaderNarration.page().ready,true);assert.equal(h.reader.ReaderNarration.page().section_id,1);assert.equal(h.doc.body.dataset.sectionId,'1');
});

test('reload accepts the latest local passage when the server committed only our older in-flight save',async()=>{
 const p=require('../../static/pagination.js'),checkpoint={server_version:3,saved_at:Date.now()+1000,section_id:1,fraction:.6,anchor:'text:750',pending_write:{version:4,section_id:1,anchor:'text:375',fraction:.3}};
 const h=harness({progress:{version:4,section_id:1,fraction:.3,anchor:'text:375'},storage:{'webnovelReadingCheckpoint:book':JSON.stringify(checkpoint)},pagination:{...p,anchor:doc=>'text:'+doc.scrollingElement.scrollLeft,restore(doc,fraction,anchor){doc.scrollingElement.scrollLeft=Number(anchor.split(':')[1])||0;}}});
 await settle();assert.equal(h.scroller.scrollLeft,750);assert.equal(h.saved.at(-1).anchor,'text:750');
});
test('unrelated newer server progress is not replaced by a stale local pending-write checkpoint',async()=>{
 const p=require('../../static/pagination.js'),checkpoint={server_version:3,saved_at:Date.now()+1000,section_id:1,fraction:.6,anchor:'text:750',pending_write:{version:4,section_id:1,anchor:'text:0',fraction:0}};
 const h=harness({progress:{version:4,section_id:1,fraction:.3,anchor:'text:375'},storage:{'webnovelReadingCheckpoint:book':JSON.stringify(checkpoint)},pagination:{...p,anchor:doc=>'text:'+doc.scrollingElement.scrollLeft,restore(doc,fraction,anchor){doc.scrollingElement.scrollLeft=Number(anchor.split(':')[1])||0;}}});await settle();assert.equal(h.scroller.scrollLeft,375);
});

test('a delayed typography restore cannot rewind a newer intentional page turn',async()=>{
 const p=require('../../static/pagination.js');
 const h=harness({delayedFrames:true,pagination:{...p,anchor:doc=>'text:'+doc.scrollingElement.scrollLeft,restore(doc,fraction,anchor){doc.scrollingElement.scrollLeft=Number(anchor.split(':')[1])||0;}}});
 await settle();h.flushFrames();h.scroller.scrollLeft=375;h.doc.handlers.scroll();
 h.get('fontSize').value='24';h.get('fontSize').handlers.change();
 h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});
 assert.equal(h.scroller.scrollLeft,750);h.flushFrames();
 assert.equal(h.scroller.scrollLeft,750);
});

test('opening and hiding chrome cannot rewind navigation queued after its layout callback',async()=>{
 const p=require('../../static/pagination.js');
 const h=harness({delayedFrames:true,pagination:{...p,anchor:doc=>'text:'+doc.scrollingElement.scrollLeft,restore(doc,fraction,anchor){doc.scrollingElement.scrollLeft=Number(anchor.split(':')[1])||0;}}});
 await settle();h.flushFrames();h.scroller.scrollLeft=375;h.doc.handlers.scroll();
 h.get('hideChrome').onclick();h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});h.flushFrames();
 assert.equal(h.scroller.scrollLeft,750);
});

test('reader restores and saves server position in an isolated publication frame', async () => {
  const h = harness(); await settle();
  assert.equal(h.scroller.scrollTop, 430);
  assert.equal(h.saved[0].fraction, .43);
  assert.equal(h.saved[0].section_id, 1);
  assert.match(h.frame.documentSource, /script-src 'none'/);
  assert.match(h.frame.documentSource, /Safe reading text/);
  assert.equal(h.get('previousSection').disabled, true);
  assert.equal(h.get('nextSection').disabled, false);
  assert.equal(h.get('originalDownload').href, '/api/books/book/original');
});

test('chapter navigation saves old position and advances in book order', async () => {
  const h = harness(); await settle();
  h.scroller.scrollTop = 720;
  h.get('nextSection').onclick(); await settle();
  assert.equal(h.saved[1].section_id, 1);
  assert.equal(h.saved[1].fraction, .72);
  assert.equal(h.saved.at(-1).section_id, 2);
  assert.equal(h.saved.at(-1).fraction, 0);
  assert.equal(h.get('sectionTitle').textContent, 'Section 2');
  assert.equal(h.get('previousSection').disabled, false);
});

test('theme/font changes retain the locator and persist preferences', async () => {
  const h = harness(); await settle();
  h.scroller.scrollTop = 640;
  h.get('theme').value = 'sepia'; h.get('theme').handlers.change();
  h.get('fontSize').value = '24'; h.get('fontSize').handlers.change();
  h.intervals[0](); await settle();
  assert.equal(h.scroller.scrollTop, 640);
  assert.equal(h.saved.at(-1).fraction, .64);
  assert.equal(h.saved.at(-1).preferences.theme, 'sepia');
  assert.equal(h.saved.at(-1).preferences.fontSize, 24);
});

test('concurrent-reader conflict stops stale progress and dependent navigation', async () => {
  const h = harness(); await settle(); h.changeRemoteRevision(); h.scroller.scrollTop = 500;
  h.intervals[0](); await settle();
  assert.match(h.get('readingStatus').textContent, /Progress conflict/);
  h.get('nextSection').onclick(); await settle();
  assert.equal(h.calls.some(([url]) => url.endsWith('/sections/2')), false);
  assert.equal(h.saved.length, 1);
});


test('failed chapter request preserves current content and permits retry', async () => {
  const h = harness(); await settle();
  h.failNextChapter(); h.get('nextSection').onclick(); await settle();
  assert.equal(h.get('sectionTitle').textContent, 'Section 1');
  assert.match(h.get('readingStatus').textContent, /Temporary chapter failure/);
  assert.equal(h.get('nextSection').disabled, false);
  h.get('nextSection').onclick(); await settle();
  assert.equal(h.get('sectionTitle').textContent, 'Section 2');
});

test('idle reader does not repeatedly write unchanged progress', async () => {
  const h = harness(); await settle();
  for (let i=0; i<5; i++) {h.intervals[0](); await settle();}
  assert.equal(h.saved.length, 1);
});


test('page mode advances a viewport and preserves section until chapter boundary',async()=>{const h=harness();await settle();h.get('mode').value='pages';h.get('mode').handlers.change();h.scroller.scrollTop=0;h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});assert.equal(h.scroller.scrollTop,900);assert.equal(h.get('sectionTitle').textContent,'Section 1');h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});assert.equal(h.scroller.scrollTop,1000);h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});await settle();assert.equal(h.get('sectionTitle').textContent,'Section 2');});

test('additional typography preferences persist without moving reading location',async()=>{const h=harness();await settle();h.scroller.scrollTop=650;for(const [id,value] of [['margin','18'],['paragraphSpacing','1.4'],['alignment','justify']]){h.get(id).value=value;h.get(id).handlers.change();}h.intervals[0]();await settle();assert.equal(h.saved.at(-1).fraction,.65);assert.equal(h.saved.at(-1).preferences.margin,18);assert.equal(h.saved.at(-1).preferences.alignment,'justify');});

test('focus mode hides chrome and provides an explicit accessible way back',async()=>{const h=harness();await settle();h.get('hideChrome').onclick();assert.equal(h.get('revealChrome').hidden,false);h.get('revealChrome').onclick();assert.equal(h.get('revealChrome').hidden,true);});


test('a swipe suppresses the following synthetic tap instead of turning twice',async()=>{const h=harness();await settle();h.get('mode').value='pages';h.get('mode').handlers.change();h.scroller.scrollTop=0;h.doc.handlers.touchstart({touches:[{clientX:300,clientY:100}]});h.doc.handlers.touchend({changedTouches:[{clientX:100,clientY:105}]});assert.equal(h.scroller.scrollTop,900);let suppressed=false;h.doc.handlers.click({clientX:100,target:{closest:()=>null},preventDefault(){suppressed=true;},stopImmediatePropagation(){}});assert.equal(suppressed,true);assert.equal(h.scroller.scrollTop,900);});

function touchTap(h,x,target={closest:()=>null}){
 h.doc.handlers.touchstart({touches:[{clientX:x,clientY:100}]});
 h.doc.handlers.touchend({touches:[],changedTouches:[{clientX:x,clientY:100}],target,preventDefault(){}});
}
test('touchend turns right and left pages without requiring a compatibility click',async()=>{
 const h=harness();await settle();h.frame.clientWidth=375;h.scroller.scrollTop=0;
 touchTap(h,340);assert.equal(h.scroller.scrollTop,900);
 touchTap(h,35);assert.equal(h.scroller.scrollTop,0);
});
test('iframe whitespace gestures use local coordinates and handlers are replaced at chapter load',async()=>{
 const h=harness();await settle();h.frame.clientWidth=375;h.frame.getBoundingClientRect=()=>({left:100});h.scroller.scrollTop=0;
 h.frame.ontouchstart({touches:[{clientX:440,clientY:100}]});h.frame.ontouchend({touches:[],changedTouches:[{clientX:440,clientY:100}],target:h.frame});
 assert.equal(h.scroller.scrollTop,900);
 const old=h.frame.ontouchend;h.get('nextSection').onclick();await settle();assert.notEqual(h.frame.ontouchend,old);
 h.scroller.scrollTop=0;h.frame.ontouchstart({touches:[{clientX:440,clientY:100}]});h.frame.ontouchend({touches:[],changedTouches:[{clientX:240,clientY:100}],target:h.frame});
 assert.equal(h.scroller.scrollTop,900);
});
test('a handled touch tap cancels its compatibility click so it turns only once',async()=>{
 const h=harness();await settle();h.frame.clientWidth=375;h.scroller.scrollTop=0;
 touchTap(h,340);let prevented=false;
 h.doc.handlers.click({clientX:340,target:{closest:()=>null},preventDefault(){prevented=true;},stopImmediatePropagation(){}});
 assert(prevented);assert.equal(h.scroller.scrollTop,900);
});
test('touch taps preserve publication links and selected text',async()=>{
 const h=harness();await settle();h.frame.clientWidth=375;h.scroller.scrollTop=0;
 touchTap(h,340,{closest:()=>({tagName:'A'})});assert.equal(h.scroller.scrollTop,0);
 h.doc.getSelection=()=>({toString:()=> 'selected passage'});
 touchTap(h,340);assert.equal(h.scroller.scrollTop,0);
});
test('vertical drags, cancelled touches, and multiple fingers do not turn pages',async()=>{
 const h=harness();await settle();h.frame.clientWidth=375;h.scroller.scrollTop=0;
 const end=()=>h.doc.handlers.touchend({touches:[],changedTouches:[{clientX:340,clientY:100}],target:{closest:()=>null}});
 h.doc.handlers.touchstart({touches:[{clientX:340,clientY:200}]});end();assert.equal(h.scroller.scrollTop,0);
 h.doc.handlers.touchstart({touches:[{clientX:340,clientY:100}]});h.doc.handlers.touchcancel();end();assert.equal(h.scroller.scrollTop,0);
 h.doc.handlers.touchstart({touches:[{clientX:340,clientY:100},{clientX:300,clientY:100}]});end();assert.equal(h.scroller.scrollTop,0);
});
test('scroll mode keeps ordinary touch handling and mouse taps remain available',async()=>{
 const h=harness();await settle();h.frame.clientWidth=375;h.get('mode').value='scroll';h.get('mode').handlers.change();h.scroller.scrollTop=0;
 touchTap(h,340);assert.equal(h.scroller.scrollTop,0);
 h.get('mode').value='pages';h.get('mode').handlers.change();h.doc.handlers.click({clientX:340,target:{closest:()=>null}});
 assert.equal(h.scroller.scrollTop,900);
});

test('chunk matching normalizes whitespace but refuses ambiguous or missing text',()=>{
 const markup=require('../../static/narration.js');
 assert.equal(markup.normalize('  café\n 世界  '),'café 世界');
 assert.equal(markup.uniqueMatch('Intro. Narrated sentence. End.','Narrated sentence.'),7);
 assert.equal(markup.uniqueMatch('repeat repeat','repeat'),-1);
 assert.equal(markup.uniqueMatch('Actual text','invented quote'),-1);
});

test('Follow Along changes the visible section while preserving manual reading progress',async()=>{
 const h=harness({narration:{book_id:'book',section_id:2,segment_index:0,quote:'Safe reading text',anchor:'narration:asset:0',fraction:0}});await settle();h.reader.ReaderNarration.playback({book_id:'book',playing:true});h.get('followNarration').click?.();h.get('followNarration').onclick();await settle();
 const location={book_id:'book',section_id:2,segment_index:0,quote:'Safe reading text',anchor:'narration:asset:0',fraction:0};
 await h.reader.ReaderNarration.update(location);await settle();
 assert.equal(h.get('sectionTitle').textContent,'Section 2');assert.equal(h.highlighted.at(-1).anchor,location.anchor);
 assert.equal(h.saved.at(-1).section_id,1);assert.equal(h.saved.at(-1).fraction,.43);assert.equal(h.saved.at(-1).anchor,''); // The DOM-less harness has no text-anchor renderer; browser tests require real text anchors.
 assert.equal(h.saved.at(-1).preferences.followAlong,true);
});
test('manual scrolling suspends follow without seeking audio, Return restores narration',async()=>{
 const h=harness();await settle();h.reader.ReaderNarration.playback({book_id:'book',playing:true});h.get('followNarration').onclick();await settle();
 const location={book_id:'book',section_id:1,quote:'Safe reading text',anchor:'narration:asset:0',fraction:0};
 await h.reader.ReaderNarration.update(location);await settle();h.doc.handlers.wheel();h.scroller.scrollTop=700;
 await h.reader.ReaderNarration.update({...location,section_id:2,anchor:'narration:asset:1'});await settle();
 assert.equal(h.get('sectionTitle').textContent,'Section 1');assert.equal(h.scroller.scrollTop,700);assert.equal(h.get('returnNarration').hidden,false);
 assert.ok(!h.calls.some(([path])=>path.includes('/audio/jobs')||path.includes('/listening')));
 h.get('returnNarration').onclick();await settle();assert.equal(h.get('sectionTitle').textContent,'Section 2');assert.equal(h.get('returnNarration').hidden,true);
});
test('Return to Narration does not let a queued settings reflow restore the old manual page',async()=>{
 const h=harness({delayedFrames:true});await settle();h.reader.ReaderNarration.playback({book_id:'book',playing:true});h.get('followNarration').onclick();await settle();
 const location={book_id:'book',section_id:1,quote:'Safe reading text',anchor:'narration:asset:0',fraction:0};
 await h.reader.ReaderNarration.update(location);await settle();h.doc.handlers.wheel();h.scroller.scrollTop=700;
 h.get('returnNarration').onclick();await settle();assert.equal(h.scroller.scrollTop,220);
 h.flushFrames();await settle();assert.equal(h.scroller.scrollTop,220);
});
test('narration in a different book never navigates this reader',async()=>{
 const h=harness();await settle();h.get('followNarration').onclick();await settle();await h.reader.ReaderNarration.update({book_id:'other',section_id:3});await settle();assert.equal(h.get('sectionTitle').textContent,'Section 1');
});

test('keyboard scrolling persists manual Follow Along suspension',async()=>{
 const h=harness();await settle();h.get('followNarration').onclick();await settle();
 h.doc.handlers.keydown({key:'PageDown',target:{closest:()=>false}});await settle();
 assert.equal(h.saved.at(-1).preferences.followAlongSuspended,true);assert.equal(h.followUpdates.at(-1)[2],true);
});

test('DOM chunk alignment matches EPUB block starts and inline footnotes without guessing spaces',()=>{
 const markup=require('../../static/narration.js'),nodes=[],wrapped=[];
 const element=tagName=>nodes.push({nodeType:1,tagName});
 const text=textContent=>nodes.push({nodeType:3,textContent});
 element('H1');text('Chapter 1');element('P');text('Text & meaning 0.');element('P');text('A footnote');element('A');text('See note');
 let index=0;const doc={body:{},querySelectorAll:()=>[],createTreeWalker:()=>({nextNode:()=>nodes[index++]}),createElement:()=>({dataset:{}}),createRange:()=>({setStart(node,offset){this.node=node;this.start=offset;},setEnd(node,offset){assert.equal(node,this.node);this.end=offset;},surroundContents(span){span.textContent=this.node.textContent.slice(this.start,this.end);wrapped.unshift(span.textContent);}})};
 const marker=markup.highlight(doc,{quote:'Chapter 1 Text & meaning 0. A footnoteSee note',anchor:'chunk',normalized_offset:0});
 assert.equal(marker.id,'chunk');assert.deepEqual(wrapped,['Chapter 1','Text & meaning 0.','A footnote','See note']);
 assert.equal(markup.matchingIndex('repeat repeat','repeat',7),7);
 assert.equal(markup.originalOffset('   world',0,true),3);
});

test('reader ignores a stale iframe load event until the active section document is ready',async()=>{const h=harness({staleFrameEvent:true});await settle();assert.equal(h.frame.contentDocument.body.dataset.sectionId,'1');assert.equal(h.reader.ReaderNarration.page().ready,true);assert.equal(h.saved.length,1);h.get('nextSection').onclick();await settle();assert.equal(h.reader.ReaderNarration.page().section_id,2);assert.equal(h.reader.ReaderNarration.page().document_section_id,'2');});

test('clamped final page advances to its immediate next section exactly once',async()=>{const pagination=require('../../static/pagination.js');const h=harness({pagination:{...pagination,anchor:()=> 'text:386',restore(){}}});await settle();h.scroller.scrollLeft=h.scroller.scrollWidth-h.doc.documentElement.clientWidth;h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});await settle();assert.equal(h.reader.ReaderNarration.page().section_id,2);assert.equal(h.calls.filter(([url])=>url.endsWith('/sections/2')).length,1);});
test('programmatic reflow does not replace a nonzero intended passage with page-start text0',async()=>{let visible='text:386';const actual=require('../../static/pagination.js'),renderer={...actual,anchor:()=>visible,restore(){}};const h=harness({pagination:renderer});await settle();assert.equal(h.saved.at(-1).anchor,'text:386');h.doc.documentElement.clientWidth=844;h.scroller.scrollLeft=0;visible='text:0';h.doc.handlers.scroll();h.intervals[0]();await settle();assert.equal(h.saved.at(-1).anchor,'text:386');visible='text:1200';h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});h.intervals[0]();await settle();assert.equal(h.saved.at(-1).anchor,'text:1200');});

test('paused narration retains a subtle theme-token mark while headphones accurately indicate pause',async()=>{
 const h=harness();await settle();h.get('followNarration').onclick();await settle();
 const loc={book_id:'book',section_id:1,quote:'Safe reading text',anchor:'narration:asset:0',fraction:0};
 await h.reader.ReaderNarration.update(loc);assert.equal(h.highlighted.length,1);
 h.reader.ReaderNarration.playback({book_id:'book',playing:true,engaged:true});await h.reader.ReaderNarration.update(loc);
 assert.equal(h.highlighted.length,1);assert.equal(h.get('dockListen').dataset.playing,'true');
 h.reader.ReaderNarration.playback({book_id:'book',playing:false,engaged:true});assert.equal(h.get('dockListen').dataset.playing,'false');assert.equal(h.cleared.length,0);
 await h.reader.ReaderNarration.update(loc);assert.equal(h.highlighted.length,1);
});
test('page slide has intermediate frames and locks overlapping keyboard turns',async()=>{
 const p=require('../../static/pagination.js'),h=harness({animation:true,pagination:{...p,anchor:()=> 'text:100',restore(){}}});await settle();h.scroller.scrollLeft=0;
 const right=()=>h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});
 right();right();assert(h.reader.ReaderNarration.page().turning);h.runFrame(120);
 assert(h.scroller.scrollLeft>0&&h.scroller.scrollLeft<375);right();h.runFrame(240);
 assert.equal(h.scroller.scrollLeft,375);assert.equal(h.reader.ReaderNarration.page().turning,false);
});
test('reduced motion preserves one-page keyboard navigation without animation',async()=>{
 const p=require('../../static/pagination.js'),h=harness({animation:true,reducedMotion:true,pagination:{...p,anchor:()=> 'text:100',restore(){}}});await settle();h.scroller.scrollLeft=0;
 h.doc.handlers.keydown({key:'ArrowRight',target:{closest:()=>null},preventDefault(){}});
 assert.equal(h.scroller.scrollLeft,375);assert.equal(h.reader.ReaderNarration.page().turning,false);
});

test('returning to a paused narration in another chapter preserves manual position and does not Play',async()=>{
 const h=harness();await settle();h.doc.handlers.wheel();h.scroller.scrollTop=700;h.intervals[0]();await settle();
 h.reader.ReaderNarration.playback({book_id:'book',playing:false,state:'paused'});
 await h.reader.ReaderNarration.update({book_id:'book',section_id:2,quote:'Safe reading text',anchor:'narration:asset:0',fraction:0});
 assert.equal(h.get('returnNarration').hidden,false);assert.equal(h.get('sectionTitle').textContent,'Section 1');
 await h.reader.ReaderNarration.follow();await settle();assert.equal(h.get('sectionTitle').textContent,'Section 2');assert.equal(h.get('dockListen').dataset.playing,'false');
 h.intervals[0]();await settle();assert.equal(h.saved.at(-1).section_id,1);assert.equal(h.saved.at(-1).fraction,.7);
 assert(!h.calls.some(([url])=>url.includes('/listening')||url.includes('/audio/jobs')));
});
test('a durable current-version theme survives reload before its backend save commits',async()=>{
 const local={section_id:1,fraction:.4,anchor:'text:100',preferences:{theme:'lightgray',fontSize:26},server_version:0,saved_at:Date.now()+1000};
 const h=harness({storage:{'webnovelReadingCheckpoint:book':JSON.stringify(local)}});await settle();assert.equal(h.get('theme').value,'lightgray');assert.equal(h.get('fontSize').value,26);assert.equal(h.saved.at(-1).preferences.theme,'lightgray');
});
test('near-black is the default but explicit existing themes are preserved',async()=>{
 const fresh=harness();await settle();assert.equal(fresh.get('theme').value,'dark');
 const existing=harness({progress:{preferences:{theme:'dark'}}});await settle();assert.equal(existing.get('theme').value,'dark');
 const gray=harness({progress:{preferences:{theme:'gray'}}});await settle();assert.equal(gray.get('theme').value,'gray');
 const global=harness({globalTheme:'lightgray'});await settle();assert.equal(global.get('theme').value,'lightgray');
});

test('a late native following snapshot cannot steal the manually selected reading chapter',async()=>{
 const h=harness({search:'?section=1',narration:{book_id:'book',section_id:1,anchor:'narration:a:0',quote:'Safe',fraction:0}});await settle();
 h.reader.ReaderNarration.playback({book_id:'book',engaged:true,intent:true,playing:true,following:true,follow_suspended:false});
 h.get('nextSection').onclick();await settle();assert.equal(h.get('sectionTitle').textContent,'Section 2');
 h.reader.ReaderNarration.playback({book_id:'book',engaged:true,intent:true,playing:true,following:true,follow_suspended:false});
 await h.reader.ReaderNarration.update({book_id:'book',section_id:3,anchor:'narration:a:1',quote:'Safe',fraction:0});await settle();
 assert.equal(h.get('sectionTitle').textContent,'Section 2');
});
test('normal reader reopen restores the manual checkpoint even when native follow is active',async()=>{
 const h=harness({search:'?case=manual-reopen',progress:{section_id:2,fraction:.4,anchor:'text:100'},player:{readerResume:async()=>({following:true,location:{book_id:'book',section_id:3,fraction:0,anchor:'narration:a:0',quote:'Safe'}})}});await settle();
 assert.equal(h.get('sectionTitle').textContent,'Section 2');
});

test('natural chapter progression never navigates the reading chapter, even after an explicit narration return',async()=>{
 const first={book_id:'book',section_id:1,anchor:'live:first',quote:'Safe',fraction:0};const h=harness({search:'?narration=1',player:{readerResume:async()=>({following:true,location:first})}});await settle();
 h.reader.ReaderNarration.playback({book_id:'book',engaged:true,intent:true,playing:true,following:true,follow_suspended:false});
 await h.reader.ReaderNarration.update({...first,section_id:2,anchor:'live:next'});await settle();assert.equal(h.reader.ReaderNarration.page().section_id,1);
});

test('Book contents uses the bounded current-chapter list when the book has no embedded TOC',async()=>{
 const h=harness({fallbackContents:true});await settle();h.get('tocTab').onclick();await settle();
 assert.equal(h.get('sectionList').dataset.virtualized,'true');assert.equal(h.get('sectionSearch').hidden,false);
 assert.equal(h.get('sectionList').children.length,102);assert(h.get('sectionList').children.some(n=>n.attributes['aria-current']==='true'));
 h.get('sectionList').children[2].onclick();await settle();assert.equal(h.reader.ReaderNarration.page().section_id,2);
});
