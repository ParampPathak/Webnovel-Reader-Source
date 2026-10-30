/* Persistent shell player: workspace navigation never replaces this audio element. */
'use strict';
(() => {
 const $=id=>document.getElementById(id), audio=$('bookAudio');
 let bookId,book,chapter,asset,version=0,voice='en-US-AndrewNeural',synthesisRate=0,token=0,poll,sleep,lastSave=0,conflict=false;
 const passageCache=new Map();
 let sliderDragging=false, sliderTotal=0, sleepDeadline=0, passageKey="",narrationTimelineComplete=true;
 let stopped=false,stoppedSeconds=0;
 let saveQueue=Promise.resolve(),signature='',pendingSeek=0,wantPlay=false,opening=false,lastAudioState,seekOverride=null,narrationEngaged=false;
 let lastKnownTime=0,pendingCursor=null,metadataRevision=-1,pendingListeningWrite=null,nativeSeek=null,nativeSeekTimer;
 const segmentSeconds=()=>stopped?stoppedSeconds:asset&&(audio.error||audio.readyState===0||audio.seeking||nativeSeek||(mediaState==='interrupted'&&audio.currentTime===0&&lastKnownTime>0))?lastKnownTime:(audio.currentTime||0);
 let progressive=false,segments=[],segmentIndex=0,buffering=false,requestedToken=0,audioRequestRevision=0,audioOperation=null,generationOperation=null,networkRetries=0,lastLocalSave=0;
 let preferencesQueue=Promise.resolve(),preferenceRevision=0;
 let mediaState='paused',sourceRevision=0,endHandler,recoveryTimer,recoveryAttempts=0,playAttempt=0,completedSource=-1,chapterTransition=null,sourceReloadAttempts=0;
 let finishingSource=-1,transitionSequence=0;
 let foregroundTimer,wasBackground=false,foregroundFollowPending=false,mediaObservation=null,pendingPlay=null;
 const transitionLog=[];
 function diagnostic(event,reason=''){
  transitionLog.push({event,reason,at:Date.now(),book_id:bookId,section_id:chapter?.id,segment_index:progressive?segmentIndex:null,seconds:Number.isFinite(audio.currentTime)?audio.currentTime:null,duration:Number.isFinite(audio.duration)?audio.duration:null,session:token,source:sourceRevision,sequence:transitionSequence,seek:seekRevision,paused:audio.paused,ended:audio.ended,ready_state:audio.readyState,error_code:audio.error?.code||0,media_state:mediaState,intent:wantPlay,engaged:narrationEngaged,following:followEnabled&&!followSuspended,hidden:document.hidden,metadata_pending:metadataRevision!==sourceRevision,reload_attempts:sourceReloadAttempts});
  if(transitionLog.length>200)transitionLog.shift();
 }
 function stopSequence(message,reason){diagnostic('sequence-rejected',reason);userPause('sequence-error');playbackState('error');status(message);checkpoint();}
 function validateSegments(result){
  const list=result.segments||[];
  if(!list.every((s,i)=>(s.index===undefined||s.index===i)&&Number.isFinite(s.duration)&&s.duration>0&&(s.url===undefined||typeof s.url==='string'&&s.url.length>0)))throw Object.assign(new Error('Audio segment sequence is incomplete. Playback stopped; retry the affected chapter.'),{name:'SequenceError'});
  if(progressive&&(result.asset_id!==asset?.asset_id||list.length<segments.length||segments.some((s,i)=>s.url!==list[i]?.url||s.duration!==list[i]?.duration)))throw Object.assign(new Error('Published audio sequence changed. Playback stopped at its saved position.'),{name:'SequenceError'});
 }
 function validSourceEnd(){
  const expected=progressive?segments[segmentIndex]?.duration:narrationTimelineComplete&&narrationSegments.length?narrationSegments.reduce((n,s)=>n+s.duration,0):0;
  const duration=Math.max(Number.isFinite(audio.duration)?audio.duration:0,expected||0);
  return duration>0&&Number.isFinite(audio.currentTime)&&audio.currentTime>=duration-.35;
 }
 function recoverIncompleteEnd(){
  diagnostic('unexpected-discontinuity','premature-eof');
  if(sourceReloadAttempts++>=2){stopSequence('Audio ended before the segment was complete. Playback stopped at the saved position. Press Play to retry.','incomplete-segment');return;}
  pendingSeek=segmentSeconds();loadAudio(audio.src);status('Incomplete audio response. Retrying the same segment at the saved position…');
 }
 const progressVersions=new Map(),progressSignatures=new Map(),readingVersions=new Map();
 const writers=new Map();let writerCounter=0,checkpointIntent='restore';
 function prepareWriter(id,progress,claim){
  const prior=writers.get(id),owner=progress.session_id||'',generation=progress.session_generation||0;
  if(prior&&((owner===prior.id&&generation===prior.generation)||(prior.claim&&owner===prior.expectedOwner&&generation+1===prior.generation)))return prior;
  if(!claim)return null;
  const writer={id:globalThis.crypto?.randomUUID?.()||Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)+'-'+(++writerCounter),
   generation:generation+1,sequence:0,queuedSequence:0,claim:true,expectedOwner:owner,baseVersion:progress.version,signature:''};
  // Publish explicit local ownership separately: concurrent offline claims may
  // share a server generation until either request is acknowledged.
  try{localStorage.setItem('webnovelPlaybackOwner:'+id,JSON.stringify({id:writer.id,generation:writer.generation}));}catch{}
  writers.set(id,writer);diagnostic('checkpoint-session-created','explicit-playback');return writer;
 }
 let narrationBase=null,narrationMarkKey='',trackingFrame=null,narrationPending=null,openingTask=Promise.resolve();
 let narrationLocation=null,narrationKey='',followEnabled=false,followSuspended=false,narrationSegments=[],seekRevision=0,readerStart=null,readerStartMapped=false,readingActivationPending=false,pausedReaderPosition=null,headphoneStarting=false,pendingChapterSeek=null,seekTarget=null;
 function narrationIndex(){if(progressive)return segmentIndex;if(!asset?.asset_id?.startsWith('legacy-')&&(!narrationSegments.length||!narrationTimelineComplete&&segmentSeconds()>=narrationSegments.reduce((n,s)=>n+s.duration,0)))return null;let seconds=segmentSeconds(),index=0;while(index<narrationSegments.length-1&&seconds>=narrationSegments[index].duration){seconds-=narrationSegments[index].duration;index++;}return index;}
 function narrationSeconds(){return progressive?segmentSeconds():segmentSeconds()-narrationSegments.slice(0,narrationIndex()).reduce((n,item)=>n+item.duration,0);}
 function reader(){try{const r=$('workspace').contentWindow?.ReaderNarration;return r?.bookId===bookId?r:null;}catch{return null;}}
 function playing(id=bookId){return id===bookId&&mediaState==='playing'&&wantPlay&&!opening&&!buffering&&!audio.paused&&!audio.ended&&!audio.error&&!audio.seeking&&!nativeSeek;}
 function nativePlaying(){return !!asset&&narrationEngaged&&!opening&&!buffering&&!conflict&&!audio.paused&&!audio.ended&&!audio.error&&!audio.seeking&&!nativeSeek&&audio.readyState>=3;}
 const waitingForMetadata=()=>!!asset&&!opening&&!audio.error&&audio.readyState===0&&metadataRevision!==sourceRevision;
 function scheduleForegroundCheck(){
  clearTimeout(foregroundTimer);
  if(document.hidden||!narrationEngaged||!asset||opening||(!wantPlay&&audio.paused))return;
  foregroundTimer=setTimeout(()=>{reconcileMedia('foreground-health');},1500);
 }
 function reconcileMedia(reason){
  if(!asset||opening||!narrationEngaged||conflict)return;
  checkpoint();if(conflict)return; // Keep the existing cross-tab owner fence authoritative.
  if(nativePlaying()){
   const changed=mediaState!=='playing'||!wantPlay;
   wantPlay=true;
   const now=Date.now(),seconds=audio.currentTime,prior=mediaObservation;
   if(!prior||prior.source!==sourceRevision||prior.session!==token||Math.abs(seconds-prior.seconds)>.01)mediaObservation={source:sourceRevision,session:token,seconds,at:now};
   const stalled=mediaObservation&&now-mediaObservation.at>8000;
   playbackState(stalled?'stalled':'playing');
   if(stalled)status('Audio is not advancing. Pause and tap Play to retry at the saved position.');
   else {clearTimeout(recoveryTimer);if(changed)status(progressive&&!asset.complete?'Playing — generating further segments.':'Playing');publishNarration(true);}
   if(foregroundFollowPending&&!document.hidden&&!stalled){
    foregroundFollowPending=false;followEnabled=true;followSuspended=false;syncReaderPlayback();
    reader()?.follow?.()?.catch?.(()=>status('Tap Back to Read Aloud to restore the reading view.'));
   }
   if(changed)diagnostic('media-reconciled',reason);
  }else if(audio.ended){
   if(wantPlay)endHandler?.();else playbackState('completed');
  }else if(audio.error){playbackState('interrupted');
  }else if(audio.paused){
   // load() resets paused before metadata arrives; that is not a pause request.
   if(wantPlay&&waitingForMetadata()){if(mediaState!=='buffering')diagnostic('source-loading','awaiting-native-metadata');playbackState('buffering');}
   // Reconciliation never starts media. A real interruption needs a user gesture.
   else if(!buffering&&!audio.seeking&&!nativeSeek){diagnostic('playback-paused','native-state-origin-unknown');wantPlay=false;++playAttempt;clearTimeout(recoveryTimer);playbackState('paused');publishNarration(true);}
  }else if(!audio.seeking&&!nativeSeek&&mediaState!=='buffering'){playbackState('buffering');recoverStalledMedia();}
  updateMediaPosition();syncReaderPlayback();mediaSymbols();scheduleForegroundCheck();
 }
 function foreground(){
  checkSleep();
  if(wasBackground){wasBackground=false;foregroundFollowPending=narrationEngaged&&(nativePlaying()||wantPlay&&(opening||buffering||waitingForMetadata()||audio.ended||audio.seeking||nativeSeek||['recovering','buffering'].includes(mediaState)));mediaObservation=null;}
  reconcileMedia('foreground-return');checkpoint();save(true);
 }
 function pauseControl(){return nativePlaying()||wantPlay&&!['paused','completed','interrupted','error'].includes(mediaState);}
 function syncReaderPlayback(){document.body.dataset.readerAudio=String(narrationEngaged&&reader()?.bookId===bookId);$('playerPanel').hidden=!narrationEngaged;const total=progressive?availableDuration():audio.duration;try{const doc=$('workspace').contentDocument;if(doc?.body)doc.body.dataset.listening=String(narrationEngaged);$('workspace').contentWindow?.ReaderNarration?.playback?.({book_id:bookId,playing:playing(),state:mediaState,intent:wantPlay,control_pause:pauseControl(),engaged:narrationEngaged,following:followEnabled,follow_suspended:followSuspended,title:book?.title,chapter_title:chapter?.title,cover_url:book?.cover_url,progress:total>0?Math.min(1000,chapterSeconds()/total*1000):0});}catch{}}
 function sameReading(a,b){return a&&b&&a.section_id===b.section_id&&a.anchor===b.anchor&&a.manual_revision===b.manual_revision;}
 function applyNarrationClock(force=false){
  if(!narrationEngaged||!narrationBase||narrationBase.section_id!==chapter?.id||narrationBase.segment_index!==narrationIndex())return;
  const cues=narrationBase.cues||[],seconds=narrationSeconds();let cue=null,index=-1;
  for(let i=0;i<cues.length&&cues[i].at<=seconds;i++){cue=cues[i];index=i;}
  const key=narrationKey+':'+index;
  if(key===narrationMarkKey&&!force)return;narrationMarkKey=key;
  narrationLocation=cue?{...narrationBase,cues:undefined,before:narrationBase.quote.slice(Math.max(0,cue.offset-140),cue.offset),after:narrationBase.quote.slice(cue.offset+cue.quote.length,cue.offset+cue.quote.length+160),quote:cue.quote,normalized_offset:narrationBase.normalized_offset+cue.offset,anchor:narrationBase.anchor+':word:'+index,precision:'word'}:{...narrationBase,cues:undefined};
  reader()?.update(narrationLocation);renderPassages();
 }
 function publishNarration(force=false){
  if(!narrationEngaged||!chapter||!asset||opening||audio.seeking||nativeSeek)return;const key=asset.asset_id+':'+narrationIndex()+':'+seekRevision+':'+sourceRevision;
  if(narrationIndex()===null){narrationBase=null;narrationLocation=null;narrationKey='';reader()?.clear?.();return;}
  if(key!==narrationKey){
   narrationKey=key;narrationBase=null;narrationLocation=null;narrationMarkKey='';narrationPending=null;
   if(asset.asset_id.startsWith('legacy-')){narrationBase={book_id:bookId,section_id:chapter.id,segment_index:0,quote:'',anchor:'',fraction:0,precision:'chapter'};applyNarrationClock(force);}
   else{const session=token,source=sourceRevision,index=narrationIndex(),section=chapter.id,id=bookId;
    narrationPending=api('/api/audio/'+asset.asset_id+'/location/'+index).then(location=>{
     if(!narrationEngaged||session!==token||source!==sourceRevision||key!==narrationKey||opening||audio.seeking||nativeSeek)return;
     if(location.book_id!==id||location.section_id!==section)throw new Error('Narration location does not match the active chapter.');
     narrationBase={...location,segment_index:index};applyNarrationClock(force);
    }).catch(()=>{if(key===narrationKey){narrationKey='';narrationBase=null;narrationLocation=null;}});
   }
  }else if(narrationBase)applyNarrationClock(force);
  return narrationPending;
 }
 async function resolveNarration(id){
  await openingTask;
  if(id!==bookId||!narrationEngaged||opening)return null;
  for(let attempt=0;attempt<3;attempt++){
   const session=token,source=sourceRevision,seek=seekRevision;
   await publishNarration();
   if(id!==bookId||!narrationEngaged)return null;
   if(session===token&&source===sourceRevision&&seek===seekRevision){applyNarrationClock();diagnostic('foreground-reconciled','actual-media');return narrationLocation||(!audio.seeking&&!nativeSeek&&asset?{book_id:bookId,section_id:chapter.id,quote:'',anchor:'',fraction:0,precision:'chapter'}:null);}
  }
  return null;
 }
 function trackMedia(){trackingFrame=null;if(!playing())return;publishNarration();trackingFrame=window.requestAnimationFrame?.(trackMedia);}

 function mediaSymbols(){const pause=pauseControl();for(const id of ['playPause','miniPlay']){$(id).textContent=pause?'Ⅱ':'▶';$(id).setAttribute('aria-label',pause?'Pause narration':'Play narration');}}
 function playbackState(state){const started=state==='playing'&&mediaState!=='playing';mediaState=state;if(state==='playing'&&trackingFrame===null)trackingFrame=window.requestAnimationFrame?.(trackMedia);if(state==='playing'&&!audio.paused)readingActivationPending=false;syncReaderPlayback();if(started)publishNarration(true);mediaSymbols();if('mediaSession' in navigator)navigator.mediaSession.playbackState=state==='playing'?'playing':'paused';scheduleForegroundCheck();}
 function checkpoint(){
  if(stopped||!bookId||!chapter||opening)return;
  // A passive remount may inspect a checkpoint, but must not erase its ordered provenance.
  if(!writers.has(bookId)&&savedCheckpoint(bookId)?.writer)return;
  // A fenced-out tab must not erase a newer tab's unacknowledged local cursor.
  const local=savedCheckpoint(bookId),currentWriter=writers.get(bookId);
  let owner;try{owner=JSON.parse(stored('webnovelPlaybackOwner:'+bookId)||'null');}catch{}
  if(currentWriter&&(local?.bookId===bookId&&local.writer?.generation>currentWriter.generation||owner&&owner.id!==currentWriter.id&&owner.generation>=currentWriter.generation)){if(!conflict){conflict=true;wantPlay=false;diagnostic('stale-event-rejected','local-session-fence');playbackState('paused');if(!audio.paused)pauseMedia('local-session-fence');status('Listening position changed in another player. Reload to resume.');}return;}
  if(asset&&!audio.error&&audio.readyState!==0&&!audio.seeking&&!nativeSeek)lastKnownTime=segmentSeconds();
  try{
   const location=narrationLocation?.section_id===chapter.id&&narrationLocation.segment_index===narrationIndex()?narrationLocation:null;
   const point={bookId,sectionId:chapter.id,assetId:asset?.asset_id||pendingCursor?.assetId,seconds:pendingChapterSeek??chapterSeconds(),segmentIndex:pendingChapterSeek!==null?null:asset?narrationIndex():(pendingCursor?.index||0),segmentSeconds:pendingChapterSeek!==null?null:asset?narrationSeconds():(pendingCursor?.seconds||0),voice,rate:synthesisRate,playbackRate:playbackRate||audio.playbackRate,state:mediaState,narration:location,readingStart:readingActivationPending?readerStart:null,engaged:narrationEngaged,wantPlay,followEnabled,followSuspended,session:token,sourceRevision,intent:checkpointIntent};
   const writer=writers.get(bookId),key=JSON.stringify(point);
   if(writer&&writer.signature!==key){writer.signature=key;writer.sequence++;}
   const saved=JSON.stringify({...point,writer:writer?{id:writer.id,generation:writer.generation,sequence:writer.sequence,claim:writer.claim,expectedOwner:writer.expectedOwner,baseVersion:writer.baseVersion}:null,revision:Date.now(),serverVersion:progressVersions.get(bookId)||0,pendingWrite:pendingListeningWrite?.book_id===bookId?pendingListeningWrite:null,savedAt:Date.now()});
   localStorage.setItem('webnovelPlaybackCheckpoint:'+bookId,saved);localStorage.setItem('webnovelPlaybackCheckpoint',saved);
  }catch{}
 }
 function savedCheckpoint(id){try{return JSON.parse(stored('webnovelPlaybackCheckpoint:'+id)||stored('webnovelPlaybackCheckpoint')||'null');}catch{return null;}}
 function checkpointVersionMatches(saved,progress,id){
  if(!saved||saved.bookId!==id)return false;
  const writer=saved.writer;
  if(writer){
   if(progress.session_id===writer.id&&progress.session_generation===writer.generation&&progress.checkpoint_revision===progress.version)
    return writer.sequence>=progress.event_sequence;
   return writer.claim&&progress.version>=writer.baseVersion&&(progress.session_id||'')===writer.expectedOwner&&(progress.session_generation||0)+1===writer.generation;
  }
  if(progress.session_id)return false;
  if(saved.serverVersion===undefined)return progress.version===0;
  if(saved.serverVersion===progress.version)return true;
  const pending=saved.pendingWrite;return pending?.book_id===id&&pending.version===progress.version&&['section_id','asset_id','voice','synthesis_rate','segment_index','segment_seconds'].every(key=>pending[key]===progress[key])&&Math.abs(pending.seconds-progress.seconds)<1e-6;
 }
 function pauseMedia(reason){diagnostic('pause-requested',reason);audio.pause();}
 function userPause(reason='user-pause'){foregroundFollowPending=false;checkpointIntent='pause';diagnostic('playback-paused',reason);pausedReaderPosition=reader()?.position?.()||reader()?.page?.();wantPlay=false;++playAttempt;cancelAudioRequest();clearTimeout(recoveryTimer);clearTimeout(nativeSeekTimer);playbackState('paused');pauseMedia(reason);publishNarration();checkpoint();save();}
 async function resumeMedia(userGesture=false){
  if(!wantPlay||opening||conflict)return;
  if(!settleNativeSeek())return;
  if(buffering||!asset){clearTimeout(poll);return waitForAudio(token);}
  if(audio.ended){if(userGesture)sourceReloadAttempts=0;return finishSource(token,sourceRevision);}
  if(nativePlaying()){reconcileMedia('already-playing');return;}
  if(pendingPlay?.session===token&&pendingPlay.source===sourceRevision&&pendingPlay.attempt===playAttempt&&!userGesture)return;
  const session=token,source=sourceRevision,attempt=++playAttempt;
  if(!writers.has(bookId)){
   if(!userGesture)return;
   const progress=await api(`/api/books/${bookId}/listening`);
   if(session!==token||source!==sourceRevision||attempt!==playAttempt||!wantPlay)return;
   if(progress.version!==(progressVersions.get(bookId)||0))return listen(bookId,null,true);
   progressVersions.set(bookId,progress.version);prepareWriter(bookId,progress,true);
  }
  playbackState('recovering');applyPlaybackRate();
  const pending={session,source,attempt};pendingPlay=pending;
  try{
   await audio.play();
   if(session!==token||source!==sourceRevision||attempt!==playAttempt)return;
   if(!wantPlay){pauseMedia('cancelled-play-attempt');return;}recoveryAttempts=0;diagnostic('segment-started',userGesture?'user-resume':'source-ready');playbackState('playing');publishNarration();checkpoint();
  }catch(error){
   if(session!==token||source!==sourceRevision||attempt!==playAttempt)return;
   if(nativePlaying()){reconcileMedia('late-play-rejection');return;}
   if(error.name==='NotAllowedError'){playbackState('interrupted');status('Playback needs a tap to resume at the saved position.');return;}
   playbackState('interrupted');status('Playback interrupted. Recovering at the same position…');
   if(wantPlay&&recoveryAttempts++<3)recoveryTimer=setTimeout(()=>{if(session===token&&source===sourceRevision)resumeMedia();},1000*recoveryAttempts);
   else status('Playback could not resume. Press Play to retry at the saved position.');
  }finally{if(pendingPlay===pending)pendingPlay=null;}
 }
 async function finishSource(session,source){
  if(session!==token||source!==sourceRevision){diagnostic('stale-event-rejected','ended');return;}
  if(finishingSource===source||!wantPlay||opening||!audio.ended||audio.error||nativeSeek||audio.seeking)return;
  if(completedSource===source&&progressive&&buffering&&!asset?.complete)return;
  if(!validSourceEnd()){recoverIncompleteEnd();return;}
  finishingSource=source;
  try{
  const seek=seekRevision;
  if(completedSource!==source){completedSource=source;diagnostic('segment-ended','validated-eof');save();}
  if(session!==token||source!==sourceRevision||seek!==seekRevision||!wantPlay)return;
  if(progressive){
   if(segments[segmentIndex+1]){loadSegment(segmentIndex+1);return;}
   if(!asset?.complete){buffering=true;playbackState('buffering');status('Buffering next audio segment…');clearTimeout(poll);await waitForAudio(session);return;}
  }
  await finishChapter(session);
  }finally{if(finishingSource===source)finishingSource=-1;}
 }
 async function finishChapter(session){
  if(session!==token||!wantPlay||opening||chapterTransition===session)return;
  if(completedSource!==sourceRevision||!audio.ended||nativeSeek||audio.seeking||!validSourceEnd()||(progressive&&(!asset?.complete||segmentIndex!==segments.length-1)))return;
  chapterTransition=session;
  try{
  // Re-read the canonical immediate neighbor, never pick an earlier cached section.
  const id=chapter.id,position=chapter.position,seek=seekRevision;
  diagnostic('chapter-completed','validated-last-segment');const current=await api(`/api/books/${bookId}/sections/${id}`);
  if(session!==token||chapter.id!==id||seek!==seekRevision||!wantPlay)return;
  if(current.next_id){
   const next=await api(`/api/books/${bookId}/sections/${current.next_id}`);
   if(session!==token||chapter.id!==id||seek!==seekRevision||!wantPlay)return;
   if(next.position<=current.position)throw Object.assign(new Error('Invalid chapter order. Playback stopped; select the next chapter manually.'),{name:'ChapterOrderError'});
   if(next.position!==current.position+1)throw Object.assign(new Error('Missing imported content between these chapters. Playback stopped; import it or select a chapter explicitly.'),{name:'ChapterOrderError'});
   diagnostic('chapter-transition-requested','completed-chapter');++transitionSequence;
   await listen(bookId,next.id,true,null,{seconds:0},'completed-chapter');
  }else{wantPlay=false;playbackState('completed');status('End of available imported content.');checkpoint();await save();}
  }finally{if(chapterTransition===session)chapterTransition=null;}
 }
 const availableDuration=()=>segments.reduce((n,s)=>n+s.duration,0);
 const segmentOffset=()=>segments.slice(0,segmentIndex).reduce((n,s)=>n+s.duration,0);
 const chapterSeconds=()=>!asset?(pendingSeek||0):progressive?segmentOffset()+segmentSeconds():segmentSeconds();
 // Prefetch at most one already-published segment, without another audio element.
 // A browser may ignore this hint; unpublished speech remains worker-generated.
 let segmentPrefetch=null,prefetchedSegment='';
 function prefetchPublishedSegment(){
  if(!document.head||!progressive)return;
  const next=segments[segmentIndex+1]?.url||'';
  if(next===prefetchedSegment)return;
  segmentPrefetch?.remove();segmentPrefetch=null;prefetchedSegment=next;
  if(!next)return;
  segmentPrefetch=document.createElement('link');segmentPrefetch.rel='prefetch';segmentPrefetch.as='audio';segmentPrefetch.href=next;document.head.appendChild(segmentPrefetch);
 }
 function loadSegment(index,seconds=0){if(!segments[index]?.url){stopSequence('Audio segment is missing. Playback stopped at the saved position. Press Play to retry.','missing-segment');return;}if(index!==segmentIndex)sourceReloadAttempts=0;segmentIndex=index;buffering=false;pendingSeek=seconds;loadAudio(segments[index].url);prefetchPublishedSegment();}
 function applyChapterSeek(seconds){const total=availableDuration();seconds=Math.max(0,Math.min(seconds,Math.max(0,total-.1)));let index=0;while(index<segments.length-1&&seconds>=segments[index].duration){seconds-=segments[index].duration;index++;}if(index===segmentIndex&&audio.readyState!==0){pendingSeek=seconds;armNativeSeek(seconds);audio.currentTime=seconds;lastKnownTime=seconds;settleNativeSeek();publishNarration();syncReaderPlayback();save();if(wantPlay&&audio.paused)resumeMedia();}else loadSegment(index,seconds);}
 function seekChapter(seconds){
  cancelAudioRequest();
  checkpointIntent='seek';
  diagnostic('user-seek-requested','chapter-timeline');
  ++seekRevision;completedSource=-1;seekTarget=Math.max(0,seconds);pendingChapterSeek=null;nativeSeek=null;clearTimeout(nativeSeekTimer);reader()?.seek?.();
  if(!asset?.complete&&seconds>=availableDuration()){
   pendingChapterSeek=seconds;buffering=true;pauseMedia('unpublished-seek');playbackState('buffering');status('Loading the requested listening position…');clearTimeout(poll);waitForAudio(token);return;
  }
  buffering=false;applyChapterSeek(seconds);
 }
 function stopReader(){
  if(stopped)return;
  stoppedSeconds=segmentSeconds();
  userPause('stop-listening'); // pause the media element before any async work
  checkpointIntent='stop';narrationEngaged=false;headphoneStarting=false;readingActivationPending=false;followEnabled=false;followSuspended=true;
  // Keep an unpublished seek from replacing the last audible position.
  pendingChapterSeek=null;nativeSeek=null;opening=false;
  checkpoint();save(true);
  stopped=true;++token;++sourceRevision;++seekRevision;++playAttempt;
  cancelAudioRequest();clearTimeout(poll);clearTimeout(recoveryTimer);clearTimeout(nativeSeekTimer);clearTimeout(foregroundTimer);clearTimeout(sleep);sleepDeadline=0;
  chapterTransition=null;buffering=false;pendingPlay=null;foregroundFollowPending=false;
  if(endHandler&&audio.removeEventListener)audio.removeEventListener('ended',endHandler);endHandler=null;
  // Preserve checkpoint metadata, detach the resource, and fence all old callbacks.
  if(audio.removeAttribute){audio.removeAttribute('src');audio.load();}
  segmentPrefetch?.remove();segmentPrefetch=null;
  narrationBase=null;narrationLocation=null;narrationKey='';reader()?.clear?.();setMini(true);$('playerPanel').hidden=true;syncReaderPlayback();
  if('mediaSession' in navigator){navigator.mediaSession.metadata=null;navigator.mediaSession.playbackState='none';}
 }

 const stored=key=>{try{return localStorage.getItem?localStorage.getItem(key):null;}catch{return null;}};
 let playbackRate=Number(stored('webnovelPlaybackRate'))||null;
 function applyPlaybackRate(){if(!playbackRate)return;audio.defaultPlaybackRate=playbackRate;audio.playbackRate=playbackRate;$('playbackSpeed').value=String(playbackRate);}
 function settleNativeSeek(){
  const goal=nativeSeek;if(!goal)return true;
  if(goal.source!==sourceRevision||goal.session!==token||goal.seek!==seekRevision){nativeSeek=null;clearTimeout(nativeSeekTimer);return true;}
  if(audio.readyState===0||audio.error||(audio.currentSrc&&audio.currentSrc!==audio.src))return false;
  const target=Math.min(goal.seconds,Number.isFinite(audio.duration)?audio.duration:goal.seconds);lastKnownTime=target;
  if(!audio.seeking&&audio.readyState>=3&&Math.abs(audio.currentTime-target)<.05){nativeSeek=null;pendingSeek=0;clearTimeout(nativeSeekTimer);return true;}
  if(!audio.seeking&&Math.abs(audio.currentTime-target)>.05&&goal.attempts++<3)audio.currentTime=target;
  return false;
 }
 function nativeSeekReady(){if(!settleNativeSeek()){syncReaderPlayback();return;}if(readerStartMapped)readingActivationPending=false;if(wantPlay&&!buffering)resumeMedia();else{syncReaderPlayback();save();}}
 function armNativeSeek(seconds){clearTimeout(nativeSeekTimer);nativeSeek=seconds>0?{session:token,source:sourceRevision,seek:seekRevision,seconds,attempts:0}:null;if(!nativeSeek)return;
  const goal=nativeSeek;nativeSeekTimer=setTimeout(()=>{if(nativeSeek!==goal||goal.session!==token||goal.source!==sourceRevision||goal.seek!==seekRevision||!wantPlay)return;if(sourceReloadAttempts++>=2){stopSequence('The requested audio position could not load. Press Play to retry at the saved position.','seek-retries-exhausted');return;}pendingSeek=goal.seconds;loadAudio(audio.src+(audio.src.includes('?')?'&':'?')+'reader_seek_retry='+(sourceRevision+1));},2000);
 }
 function loadAudio(url){lastKnownTime=pendingSeek;const session=token,source=++sourceRevision;armNativeSeek(pendingSeek);
  completedSource=-1;finishingSource=-1;if(endHandler&&audio.removeEventListener)audio.removeEventListener('ended',endHandler);
  const ended=()=>finishSource(session,source).catch(e=>{if(session!==token||source!==sourceRevision)return;if(e.name==='ChapterOrderError'||recoveryAttempts++>=3){stopSequence(e.message,'chapter-transition-failed');return;}playbackState('interrupted');status(e.message);checkpoint();if(wantPlay)recoveryTimer=setTimeout(()=>{if(session===token&&source===sourceRevision&&wantPlay)ended();},3000);});
  endHandler=ended;audio.addEventListener('ended',endHandler);audio.preload=progressive&&(wantPlay||pendingSeek>0)?'auto':'metadata';audio.src=url;audio.load();diagnostic('segment-loaded','source-load');applyPlaybackRate();checkpoint();}
 const remember=()=>{try{localStorage.setItem('webnovelListeningBook',bookId);localStorage.setItem('webnovelListeningSelection',JSON.stringify({bookId,sectionId:chapter?.id,voice,rate:synthesisRate,pending:!asset}));}catch{}};
 const status=text=>{$('playerStatus').textContent=text;};
 async function api(path,options){const r=await fetch(path,options),data=await r.json();if(!r.ok)throw Object.assign(new Error(data.error||`Request failed (${r.status})`),{status:r.status,code:data.code,details:data});return data;}
 const json=value=>({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
 function savePreferences(patch){preferenceRevision++;preferencesQueue=preferencesQueue.catch(()=>{}).then(()=>api('/api/edge/preferences',{...json(patch),method:'PUT'}));return preferencesQueue;}
 function navigate(path,replace=false){if(!/^\/(library|reader\/|books\/|importer|jobs|settings|audio)/.test(path))path='/library';document.body.dataset.reading=String(path.startsWith('/reader/'));setMini(true);$('workspace').src=path;history[replace?'replaceState':'pushState'](null,'','/?view='+encodeURIComponent(path));}
 document.querySelectorAll('[data-view]').forEach(link=>link.onclick=e=>{e.preventDefault();navigate(link.getAttribute('href'));});
 function time(value){if(!Number.isFinite(value))return '0:00';return `${Math.floor(value/60)}:${String(Math.floor(value%60)).padStart(2,'0')}`;}
 async function listeningRequest(path,options={}){
  const controller=typeof AbortController==='function'?new AbortController():null;let timer;
  try{return await Promise.race([api(path,{...options,...(controller?{signal:controller.signal}:{})}),
   new Promise((resolve,reject)=>{timer=setTimeout(()=>{controller?.abort();reject(new Error('Listening checkpoint is preserved locally; the server save timed out.'));},5000);})]);}
  finally{clearTimeout(timer);}
 }
 async function writeListening(snapshot,writer,keepalive){
  for(let attempt=0;attempt<2;attempt++){
   try{return await listeningRequest(`/api/books/${snapshot.book_id}/listening`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({...snapshot,version:progressVersions.get(snapshot.book_id)||0}),keepalive});}
   catch(error){
    if(attempt||error.code!=='version_conflict')throw error;
    const remote=await listeningRequest(`/api/books/${snapshot.book_id}/listening`);
    const own=remote.session_id===writer.id&&remote.session_generation===writer.generation;
    const claiming=writer.claim&&(remote.session_id||'')===writer.expectedOwner&&(remote.session_generation||0)+1===writer.generation;
    if((!own&&!claiming)||(own&&remote.event_sequence>snapshot.event_sequence))throw error;
    // Rebase only within this fenced writer, never take over another session.
    progressVersions.set(snapshot.book_id,remote.version);
   }
  }
 }
 function save(keepalive=false){
  checkpoint();const writer=writers.get(bookId);
  if(!writer||!asset||!chapter||conflict||opening||readingActivationPending||pendingChapterSeek!==null||audio.seeking||nativeSeek)return saveQueue;
  const location=narrationLocation?.section_id===chapter.id&&narrationLocation.segment_index===narrationIndex()?narrationLocation:null;
  const snapshot={book_id:bookId,section_id:chapter.id,asset_id:asset.asset_id,seconds:chapterSeconds(),...((progressive||narrationSegments.length)&&narrationIndex()!==null?{segment_index:narrationIndex(),segment_seconds:narrationSeconds()}:{}),playback_rate:playbackRate||audio.playbackRate,voice,synthesis_rate:voice==='legacy'?0:synthesisRate,
   session_id:writer.id,session_generation:writer.generation,event_sequence:writer.sequence,claim_session:writer.claim,expected_session_id:writer.expectedOwner,
   // Keep the granular UI/local diagnosis, but use the existing server recovery state on the wire.
   checkpoint_intent:checkpointIntent,playback_state:mediaState==='stalled'?'buffering':mediaState,text_anchor:location?.anchor||'',normalized_offset:Number.isInteger(location?.normalized_offset)?location.normalized_offset:null};
  writer.queuedSequence=snapshot.event_sequence;
  const session=token;const key=JSON.stringify(snapshot);if(key===progressSignatures.get(bookId))return saveQueue;
  saveQueue=saveQueue.catch(()=>{}).then(async()=>{
   if(writers.get(snapshot.book_id)!==writer||snapshot.event_sequence<writer.queuedSequence||(conflict&&snapshot.book_id===bookId)||key===progressSignatures.get(snapshot.book_id))return;
   try{
    pendingListeningWrite={...snapshot,version:(progressVersions.get(snapshot.book_id)||0)+1};checkpoint();diagnostic('checkpoint-attempt',snapshot.checkpoint_intent);
    const result=await writeListening(snapshot,writer,keepalive);
    if(result.session_id!==writer.id||result.session_generation!==writer.generation||result.event_sequence!==snapshot.event_sequence)throw new Error('The server did not confirm the ordered listening checkpoint. Your position remains saved locally.');
    if(pendingListeningWrite?.session_id===snapshot.session_id&&pendingListeningWrite?.event_sequence===snapshot.event_sequence)pendingListeningWrite=null;
    writer.claim=false;progressVersions.set(snapshot.book_id,Math.max(progressVersions.get(snapshot.book_id)||0,result.version));progressSignatures.set(snapshot.book_id,key);diagnostic('checkpoint-accepted',String(snapshot.event_sequence));
    if(session===token){version=progressVersions.get(snapshot.book_id);checkpoint();
 // Only the attached reader owns reading progress. Listening may never overwrite
 // it during iframe startup, navigation, or when the user is outside the reader.
    }
   }catch(e){diagnostic('checkpoint-rejected',e.code||'network');if(session===token){
    if(e.code==='stale_checkpoint'){progressVersions.set(snapshot.book_id,Math.max(progressVersions.get(snapshot.book_id)||0,e.details?.version||0));return;}
    status(e.message);if(e.message.includes('another player')){conflict=true;userPause('server-session-conflict');status(e.message);}
   }}
  });return saveQueue;
 }
 function cancelAudioRequest(){if(generationOperation){const prior=generationOperation;generationOperation=null;prior.cancel?.();}if(!audioOperation)return;const prior=audioOperation;audioOperation=null;++audioRequestRevision;prior.cancel?.();}
 async function audioRequest(path,options,operation){
  const controller=typeof AbortController==='function'?new AbortController():null;let timer,cancel;
  const interrupted=new Promise((resolve,reject)=>{cancel=()=>{controller?.abort();reject(Object.assign(new Error('Obsolete audio request.'),{name:'AbortError'}));};timer=setTimeout(()=>{controller?.abort();reject(Object.assign(new Error('Audio request timed out. Retrying the saved listening position…'),{name:'AudioRequestTimeout'}));},10000);});
  operation.cancel=cancel;
  try{return await Promise.race([api(path,{...options,...(controller?{signal:controller.signal}:{})}),interrupted]);}
  finally{clearTimeout(timer);if(operation.cancel===cancel)operation.cancel=null;}
 }
 function waitForAudio(currentToken){
  if(currentToken!==token)return Promise.resolve();
  if(generationOperation?.session===currentToken)return generationOperation.task.then(ready=>ready?waitForAudio(currentToken):undefined);
  if(audioOperation?.session===currentToken&&audioOperation.seek===seekRevision)return audioOperation.task;
  cancelAudioRequest();const operation={session:currentToken,seek:seekRevision,query:++audioRequestRevision};audioOperation=operation;
  operation.task=readAudio(currentToken,operation);return operation.task;
 }
 function generationRequest(path,options){
  const key=path+JSON.stringify(options);
  if(generationOperation?.session===token&&generationOperation.key===key)return generationOperation.task;
  cancelAudioRequest();const operation={session:token,seek:seekRevision,attempt:playAttempt,key};generationOperation=operation;wantPlay=true;playbackState('recovering');
  const current=()=>generationOperation===operation&&operation.session===token&&operation.seek===seekRevision&&operation.attempt===playAttempt;
  operation.task=(async()=>{try{await audioRequest(path,options,operation);return current();}
   catch(error){if(current()){wantPlay=false;playbackState('error');status(error.message+' Press Play to retry at the saved position.');}return false;}
   finally{if(generationOperation===operation)generationOperation=null;}})();return operation.task;
 }
 async function readAudio(currentToken,operation){
  const query=operation.query;let failed=false;clearTimeout(poll);
  try{
   const result=await audioRequest(`/api/books/${bookId}/audio?section_id=${chapter.id}&voice=${encodeURIComponent(voice)}&rate=${synthesisRate}&provider=${voice==='legacy'?'legacy':'edge'}`,{},operation);
   if(currentToken!==token||query!==audioRequestRevision)return;
   validateSegments(result);
   lastAudioState=result;
   if(voice!=='legacy'&&wantPlay&&result.status!=='ready'&&requestedToken!==currentToken&&!['failed','paused','cancelled'].includes(result.generation_status||result.status)){
    await audioRequest(`/api/books/${bookId}/audio/jobs`,json({section_id:chapter.id,voice,rate:synthesisRate}),operation);if(currentToken!==token||query!==audioRequestRevision)return;requestedToken=currentToken;
   }
   if(readerStart&&!readerStartMapped&&['streaming','ready'].includes(result.status)){
    const mapped=await audioRequest(`/api/audio/${result.asset_id}/start?offset=${readerStart.normalized_offset}`,{},operation);
    if(currentToken!==token||query!==audioRequestRevision)return;
    if(!mapped.available){if(result.status==='ready'&&result.timeline_complete===false)throw Object.assign(new Error('This cached chapter lacks passage timing for this page. Use the book listening controls to seek by native chapter time.'),{name:'GenerationError'});buffering=true;playbackState('buffering');status('Generating audio at your current reading position…');poll=setTimeout(()=>waitForAudio(currentToken),result.worker_active===false||result.can_generate===false?15000:1500);return;}
    pendingSeek=mapped.seconds;pendingCursor={assetId:result.asset_id,index:mapped.segment_index,seconds:mapped.segment_seconds||0};readerStartMapped=true;buffering=false;
   }
   if(result.status==='streaming'||((progressive||readerStart||seekOverride)&&result.status==='ready'&&result.timeline_complete!==false&&result.segments?.length)){
    if(buffering&&progressive&&!result.segments[segmentIndex+1]&&['failed','paused','cancelled'].includes(result.generation_status))throw Object.assign(new Error(`Generation ${result.generation_status}. ${result.error||'Press Play to retry at the saved position.'}`),{name:'GenerationError'});
    const initial=!progressive;progressive=true;segments=result.segments;asset=result;remember();prefetchPublishedSegment();
    if(initial&&!result.complete&&pendingSeek>=availableDuration())pendingChapterSeek=pendingSeek;
    if(pendingChapterSeek!==null){const target=pendingChapterSeek;if(target<availableDuration()||result.complete){pendingChapterSeek=null;buffering=false;applyChapterSeek(target);}else{buffering=true;playbackState('buffering');poll=setTimeout(()=>waitForAudio(currentToken),1500);return;}if(!result.complete)poll=setTimeout(()=>waitForAudio(currentToken),1500);return;}
    if(initial){let seconds=pendingSeek,index=0;const exact=pendingCursor?.assetId===result.asset_id&&Number.isInteger(pendingCursor.index)&&pendingCursor.index>=0&&segments[pendingCursor.index]&&Number.isFinite(pendingCursor.seconds)&&pendingCursor.seconds>=0&&pendingCursor.seconds<=segments[pendingCursor.index].duration+.1;if(exact){index=pendingCursor.index;seconds=pendingCursor.seconds;}else while(index<segments.length-1&&seconds>=segments[index].duration){seconds-=segments[index].duration;index++;}loadSegment(index,seconds);}
    else if(buffering&&segments[segmentIndex+1])loadSegment(segmentIndex+1);
    if(currentToken!==token)return;
    if(initial&&wantPlay&&chapter.next_id&&result.worker_active!==false&&result.can_generate!==false)api(`/api/books/${bookId}/audio/jobs`,json({section_id:chapter.next_id,voice,rate:synthesisRate,prefetch:true})).catch(()=>{});
    if(buffering){status(result.complete?'Chapter finished.':result.worker_active===false||result.can_generate===false?result.worker_message:result.error?`Generation error: ${result.error}. Press Play to retry.`:`Buffering next segment — ${segments.length} playable segments saved${result.generation_status==='paused'?' · generation paused':''}.`);if(result.complete&&wantPlay){buffering=false;await finishChapter(currentToken);}else if(!wantPlay)status('Paused at chapter end. Press Play to continue.');}
    else if(audio.paused)status(!wantPlay?'Paused — generation continues in the background.':result.error?`Audio available; generation error: ${result.error}`:'Audio ready — further segments generate in the background.');
    if(!result.complete)poll=setTimeout(()=>waitForAudio(currentToken),result.worker_active===false||result.can_generate===false?15000:(buffering&&wantPlay?500:1500));
    return;
   }
   if(result.status==='ready'){
    if(asset?.asset_id===result.asset_id&&!progressive&&!buffering){if(wantPlay&&audio.paused)resumeMedia();return;}
    asset=result;narrationSegments=result.segments||[];narrationTimelineComplete=result.timeline_complete!==false;remember();loadAudio(result.url);status(narrationTimelineComplete?'Ready to listen':'Native chapter timeline; passage timing is incomplete.');$('audioTime').textContent='0:00 / loading duration';
    $('audioPrevious').disabled=!chapter.previous_id;$('audioNext').disabled=!chapter.next_id;$('miniPrevious').disabled=false;$('miniNext').disabled=false;
    if('mediaSession' in navigator&&typeof MediaMetadata!=='undefined')navigator.mediaSession.metadata=new MediaMetadata({title:chapter.title,artist:book.author||'',album:book.title,artwork:book.cover_url?[{src:book.cover_url}]:[]});
    if(wantPlay&&chapter.next_id&&voice!=='legacy'&&result.worker_active!==false&&result.can_generate!==false)api(`/api/books/${bookId}/audio/jobs`,json({section_id:chapter.next_id,voice,rate:synthesisRate,prefetch:true})).catch(()=>{});
    return;
   }
   if(voice==='legacy')throw Object.assign(new Error('No preserved audio is available for this chapter. Select an Edge voice to generate it.'),{status:404});
   if(result.status==='failed'||result.status==='paused'||result.status==='cancelled')throw Object.assign(new Error(`Generation ${result.status}. ${result.error||'Manage this job in Background Jobs.'}`),{name:'GenerationError'});
   if(result.status==='missing'&&!wantPlay){status('No cached audio for this chapter. Press Play to generate it.');return;}
   if(currentToken!==token)return;
   if(result.worker_active===false||result.can_generate===false)status(result.worker_message||'Background worker is stopped. Chapter queued; generation will begin when it starts.');
   else if(result.error)status(`Generation retry pending: ${result.error}`);
   else status(`${result.item_status==='running'?'Generating':'Queued for'} online Edge TTS… ${result.completed_chunks||0} chunks saved`);
   poll=setTimeout(()=>waitForAudio(currentToken),result.worker_active===false||result.can_generate===false?15000:(buffering&&wantPlay?500:1500));
  }catch(e){failed=true;if(currentToken===token&&query===audioRequestRevision){
   if(e.name==='SequenceError'){clearTimeout(poll);stopSequence(e.message,'invalid-manifest');return;}
   diagnostic('audio-request-failed',e.name==='AudioRequestTimeout'?'transport-timeout':e.status?'http-'+e.status:'transport-or-generation');
   const fatal=e.name==='GenerationError'||[400,403,404].includes(e.status)||++networkRetries>=3;
   if(nativePlaying()){playbackState('playing');status(e.message+' Cached audio continues; retry when the connection returns.');if(fatal)clearTimeout(poll);else poll=setTimeout(()=>waitForAudio(currentToken),3000*2**(networkRetries-1));return;}
   status(fatal?e.message+' Press Play to retry at the saved position.':e.message);
   if(fatal){wantPlay=false;buffering=!!asset&&progressive&&!asset.complete;clearTimeout(poll);if(!asset)$('audioTime').textContent='Audio unavailable · press Play to retry';}
   playbackState(fatal?'error':'interrupted');if(wantPlay)poll=setTimeout(()=>waitForAudio(currentToken),Math.min(30000,3000*2**Math.min(networkRetries-1,4)));mediaSymbols();
  }}
  finally{if(!failed&&currentToken===token&&query===audioRequestRevision)networkRetries=0;if(audioOperation===operation)audioOperation=null;}
 }
 function setMini(mini){$('playerPanel').dataset.mini=String(mini);$('expandPlayer').textContent=mini?'⌃':'Collapse';$('expandPlayer').setAttribute('aria-expanded',String(!mini));$('expandPlayer').setAttribute('aria-label',mini?'Expand listening controls':'Dismiss expanded player');window.PlayerSheet?.sync?.();}
 function listen(...args){openingTask=openListening(...args);return openingTask;}
 async function openListening(id,sectionId=null,autoplay=true,fromReader=null,fromSeek=null,reason='user-navigation'){
  const sameSession=id===bookId;
  nativeSeek=null;clearTimeout(nativeSeekTimer);
  narrationEngaged=autoplay||!!fromReader||(id===bookId&&narrationEngaged)||(!sectionId&&savedCheckpoint(id)?.engaged===true);
  pendingChapterSeek=null;seekTarget=null;
  seekOverride=fromSeek;
  checkpoint();save();stopped=false;cancelAudioRequest();const currentToken=++token;opening=true;networkRetries=0;segmentPrefetch?.remove();segmentPrefetch=null;prefetchedSegment='';clearTimeout(poll);clearTimeout(recoveryTimer);recoveryAttempts=0;sourceReloadAttempts=0;const openingAttempt=++playAttempt;wantPlay=autoplay&&narrationEngaged;pauseMedia('chapter-or-book-switch');if(currentToken!==token)return;asset=null;readerStart=fromReader;readerStartMapped=false;readingActivationPending=!!fromReader;pausedReaderPosition=null;lastAudioState=null;progressive=false;segments=[];segmentIndex=0;buffering=false;$('audioTime').textContent='0:00 / waiting for audio';$('playerPanel').hidden=false;$('playingBook').textContent='Opening selected book…';$('playingChapter').textContent='';status('Loading the selected chapter…');if(audio.removeAttribute){audio.removeAttribute('src');audio.load();}conflict=false;signature='';
  try{
   const changed=id!==bookId;
   await preferencesQueue.catch(()=>{});const revision=preferenceRevision;
   const globalPreferences=await api('/api/edge/preferences');
   const nextBook=await api(`/api/books/${id}`),progress=await api(`/api/books/${id}/listening`);if(currentToken!==token)return;
   bookId=id;book=nextBook;narrationSegments=[];narrationBase=null;narrationMarkKey='';narrationKey='';narrationLocation=null;if(!sameSession){followEnabled=!!book.progress?.preferences?.followAlong;followSuspended=!!book.progress?.preferences?.followAlongSuspended;}readingVersions.set(id,book.progress?.version||0);api(`/api/books/${id}/opened`,json({})).catch(()=>{});
   version=progress.version;progressVersions.set(id,version);
   if(autoplay&&!prepareWriter(id,progress,reason!=='completed-chapter'))throw new Error('Listening position changed in another player. Reload to resume.');
   checkpointIntent=fromReader?'reader-start':reason==='completed-chapter'?'chapter-transition':autoplay?'playback':'restore';
   if(revision===preferenceRevision){if(changed||voice!=='legacy'){voice=globalPreferences.voice||'en-US-AndrewNeural';synthesisRate=globalPreferences.rate||0;}playbackRate=globalPreferences.playback_rate??1;audio.volume=globalPreferences.volume??1;$('volume').value=String(audio.volume);applyPlaybackRate();}
   let selection;try{selection=JSON.parse(stored('webnovelListeningSelection')||'null');}catch{}
   const localCheckpoint=savedCheckpoint(id);
   if(fromReader){followEnabled=true;followSuspended=false;}
   else if(!sameSession&&!sectionId&&checkpointVersionMatches(localCheckpoint,progress,id)){followEnabled=!!localCheckpoint.followEnabled;followSuspended=!!localCheckpoint.followSuspended;}
   const serverSaved=progress.updated_at?Date.parse(progress.updated_at.replace(' ','T')+'Z'):0;
   // Passive restoration and headphones ON recover the saved variant, even if
   // another book subsequently changed the global default voice or speed.
   // Explicit chapter/voice selection continues to use the current defaults.
   if(!sectionId&&!fromReader&&!fromSeek){
    const recent=localCheckpoint?.bookId===id&&checkpointVersionMatches(localCheckpoint,progress,id)&&(localCheckpoint.writer||localCheckpoint.savedAt>=serverSaved);
    const pending=selection?.bookId===id&&selection.pending,previous=pending?selection:recent?localCheckpoint:progress;
    if(previous.voice){voice=previous.voice;synthesisRate=recent||pending?previous.rate:previous.synthesis_rate;}
    const speed=recent||pending?previous.playbackRate:previous.playback_rate;if(Number.isFinite(speed)){playbackRate=speed;applyPlaybackRate();}
   }
   const selected=Number(sectionId)||(localCheckpoint?.bookId===id&&checkpointVersionMatches(localCheckpoint,progress,id)&&(localCheckpoint.writer||localCheckpoint.savedAt>=serverSaved)?localCheckpoint.sectionId:null)||(selection?.bookId===id&&selection.pending?selection.sectionId:null)||progress.section_id||(selection?.bookId===id?selection.sectionId:null)||book.first_section_id;if(!selected)throw new Error('This book has no imported chapters yet.');
   const nextChapter=await api(`/api/books/${id}/sections/${selected}`);if(currentToken!==token)return;chapter=nextChapter;
   const saved=savedCheckpoint(id);
   const serverTime=progress.updated_at?Date.parse(progress.updated_at.replace(' ','T')+'Z'):0;
   const localMatches=saved?.bookId===id&&saved.sectionId===selected&&saved.voice===voice&&saved.rate===synthesisRate&&checkpointVersionMatches(saved,progress,id)&&(saved.writer||saved.savedAt>=serverTime);
   if(!fromReader&&!fromSeek&&localMatches&&saved.readingStart?.section_id===selected){fromReader=saved.readingStart;readerStart=fromReader;readingActivationPending=true;}
   if(!sectionId&&!fromReader&&!fromSeek&&localMatches&&Number.isFinite(saved.playbackRate)){playbackRate=saved.playbackRate;applyPlaybackRate();}narrationLocation=localMatches?saved.narration||null:null;
   pendingSeek=fromSeek?fromSeek.seconds:fromReader?0:localMatches?saved.seconds:progress.section_id===selected&&progress.voice===voice&&progress.synthesis_rate===synthesisRate?progress.seconds:0;wantPlay=autoplay&&narrationEngaged&&playAttempt===openingAttempt;
   const cursor=localMatches?saved:progress;pendingCursor=fromReader||fromSeek?null:{assetId:localMatches?cursor.assetId:cursor.asset_id,index:localMatches?cursor.segmentIndex:cursor.segment_index,seconds:localMatches?cursor.segmentSeconds:cursor.segment_seconds};
   playbackState(wantPlay?'buffering':'paused');
   $('playerPanel').hidden=false;$('playerCover').hidden=!book.cover_url;if(book.cover_url)$('playerCover').src=book.cover_url;$('playingBook').textContent=book.title;$('playingChapter').textContent=`${chapter.position+1}. ${chapter.title}`;
   $('playerVoice').value=voice;$('synthesisSpeed').value=synthesisRate;$('synthesisSpeed').disabled=voice==='legacy';$('playbackSpeed').value=audio.playbackRate;
   if('mediaSession' in navigator&&typeof MediaMetadata!=='undefined')navigator.mediaSession.metadata=new MediaMetadata({title:chapter.title,artist:book.author||'',album:book.title,artwork:book.cover_url?[{src:book.cover_url}]:[]});
   remember();opening=false;diagnostic(reason==='completed-chapter'?'chapter-transition-committed':!autoplay?'progress-restored':'session-started',reason);checkpoint();$('audioPrevious').disabled=!chapter.previous_id;$('audioNext').disabled=!chapter.next_id;$('miniPrevious').disabled=false;$('miniNext').disabled=false;
  const route=new URLSearchParams(location.search);if(route.get('listen')===id&&route.has('section')){route.delete('section');history.replaceState(null,'','/?'+route.toString());}
   await waitForAudio(currentToken);
  }catch(e){if(currentToken===token){opening=false;wantPlay=false;status(e.message);$('playPause').textContent='Retry';}}
 }
 audio.addEventListener('loadedmetadata',()=>{applyPlaybackRate();if(!asset||opening||audio.readyState===0||metadataRevision===sourceRevision)return;metadataRevision=sourceRevision;if(nativeSeek){settleNativeSeek();nativeSeekReady();}else{const target=Math.min(pendingSeek,Number.isFinite(audio.duration)?Math.max(0,audio.duration):pendingSeek);if(Math.abs(audio.currentTime-target)>.001)audio.currentTime=target;pendingSeek=0;publishNarration();if(wantPlay)resumeMedia();}});
 audio.addEventListener('play',()=>{if(opening||!wantPlay){pauseMedia('unrequested-native-play');return;}playbackState('recovering');applyPlaybackRate();mediaSymbols();status(progressive&&!asset?.complete?'Playing — generating further segments.':'Playing');if('mediaSession' in navigator)navigator.mediaSession.playbackState='playing';});
 audio.addEventListener('pause',()=>{if(!audio.paused)return;diagnostic('native-pause','origin-not-exposed');syncReaderPlayback();if(wantPlay&&!opening&&!audio.ended&&!buffering){wantPlay=false;++playAttempt;clearTimeout(recoveryTimer);playbackState('interrupted');status('Playback interrupted. Tap Play to resume at the saved position.');checkpoint();}mediaSymbols();if(asset&&!opening&&!audio.ended&&!buffering)status('Paused');save();if('mediaSession' in navigator)navigator.mediaSession.playbackState='paused';});
 function updateMediaPosition(){if(opening||!asset||audio.error||audio.readyState===0||audio.seeking||nativeSeek)return;lastKnownTime=audio.currentTime||0;publishNarration();renderPassages();const total=progressive?availableDuration():audio.duration;if(!sliderDragging)$('audioTime').textContent=`${time(chapterSeconds())} / ${progressive&&!asset?.complete?'generating…':time(total)}${progressive&&!asset?.complete?' · '+time(Math.max(0,total-chapterSeconds()))+' buffered ('+time(Math.max(0,total-chapterSeconds())/(playbackRate||1))+' at '+(playbackRate||1)+'x)':''}`;if(!sliderDragging)$('audioSeek').value=Number.isFinite(total)&&total?Math.round(chapterSeconds()/total*1000):0;$('miniProgress').value=Number($('audioSeek').value);if(Number.isFinite(total)&&total>0&&navigator.mediaSession?.setPositionState){try{navigator.mediaSession.setPositionState({duration:total,playbackRate:playbackRate||1,position:Math.min(total,chapterSeconds())});}catch{}}if(Date.now()-lastLocalSave>=1000){lastLocalSave=Date.now();checkpoint();}if(Date.now()-lastSave>5000){lastSave=Date.now();save();}}
 audio.addEventListener('timeupdate',updateMediaPosition);
 audio.addEventListener('error',()=>{if(!asset||opening||!audio.error)return;diagnostic('media-error','decode-or-network');playbackState('interrupted');const session=token,source=sourceRevision;if(wantPlay&&sourceReloadAttempts++<2)recoveryTimer=setTimeout(()=>{if(session!==token||source!==sourceRevision||!wantPlay)return;pendingSeek=segmentSeconds();loadAudio(audio.src);},2000);else if(wantPlay){stopSequence('Audio could not load. Playback stopped at its saved position. Press Play to retry.','media-retries-exhausted');return;}status('Audio interrupted. Retry at the saved position if recovery is unavailable.');});
 $('playPause').onclick=async()=>{if(mediaState==='error'){networkRetries=0;requestedToken=0;}if(wantPlay&&!['interrupted','error'].includes(mediaState)){userPause();status('Paused');return;}if(opening){status('Opening the selected chapter…');return;}if(buffering){if(wantPlay&&!['failed','paused','cancelled'].includes(lastAudioState?.generation_status)){userPause();mediaSymbols();status('Paused while buffering.');return;}wantPlay=true;try{if(lastAudioState?.job_id&&['failed','paused'].includes(lastAudioState.generation_status)){if(!await generationRequest('/api/jobs/'+lastAudioState.job_id,json({action:lastAudioState.generation_status==='paused'?'resume':'retry'})))return;}else if(lastAudioState?.generation_status==='cancelled'){if(!await generationRequest(`/api/books/${bookId}/audio/jobs`,json({section_id:chapter.id,voice,rate:synthesisRate})))return;}clearTimeout(poll);await waitForAudio(token);}catch(e){status(e.message);}return;}if(!asset){if(bookId){try{if(lastAudioState?.job_id&&['failed','paused'].includes(lastAudioState.status)){if(!await generationRequest('/api/jobs/'+lastAudioState.job_id,json({action:lastAudioState.status==='paused'?'resume':'retry'})))return;}else if(lastAudioState?.status==='cancelled'){if(!await generationRequest(`/api/books/${bookId}/audio/jobs`,json({section_id:chapter.id,voice,rate:synthesisRate})))return;}listen(bookId,chapter?.id,true,readerStart);}catch(e){status(e.message);}}return;}if(audio.ended&&asset?.complete){wantPlay=true;try{await finishChapter(token);}catch(e){playbackState('interrupted');status(e.message);}return;}if(audio.paused){wantPlay=true;if(audio.error||mediaState==='error'){sourceReloadAttempts=0;pendingSeek=segmentSeconds();loadAudio(audio.src);}else await resumeMedia(true);}else{userPause();}};
 async function seek(delta){
  if(!asset||opening||!Number.isFinite(delta))return;
  const target=(pendingChapterSeek??chapterSeconds())+delta,total=progressive?availableDuration():audio.duration;
  if(progressive){seekChapter(target);return;}
  commitNativeSeek(target);
 }
 $('rewind').onclick=()=>seek(-15);$('forward').onclick=()=>seek(15);
 function timelineTotal(){const value=progressive?availableDuration():audio.duration;return Number.isFinite(value)&&value>0?value:0;}
 function commitNativeSeek(target){checkpointIntent='seek';diagnostic('user-seek-requested','native-timeline');++seekRevision;completedSource=-1;reader()?.seek?.();seekTarget=Math.max(0,Math.min(Number.isFinite(audio.duration)?audio.duration:Infinity,target));lastKnownTime=seekTarget;armNativeSeek(seekTarget);audio.currentTime=seekTarget;nativeSeekReady();checkpoint();}
 function beginSlider(){sliderDragging=true;sliderTotal=timelineTotal();}
 $('audioSeek').onpointerdown=beginSlider;
 $('audioSeek').oninput=()=>{if(!sliderDragging)beginSlider();$('audioTime').textContent=time(sliderTotal*Number($('audioSeek').value)/1000)+' / '+time(sliderTotal)+(progressive&&!asset?.complete?' · available audio only':'');};
 $('audioSeek').onchange=()=>{const target=(sliderTotal||timelineTotal())*Number($('audioSeek').value)/1000;sliderDragging=false;sliderTotal=0;if(!asset||!timelineTotal())return;if(progressive){seekChapter(target);return;}commitNativeSeek(target);};
 $('audioSeek').onpointercancel=()=>{sliderDragging=false;sliderTotal=0;};
 async function renderPassages(){
  if(!asset||!chapter)return;const key=asset.asset_id+':'+narrationIndex()+':'+(narrationLocation?.anchor||'');if(key===passageKey)return;passageKey=key;
  if(narrationIndex()===null){$('spokenPassages').textContent='Passage timing is unavailable here. The slider uses the recording’s native chapter duration.';return;}
  if(asset.asset_id.startsWith('legacy-')){$('spokenPassages').textContent='Passage timing is unavailable for this preserved recording.';return;}
  const session=token,index=narrationIndex(),cacheKey=asset.asset_id+':'+index;
  let rows=passageCache.get(cacheKey);if(!rows)rows=await Promise.all([index-1,index,index+1].map(async n=>{if(n<0)return null;try{return {n,location:await api('/api/audio/'+asset.asset_id+'/location/'+n)};}catch{return null;}}));
  if(session!==token||key!==passageKey)return;
  passageCache.set(cacheKey,rows);if(passageCache.size>5)passageCache.delete(passageCache.keys().next().value);
  $('spokenPassages').replaceChildren(...rows.filter(Boolean).map(row=>{const p=document.createElement('p');p.textContent=row.n===index?(narrationLocation?.quote||row.location.quote):row.n<index?row.location.quote.slice(-280):row.location.quote.slice(0,280);p.className=row.n===index?'current-passage':'nearby-passage';if(row.n===index&&narrationLocation?.precision==='word'){const before=document.createElement('span'),word=document.createElement('mark'),after=document.createElement('span');before.textContent=narrationLocation.before;word.textContent=narrationLocation.quote;after.textContent=narrationLocation.after;p.replaceChildren(before,word,after);}return p;}));
  const current=$('spokenPassages').querySelector?.('.current-passage');if(current)$('spokenPassages').scrollTo?.({top:current.offsetTop-$('spokenPassages').offsetTop-16,behavior:'smooth'});
 }
 audio.addEventListener('loadeddata',()=>{renderPassages();$('audioSeek').disabled=!timelineTotal();});
 audio.addEventListener('seeked',renderPassages);
 $('audioPrevious').onclick=()=>{if(chapter?.previous_id)listen(bookId,chapter.previous_id,true);};
 $('audioNext').onclick=()=>{if(chapter?.next_id)listen(bookId,chapter.next_id,true);};
 $('playbackSpeed').onchange=()=>{const value=Number($('playbackSpeed').value);if(!Number.isFinite(value)||value<.25||value>4)return;playbackRate=value;try{localStorage.setItem('webnovelPlaybackRate',String(value));}catch{}applyPlaybackRate();savePreferences({playback_rate:value}).catch(e=>status('Could not save global speed: '+e.message));save();};
 async function changeVoice(){const autoplay=wantPlay,location=narrationLocation;pauseMedia('voice-change');await save();voice=$('playerVoice').value;synthesisRate=voice==='legacy'?0:Number($('synthesisSpeed').value);asset=null;try{if(voice!=='legacy')await savePreferences({voice,rate:synthesisRate});const anchor=voice!=='legacy'&&Number.isInteger(location?.normalized_offset)?{section_id:chapter.id,anchor:'text:'+location.normalized_offset,normalized_offset:location.normalized_offset}:null;if(chapter)await listen(bookId,chapter.id,autoplay,anchor);}catch(e){status('Could not save global voice: '+e.message);}}

 $('playerVoice').onchange=changeVoice;$('synthesisSpeed').onchange=changeVoice;
 function checkSleep(){if(sleepDeadline&&Date.now()>=sleepDeadline){sleepDeadline=0;clearTimeout(sleep);userPause('sleep-timer');clearTimeout(poll);$('sleepTimer').value='0';$('sleepStatus').textContent='Sleep timer finished.';}return sleepDeadline;}
 $('sleepTimer').onchange=()=>{clearTimeout(sleep);const minutes=Number($('sleepTimer').value);sleepDeadline=minutes?Date.now()+minutes*60000:0;const deadline=sleepDeadline;$('sleepStatus').textContent=minutes?'Pauses at '+new Date(sleepDeadline).toLocaleTimeString()+'. iOS suspension may delay this until the browser resumes.':'Sleep timer off.';if(minutes)sleep=setTimeout(()=>{if(sleepDeadline!==deadline)return;sleepDeadline=Math.min(sleepDeadline,Date.now());checkSleep();status('Sleep timer finished.');},minutes*60000);};
 audio.addEventListener('timeupdate',checkSleep);window.addEventListener('pageshow',checkSleep);document.addEventListener('visibilitychange',checkSleep);
 $('closePlayer').onclick=e=>{e?.preventDefault();e?.stopPropagation();stopReader();};
 function openNarratedReader(expandInReader=false){if(!chapter||!narrationEngaged)return;publishNarration();checkpoint();const existing=reader();if(existing){existing.follow?.();if(expandInReader){setMini(false);renderPassages();}else setMini(true);return;}navigate(`/reader/${bookId}?section=${chapter.id}&narration=1`);}
 $('readPlaying').onclick=()=>openNarratedReader();
 $('playerCover').onclick=()=>setMini(false);
 window.addEventListener('pagehide',()=>{wasBackground=true;clearTimeout(foregroundTimer);checkpoint();save(true);});
 window.addEventListener('pageshow',foreground);
 document.addEventListener?.('visibilitychange',()=>{diagnostic('visibility-changed',document.hidden?'background':'foreground');if(document.hidden){wasBackground=true;clearTimeout(foregroundTimer);checkSleep();checkpoint();save(true);}else foreground();});
 function recoverStalledMedia(){
  if(!wantPlay||opening||buffering||!asset||audio.readyState>=3)return;
  const session=token,source=sourceRevision,position=segmentSeconds();clearTimeout(recoveryTimer);
  recoveryTimer=setTimeout(()=>{if(session!==token||source!==sourceRevision||!wantPlay||buffering||audio.readyState>=3||Math.abs(segmentSeconds()-position)>.05)return;
   if(sourceReloadAttempts++>=2){stopSequence('Audio loading stalled. Press Play to retry at the saved position.','loading-retries-exhausted');return;}
   pendingSeek=position;status('Recovering cached audio at the requested position…');
   // WebKit can stall resuming a previously decoded MP3 after a cross-chunk
   // seek. Reopen the same immutable cached file, never request new synthesis.
   loadAudio(audio.src+(audio.src.includes('?')?'&':'?')+'reader_retry='+(sourceRevision+1));
  },2000);
 }
 audio.addEventListener('waiting',()=>{diagnostic('native-waiting','media-buffer');if(wantPlay){playbackState('buffering');recoverStalledMedia();}});
 audio.addEventListener('stalled',()=>{diagnostic('native-stalled','media-network');if(wantPlay&&audio.readyState<3){playbackState('buffering');recoverStalledMedia();}});
 audio.addEventListener('canplay',()=>{if(wantPlay&&!audio.ended&&!buffering)resumeMedia();});
 audio.addEventListener('playing',()=>{if(!audio.paused&&!audio.ended&&asset&&!opening&&narrationEngaged&&!conflict){reconcileMedia('native-playing');save();}});
 try{if(navigator.audioSession){navigator.audioSession.type='playback';navigator.audioSession.addEventListener('statechange',()=>{if(navigator.audioSession.state==='interrupted'){diagnostic('audio-session-interrupted','platform-session');lastKnownTime=segmentSeconds();wantPlay=false;++playAttempt;clearTimeout(recoveryTimer);playbackState('interrupted');checkpoint();save(true);}});}}catch{}

 if('mediaSession' in navigator){for(const [name,handler] of Object.entries({play:()=>{if(!bookId||!chapter)return;wantPlay=true;return resumeMedia(true);},pause:()=>userPause('media-session-pause'),seekbackward:d=>seek(-(d.seekOffset||15)),seekforward:d=>seek(d.seekOffset||15),previoustrack:()=>$('audioPrevious').click(),nexttrack:()=>$('audioNext').click(),seekto:d=>{if(progressive)seekChapter(d.seekTime);else commitNativeSeek(d.seekTime);}})){try{navigator.mediaSession.setActionHandler(name,(...args)=>{if(!stopped)return handler(...args);});}catch{}}}
 function installVoices(data){const choices=[{ShortName:'legacy',Locale:'Preserved Piper/audio'},...data.items];$('playerVoice').replaceChildren(...choices.map(v=>{const option=document.createElement('option');option.value=v.ShortName;option.textContent=v.DisplayName||`${v.ShortName} · ${v.Locale}`;return option;}));$('playerVoice').value=voice;}
 api('/api/edge/voices').then(data=>{installVoices(data);if(data.source==='fallback')api('/api/edge/voices/refresh',json({})).then(fresh=>installVoices({...fresh,items:[...data.items,...fresh.items.filter(v=>!data.items.some(x=>x.ShortName===v.ShortName))]})).catch(()=>{});}).catch(()=>{});
 $('saveDefaultVoice').onclick=async()=>{try{if(voice==='legacy')throw new Error('Choose an Edge voice to save a default.');await api('/api/edge/preferences',{...json({voice,rate:synthesisRate}),method:'PUT'});$('defaultVoiceStatus').textContent='Default voice and synthesis speed saved.';}catch(e){$('defaultVoiceStatus').textContent=e.message;}};
 function rememberReader(id){const params=new URLSearchParams(location.search);if((params.get('view')||'').split('?')[0]===`/reader/${id}`){params.set('view',`/reader/${id}`);history.replaceState(null,'','/?'+params.toString());}}
 function forgetBook(id){if(id!==bookId)return;userPause('book-removed');clearTimeout(recoveryTimer);++sourceRevision;clearTimeout(poll);++token;try{localStorage.removeItem('webnovelPlaybackCheckpoint:'+id);}catch{}asset=null;chapter=null;bookId=null;$('playerPanel').hidden=true;try{localStorage.removeItem('webnovelListeningBook');localStorage.removeItem('webnovelListeningSelection');const saved=JSON.parse(stored('webnovelPlaybackCheckpoint')||'null');if(saved?.bookId===id)localStorage.removeItem('webnovelPlaybackCheckpoint');}catch{}}
 $('playerPanel').addEventListener('click',e=>{if($('playerPanel').dataset.mini==='true'&&!e.target.closest('button,input,select,a,label'))setMini(false);});
 $('miniPlay').onclick=()=> $('playPause').click();
 $('expandPlayer').onclick=()=>setMini($('playerPanel').dataset.mini!=='true');
 $('miniOpen').onclick=()=>{setMini(false);renderPassages();};
 $('workspace').addEventListener('load',()=>{reconcileMedia('reader-remount');syncReaderPlayback();narrationMarkKey='';publishNarration();});
 $('miniPrevious').onclick=()=>seek(-15);
 $('miniNext').onclick=()=>seek(15);
 setMini(true);
 $('volume').oninput=()=>{audio.volume=Number($('volume').value);};
 $('volume').onchange=()=>savePreferences({volume:audio.volume}).catch(e=>status('Could not save global volume: '+e.message));
 audio.addEventListener('play',()=>{if(stopped){audio.pause();return;}mediaSymbols();});audio.addEventListener('pause',()=>{mediaSymbols();});
 const showNetwork=()=>{$('networkState').hidden=navigator.onLine!==false;$('networkState').textContent='Offline: the server must be reachable to open books or audio. New Edge speech requires internet access.';};window.addEventListener('online',showNetwork);window.addEventListener('offline',showNetwork);showNetwork();
 async function toggleReader(id,position){
  if(id===bookId&&(wantPlay||headphoneStarting)){stopReader();return;}
  return startReader(id,position);
 }
 async function startReader(id,position){
  if(id===bookId&&wantPlay&&(audio.seeking||nativeSeek))return;
  if(headphoneStarting||opening||(wantPlay&&readingActivationPending&&['buffering','recovering'].includes(mediaState)&&id===bookId&&sameReading(readerStart,position)))return;
  if(!Number.isInteger(position?.section_id)||!Number.isInteger(position.normalized_offset)||position.normalized_offset<0||!/^text:\d+$/.test(position.anchor))throw new Error('The current page has no valid narration position.');
  headphoneStarting=true;setMini(true);
  try{
   // A new listening session deliberately starts at the visible reading anchor.
   // Play/Pause while engaged uses resumeMedia instead and retains its cursor.
   const failed=lastAudioState?.generation_status||lastAudioState?.status;
   if(id===bookId&&chapter?.id===position.section_id&&lastAudioState?.job_id&&['failed','paused'].includes(failed)&&!await generationRequest('/api/jobs/'+lastAudioState.job_id,json({action:failed==='paused'?'resume':'retry'})))return;
   await listen(id,position.section_id,true,position);
  }finally{headphoneStarting=false;syncReaderPlayback();}
 }
 window.Player={playing,theme:value=>{document.body.dataset.readerTheme=value;},expanded:()=>$('playerPanel').dataset.mini!=='true'&&!$('playerPanel').hidden,toggleReader,stopReader,expand:()=>{narrationEngaged=true;setMini(false);$('playerPanel').hidden=false;syncReaderPlayback();renderPassages();},seek,narration:()=>narrationEngaged?narrationLocation:null,setFollow:(id,enabled,suspended=false,revision=null,reason=null)=>{if(id!==bookId)return;if(reason!==null&&reason!=='reader-state'||reason===null&&(suspended||!enabled))foregroundFollowPending=false;if(followEnabled!==enabled||followSuspended!==suspended){followEnabled=enabled;followSuspended=suspended;diagnostic('follow-mode-changed',suspended?'manual-navigation':enabled?'follow-enabled':'follow-disabled');checkpoint();syncReaderPlayback();}if(revision!==null)readingVersions.set(id,revision);},listen:(id,sectionId=null,autoplay=true)=>{setMini(false);if(!stopped&&id===bookId&&chapter&&(!sectionId||Number(sectionId)===chapter.id)){setMini(false);$('playerPanel').hidden=false;if(autoplay&&audio.paused){wantPlay=true;return resumeMedia(true);}return Promise.resolve();}return listen(id,sectionId,autoplay);},navigate,rememberReader,forgetBook,metrics:()=>({book_id:bookId,engaged:narrationEngaged,section_id:chapter?.id,voice,playback_rate:playbackRate||audio.playbackRate,volume:audio.volume,seconds:chapterSeconds(),published_ahead_seconds:progressive?Math.max(0,availableDuration()-chapterSeconds()):audio.buffered?.length?Math.max(0,audio.buffered.end(audio.buffered.length-1)-segmentSeconds()):0,buffering,want_play:wantPlay,media_state:mediaState,session:token,source_revision:sourceRevision,segment_index:narrationIndex(),segment_seconds:narrationSeconds(),complete:!!asset?.complete,requested_reading_anchor:readerStart?.anchor,requested_segment_index:readerStartMapped?pendingCursor?.index:null,pending_seek:pendingChapterSeek,seek_target:seekTarget,status:$('playerStatus').textContent})};
 audio.addEventListener('timeupdate',syncReaderPlayback);
 audio.addEventListener('seeking',syncReaderPlayback);
 audio.addEventListener('seeked',()=>{if(opening||!asset||audio.readyState===0||(audio.currentSrc&&audio.currentSrc!==audio.src))return;if(!settleNativeSeek())return;lastKnownTime=audio.currentTime||0;publishNarration();nativeSeekReady();syncReaderPlayback();save();});
 audio.addEventListener('loadeddata',nativeSeekReady);
 const normalPlayToggle=$('playPause').onclick;
 $('playPause').onclick=()=>{if(!nativeSeek){if(audio.ended&&asset&&!opening&&(!wantPlay||['interrupted','error'].includes(mediaState))&&!['failed','paused','cancelled'].includes(lastAudioState?.generation_status)){wantPlay=true;sourceReloadAttempts=0;return endHandler?.();}return normalPlayToggle();}if(wantPlay){userPause();return;}wantPlay=true;sourceReloadAttempts=0;pendingSeek=nativeSeek.seconds;loadAudio(audio.src+(audio.src.includes('?')?'&':'?')+'reader_seek_retry='+(sourceRevision+1));};
 window.Player.diagnostics=()=>transitionLog.map(entry=>({...entry}));
 window.Player.resolveNarration=resolveNarration;
 window.Player.readerResume=async id=>{await openingTask;if(id!==bookId||!narrationEngaged)return null;const location=await resolveNarration(id);return location?{location,following:followEnabled&&!followSuspended}:null;};
 window.Player.startReader=startReader;
 window.Player.togglePlayback=()=>$('playPause').click();
 window.addEventListener('online',()=>save());
 window.addEventListener('popstate',()=>navigate(new URLSearchParams(location.search).get('view')||'/library',true));
 const params=new URLSearchParams(location.search);if(params.get('view'))navigate(params.get('view'),true);
 if(params.get('listen')){const id=params.get('listen');if(savedCheckpoint(id)?.intent!=='stop')listen(id,Number(params.get('section'))||null,false);}
 else{const last=stored('webnovelListeningBook'),view=params.get('view')||'';if(last&&savedCheckpoint(last)?.intent!=='stop'&&(!view.startsWith('/reader/')||view.split('?')[0]===`/reader/${last}`))listen(last,null,false);}
})();
