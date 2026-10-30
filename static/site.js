'use strict';
(() => {
 if(window.parent!==window&&!document.body.classList.contains('reader-page')){const header=document.querySelector('header.topbar');if(header)header.hidden=true;}
 function navigate(path){if(window.parent!==window&&window.parent.Player)window.parent.Player.navigate(path);else location.href='/?view='+encodeURIComponent(path);}
 document.addEventListener('click',event=>{const link=event.target.closest('a');if(!link||event.ctrlKey||event.metaKey||event.shiftKey||link.download)return;const path=link.getAttribute('href');if(path&&/^\/(?:$|library|reader\/|books\/|importer|jobs|settings)/.test(path)){event.preventDefault();navigate(path==='/'?'/library':path);}});
 window.listenToBook=(bookId,sectionId=null)=>{if(window.parent!==window&&window.parent.Player)window.parent.Player.listen(bookId,sectionId,true);else location.href=`/?view=${encodeURIComponent('/reader/'+bookId)}&listen=${encodeURIComponent(bookId)}&section=${sectionId||''}`;};
 if(document.body.dataset.bookId)fetch(`/api/books/${document.body.dataset.bookId}/opened`,{method:'POST'}).catch(()=>{});
 const button=document.getElementById('listenChapter');
 if(button)button.onclick=()=>{const frame=document.getElementById('readingFrame');const id=Number(frame?.dataset.sectionId);window.listenToBook(document.body.dataset.bookId,id||null);};
})();
