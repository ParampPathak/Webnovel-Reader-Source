"""Speech providers; imports never contact services or download models."""
import asyncio
import hashlib
import json
import re
from pathlib import Path
from edge_connection import configure_edge_endpoint

DEFAULT_VOICE = 'en-US-AndrewNeural'
FALLBACK_VOICES = [
    {'ShortName':name,'Locale':locale,'Gender':gender}
    for name,locale,gender in [
        ('en-US-AndrewNeural','en-US','Male'),
        ('en-US-AriaNeural','en-US','Female'),('en-US-GuyNeural','en-US','Male'),
        ('en-US-JennyNeural','en-US','Female'),('en-GB-SoniaNeural','en-GB','Female'),
        ('en-GB-RyanNeural','en-GB','Male'),('en-AU-NatashaNeural','en-AU','Female')]]


def speech_error_message(error):
    """Public error only: never expose SDK request URLs or token parameters."""
    status = getattr(error, 'status', None)
    message = str(error or '')
    if status is None:
        match = re.match(r"(?:Generation failed\.\s*)?(\d{3}),\s*message=", message)
        status = int(match[1]) if match else None
    if status == 403:
        return 'Microsoft rejected online speech (HTTP 403). Cached audio still works. Retry later.'
    if status == 429:
        return 'Microsoft is limiting speech requests (HTTP 429). Generation will retry with a delay.'
    if status and status >= 500:
        return f'Microsoft speech is temporarily unavailable (HTTP {status}). Retry later.'
    return re.sub(r"(?:https?|wss?)://[^\s\"<>]+", '[service URL omitted]', message)[:300]


def settings(voice=DEFAULT_VOICE, rate=0):
    if not isinstance(voice,str) or not re.fullmatch(r'[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9-]+Neural',voice):
        raise ValueError('Choose a valid Edge Neural voice.')
    if type(rate) is not int or not -50 <= rate <= 100:
        raise ValueError('Synthesis speed must be an integer from -50 to 100 percent.')
    return {'provider':'edge','voice':voice,'rate':rate,'format':'mp3','chunk_version':1}


def cache_key(book_id, section_id, text, config):
    content_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
    value = json.dumps([book_id,section_id,content_hash,config],sort_keys=True,separators=(',',':'))
    return hashlib.sha256(value.encode()).hexdigest(),content_hash


def text_chunks(text, max_bytes=3000):
    """UTF-8 bounded chunks with natural boundaries; no text lost at splits."""
    if max_bytes < 8:
        raise ValueError('Chunk budget is too small.')
    # XML control characters have no spoken meaning and are refused by Edge.
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',' ',text)
    start = 0
    while start < len(text):
        end = start
        size = 0
        while end < len(text):
            count = len(text[end].encode('utf-8'))
            if size+count > max_bytes:
                break
            size += count
            end += 1
        if end < len(text):
            window = text[start:end]
            boundaries = list(re.finditer(r'(?:[.!?。！？]\s+|\n+)',window))
            boundary = boundaries[-1].end() if boundaries else window.rfind(' ')+1
            if boundary > len(window)//2:
                end = start+boundary
        piece = text[start:end]
        if piece.strip():
            yield piece
        start = end


def chapter_text_chunks(text,first_bytes=3000,chunk_plan=None):
    # Existing assets keep their original3000-byte boundaries. A new asset
    # persists its shorter opening segment plan; restarts never change it.
    if chunk_plan:
        clean=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',' ',text).strip()
        offset=0
        for budget in chunk_plan:
            piece=next(text_chunks(clean[offset:],budget),'')
            if not piece:return
            yield piece;offset+=len(piece)
        yield from text_chunks(clean[offset:],chunk_plan[-1]);return
    if first_bytes==3000:
        yield from text_chunks(text);return
    clean=re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',' ',text).strip()
    first=next(text_chunks(clean,first_bytes),'')
    if first: yield first
    yield from text_chunks(clean[len(first):])


class EdgeProvider:
    name = 'edge'
    supports_checkpoint=True

    def synthesize(self,text,config,destination,checkpoint=None):
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError('edge-tts is not installed. Rebuild the application image with updated requirements.') from exc
        async def generate():
            configure_edge_endpoint()
            communicator = edge_tts.Communicate(text,config['voice'],rate=f"{config['rate']:+d}%",
                                                connect_timeout=10,receive_timeout=45,boundary='WordBoundary')
            async def save_checked():
                task=asyncio.create_task(communicator.save(str(destination),str(destination)+'.events'))
                try:
                    while not task.done():
                        await asyncio.wait({task},timeout=.5)
                        if checkpoint and not task.done(): await asyncio.to_thread(checkpoint)
                    await task
                finally:
                    if not task.done(): task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
            await asyncio.wait_for(save_checked(),timeout=120)
        try:
            asyncio.run(generate())
        except Exception as exc:
            # Worker cancellation/resource-safety exceptions must retain their
            # identity so the worker checkpoints rather than failing the job.
            if getattr(exc, 'status', None) is None:
                raise
            raise RuntimeError(speech_error_message(exc)) from exc
        if not Path(destination).is_file() or Path(destination).stat().st_size == 0:
            raise RuntimeError('Edge returned no audio.')

    def voices(self):
        import edge_tts
        async def fetch():
            return await asyncio.wait_for(edge_tts.list_voices(),timeout=20)
        return asyncio.run(fetch())


def assemble_mp3(parts,destination):
    """Remux chapter streams without re-encoding; write accurate seek metadata."""
    import shutil
    import subprocess
    executable=shutil.which('ffmpeg')
    if not executable:
        raise RuntimeError('ffmpeg is required to assemble chapter audio; it is included in the Docker image.')
    destination=Path(destination)
    listing=destination.parent/'parts.ffconcat'
    # Paths are generated internally and kept relative to this asset directory.
    with listing.open('w') as stream:
        stream.write('ffconcat version 1.0\n')
        for part in parts:
            part=Path(part)
            if part.parent!=destination.parent or not re.fullmatch(r'[0-9]+\.mp3',part.name):
                raise ValueError('Invalid audio chunk path.')
            stream.write("file '"+part.name+"'\n")
    errors=destination.parent/'assembly.log'
    with errors.open('wb') as log:
        result=subprocess.run([executable,'-nostdin','-hide_banner','-loglevel','error','-threads','1',
                               '-f','concat','-safe','1','-i',str(listing),'-c','copy','-map_metadata','-1',
                               '-write_xing','1','-f','mp3','-y',str(destination)],stderr=log,
                              stdout=subprocess.DEVNULL,timeout=90)
    if result.returncode or not destination.is_file() or not destination.stat().st_size:
        raise RuntimeError('Could not assemble chapter audio; inspect the asset assembly.log.')


def segment_duration(path):
    """Validate a completed MP3 before publishing it; bounded subprocess, no decoding."""
    import subprocess,math
    result=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,timeout=15,check=True)
    duration=float(result.stdout.strip())
    if not math.isfinite(duration) or duration<=0: raise RuntimeError('Edge returned an invalid MP3 segment.')
    return duration
