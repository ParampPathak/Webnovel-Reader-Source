const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function bridge(){
 const commands=[],window={addEventListener(){},webkit:{messageHandlers:{reader:{postMessage:async data=>{commands.push(data);return {};}}}}};window.top=window;
 const context=vm.createContext({window,document:{getElementById(){return null;}},location:{origin:'https://reader.example.invalid'},HTMLMediaElement:class{},URL,URLSearchParams,DOMException,history:{},fetch:async()=>{throw Error('Unexpected network');}});
 vm.runInContext(fs.readFileSync('NativeReader/Resources/NativeBridge.js','utf8'),context);
 return {window,commands};
}
test('native explicit reader start keeps the selected chapter/anchor even when audio is already playing',async()=>{
 const h=bridge();h.window.NativeReader.receive({revision:1,book_id:'book',intent:true,engaged:true});
 await (h.window.Player.startReader||h.window.Player.toggleReader)('book',{section_id:500,normalized_offset:345,anchor:'text:345'});
 assert.equal(h.commands[0].command,'listen');assert.equal(h.commands[0].section_id,500);assert.equal(h.commands[0].normalized_offset,345);
});
test('ordinary native Play remains a resume command with no manual-reading offset',async()=>{
 const h=bridge();await h.window.Player.togglePlayback();
 assert.equal(h.commands[0].command,'toggle');assert.equal(h.commands[0].normalized_offset,undefined);
 await h.window.Player.listen('book');assert.equal(h.commands[1].normalized_offset,undefined);
});

async function reloadWithSelection(live){
 const events={},commands=[],elements=new Map(),historyCalls=[];
 const element=id=>{if(!elements.has(id))elements.set(id,{dataset:{},value:0,options:[],style:{},contentWindow:null,addEventListener(){},setAttribute(){},append(){},replaceChildren(){},querySelector(){return null;}});return elements.get(id);};
 let revision=20;
 const window={addEventListener:(name,fn)=>events[name]=fn,webkit:{messageHandlers:{reader:{postMessage:async data=>{commands.push(data);return {...live,revision:++revision};}}}}};window.top=window;
 const document={body:{dataset:{}},activeElement:null,getElementById:element,addEventListener(){},createElement:()=>element('new'),createTextNode:()=>({})};
 const context=vm.createContext({window,document,location:{origin:'https://reader.example.invalid',search:'?listen=book&section=151'},HTMLMediaElement:class{},URL,URLSearchParams,DOMException,history:{replaceState:(...args)=>historyCalls.push(args)},fetch:async()=>({json:async()=>({items:[]})})});
 vm.runInContext(fs.readFileSync('NativeReader/Resources/NativeBridge.js','utf8'),context);
 await events.DOMContentLoaded();return {commands,historyCalls,element,events,window};
}
for(const playing of [true,false])test(`web remount with an old listen URL cannot reopen native chapter151 while current155 is ${playing?'playing':'paused'}`,async()=>{
 const h=await reloadWithSelection({book_id:'book',section_id:155,engaged:true,playing,intent:playing,total:90,seconds:40,state:playing?'playing':'paused'});
 assert(!h.commands.some(x=>x.command==='listen'),'Passive web reload dispatched a destructive native listen command');
 assert(!h.commands.some(x=>x.command==='restore-selection'),'An existing native position must survive passive restore');
});
test('cold web selection uses a native-guarded passive request rather than an explicit playback command',async()=>{
 const h=await reloadWithSelection({});assert(!h.commands.some(x=>x.command==='listen'));
 assert(h.commands.some(x=>x.command==='restore-selection'&&x.section_id===151));
});

test('native shell X dispatches stop and isolates the tap from reader navigation',async()=>{
 const h=await reloadWithSelection({book_id:'book',section_id:155,engaged:true,intent:true});
 let prevented=false,propagated=false;h.element('closePlayer').onclick({preventDefault(){prevented=true;},stopPropagation(){propagated=true;}});
 await Promise.resolve();assert.equal(h.commands.at(-1).command,'stop');assert(prevented&&propagated);
});

test('native foreground refreshes server-owned iframe inventory without issuing playback commands',async()=>{
 const h=await reloadWithSelection({book_id:'book',section_id:155,engaged:true,intent:true});let refreshed=0;
 h.element('workspace').contentWindow={Event:class{constructor(type){this.type=type;}},dispatchEvent:e=>{assert.equal(e.type,'pageshow');refreshed++;}};
 h.events['native-reader-foreground']();await Promise.resolve();assert.equal(refreshed,1);assert(!h.commands.some(x=>x.command==='listen'));
});

test('native empty state refresh is safe while the reader frame is absent',async()=>{
 const h=await reloadWithSelection({});assert.doesNotThrow(()=>h.window.NativeReader.receive({revision:100,engaged:false}));
 assert(!h.commands.some(x=>x.command==='listen'));
});
