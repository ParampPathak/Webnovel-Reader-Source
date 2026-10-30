const {test}=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
async function settle(){for(let i=0;i<100;i++)await Promise.resolve();}
function harness(){
 const elements=new Map(),get=id=>{if(!elements.has(id)){const n={value:'',dataset:{},children:[],elements:new Proxy({},{get:(o,k)=>o[k]||(o[k]={value:''})}),append(...v){this.children.push(...v);},replaceChildren(...v){this.children=v;},querySelector(){return {disabled:false};}};elements.set(id,n);}return elements.get(id);};
 let state={enabled:false,eligible:true,interval:86400,config_revision:4,source_url:'https://freewebnovel.com/novel/fixture',status:'idle'},offline=false,needsConfirmation=false,hangPreview=false;
 const events={};const calls=[],timers=[];const document={addEventListener:(name,fn)=>events[name]=fn,body:{dataset:{bookId:'B'}},getElementById:get,createElement:()=>({append(){}}),activeElement:null};
 const context=vm.createContext({document,window:{addEventListener:(name,fn)=>events["window:"+name]=fn},AbortController,FormData:class{},setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout(){},encodeURIComponent,BookManagement:{},fetch:async(path,options={})=>{
   calls.push([path,options]);let data;
   if(path.endsWith('/chapter-updates/preview')){
    if(offline)throw Error('Network unavailable');
    if(hangPreview)return await new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const e=Error('aborted');e.name='AbortError';reject(e);}));
    const sent=JSON.parse(options.body);
    if(sent.config_revision!==state.config_revision)return {ok:false,status:409,json:async()=>({error:'Source settings changed in another client. Reload and retry.'})};
    data={preview_id:'fixture-preview',source_url:sent.source_url,title:'Fixture',available:3,matched:3,existing_count:3,needs_confirmation:needsConfirmation,message:'Verified chapter overlap.'};
   }else if(path.endsWith('/chapter-updates')){
    if(options.method==='PUT'){
     if(offline)throw Error('Network unavailable');const sent=JSON.parse(options.body);
     if(sent.config_revision!==state.config_revision)return {ok:false,status:409,json:async()=>({error:'Source settings changed in another client. Reload and retry.'})};
     state={...state,...sent,config_revision:state.config_revision+1};
    }
    data={...state};
   }else if(path==='/api/books/B')data={title:'Fixture',section_count:1,last_position:0,source_kind:'epub',hidden:false};
   else if(path.includes('/preferences'))data={favorite:false};
   else data={items:[],total:0};
   return {ok:true,status:200,json:async()=>data};
 }});
 vm.runInContext(fs.readFileSync('static/book.js','utf8'),context);
 return {get,calls,timers,foreground:()=>events["window:pageshow"]?.(),setOffline(value){offline=value;},remoteEdit(){state={...state,source_url:'https://freewebnovel.com/novel/changed',config_revision:5};},state:()=>state,setConfirmation(v){needsConfirmation=v;},setHang(v){hangPreview=v;}};
}
test('source UI loads the server value and includes its revision on a native/web shared save',async()=>{
 const h=harness();await settle();assert.equal(h.get('updatesUrl').value,h.state().source_url);
 h.get('updatesUrl').value='https://freewebnovel.com/novel/new';h.get('updatesUrl').oninput();
 await h.get('updatesSourceForm').onsubmit({preventDefault(){}});
 assert(!h.calls.some(([,o])=>o.method==='PUT'),'First submission validates without changing saved settings');
 await h.get('updatesSourceForm').onsubmit({preventDefault(){}});
 const put=h.calls.find(([,o])=>o.method==='PUT');assert.equal(JSON.parse(put[1].body).config_revision,4);
 assert.equal(h.get('updatesSource').textContent,'https://freewebnovel.com/novel/new');
});
test('offline source save ends with an error and retains both the saved value and editable draft',async()=>{
 const h=harness();await settle();const saved=h.state().source_url;
 h.get('updatesUrl').value='https://freewebnovel.com/novel/draft';h.get('updatesUrl').oninput();h.setOffline(true);
 await h.get('updatesSourceForm').onsubmit({preventDefault(){}});
 assert.equal(h.state().source_url,saved);assert.equal(h.get('updatesUrl').value,'https://freewebnovel.com/novel/draft');
 assert.match(h.get('updatesResult').textContent,/Network unavailable/);assert.match(h.get('updatesResult').textContent,/retained/);
 assert(h.timers.some(t=>t.delay===15000),'Requests must have a finite timeout');
});
test('a conflicting source edit displays the new server value without silently discarding the draft',async()=>{
 const h=harness();await settle();h.get('updatesUrl').value='https://freewebnovel.com/novel/draft';h.get('updatesUrl').oninput();h.remoteEdit();
 await h.get('updatesSourceForm').onsubmit({preventDefault(){}});
 assert.equal(h.state().config_revision,5);assert.match(h.get('updatesSource').textContent,/changed/);
 assert.match(h.get('updatesUrl').value,/draft/);assert.match(h.get('updatesResult').textContent,/another client/);
});

test('uncertain overlap needs explicit future-only confirmation before shared save',async()=>{
 const h=harness();await settle();h.setConfirmation(true);h.get('updatesUrl').value='https://www.royalroad.com/fiction/42';h.get('updatesUrl').oninput();
 await h.get('updatesValidate').onclick();await h.get('updatesSourceForm').onsubmit({preventDefault(){}});
 assert(!h.calls.some(([,o])=>o.method==='PUT'));assert.equal(h.get('updatesConfirmLabel').hidden,false);
 h.get('updatesConfirm').checked=true;await h.get('updatesSourceForm').onsubmit({preventDefault(){}});
 const sent=JSON.parse(h.calls.find(([,o])=>o.method==='PUT')[1].body);assert.equal(sent.confirm_baseline,true);assert.equal(sent.preview_id,'fixture-preview');
});
test('preview timeout exits Checking and preserves editable draft',async()=>{
 const h=harness();await settle();h.setHang(true);h.get('updatesUrl').value='https://www.royalroad.com/fiction/42';h.get('updatesUrl').oninput();
 const work=h.get('updatesValidate').onclick();await settle();h.timers.find(t=>t.delay===75000).fn();await work;
 assert.match(h.get('updatesResult').textContent,/timed out/);assert.equal(h.get('updatesValidate').disabled,false);assert.match(h.get('updatesUrl').value,/fiction\/42/);
});
test('editing a validated URL discards its proof and never saves the stale preview',async()=>{
 const h=harness();await settle();h.get('updatesUrl').value='https://www.royalroad.com/fiction/42';h.get('updatesUrl').oninput();await h.get('updatesValidate').onclick();
 h.get('updatesUrl').value='https://www.royalroad.com/fiction/43';h.get('updatesUrl').oninput();await h.get('updatesSourceForm').onsubmit({preventDefault(){}});
 assert(!h.calls.some(([,o])=>o.method==='PUT'));
});

test('foreground inventory refresh preserves an unsaved source draft',async()=>{
 const h=harness();await settle();const draft='https://freewebnovel.com/novel/unsaved';h.get('updatesUrl').value=draft;h.get('updatesUrl').oninput();
 const saved=h.state().source_url;await h.foreground();await settle();assert.equal(h.get('updatesUrl').value,draft);assert.equal(h.state().source_url,saved);
});
