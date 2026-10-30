const {test}=require('node:test');const assert=require('node:assert/strict');const {importSelection}=require('../../static/importer.js');
test('first N and custom range use inclusive discovered order',()=>{for(const n of [10,50,100,500])assert.equal(importSelection('first',n,0,0,1501).last,n);assert.deepEqual(importSelection('range',0,200,400,1501),{mode:'range',first:200,last:400});});
test('all and new have no arbitrary chapter limit',()=>{assert.equal(importSelection('all',0,0,0,3001).last,3001);assert.equal(importSelection('new',0,0,0,1501).last,1501);});
test('invalid and undiscovered ranges are rejected',()=>{for(const args of [['first',1502,0,0,1501],['first',1.5,0,0,50],['range',0,20,10,50],['range',0,0,10,50],['all',0,0,0,0]])assert.throws(()=>importSelection(...args));});


async function wizard(hidden=false,imported=0){
 const vm=require('node:vm'),fs=require('node:fs'),els=new Map();
 const get=id=>{if(!els.has(id))els.set(id,{value:{selectionMode:'first',chapterCount:'10',chapterFirst:'1',chapterLast:'10'}[id]||'',replaceChildren(){},append(){},getAttribute(){return '';}});return els.get(id);};
 const document={hidden:false,getElementById:get,createElement:()=>({append(){}}),addEventListener(){}};
 const window={addEventListener(){}};window.parent=window;
 const context=vm.createContext({document,window,location:{search:'?book=B'},localStorage:{getItem(){return null;}},URLSearchParams,history:{},setTimeout(){},clearTimeout(){},console,
 fetch:async path=>({ok:true,json:async()=>path.endsWith('/summary')?{source:'https://public.example',discovered:47,imported,discovery_complete:true,catalog_job_id:'completed',catalog_completed_at:'2026-10-10',jobs:{discover:{status:'queued',result:{}},import:null,export:null},worker_active:false,worker_message:'Background worker stopped.'}:path.includes('/chapters')?{items:[],total:47}:{title:'Book',hidden,section_count:0}})});
 vm.runInContext(fs.readFileSync('static/importer.js','utf8'),context);for(let i=0;i<60;i++)await Promise.resolve();return get;
}
test('usable completed snapshot enables Import even with queued refresh and stopped worker',async()=>{
 const get=await wizard();assert.equal(get('beginImport').disabled,false);assert.match(get('discoveryStatus').textContent,/Refresh queued/);assert.match(get('dashboardStatus').textContent,/worker stopped/);
});
test('archived source shows exact blocker and explicit restore action',async()=>{
 const get=await wizard(true);assert.equal(get('beginImport').disabled,true);assert.equal(get('restoreImportBook').hidden,false);assert.match(get('selectedCount').textContent,/archived/);
});

test('all and custom ranges remain enabled during refresh; only-new explains empty selection',async()=>{
 const get=await wizard(false,47);
 get('selectionMode').value='all';get('selectionMode').oninput();assert.equal(get('beginImport').disabled,false);
 get('selectionMode').value='range';get('chapterFirst').value='2';get('chapterLast').value='4';get('selectionMode').oninput();assert.equal(get('beginImport').disabled,false);assert.match(get('selectedCount').textContent,/3 chapters selected/);
 get('selectionMode').value='new';get('selectionMode').oninput();assert.equal(get('beginImport').disabled,true);assert.match(get('selectedCount').textContent,/No new chapters/);
});
