"""Workstation-only acceptance: existing staging books/caches; never upload fixtures."""
import json,os,time,traceback,hashlib
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
BASE=os.environ.get('WEBNOVEL_BASE_URL','http://127.0.0.1:15000').rstrip('/')
if urlparse(BASE).port!=15000:raise SystemExit('Use isolated staging port15000; this test writes staged reading progress only.')
OUT=Path.cwd();report={'passed':False,'browser_verified':False,'physical_iphone_verified':False,'console_errors':[],'viewports':[],'last_stage':'initializing','script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()};page=None

def stage(name):report['last_stage']=name

def content(page):return page.frame_locator('#workspace').frame_locator('#readingFrame')

def reader(page):return page.frame_locator('#workspace')

def state(page):return reader(page).locator('body').evaluate('()=>ReaderNarration.page()')

def navigation_snapshot(page,expected=None):
 return reader(page).locator('body').evaluate("""(element,expected)=>{const f=document.getElementById('readingFrame'),d=f.contentDocument,s=d.scrollingElement,w=d.documentElement.clientWidth,r=ReaderNarration.page();return {expected_section_id:expected,expected_type:typeof expected,actual_section_id:r.section_id,actual_type:typeof r.section_id,document_section_id:d.body.dataset.sectionId,document_token:d.body.dataset.sectionToken,ready:r.ready,page_index:ReaderPagination.pageFor(s.scrollLeft,w,s.scrollWidth),page_count:ReaderPagination.pageCount(s.scrollWidth,w),scroll_left:s.scrollLeft,scroll_max:Math.max(0,s.scrollWidth-w),width:w,extent:s.scrollWidth,at_native_end:s.scrollLeft>=Math.max(0,s.scrollWidth-w)-1,anchor:r.anchor,status:document.getElementById('readingStatus').textContent,previous_disabled:document.getElementById('previousSection').disabled,next_disabled:document.getElementById('nextSection').disabled,active_element:d.activeElement?.tagName};}""",expected)

def wait_reader(page,section_id=None,previous_token=None):
 try:
  page.wait_for_function("""args=>{const w=document.getElementById('workspace')?.contentWindow,s=w?.ReaderNarration?.page(),f=w?.document.getElementById('readingFrame'),d=f?.contentDocument;return s?.ready===true&&d?.readyState==='complete'&&d.body?.textContent.trim().length>0&&String(s.section_id)===d.body.dataset.sectionId&&(!args.id||s.section_id===args.id)&&(!args.token||d.body.dataset.sectionToken!==args.token);}""",arg={'id':section_id,'token':previous_token})
 except Exception:
  try:report.setdefault('wait_failures',[]).append(navigation_snapshot(page,section_id))
  except Exception:pass
  raise

def document_token(page):return content(page).locator('body').get_attribute('data-section-token')

def settle(page):
 wait_reader(page)
 page.wait_for_timeout(150) # Reader's80ms resize debounce, after actual document readiness.

def assert_passage(page,anchor,label,vertical=True):
 check=reader(page).locator('body').evaluate("""(element,anchor)=>{const d=document.getElementById('readingFrame').contentDocument,r=ReaderPagination.target(d,anchor),box=r?.getBoundingClientRect(),walk=d.createTreeWalker(d.body,4);let n,total=0;while((n=walk.nextNode()))total+=n.length;return {anchor,found:!!r,rect:box?{left:box.left,top:box.top,right:box.right,bottom:box.bottom}:null,width:d.documentElement.clientWidth,height:d.documentElement.clientHeight,text_length:total,document_token:d.body.dataset.sectionToken,document_section:d.body.dataset.sectionId,reader:ReaderNarration.page(),visible:!!box&&box.left>=-2&&box.left<d.documentElement.clientWidth,vertical_visible:!!box&&box.top>=-2&&box.top<d.documentElement.clientHeight};}""",anchor)
 report.setdefault('anchor_checks',[]).append({'stage':label,**check})
 assert check['found'],f'{label}: anchor did not resolve: {json.dumps(check)}'
 assert check['visible'] and (not vertical or check['vertical_visible']),f'{label}: saved passage moved outside its page: {json.dumps(check)}'


def wait_saved_passage(page,bid,section_id,anchor):
 # A page-start anchor can change during reflow. Require both locators to
 # resolve on the SAME visible page, then observe a stable acknowledged save.
 deadline=time.monotonic()+20;stable_since=None;previous=None;history=[]
 while time.monotonic()<deadline:
  saved=page.request.get(BASE+f'/api/books/{bid}/progress',headers={'Cache-Control':'no-cache'}).json()
  check=reader(page).locator('body').evaluate("""(element,args)=>{const d=document.getElementById('readingFrame').contentDocument,w=d.documentElement.clientWidth;const visible=a=>{const r=ReaderPagination.target(d,a)?.getBoundingClientRect();return !!r&&r.left>=-2&&r.left<w;};return {reader:ReaderNarration.page(),pending_scroll_debounce_ms:Math.max(0,800-(Date.now()-(window.__verificationLastScroll||0))),expected_visible:visible(args.expected),saved_visible:visible(args.saved)};}""",{'expected':anchor,'saved':saved.get('anchor')})
  sample={'expected_anchor':anchor,'saved':saved,**check,'pending_writes':len(pending_writes),'last_write':progress_writes[-1] if progress_writes else None}
  if not history or history[-1]['saved']!=saved or history[-1]['reader']!=check['reader']:history.append(sample)
  report['save_diagnostics']={'expected_section':section_id,'expected_anchor':anchor,'samples':history,'progress_writes':progress_writes}
  valid=(saved.get('section_id')==section_id and saved.get('anchor','').startswith('text:') and int(saved['anchor'].split(':')[1])>0 and saved.get('fraction',0)>0 and check['reader']['section_id']==section_id and check['expected_visible'] and check['saved_visible'] and not pending_writes and check['pending_scroll_debounce_ms']==0)
  signature=(saved.get('version'),saved.get('anchor'),saved.get('fraction'))
  if valid and signature==previous:
   if stable_since is not None and time.monotonic()-stable_since>=1.2:return saved
  else:stable_since=time.monotonic() if valid else None
  previous=signature;page.wait_for_timeout(250)
 raise AssertionError('Nonzero passage was not durably saved on its intended page: '+json.dumps(report['save_diagnostics']))

progress_writes=[];pending_writes=set()
def track_progress_request(request):
 if request.method=='PUT' and request.url.split('?')[0].endswith('/progress'):
  pending_writes.add(request);progress_writes.append({'event':'request','time':time.time(),'payload':request.post_data_json})
def track_progress_response(response):
 if response.request in pending_writes:
  progress_writes.append({'event':'response','time':time.time(),'status':response.status,'payload':response.json()});pending_writes.discard(response.request)
def track_progress_failure(request):
 if request in pending_writes:
  progress_writes.append({'event':'failure','time':time.time(),'error':request.failure});pending_writes.discard(request)

def ready_narration(page,books,preferred_book):
 # Pagination may use an EPUB edition while audio is cached for its webnovel
 # edition. Probe only existing chapters; never submit generation jobs here.
 ordered=sorted(books,key=lambda b:b['id']!=preferred_book)
 probes=[]
 for book in ordered:
  items=page.request.get(BASE+f"/api/books/{book['id']}/sections?limit=10").json()['items']
  for section in items:
   audio=page.request.get(BASE+f"/api/books/{book['id']}/audio?section_id={section['id']}&voice=en-US-AndrewNeural&rate=0").json()
   probes.append({'book_id':book['id'],'section_id':section['id'],'title':section['title'],'status':audio.get('status'),'complete':audio.get('complete'),'asset_id':audio.get('asset_id')})
   report['cache_probes']=probes
   if audio.get('status')=='ready' and audio.get('complete') and audio.get('url'):
    response=page.request.get(BASE+audio['url'],headers={'Range':'bytes=0-1023'})
    assert response.status==206 and response.headers.get('content-type','').startswith('audio/mpeg'),'Cached audio must support valid MP3 range playback'
    assert len(response.body())>0,'Cached audio is empty'
    report['audio_cache_selection']={**probes[-1],'voice':'en-US-AndrewNeural','rate':0,'range_status':response.status}
    return book['id'],section['id']
 raise AssertionError('No complete existing Andrew/rate0 staging cache: '+json.dumps(probes))

def right(page):content(page).locator('body').press('ArrowRight');settle(page)

try:
 with sync_playwright() as p:
  browser=p.chromium.launch()
  context=browser.new_context(viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
  page=context.new_page();page.on('request',track_progress_request);page.on('response',track_progress_response);page.on('requestfailed',track_progress_failure);page.on('pageerror',lambda e:report['console_errors'].append(str(e)))
  page.goto(BASE);assert 'player.js?v=20261012-pages' in page.content(),'Wrong candidate assets';books=page.request.get(BASE+'/api/books').json()['items'];bid=os.environ.get('WEBNOVEL_TEST_BOOK') or next(b['id'] for b in books if b['section_count']>=2)
  sections=page.request.get(BASE+f'/api/books/{bid}/sections?limit=10').json()['items'];sid=int(os.environ.get('WEBNOVEL_TEST_SECTION') or sections[0]['id']);report.update(book_id=bid,section_id=sid)
  for width in [375,390,430,1440]:
   stage('pagination '+str(width));page.set_viewport_size({'width':width,'height':844 if width<600 else 900});page.goto(BASE+'/?view='+f'/reader/{bid}?section={sid}')
   wait_reader(page,sid);assert state(page)['mode']=='pages';assert 'reader.js?v=20261012-anchors2' in reader(page).locator('head').inner_html(),'Wrong anchor-fix reader assets'
   settle(page);frame=content(page);frame.locator('body').press('Home')
   # Explicit canonical section hint; start from first page through existing contents UI.
   r=reader(page);old_token=document_token(page);r.locator('#dockContents').click();r.locator('#sectionList button').first.click();wait_reader(page,sections[0]['id'],old_token);settle(page)
   dimensions=frame.locator('body').evaluate('()=>({w:document.documentElement.clientWidth,h:document.documentElement.clientHeight,extent:document.scrollingElement.scrollWidth,vertical:document.scrollingElement.scrollHeight,left:document.scrollingElement.scrollLeft})')
   assert dimensions['extent']>dimensions['w']*1.5,'Need an existing long chapter to exercise real columns'
   assert dimensions['vertical']<=dimensions['h']+2,'Unexpected vertical chapter scrolling'
   text=frame.locator('body').text_content();right(page);after=frame.locator('body').evaluate('()=>document.scrollingElement.scrollLeft');assert after>=dimensions['w']-2
   assert frame.locator('body').text_content()==text,'Page turn mutated text'
   audit=r.locator('body').evaluate("""()=>{const d=document.getElementById('readingFrame').contentDocument,s=d.scrollingElement,w=d.documentElement.clientWidth,count=ReaderPagination.pageCount(s.scrollWidth,w),saved=s.scrollLeft,checked=[];try{for(let i=0;i<count;i++){s.scrollLeft=i*w;const anchor=ReaderPagination.anchor(d),target=ReaderPagination.target(d,anchor),box=target?.getBoundingClientRect();checked.push({page:i+1,anchor,left:s.scrollLeft,visible:!!box&&box.left>=-2&&box.left<w&&box.top>=-2&&box.top<d.documentElement.clientHeight});}}finally{s.scrollLeft=saved;}return checked;}""")
   report.setdefault('page_audits',{})[str(width)]=audit
   assert all(x['visible'] for x in audit),f'Blank or unresolved text column: {[x for x in audit if not x["visible"]]}'
   assert all(int(a['anchor'].split(':')[1])<int(b['anchor'].split(':')[1]) for a,b in zip(audit,audit[1:])), 'Page text locations repeated or moved backward'

   before=state(page)['anchor'];assert before.startswith('text:'),f'No visible text anchor after page turn: {state(page)}';assert_passage(page,before,'page turn '+str(width))
   token_before_font=document_token(page);r.locator('#dockSettings').click();old_font=float(r.locator('#fontSize').input_value());r.locator('#fontSize').press('ArrowLeft' if old_font>=32 else 'ArrowRight');font=float(r.locator('#fontSize').input_value());r.locator('#closeSettings').click();settle(page)
   assert font!=old_font,'Font test must change actual font size'
   assert document_token(page)==token_before_font,'Changing font unexpectedly replaced the chapter'
   assert content(page).locator('body').evaluate('(b)=>parseFloat(getComputedStyle(b).fontSize)')==font,'Font change has not reached the reading document'
   assert frame.locator('body').text_content()==text,'Font reflow mutated chapter text'
   # The previous stable text passage must remain on the current rendered page.
   assert_passage(page,before,'font reflow '+str(width))
   page.set_viewport_size({'width':844,'height':width});settle(page)
   assert_passage(page,before,'orientation '+str(width),vertical=False)
   assert frame.locator('body').text_content()==text,'Orientation reflow mutated chapter text'
   page.set_viewport_size({'width':width,'height':844 if width<600 else 900});settle(page)
   assert r.locator('body').evaluate('(b)=>b.scrollWidth<=innerWidth+1');assert page.locator('body').evaluate('(b)=>b.scrollWidth<=innerWidth+1')
   page.screenshot(path=str(OUT/f'pages-reader-{width}.png'));report['viewports'].append(width)
   stage('saved NONZERO passage '+str(width));old_token=document_token(page);r.locator('#dockContents').click();r.locator('#sectionList button').first.click();wait_reader(page,sections[0]['id'],old_token);settle(page)
   r.locator('body').evaluate("()=>{window.__verificationLastScroll=Date.now();document.getElementById('readingFrame').contentDocument.addEventListener('scroll',()=>{window.__verificationLastScroll=Date.now();},{passive:true});}")
   right(page)
   nonzero=state(page)['anchor'];assert nonzero.startswith('text:') and int(nonzero.split(':')[1])>0,'Reload test must save a nonzero passage'
   pre_reload=navigation_snapshot(page,sections[0]['id']);assert pre_reload['page_index']>0 and pre_reload['scroll_left']>0,'Reload test is still on first page'
   saved=wait_saved_passage(page,bid,sections[0]['id'],nonzero)
   page.reload();wait_reader(page,saved['section_id']);settle(page)
   post_reload=navigation_snapshot(page,saved['section_id']);assert post_reload['page_index']>0 and post_reload['scroll_left']>0,'Reload reset to first page'
   report.setdefault('nonzero_reload_checks',{})[str(width)]={'saved':saved,'before':pre_reload,'after':post_reload}
   assert state(page)['mode']=='pages'
   assert_passage(page,saved['anchor'],'reload saved '+str(width),vertical=False)
   assert_passage(page,nonzero,'reload intended NONZERO '+str(width),vertical=False)
   reopened=page.request.get(BASE+f'/api/books/{bid}/progress').json();assert reopened['section_id']==saved['section_id'] and reopened['anchor'].startswith('text:') and int(reopened['anchor'].split(':')[1])>0 and reopened['fraction']>0,'Reload overwrote nonzero progress'
   report['nonzero_reload_checks'][str(width)]['reopened']=reopened
  stage('swipe and chapter boundary');page.set_viewport_size({'width':390,'height':844});r=reader(page);old_token=document_token(page);r.locator('#dockContents').click();r.locator('#sectionList button').first.click();wait_reader(page,sections[0]['id'],old_token);settle(page)
  before_left=content(page).locator('body').evaluate('()=>document.scrollingElement.scrollLeft')
  outer=page.locator('#workspace').bounding_box();inner=r.locator('#readingFrame').bounding_box();x=outer['x']+inner['x']+290;y=outer['y']+inner['y']+inner['height']/2
  cdp=context.new_cdp_session(page);cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]});cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x-150,'y':y}]});cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]});settle(page)
  assert content(page).locator('body').evaluate('()=>document.scrollingElement.scrollLeft')>before_left,'Swipe did not turn page';report['swipe_left']=True
  cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x-150,'y':y}]});cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y}]});cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]});settle(page);assert abs(content(page).locator('body').evaluate('()=>document.scrollingElement.scrollLeft')-before_left)<2,'Right swipe did not restore page';report['swipe_right']=True
  # Use the reader's own rendered page count; native scrollLeft may clamp below
  # the nominal grid. Turn with the already-tested touch gesture, not a locator
  # press that can focus/scroll the chapter body back toward its beginning.
  current_id=state(page)['section_id'];current=page.request.get(BASE+f'/api/books/{bid}/sections/{current_id}').json();expected_next=current['next_id'];assert expected_next is not None
  r.locator('body').evaluate('()=>{const d=document.getElementById("readingFrame").contentDocument,s=d.scrollingElement,w=d.documentElement.clientWidth;s.scrollLeft=(ReaderPagination.pageCount(s.scrollWidth,w)-1)*w;}');settle(page)
  boundary=navigation_snapshot(page,expected_next);report['boundary_before']=boundary
  assert boundary['at_native_end'] and boundary['page_index']==boundary['page_count']-1,'Not at the true final rendered page'
  old_token=document_token(page)
  cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]});cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x-150,'y':y}]});cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
  wait_reader(page,expected_next,old_token);settle(page);report['boundary_after']=navigation_snapshot(page,expected_next)
  assert report['boundary_after']['actual_section_id']==expected_next and report['boundary_after']['page_index']==0;report['forward_chapter_boundary']=True
  old_token=document_token(page)
  cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x-150,'y':y}]});cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y}]});cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
  wait_reader(page,current_id,old_token);settle(page);back=navigation_snapshot(page,current_id);report['backward_boundary']=back
  assert back['actual_section_id']==current_id and back['page_index']==back['page_count']-1;report['backward_chapter_boundary']=True
  stage('cached narration and floating pill');audio_bid,audio_sid=ready_narration(page,books,bid)
  prefs=page.request.get(BASE+'/api/edge/preferences').json();assert prefs['voice']=='en-US-AndrewNeural' and prefs['rate']==0,'Staging global voice/rate must match selected ready cache'
  page.locator('body').evaluate('(element,route)=>Player.navigate(route)',f'/reader/{audio_bid}?section={audio_sid}')
  wait_reader(page,audio_sid);settle(page);r=reader(page)
  assert r.locator('body').get_attribute('data-book-id')==audio_bid,'Wrong cached book opened'
  r.locator('#dockListen').click();page.wait_for_function('()=>{const a=document.getElementById("bookAudio");return !a.paused&&a.currentTime>.1}',timeout=15000)
  assert page.evaluate('()=>Player.metrics().section_id')==audio_sid,'Playback selected wrong cached section'
  page.locator('details.player-settings summary').click();page.locator('#playbackSpeed').select_option('2');assert page.locator('#bookAudio').evaluate('(a)=>a.playbackRate')==2
  page.locator('#closePlayer').click();metrics=page.evaluate('()=>Player.metrics()');source=page.locator('#bookAudio').get_attribute('src');page.evaluate('()=>Player.navigate("/library")');page.wait_for_timeout(500)
  assert page.locator('#playerPanel').is_visible();assert page.locator('.transport').is_hidden();assert page.locator('#miniPlay').is_visible();assert page.locator('#closePlayer').is_visible();assert page.locator('#playerPanel').bounding_box()['height']<=66;assert page.locator('audio').count()==1
  page.screenshot(path=str(OUT/'pages-floating-pill.png'));page.locator('#miniOpen').click();wait_reader(page,metrics['section_id']);page.wait_for_timeout(800)
  assert page.evaluate('()=>Player.metrics().section_id')==metrics['section_id'];assert page.locator('#bookAudio').get_attribute('src')==source;assert page.locator('#bookAudio').evaluate('(a)=>!a.paused&&a.playbackRate===2')
  content(page).locator('[data-narration-mark]').first.wait_for(timeout=10000);report['floating_title_returns_to_narration']=True;report['highlighting']=True
  reader(page).locator('#dockListen').click();page.locator('#playPause').click();assert page.locator('#bookAudio').evaluate('(a)=>a.paused');page.locator('#playPause').click();page.wait_for_function('()=>!document.getElementById("bookAudio").paused')
  page.locator('#closePlayer').click();assert page.locator('#bookAudio').evaluate('(a)=>!a.paused');report['x_preserves_playback']=True
  page.screenshot(path=str(OUT/'pages-narration.png'));report['passed']=not report['console_errors'];report['browser_verified']=report['passed'];stage('completed');context.close();browser.close()
except Exception as e:
 report['progress_writes']=progress_writes;report['pending_progress_writes']=len(pending_writes)
 report.update(error_type=type(e).__name__,error=str(e),traceback=traceback.format_exc())
 if page:
  try:
   page.screenshot(path=str(OUT/'pages-failure.png'));report['player_metrics']=page.evaluate('()=>Player?.metrics()');report['reader_state']=state(page);report['navigation_failure']=navigation_snapshot(page); report['document_token']=document_token(page);report['document_metrics']=content(page).locator('body').evaluate('(b)=>({text_length:b.textContent.length,width:document.documentElement.clientWidth,height:document.documentElement.clientHeight,scroll_left:document.scrollingElement.scrollLeft,scroll_width:document.scrollingElement.scrollWidth,section:b.dataset.sectionId,font:getComputedStyle(b).fontSize})')
  except Exception:pass
finally:
 (OUT/'paginated-browser.json').write_text(json.dumps(report,indent=2));print(json.dumps(report,indent=2))
if not report['passed']:raise SystemExit(1)
