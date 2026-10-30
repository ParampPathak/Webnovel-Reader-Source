'use strict';
(() => {
 const $=id=>document.getElementById(id);let selected,complete;
 window.BookManagement={remove(book,callback){selected={id:book.id,title:book.title};complete=callback;$('deleteTitle').textContent=`“${book.title}”`;$('deleteStatus').textContent='';$('deleteDialog').showModal();}};
 $('cancelDelete').onclick=()=>$('deleteDialog').close();
 $('confirmDelete').onclick=async()=>{const button=$('confirmDelete');button.disabled=true;try{const response=await fetch(`/api/books/${selected.id}`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'permanent',confirmation:selected.title})});const result=await response.json();if(!response.ok)throw new Error(result.error||'Deletion failed.');if(window.parent?.Player?.forgetBook)window.parent.Player.forgetBook(selected.id);$('deleteDialog').close();if(complete)await complete(result);}catch(e){$('deleteStatus').textContent=e.message;}finally{button.disabled=false;}};
})();
