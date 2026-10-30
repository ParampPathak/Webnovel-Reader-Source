"""Optional, recording-bound Edge word cues. Old recordings remain untouched."""
import hashlib
import json
import math
import re
from pathlib import Path


def normalized(text):
    return re.sub(r'\s+', ' ', text).strip()


def utf16(text):
    return len(text.encode('utf-16-le')) // 2


def publish(audio, text, events):
    audio, events = Path(audio), Path(events)
    if not events.is_file():
        return
    clean = normalized(text)
    cues, cursor, last = [], 0, -1
    for line in events.read_text(encoding='utf-8').splitlines():
        event = json.loads(line)
        if event.get('type') != 'WordBoundary':
            continue
        quote = normalized(event.get('text', ''))
        at, duration = event['offset'] / 10000000, event['duration'] / 10000000
        start = clean.find(quote, cursor) if quote else -1
        if start < 0 or not math.isfinite(at + duration) or at < last or duration < 0:
            return  # Never guess an alignment when metadata does not match text.
        cues.append(dict(at=at, duration=duration, offset=utf16(clean[:start]), quote=quote))
        cursor, last = start + len(quote), at
    if cues:
        payload = dict(schema=1, audio_sha256=hashlib.sha256(audio.read_bytes()).hexdigest(),
                       text_sha256=hashlib.sha256(clean.encode()).hexdigest(), cues=cues)
        target = audio.with_suffix('.timing.json')
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        temporary.replace(target)


def load(audio, checksum, text):
    """Only accept cues published with this exact immutable audio/text pair."""
    if not audio:
        return []
    path = Path(audio).with_suffix('.timing.json')
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return []
        data = json.loads(path.read_text(encoding='utf-8'))
        if (data.get('schema') != 1 or data.get('audio_sha256') != checksum or
                data.get('text_sha256') != hashlib.sha256(normalized(text).encode()).hexdigest()):
            return []
        cues, last = data['cues'], -1
        for cue in cues:
            if (not math.isfinite(cue['at'] + cue['duration']) or cue['at'] < last or cue['duration'] < 0 or
                    type(cue['offset']) is not int or cue['offset'] < 0 or not cue['quote']):
                return []
            last = cue['at']
        return cues
    except (OSError, ValueError, KeyError, TypeError):
        return []
