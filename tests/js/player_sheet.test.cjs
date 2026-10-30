const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
function harness(){
 const events={},nodes=new Map();let stops=0,plays=0;
 const element=id=>{if(!nodes.has(id))nodes.set(id,{hidden:false,dataset:{},style:{},attrs:{},handlers:{},addEventListener(n,fn){this.handlers[n]=fn;},setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];},focus(){},setPointerCapture(){},releasePointerCapture(){}});return nodes.get(id);};
 const panel=element('playerPanel');panel.dataset.mini='true';
 const document={getElementById:element,body:{dataset:{},classList:{toggle(){}}},addEventListener:(n,f)=>events[n]=f,activeElement:{focus(){}}};
 const window={Player:{stopReader(){stops++;},togglePlayback(){plays++;}}};
 vm.runInNewContext(fs.readFileSync('static/player_sheet.js','utf8'),{window,document});events.DOMContentLoaded();
 element('expandPlayer').click=()=>{panel.dataset.mini=panel.dataset.mini==='false'?'true':'false';window.PlayerSheet.sync();};
 const open=()=>{panel.dataset.mini='false';window.PlayerSheet.sync();};
 const target=kind=>({closest:selector=>kind==='handle'&&selector==='#playerHandle'||kind==='heading'&&selector==='.player-heading'?{}:null});
 const gesture=(kind,dy,type='pointerup')=>{const base={pointerId:1,isPrimary:true,button:0,target:target(kind),clientX:0,clientY:0,timeStamp:0,preventDefault(){}};panel.handlers.pointerdown(base);panel.handlers.pointermove({...base,clientY:dy,timeStamp:100});panel.handlers[type]({...base,clientY:dy,timeStamp:200,type});};
 return {element,panel,open,gesture,window,events,counters:()=>({stops,plays})};
}
test('backdrop and drag-handle dismissal only close presentation and release the underlying reader',()=>{
 const h=harness();for(const action of ['backdrop','handle','drag']){h.open();assert.equal(h.element('closePlayer').hidden,true);assert.equal(h.element('expandPlayer').hidden,true);assert.equal(h.element('workspace').inert,true);assert.equal(h.panel.attrs.role,'dialog');
 if(action==='drag')h.gesture('handle',120);else h.element(action==='backdrop'?'playerBackdrop':'playerHandle').onclick({preventDefault(){}});
 assert.equal(h.panel.dataset.mini,'true');assert.equal(h.element('playerBackdrop').hidden,true);assert.equal(h.element('workspace').inert,false);assert.equal(h.element('closePlayer').hidden,false);assert.deepEqual(h.counters(),{stops:0,plays:0});}
});
test('text, sliders and settings gestures cannot dismiss the sheet; short or cancelled drags snap back',()=>{
 const h=harness();for(const kind of ['text','slider','settings']){h.open();h.gesture(kind,200);assert.equal(h.panel.dataset.mini,'false');}
 h.gesture('handle',10);assert.equal(h.panel.dataset.mini,'false');h.gesture('handle',150,'pointercancel');assert.equal(h.panel.dataset.mini,'false');assert.equal(h.panel.style.transform,'');assert.deepEqual(h.counters(),{stops:0,plays:0});
});
test('stopping or hiding the player cannot strand an invisible backdrop and repeated init is idempotent',()=>{
 const h=harness();h.open();h.panel.hidden=true;h.window.PlayerSheet.sync();h.window.PlayerSheet.init();assert.equal(h.element('playerBackdrop').hidden,true);assert.equal(h.element('workspace').inert,false);assert.deepEqual(h.counters(),{stops:0,plays:0});
});

test('the synthetic click following a short drag cannot accidentally dismiss the sheet',()=>{
 const h=harness();h.open();h.gesture('handle',10);h.element('playerHandle').onclick({detail:1,preventDefault(){}});assert.equal(h.panel.dataset.mini,'false');
 h.element('playerHandle').onclick({detail:0,preventDefault(){}});assert.equal(h.panel.dataset.mini,'true');
});

test('library bar returns to live narration without invoking Stop or the sheet toggle',()=>{
 const h=harness();let returns=0,prevented=0,isolated=0;h.element('readPlaying').click=()=>returns++;
 for(const kind of ['title','cover','background','transport']){
  const e={target:{closest:s=>s==='#miniOpen,#playerCover'&&['title','cover'].includes(kind)?{}:s==='button,input,select,a,label'&&kind==='transport'?{}:null},preventDefault(){prevented++;},stopImmediatePropagation(){isolated++;}};
  h.panel.handlers.click(e);
 }
 assert.equal(returns,3);assert.equal(prevented,3);assert.equal(isolated,3);assert.equal(h.panel.dataset.mini,'true');assert.deepEqual(h.counters(),{stops:0,plays:0});
});
