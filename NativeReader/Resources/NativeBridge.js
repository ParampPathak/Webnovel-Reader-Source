/* Runs at document start. Native owns audio; the original player.js is blocked. */
(() => {
  'use strict';
  const ownOrigin = location.origin;
  // Disable media even inside legacy pages and EPUB frames in this client.
  Object.defineProperty(HTMLMediaElement.prototype, 'play', {configurable:false, writable:false, value: function() { return Promise.reject(new DOMException('Audio belongs to the native player.', 'NotAllowedError')); }});
  if (window !== window.top) return;
  let state = {}, locationNow = null, initialized = false, mini = true, lastSequence = -1;
  const $ = id => document.getElementById(id);
  let openingReader = false, passageKey = '';
  const passageCache = new Map();
  const send = async (command, args = {}) => {
    const result = await window.webkit.messageHandlers.reader.postMessage({command, ...args});
    if (result?.error) throw new Error(result.error);
    return result;
  };
  let preferencesQueue = Promise.resolve();
  function savePreferences(values) {
    preferencesQueue = preferencesQueue.catch(()=>{}).then(async()=>{
      const response = await fetch('/api/edge/preferences',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});
      if (!response.ok) throw Error('Unable to save playback preferences');
    });
    return preferencesQueue;
  }
  const run = task => Promise.resolve(task).catch(error => { if ($('playerStatus')) $('playerStatus').textContent = error.message; });
  const reader = () => { try { return $('workspace')?.contentWindow?.ReaderNarration; } catch { return null; } };
  const renderTime = s => `${Math.floor((s||0)/60)}:${String(Math.floor((s||0)%60)).padStart(2,'0')}`;
  function setMini(value) { if (mini !== value) passageKey = ''; mini = value; if ($('playerPanel')) $('playerPanel').dataset.mini = String(value); if ($('expandPlayer')) { $('expandPlayer').textContent = value ? '⌃' : 'Collapse'; $('expandPlayer').setAttribute('aria-expanded',String(!value)); } window.PlayerSheet?.sync?.();if (!value && initialized) run(renderPassages()); }
  function navigate(path, replace = false) {
    if (!/^\/(library(?:\?|$)|reader\/|books\/|importer(?:\?|$)|jobs(?:\?|$)|settings(?:\?|$)|audio(?:\?|$))/.test(path)) path='/library';
    const url = new URL(path, ownOrigin); if (url.origin !== ownOrigin) return;
    document.body.dataset.reading = String(path.startsWith('/reader/')); setMini(true);
    $('workspace').src = url.href;
    history[replace?'replaceState':'pushState'](null,'','/?view='+encodeURIComponent(path));
  }
  async function live() { const result = await send('state'); receive(result); return state; }
  async function openReader(expand = false) {
    if (openingReader) return;
    openingReader = true;
    try {
      await live(); if (!state.book_id || !state.engaged) return;
      if (reader()?.bookId === state.book_id) {
        await reader().follow?.(); setMini(!expand); return;
      }
      // Navigation changes following, never the native playback session or intent.
      receive(await send('follow', {enabled:true, suspended:false}));
      const path = `/reader/${encodeURIComponent(state.book_id)}`;
      if (new URL($('workspace').src, ownOrigin).pathname !== path)
        navigate(`${path}?section=${state.section_id}&narration=1`);
      else setMini(true);
    } finally { openingReader = false; }
  }
  async function renderPassages() {
    if (mini || !initialized) return;
    const passage = $('spokenPassages'), loc = locationNow;
    if (!passage) return;
    const asset = state.asset_id, index = state.segment_index;
    const key = `${state.book_id}:${asset}:${index}:${loc?.anchor || ''}`;
    if (key === passageKey) return;
    passageKey = key;
    if (!loc || !Number.isInteger(index) || !asset || asset.startsWith('legacy-')) {
      passage.textContent = 'Passage timing is unavailable for this recording.'; return;
    }
    const cacheKey = `${asset}:${index}`;
    let rows = passageCache.get(cacheKey);
    if (!rows) rows = await Promise.all([index-1,index,index+1].map(async n => {
      if (n < 0) return null;
      try {
        const response = await fetch(`/api/audio/${encodeURIComponent(asset)}/location/${n}`);
        if (!response.ok) return null;
        return {n, location:await response.json()};
      } catch { return null; }
    }));
    if (key !== passageKey || mini) return;
    passageCache.set(cacheKey, rows);
    if (passageCache.size > 5) passageCache.delete(passageCache.keys().next().value);
    if (!rows.some(row => row?.n === index)) rows = [...rows, {n:index,location:loc}].filter(Boolean).sort((a,b)=>a.n-b.n);
    passage.replaceChildren(...rows.filter(Boolean).map(row => {
      const p = document.createElement('p');
      p.className = row.n === index ? 'current-passage' : 'nearby-passage';
      p.textContent = row.n === index ? loc.quote : row.n < index ? row.location.quote.slice(-280) : row.location.quote.slice(0,280);
      if (row.n === index && loc.precision === 'word') {
        const before = document.createElement('span'), word = document.createElement('mark'), after = document.createElement('span');
        // Context belongs to the expanded player, not a replacement book page.
        before.textContent = loc.before || ''; word.textContent = loc.quote; after.textContent = loc.after || '';
        p.replaceChildren(before,word,after);
      }
      return p;
    }));
    const current = passage.querySelector('.current-passage');
    if (current) passage.scrollTo?.({top:current.offsetTop-passage.offsetTop-16,behavior:'smooth'});
  }
  function receive(next) {
    if (!next || next.revision < lastSequence) return;
    lastSequence = next.revision; state = next; locationNow = next.narration || null;
    if (!initialized) return;
    $('playerPanel').hidden = !state.engaged;
    document.body.dataset.readerAudio=String(!!state.engaged&&reader()?.bookId===state.book_id);
    $('playingBook').textContent = state.title || 'Audiobook'; $('playingChapter').textContent = state.chapter_title || '';
    $('playerStatus').textContent = state.status || '';
    for (const id of ['playPause','miniPlay']) { $(id).textContent = state.intent ? 'Ⅱ' : '▶'; $(id).setAttribute('aria-label',state.state==='error'&&state.retry_target_available?'Retry selected listening position':state.intent?'Pause narration':'Play narration'); }
    $('playerCover').hidden = !state.cover_url; if (state.cover_url) $('playerCover').src = state.cover_url;
    $('audioTime').textContent = `${renderTime(state.seconds)} / ${renderTime(state.total)}`;
    if (document.activeElement !== $('audioSeek')) $('audioSeek').value = state.total > 0 ? state.seconds/state.total*1000 : 0;
    $('audioSeek').disabled = !(state.total > 0); $('miniProgress').value = Number($('audioSeek').value);
    $('playbackSpeed').value = String(state.playback_rate || 1); $('volume').value = state.volume ?? 1;
    if (state.voice && [...$('playerVoice').options].some(o=>o.value===state.voice)) $('playerVoice').value = state.voice;
    $('synthesisSpeed').value = String(state.synthesis_rate || 0); $('synthesisSpeed').disabled = state.voice === 'legacy';
    $('audioPrevious').disabled = !state.previous_id; $('audioNext').disabled = !state.next_id;
    const activeReader=reader();
    if (activeReader && activeReader.bookId === state.book_id) {
      activeReader.playback?.({...state, control_pause: state.intent, progress: state.total ? state.seconds/state.total*1000 : 0});
      if (locationNow) activeReader.update?.(locationNow);
    }
    const network = $('networkState'); if (network) { network.hidden = !state.sync_conflict; network.replaceChildren(); if(state.sync_conflict) {
      network.append(document.createTextNode('Listening changed in another player. '));
      for(const [label,resolution] of [['Resume server position','server'],['Keep local position','local']]) {const b=document.createElement('button');b.textContent=label;b.onclick=()=>run(send('resolve',{resolution}));network.append(b);}
    }}
    $('sleepStatus').textContent = state.sleep_deadline ? 'Sleep timer: '+new Date(state.sleep_deadline*1000).toLocaleTimeString() : '';
    run(renderPassages());
  }
  window.NativeReader = Object.freeze({receive});
  const Player = {
    navigate, playing: id => id === state.book_id && state.playing,
    expanded: () => !mini && state.engaged,
    theme: value => document.body.dataset.readerTheme = value,
    listen: async (id, sectionId=null, autoplay=true) => {setMini(true); return send('listen',{book_id:id,section_id:sectionId,autoplay});},
    startReader: async (id,position) => {
      if (!Number.isInteger(position?.section_id) || !Number.isInteger(position.normalized_offset) || position.normalized_offset < 0)
        throw Error('The current page has no valid narration position.');
      const result=await send('listen',{book_id:id,section_id:position.section_id,autoplay:true,normalized_offset:position.normalized_offset});receive(result);return result;
    },
    toggleReader: async (id,position) => {
      if (id===state.book_id && state.intent) return send('pause');
      return send('listen',{book_id:id,section_id:position.section_id,autoplay:true,normalized_offset:position.normalized_offset});
    },
    stopReader: () => run(send('stop')),
    togglePlayback: () => run(send('toggle')),
    expand: () => setMini(false),
    seek: delta => run(send('skip',{seconds:delta})),
    narration: () => locationNow,
    resolveNarration: async id => {await live();return state.book_id===id?locationNow:null;},
    readerResume: async id => {await live();return state.engaged&&state.book_id===id&&locationNow?{location:locationNow,following:state.following&&!state.follow_suspended}:null;},
    setFollow: (id,enabled,suspended=false) => {if(id===state.book_id)run(send('follow',{enabled,suspended}));},
    rememberReader: id => {const route=new URLSearchParams(location.search);if((route.get('view')||'').split('?')[0]===`/reader/${id}`)history.replaceState(null,'','/?view='+encodeURIComponent('/reader/'+id));},
    forgetBook: id => {if(id===state.book_id)run(send('stop'));},
    metrics: () => ({...state,native:true}), diagnostics: () => [{native:true, revision:state.revision, session:state.session_id, sequence:state.event_sequence}]
  };
  window.Player = Object.freeze(Player);
  window.addEventListener('DOMContentLoaded', async () => {
    if (!$('workspace') || !$('playerPanel')) return;
    initialized = true; setMini(true);
    const click = (id,fn) => { if($(id))$(id).onclick=e=>{e.preventDefault();e.stopPropagation();run(fn());}; };
    $('playerPanel').addEventListener('click',e=>{
      if (mini && !e.target.closest('button,input,select,a,label')) setMini(false);
    });
    for (const id of ['playPause','miniPlay']) click(id,()=>send('toggle'));
    for (const id of ['rewind','miniPrevious']) click(id,()=>send('skip',{seconds:-15}));
    for (const id of ['forward','miniNext']) click(id,()=>send('skip',{seconds:15}));
    click('audioPrevious',()=>send('chapter',{direction:'previous'}));click('audioNext',()=>send('chapter',{direction:'next'}));
    click('readPlaying',()=>openReader());click('miniOpen',()=>setMini(false));click('playerCover',()=>setMini(false));
    click('expandPlayer',()=>setMini(!mini));click('closePlayer',()=>send('stop'));
    $('audioSeek').onchange=()=>run(send('seek',{seconds:state.total*Number($('audioSeek').value)/1000}));
    $('playbackSpeed').onchange=()=>{const value=Number($('playbackSpeed').value);run(send('rate',{value}));run(savePreferences({playback_rate:value}));};
    $('volume').oninput=()=>{const value=Number($('volume').value);run(send('volume',{value}));run(savePreferences({volume:value}));};
    const changeVoice=async()=>{const voice=$('playerVoice').value,rate=Number($('synthesisSpeed').value);if(voice!=='legacy')await savePreferences({voice,rate});return send('voice',{voice,rate});};
    $('playerVoice').onchange=()=>run(changeVoice());$('synthesisSpeed').onchange=()=>run(changeVoice());
    $('sleepTimer').onchange=()=>run(send('sleep',{minutes:Number($('sleepTimer').value)}));
    click('saveDefaultVoice',async()=>{if($('playerVoice').value==='legacy')throw Error('Choose an Edge voice to save a default.');await savePreferences({voice:$('playerVoice').value,rate:Number($('synthesisSpeed').value)});$('defaultVoiceStatus').textContent='Default voice and synthesis speed saved.';});
    document.addEventListener('click',e=>{const a=e.target.closest('a[data-view]');if(a){e.preventDefault();navigate(a.getAttribute('href'));}});
    $('workspace').addEventListener('load',()=>run(live()));
    window.addEventListener('popstate',()=>navigate(new URLSearchParams(location.search).get('view')||'/library',true));
    window.addEventListener('native-reader-foreground',()=>{try{const frame=$('workspace')?.contentWindow;frame?.dispatchEvent(new frame.Event('pageshow'));}catch{}run(live());});
    window.addEventListener('pageshow',()=>run(live()));document.addEventListener('visibilitychange',()=>{if(!document.hidden)run(live());});
    try {const v=await (await fetch('/api/edge/voices')).json();$('playerVoice').replaceChildren(...[{ShortName:'legacy',Locale:'Preserved Piper/audio'},...(v.items||[])].map(row=>{const o=document.createElement('option');o.value=row.ShortName;o.textContent=row.DisplayName||`${row.ShortName} · ${row.Locale}`;return o;}));}catch{}
    const params=new URLSearchParams(location.search);if(params.get('view'))navigate(params.get('view'),true);
    await run(live());
    if(params.get('listen')) {
      const book=params.get('listen'), section=Number(params.get('section'))||null;
      // A URL left by an earlier launch is not a new playback operation. WebKit
      // may reload while native audio has advanced many chapters in background.
      params.delete('listen');params.delete('section');
      history.replaceState(null,'',(location.pathname||'/')+(params.toString()?'?'+params.toString():'')+(location.hash||''));
      if(!state.book_id&&!state.engaged)await run(send('restore-selection',{book_id:book,section_id:section}));
    }
  });
})();
