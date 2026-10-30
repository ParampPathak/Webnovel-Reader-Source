const {test}=require('node:test');const assert=require('node:assert/strict');const p=require('../../static/pagination.js');
test('narration already visible on its current page causes zero scroll assignments',()=>{let left=375,writes=0;const s={scrollWidth:1125,get scrollLeft(){return left;},set scrollLeft(v){left=v;writes++;}},doc={scrollingElement:s,documentElement:{clientWidth:375}};for(let x=24;x<330;x+=15)p.reveal(doc,{getBoundingClientRect:()=>({left:x,top:20})},true);assert.equal(writes,0);assert.equal(left,375);});
test('repeated anchors across one rendered page boundary move exactly once',()=>{let left=0,writes=0;const s={scrollWidth:1125,get scrollLeft(){return left;},set scrollLeft(v){left=v;writes++;}},doc={scrollingElement:s,documentElement:{clientWidth:375}};for(let x=390;x<650;x+=15)p.reveal(doc,{getBoundingClientRect:()=>({left:x-left,top:20})},true);assert.equal(writes,1);assert.equal(left,375);});
test('rendered widths give discrete pages without a trailing blank from rounding',()=>{assert.equal(p.pageCount(390,390),1);assert.equal(p.pageCount(1170,390),3);assert.equal(p.pageCount(1170.5,390),3);assert.equal(p.pageFor(780,390),2);});
test('a stable text offset resolves the same passage across inline node boundaries',()=>{const nodes=[{length:3},{length:8}],doc={body:{},createTreeWalker(){let i=0;return {nextNode:()=>nodes[i++]||null};},createRange(){return {setStart(node,index){this.node=node;this.index=index;},setEnd(){}};}};nodes.forEach(n=>n.textContent='x'.repeat(n.length));const r=p.target(doc,'text:7');assert.equal(r.node,nodes[1]);assert.equal(r.index,4);assert.equal(p.target(doc,'narration:asset:0'),null);});
test('narration reveal turns to its rendered column without seeking media',()=>{const doc={scrollingElement:{scrollLeft:390},documentElement:{clientWidth:390}};p.reveal(doc,{getBoundingClientRect:()=>({left:430,top:20})},true);assert.equal(doc.scrollingElement.scrollLeft,780);});
test('fraction fallback restores the last rendered page and never overscrolls',()=>{const d={body:{},scrollingElement:{scrollWidth:1170,scrollLeft:0},documentElement:{clientWidth:390},getElementById:()=>null};p.restore(d,1,'',true);assert.equal(d.scrollingElement.scrollLeft,780);});

function textDocument(text){const node={textContent:text,length:text.length};return {body:{},scrollingElement:{scrollWidth:1000},documentElement:{clientWidth:375,clientHeight:700},createTreeWalker(){let done=false;return {nextNode:()=>done?null:(done=true,node)};},createRange(){return {setStart(n,index){this.node=n;this.index=index;},setEnd(){},selectNodeContents(){},getClientRects:()=>[{left:800,right:900,top:10,bottom:30}]};}};}
test('legacy EOF anchor resolves the final meaningful character; beyond-document offsets fail',()=>{const doc=textDocument('a passage  '),r=p.target(doc,'text:11');assert.ok(r);assert.equal(r.index,8);assert.equal(p.target(doc,'text:12'),null);assert.equal(p.target(doc,'text:999999999999999999999'),null);});
test('blank or not-yet-laid-out pages do not manufacture an unresolvable EOF anchor',()=>{assert.equal(p.anchor(textDocument('a passage')), '');assert.equal(p.anchor({body:null,createTreeWalker(){throw Error('Must not walk absent body');}}),'');});

test('native clamped final-column position is the final page, not the penultimate grid cell',()=>{const width=375,extent=4*width-24,last=extent-width;assert.equal(p.pageCount(extent,width),4);assert.equal(p.pageFor(last,width,extent),3);assert.equal(p.pageFor(last-.5,width,extent),3);assert.equal(p.pageFor(width,width,extent),1);assert.equal(p.pageFor(last,width),2);});

function tailDocument(tail){
 const text=value=>({nodeType:3,textContent:value});
 const element=(tag,children=[],attributes={})=>{const n={nodeType:1,tagName:tag,childNodes:children,attrs:{...attributes},setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];}};Object.defineProperties(n,{attributes:{get:()=>Object.keys(n.attrs).map(name=>({name}))},lastChild:{get:()=>children.at(-1)}});children.forEach((c,i)=>c.previousSibling=children[i-1]);return n;};
 const passage=element('P',[text('Actual final passage.')]),end=tail(element,text),body=element('BODY',[passage,...end]);
 const all=node=>node.nodeType===1?[node,...node.childNodes.flatMap(all)]:[];
 const doc={body,querySelectorAll:()=>all(body).filter(n=>'data-reader-empty-tail'in n.attrs||'data-reader-last-block'in n.attrs),scrollingElement:{scrollLeft:0,scrollWidth:375},documentElement:{clientWidth:375,style:{removeProperty(){},setProperty(){}}},defaultView:{getComputedStyle:()=>({breakBefore:'auto',breakAfter:'auto',backgroundImage:'none',backgroundColor:'rgba(0, 0, 0, 0)',paddingRight:'24px'})},createRange:()=>({selectNodeContents(){},getClientRects:()=>[{right:200}]})};
 return {doc,passage,end};
}
test('trailing empty paragraphs retain source nodes but cannot create generated pages',()=>{
 const {doc,passage,end}=tailDocument((el,t)=>[el('P',[el('BR')]),el('DIV',[el('P',[t(' \n\u00a0 ')])])]);p.layout(doc,true);
 assert(end.every(n=>'data-reader-empty-tail'in n.attrs));assert('data-reader-last-block'in passage.attrs);assert.equal(doc.body.childNodes.length,3);
 p.layout(doc,true);assert(end.every(n=>'data-reader-empty-tail'in n.attrs));p.layout(doc,false);assert(end.every(n=>!('data-reader-empty-tail'in n.attrs)));
});
test('legitimate image-only final content is never trimmed',()=>{
 const {doc,end}=tailDocument(el=>[el('DIV',[el('IMG')])]);p.layout(doc,true);assert(!('data-reader-empty-tail'in end[0].attrs));assert(!('data-reader-empty-tail'in end[0].childNodes[0].attrs));
});
test('explicit source styling and intentional blank breaks are preserved',()=>{
 const {doc,end}=tailDocument(el=>[el('DIV',[el('BR')],{style:'break-before:column;break-after:column'})]);p.layout(doc,true);assert(!('data-reader-empty-tail'in end[0].attrs));
});
