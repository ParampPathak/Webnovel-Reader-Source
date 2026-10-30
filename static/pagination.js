/* Rendered CSS-column pages with stable DOM text offsets, never character-count pages. */
'use strict';
(function(root){
 const pageCount=(extent,width)=>Math.max(1,Math.ceil((extent-1)/Math.max(1,width)));
 const pageFor=(left,width,extent=null)=>{width=Math.max(1,width);if(Number.isFinite(extent)&&extent>width&&left>=extent-width-1)return pageCount(extent,width)-1;return Math.max(0,Math.floor((left+.5)/width));};
 function trimEmptyTail(doc,pages){
  // Keep source nodes/text offsets intact. Only ordinary trailing empty blocks
  // lose layout in page mode; media, decorated blocks and explicit breaks stay.
  for(const el of doc.querySelectorAll('[data-reader-empty-tail],[data-reader-last-block]')){el.removeAttribute('data-reader-empty-tail');el.removeAttribute('data-reader-last-block');}
  if(!pages)return;
  const empty=node=>{
   if(node.nodeType===3)return !node.textContent.trim();
   if(node.nodeType!==1)return node.nodeType===8;
   if(!['P','DIV','SPAN','BR'].includes(node.tagName)||[...node.attributes].some(a=>!['id','class'].includes(a.name)))return false;
   const c=doc.defaultView.getComputedStyle(node);
   if(c.breakBefore!=='auto'||c.breakAfter!=='auto'||c.backgroundImage!=='none'||!['transparent','rgba(0, 0, 0, 0)'].includes(c.backgroundColor)||['Top','Right','Bottom','Left'].some(side=>parseFloat(c['border'+side+'Width'])>0))return false;
   return [...node.childNodes].every(empty);
  };
  const trim=parent=>{for(let node=parent.lastChild;node;node=node.previousSibling){if(empty(node)){if(node.nodeType===1)node.setAttribute('data-reader-empty-tail','');}else{if(node.nodeType===1){node.setAttribute('data-reader-last-block','');trim(node);}break;}}};
  trim(doc.body);
 }
 function layout(doc,pages){
  if(!doc?.body||!doc.documentElement?.style||!doc.createRange)return;
  const root=doc.documentElement,s=doc.scrollingElement,left=s.scrollLeft;
  root.style.removeProperty('--reader-page-end');
  trimEmptyTail(doc,pages);
  if(!pages)return;
  // CSS columns omit the final inline padding from their scrollable overflow.
  // Reserve a complete final viewport, without adding a column to book content.
  const width=root.clientWidth,range=doc.createRange();range.selectNodeContents(doc.body);
  const margin=parseFloat(doc.defaultView.getComputedStyle(doc.body).paddingRight)||0;
  let right=0;for(const rect of range.getClientRects())right=Math.max(right,rect.right+s.scrollLeft);
  const count=pageCount(right+margin,width);
  root.style.setProperty('--reader-page-end',(count*width-1)+'px');if(Math.abs(s.scrollLeft-left)>.5)s.scrollLeft=left;
  return {width,pages:count,gutter:margin,extent:s.scrollWidth};
 }
 function textNodes(doc){const out=[],walk=doc.createTreeWalker(doc.body,4);let node;while((node=walk.nextNode()))if(node.textContent)out.push(node);return out;}
 function anchor(doc){
  if(!doc?.body||!doc?.createTreeWalker)return '';
  const nodes=textNodes(doc),scroll=doc.scrollingElement;let offset=0;
  for(const node of nodes){const range=doc.createRange();range.selectNodeContents(node);const rects=range.getClientRects();
   if([...rects].some(r=>r.right>8&&r.left<doc.documentElement.clientWidth-8&&r.bottom>8&&r.top<doc.documentElement.clientHeight-8)){
    // Locate the first visible character even when one paragraph spans many pages.
    let lo=0,hi=node.length-1;const horizontal=scroll.scrollWidth>doc.documentElement.clientWidth+1;
    while(lo<hi){const mid=Math.floor((lo+hi)/2);range.setStart(node,mid);range.setEnd(node,mid+1);const r=range.getBoundingClientRect();if(horizontal?r.right<=8:r.bottom<=8)lo=mid+1;else hi=mid;}
    return 'text:'+String(offset+lo);
   }offset+=node.length;
  }return ''; // No visible text is a loading/layout condition, never an EOF locator.

 }
 function target(doc,locator){
  const match=/^text:(\d+)$/.exec(locator||'');if(!match||!doc?.body)return null;
  let offset=Number(match[1]);if(!Number.isSafeInteger(offset))return null;
  const nodes=textNodes(doc);let last;
  for(const node of nodes){
   if(offset<node.length){const range=doc.createRange();range.setStart(node,offset);range.setEnd(node,offset+1);return range;}
   offset-=node.length;
   const meaningful=node.textContent.search(/\S(?=\s*$)/);
   if(meaningful>=0)last={node,index:meaningful};
  }
  // Older capture code could persist exactly the total length (EOF). Resolve
  // that boundary to the final passage; reject beyond-end/wrong-document offsets.
  if(offset===0&&last){const range=doc.createRange();range.setStart(last.node,last.index);range.setEnd(last.node,last.index+1);return range;}
  return null;
 }
 function reveal(doc,element,pages){const r=element.getBoundingClientRect(),s=doc.scrollingElement;if(pages){const left=pageFor(r.left+s.scrollLeft,doc.documentElement.clientWidth)*doc.documentElement.clientWidth;if(Math.abs(left-s.scrollLeft)<1)return false;s.scrollLeft=left;}else{if(r.top>=0&&r.bottom<=doc.documentElement.clientHeight)return false;s.scrollTop+=r.top-24;}return true;}
 function restore(doc,fraction,locator,pages){const s=doc.scrollingElement;const found=target(doc,locator)||doc.getElementById(locator);if(found){reveal(doc,found,pages);return;}if(pages){const w=doc.documentElement.clientWidth,left=Math.round(Math.max(0,Math.min(1,fraction))*(pageCount(s.scrollWidth,w)-1))*w;if(Math.abs(s.scrollLeft-left)>.5)s.scrollLeft=left;}else s.scrollTop=Math.max(0,s.scrollHeight-s.clientHeight)*fraction;}
 root.ReaderPagination={pageCount,pageFor,layout,anchor,target,reveal,restore};if(typeof module!=='undefined')module.exports=root.ReaderPagination;
})(typeof window!=='undefined'?window:globalThis);
