/* Cached-chunk text alignment. No guessed word timings or EPUB mutations. */
'use strict';
(function(root){
 const normalize=text=>text.replace(/\s+/g,' ').trim();
 function uniqueMatch(text,quote){const start=text.indexOf(quote);return quote&&start>=0&&text.indexOf(quote,start+1)<0?start:-1;}
 function matchingIndex(text,quote,expected){return Number.isInteger(expected)&&text.slice(expected,expected+quote.length)===quote?expected:uniqueMatch(text,quote);}
 function originalOffset(text,index,previousSpace){
  let count=0,space=previousSpace;
  for(let i=0;i<text.length;i++){
   const ws=/\s/.test(text[i]);if(ws&&space)continue;space=ws;
   if(count===index)return i;count++;
  }
  return text.length;
 }
 function clear(doc){
  for(const span of doc?.querySelectorAll('[data-narration-mark]')||[]){const parent=span.parentNode;span.replaceWith(doc.createTextNode(span.textContent));parent.normalize();}
 }
 function documentIndex(doc){
  const blocks=new Set(['P','DIV','BR','H1','H2','H3','LI']);
  const walker=doc.createTreeWalker(doc.body,5),records=[];let node,text='';
  while((node=walker.nextNode())){
   if(node.nodeType===1){if(blocks.has(node.tagName)&&text&&!text.endsWith(' '))text+=' ';continue;}
   const previousSpace=text.endsWith(' ');let fragment=node.textContent.replace(/\s+/g,' ');
   if(previousSpace&&fragment.startsWith(' '))fragment=fragment.slice(1);
   if(fragment){records.push({node,start:text.length,end:text.length+fragment.length,previousSpace});text+=fragment;}
  }
  return {records,text};
 }
 function position(doc,locator,titlePrefix=null){
  const target=root.ReaderPagination?.target(doc,locator);if(!target)return null;
  const {records,text}=documentIndex(doc),record=records.find(r=>r.node===target.startContainer);if(!record)return null;
  let count=0,space=record.previousSpace;
  for(let i=0;i<target.startOffset;i++){const ws=/\s/.test(record.node.textContent[i]);if(ws&&space)continue;space=ws;count++;}
  const leading=text.length-text.trimStart().length,prefix=titlePrefix===null?'':normalize(titlePrefix)+' ';
  if(prefix&&!text.slice(leading).startsWith(prefix))return null;
  return {anchor:locator,normalized_offset:Math.max(0,record.start+count-leading-prefix.length)};
 }
 function locate(doc,location,titlePrefix=null){
  const {records,text}=documentIndex(doc),quote=normalize(location.quote);if(!quote)return null;
  const leading=text.length-text.trimStart().length,prefix=titlePrefix===null?'':normalize(titlePrefix)+' ';
  const expected=Number.isInteger(location.normalized_offset)&&text.slice(leading).startsWith(prefix)?leading+prefix.length+location.normalized_offset:null;
  const start=matchingIndex(text,quote,expected),record=records.find(r=>start>=r.start&&start<r.end);if(!record)return null;
  const offset=originalOffset(record.node.textContent,start-record.start,record.previousSpace),range=doc.createRange();range.setStart(record.node,offset);range.setEnd(record.node,Math.min(record.node.length,offset+1));return range;
 }
 function highlight(doc,location,titlePrefix=null){
  clear(doc);
  const {records,text}=documentIndex(doc);
  const quote=normalize(location.quote);if(!quote)return null;
  const prefix=titlePrefix===null?'':normalize(titlePrefix)+' ';
  const leading=text.length-text.trimStart().length;
  const expected=Number.isInteger(location.normalized_offset)&&text.slice(leading).startsWith(prefix)?leading+prefix.length+location.normalized_offset:null;
  const start=matchingIndex(text,quote,expected);if(start<0)return null;
  const groups=[];
  for(const record of records){
   const a=Math.max(start,record.start),b=Math.min(start+quote.length,record.end);if(a>=b)continue;
   groups.push({node:record.node,start:originalOffset(record.node.textContent,a-record.start,record.previousSpace),end:originalOffset(record.node.textContent,b-record.start,record.previousSpace)});
  }
  let first;
  for(let i=groups.length-1;i>=0;i--){const g=groups[i],range=doc.createRange(),span=doc.createElement('span');span.dataset.narrationMark='true';span.className='narration-highlight';range.setStart(g.node,g.start);range.setEnd(g.node,g.end);range.surroundContents(span);if(i===0){span.id=location.anchor;first=span;}}
  return first;
 }
 root.NarrationMarkup={normalize,uniqueMatch,matchingIndex,originalOffset,highlight,clear,locate,position};
 if(typeof module!=='undefined')module.exports=root.NarrationMarkup;
})(typeof window!=='undefined'?window:globalThis);
