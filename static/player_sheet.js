/* Presentation only. Dismissal never commands, pauses or checkpoints audio. */
(() => {
 'use strict';
 let panel,backdrop,handle,workspace,expanded=false,drag=null,restoreFocus=null,observer,ignoreClick=false;
 const $=id=>document.getElementById(id);
 const dismiss=()=>{if(expanded)$('expandPlayer')?.click();};
 function sync(){
  if(!panel){init();return;}
  const next=!panel.hidden&&panel.dataset.mini==='false';
  backdrop.hidden=!next;handle.hidden=!next;
  $('closePlayer').hidden=next;$('expandPlayer').hidden=next;
  panel.setAttribute('role',next?'dialog':'region');panel.setAttribute('aria-label',next?'Expanded audio player':'Audiobook player');
  if(next)panel.setAttribute('aria-modal','true');else panel.removeAttribute('aria-modal');
  document.body.classList.toggle('player-sheet-open',next);if(workspace)workspace.inert=next;
  if(next&&!expanded){restoreFocus=document.activeElement;handle.focus({preventScroll:true});}
  if(!next&&expanded){panel.style.transform='';panel.style.transition='';drag=null;restoreFocus?.focus?.({preventScroll:true});}
  expanded=next;
 }
 function init(){
  if(panel)return;
  panel=$('playerPanel');backdrop=$('playerBackdrop');handle=$('playerHandle');workspace=$('workspace');
  if(!panel||!backdrop||!handle){panel=null;return;}
  // Outside the narrated reader, the bar returns to the live book. Keep the
  // separate chevron for the temporary sheet, including older installed bridges.
  panel.addEventListener('click',e=>{
   if(panel.dataset.mini!=='true'||document.body.dataset.reading==='true'&&document.body.dataset.readerAudio==='true')return;
   const target=e.target;
   if(!target.closest('#miniOpen,#playerCover')&&target.closest('button,input,select,a,label'))return;
   e.preventDefault();e.stopImmediatePropagation();$('readPlaying')?.click();
  },true);
  backdrop.onclick=dismiss;handle.onclick=e=>{if(ignoreClick&&e.detail!==0){ignoreClick=false;e.preventDefault();return;}dismiss();};
  panel.addEventListener('pointerdown',e=>{
   if(!expanded||e.isPrimary===false||e.button>0)return;
   const target=e.target;
   // Drag the handle/heading only. Text scrolling, seek and settings keep their
   // own gestures and never become accidental sheet dismissal requests.
   if(!target.closest('#playerHandle')&&(!target.closest('.player-heading')||target.closest('button,input,select,a,label,summary,img')))return;
   ignoreClick=false;drag={id:e.pointerId,y:e.clientY,x:e.clientX,at:e.timeStamp,delta:0,moved:false};
   try{handle.setPointerCapture?.(e.pointerId);}catch{}
  });
  panel.addEventListener('pointermove',e=>{
   if(!drag||drag.id!==e.pointerId||!expanded)return;
   const dy=e.clientY-drag.y,dx=e.clientX-drag.x;
   if(!drag.moved&&(dy<8||dy<Math.abs(dx)*1.2))return;
   drag.moved=true;drag.delta=Math.max(0,dy);e.preventDefault();
   panel.style.transition='none';panel.style.transform=`translate(-50%, ${drag.delta}px)`;
  });
  function finish(e){
   if(!drag||drag.id!==e.pointerId)return;
   const prior=drag;drag=null;ignoreClick=prior.moved;try{handle.releasePointerCapture?.(e.pointerId);}catch{}panel.style.transition='';panel.style.transform='';
   if(e.type!=='pointercancel'&&prior.moved&&(prior.delta>=80||prior.delta>24&&prior.delta/Math.max(1,e.timeStamp-prior.at)>.7))dismiss();
  }
  panel.addEventListener('pointerup',finish);panel.addEventListener('pointercancel',finish);
  document.addEventListener('keydown',e=>{if(expanded&&e.key==='Escape'){e.preventDefault();dismiss();}});
  if(typeof MutationObserver!=='undefined'){observer=new MutationObserver(sync);observer.observe(panel,{attributes:true,attributeFilter:['data-mini','hidden']});}
  sync();
 }
 window.PlayerSheet=Object.freeze({init,sync});
 document.addEventListener('DOMContentLoaded',init);
})();
