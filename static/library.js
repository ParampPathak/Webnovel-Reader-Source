'use strict';
const $ = id => document.getElementById(id);
let offset = 0, total = 0, requestSequence = 0;
const limit = 24;
async function api(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
const bookMenu=element('dialog','book-menu-dialog');
bookMenu.setAttribute('aria-labelledby','bookMenuTitle');
document.body.append(bookMenu);
let menuOwner=null,menuCard=null;
function closeBookMenu(back=true){
 if(!bookMenu.open)return Promise.resolve();
 bookMenu.close();menuOwner?.setAttribute('aria-expanded','false');menuCard?.classList.remove('book-menu-selected');menuOwner?.focus();menuOwner=null;menuCard=null;
 if(history.state?.webnovelBookMenu){
  if(back)return new Promise(resolve=>{window.addEventListener('popstate',resolve,{once:true});history.back();});
  const state={...history.state};delete state.webnovelBookMenu;history.replaceState(state,'');
 }
 return Promise.resolve();
}
function openBookMenu(book,owner,card,actions){
 closeBookMenu(false);menuOwner=owner;menuCard=card;
 const heading=element('h2','',book.title);heading.id='bookMenuTitle';
 const close=element('button','book-menu-close','×');close.setAttribute('aria-label','Close book menu');close.onclick=()=>closeBookMenu();
 bookMenu.replaceChildren(close,heading,element('p','muted',book.author||'Book actions'),actions);
 for(const link of actions.querySelectorAll('a:not([download])'))link.onclick=async event=>{event.preventDefault();event.stopPropagation();const path=link.getAttribute('href');await closeBookMenu();if(window.parent!==window&&window.parent.Player)window.parent.Player.navigate(path);else location.href=path;};
 for(const button of actions.querySelectorAll('button')){const action=button.onclick;button.onclick=async()=>{await closeBookMenu();action?.();};}
 owner.setAttribute('aria-expanded','true');card.classList.add('book-menu-selected');history.pushState({...history.state,webnovelBookMenu:true},'');bookMenu.showModal();
}
bookMenu.addEventListener('cancel',event=>{event.preventDefault();closeBookMenu();});
bookMenu.addEventListener('click',event=>{const r=bookMenu.getBoundingClientRect();if(event.target===bookMenu&&(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom))closeBookMenu();});
window.addEventListener('popstate',()=>closeBookMenu(false));
async function loadBooks() {
  const token = ++requestSequence;
  $('libraryStatus').textContent = 'Loading your books…';
  try {
    const data = await api(`/api/books?limit=${limit}&offset=${offset}&q=${encodeURIComponent($('bookSearch').value)}&sort=${$('librarySort').value}&favorite=${$('favoritesOnly').checked?'1':'0'}&kind=${$('bookKind').value}&archived=${$('removedBooks').checked?'1':'0'}`);
    if (token !== requestSequence) return;
    total = data.total;
    closeBookMenu(false);$('books').replaceChildren();
    for (const book of data.items) {
      const card = element('article', 'book-card');
      const cover = element('a', 'book-cover');
      cover.href = `/reader/${book.id}`;
      cover.setAttribute('aria-label', `Read ${book.title}`);
      if (book.cover_url) {
        const image = element('img');
        image.src = book.cover_url; image.alt = `Cover of ${book.title}`; image.loading = 'lazy';
        const fallback=()=>{cover.replaceChildren(element('span','cover-fallback','No artwork'));};image.onerror=fallback;image.onload=()=>{if(image.naturalWidth<16||image.naturalHeight<16)fallback();};
        cover.append(image);
      } else cover.append(element('span', 'cover-fallback', 'No artwork'));
      const info = element('div', 'book-card-content');
      const title=element('a','book-title',book.title);title.href=`/reader/${book.id}`;info.append(element('h3', '', ''), element('p', 'muted', book.author || 'Unknown author'),
        element('p', 'muted', `${book.section_count.toLocaleString()} sections · ${book.source_kind === 'epub' ? 'EPUB' : 'Webnovel'}`));
      info.children[0].append(title);
      if(book.section_id&&book.section_count){const progress=element('progress','book-progress');progress.max=1;progress.value=Number.isFinite(book.reading_percentage)?book.reading_percentage/100:Math.min(1,(Number(book.reading_position||0)+Number(book.fraction||0))/book.section_count);progress.setAttribute('aria-label','Reading progress');info.append(progress,element('p','muted',progress.value>=.999?(book.source_kind==='epub'?'Last page reached':'Last imported page'):'In progress · '+Math.round(progress.value*100)+'%'));}
      const read = element('a', 'read-link', 'Open reader');
      read.href = `/reader/${book.id}`;
      const menu=element('button','card-menu','⋮');menu.type='button';menu.setAttribute('aria-label',`Manage ${book.title}`);menu.setAttribute('aria-haspopup','dialog');menu.setAttribute('aria-expanded','false');const actions=element('div','book-menu-actions');menu.onclick=()=>openBookMenu(book,menu,card,actions);
      for(const [label,path] of [['Details',`/books/${book.id}`],['Edit metadata',`/books/${book.id}#metadataSection`]]){const a=element('a','',label);a.href=path;actions.append(a);}
      if(book.download_url){const a=element('a','','Download EPUB');a.href=book.download_url;a.download='';actions.append(a);}
      const favorite=element('button','',book.favorite?'Unfavorite':'Favorite');favorite.onclick=async()=>{try{await api(`/api/books/${book.id}/favorite`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({favorite:!book.favorite})});await loadBooks();}catch(e){$('libraryStatus').textContent=e.message;}};if(!book.hidden)actions.append(favorite);
      const remove=element('button','danger','Delete Book');remove.onclick=()=>BookManagement.remove(book,async result=>{await loadBooks();$('libraryStatus').textContent=result.permanent?`Removed “${book.title}”. Permanent cleanup queued. ${result.worker_active?'':'Background worker is stopped.'}`:`Removed “${book.title}” from library. Files are preserved.`;});actions.append(remove);
      const details=element('a','quiet-link','Details');details.href=`/books/${book.id}`;info.append(details);
      if(book.hidden){read.hidden=true;const restore=element('button','','Restore to library');restore.onclick=async()=>{try{await api(`/api/books/${book.id}/restore`,{method:'POST'});await loadBooks();$('libraryStatus').textContent=`Restored “${book.title}”.`;}catch(e){$('libraryStatus').textContent=e.message;}};info.append(restore);}

      card.append(cover,menu,info);$('books').append(card);
    }
    $('bookCount').textContent = `(${total})`;
    $('libraryStatus').textContent = total ? '' : $('removedBooks').checked?'No removed books.':'No matching books. Upload an EPUB or adjust your filters.';
    $('pageInfo').textContent = total ? `${offset + 1}–${Math.min(offset + limit, total)} of ${total}` : '0 books';
    $('previousPage').disabled = offset === 0;
    $('nextPage').disabled = offset + limit >= total;
  } catch (error) { if (token === requestSequence) $('libraryStatus').textContent = error.message; }
}
$('uploadForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button');
  button.disabled = true; $('uploadStatus').textContent = 'Importing your book…';
  try {
    const result = await api('/api/books/upload', {method: 'POST', body: new FormData(form)});
    $('uploadStatus').textContent = result.created ? 'Book added. Ready to read.' : 'This EPUB is already in your library.';
    form.reset(); offset = 0; await loadBooks();
  } catch (error) { $('uploadStatus').textContent = error.message; }
  finally { button.disabled = false; }
});
let searchTimer;
$('bookSearch').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => {offset = 0; loadBooks();}, 250); });
$('previousPage').onclick = () => {offset = Math.max(0, offset - limit); loadBooks();};
$('nextPage').onclick = () => {offset += limit; loadBooks();};
loadBooks();

for(const id of ['librarySort','favoritesOnly','bookKind','removedBooks']) $(id).onchange=()=>{offset=0;loadBooks();};

// Imported inventory is owned by the server; reopening never requires another import.
window.addEventListener('pageshow',()=>loadBooks());
document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadBooks();});
const libraryRefresh=setInterval(()=>{if(!document.hidden&&!bookMenu.open)loadBooks();},30000);
window.addEventListener('pagehide',()=>clearInterval(libraryRefresh));
