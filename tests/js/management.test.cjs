const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
test('Delete Book uses stable ID, explicit confirmation and permanent action without mode selector',async()=>{
 const ids=['deleteTitle','deleteStatus','deleteDialog','cancelDelete','confirmDelete'],nodes=Object.fromEntries(ids.map(id=>[id,{textContent:'',disabled:false,showModal(){this.open=true},close(){this.open=false}}]));let request,completed=false;
 const context={document:{getElementById:id=>nodes[id]},window:{},fetch:async(url,options)=>{request={url,body:JSON.parse(options.body)};return {ok:true,json:async()=>({removed:true,permanent:true})}}};vm.createContext(context);vm.runInContext(fs.readFileSync('static/management.js','utf8'),context);
 context.window.BookManagement.remove({id:'stable-id-a',title:'Same title'},()=>{completed=true});assert.equal(nodes.deleteTitle.textContent,'“Same title”');await nodes.confirmDelete.onclick();assert.equal(request.url,'/api/books/stable-id-a');assert.equal(request.body.mode,'permanent');assert.equal(request.body.confirmation,'Same title');assert.ok(completed);assert.equal(nodes.deleteDialog.open,false);
});
