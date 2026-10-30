"""Validate workstation evaluate callback contracts without launching a browser."""
import ast
import json
import re
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]

class BrowserScriptTests(unittest.TestCase):
    def test_supplied_locator_arguments_use_second_javascript_parameter(self):
        tree=ast.parse((ROOT/'tools/verify_paginated_browser.py').read_text())
        supplied=[]
        for call in ast.walk(tree):
            if isinstance(call,ast.Call) and isinstance(call.func,ast.Attribute) and call.func.attr=='evaluate' and len(call.args)>1:
                # This script's only evaluate-with-argument is locator.evaluate.
                supplied.append(call)
                self.assertIsInstance(call.args[0],ast.Constant)
                self.assertRegex(call.args[0].value,r'^\(element,\w+\)=>')
        self.assertEqual(len(supplied),4)

    @unittest.skipUnless(shutil.which('node'),'Node needed for actual callback invocation')
    def test_real_passage_callback_receives_text_anchor_not_dom_element(self):
        tree=ast.parse((ROOT/'tools/verify_paginated_browser.py').read_text())
        func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='assert_passage')
        call=next(n for n in ast.walk(func) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='evaluate')
        callback=call.args[0].value
        program=r'''
const fs=require('node:fs'),assert=require('node:assert/strict');
const ReaderPagination=require('./static/pagination.js');
const text={textContent:'a'.repeat(1000),length:1000};
const d={body:{dataset:{sectionToken:'2',sectionId:'4'}},documentElement:{clientWidth:375,clientHeight:700},
createTreeWalker(){let visited=false;return {nextNode:()=>visited?null:(visited=true,text)};},
createRange(){return {setStart(node,index){this.index=index;},setEnd(){},getBoundingClientRect:()=>({left:24,right:34,top:20,bottom:40})};}};
const document={getElementById:()=>({contentDocument:d})};
const ReaderNarration={page:()=>({ready:true,section_id:4})};
const callback=eval('('+fs.readFileSync(0,'utf8')+')');
const result=callback({nodeType:1,toString:()=> 'ref: <Node>'},'text:386');
assert.equal(result.anchor,'text:386');assert.equal(result.found,true);
assert.equal(result.visible,true);assert.equal(result.vertical_visible,true);
assert.equal(result.text_length,1000);console.log(JSON.stringify(result));
'''
        result=subprocess.run(['node','-e',program],input=callback,text=True,cwd=ROOT,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['anchor'],'text:386')

    def test_save_wait_accepts_same_page_reflow_but_rejects_reset_and_wrong_chapter(self):
        tree=ast.parse((ROOT/'tools/verify_paginated_browser.py').read_text())
        func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='wait_saved_passage')
        class Clock:
            value=0
            def monotonic(self):return self.value
        class Locator:
            def __init__(self,page):self.page=page
            def evaluate(self,code,args):
                return {'reader':{'section_id':4},'pending_scroll_debounce_ms':0,'expected_visible':True,'saved_visible':self.page.visible}
        class Page:
            visible=True
            def __init__(self,saved,clock):self.saved=saved;self.clock=clock;self.request=self
            def get(self,*args,**kwargs):return self
            def json(self):return self.saved
            def locator(self,*args):return Locator(self)
            def wait_for_timeout(self,ms):self.clock.value+=ms/1000
        for anchor,section,fraction,visible,accept in [('text:400',4,.01,True,True),('text:0',4,0,True,False),('text:400',3,.01,True,False),('text:400',4,.01,False,False)]:
            with self.subTest(anchor=anchor,section=section,visible=visible):
                clock=Clock();page=Page({'anchor':anchor,'section_id':section,'fraction':fraction,'version':8},clock);page.visible=visible
                scope={'time':clock,'BASE':'http://stage:15000','reader':lambda p:p,'report':{},'progress_writes':[],'pending_writes':set(),'json':json}
                exec(compile(ast.Module(body=[func],type_ignores=[]),'<save-wait>','exec'),scope)
                if accept:
                    result=scope['wait_saved_passage'](page,'book',4,'text:386')
                    self.assertEqual(result['anchor'],'text:400')
                    self.assertGreaterEqual(clock.value,1.2)
                else:
                    with self.assertRaisesRegex(AssertionError,'not durably saved'):
                        scope['wait_saved_passage'](page,'book',4,'text:386')

    def test_audio_selection_uses_existing_ready_cache_not_pagination_book(self):
        tree=ast.parse((ROOT/'tools/verify_paginated_browser.py').read_text())
        func=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='ready_narration')
        class Response:
            status=206
            headers={'content-type':'audio/mpeg'}
            def __init__(self,data):self.data=data
            def json(self):return self.data
            def body(self):return b'valid existing bytes'
        class Requests:
            calls=[]
            def get(self,url,**kwargs):
                self.calls.append(url)
                if '/sections?' in url:return Response({'items':[{'id':4 if '/epub/' in url else 1,'title':'Chapter'}]})
                if '/audio?' in url:return Response({'status':'missing'} if '/epub/' in url else {'status':'ready','complete':True,'url':'/api/audio/existing','asset_id':'cached'})
                return Response(None)
        class Page:request=Requests()
        scope={'BASE':'http://stage:15000','report':{},'json':json}
        exec(compile(ast.Module(body=[func],type_ignores=[]),'<cache-choice>','exec'),scope)
        self.assertEqual(scope['ready_narration'](Page(),[{'id':'epub'},{'id':'webnovel'}],'epub'),('webnovel',1))
        self.assertEqual(scope['report']['audio_cache_selection']['rate'],0)
        self.assertEqual(scope['report']['audio_cache_selection']['range_status'],206)
        self.assertTrue(all('/jobs' not in u for u in Page.request.calls))
