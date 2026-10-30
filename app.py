import os
import html
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from urllib.parse import urlparse

import requests
from ebooklib import epub
from flask import Blueprint, Flask, current_app, jsonify, render_template, request, send_file
from audio_cache import protect_audio_response

from scraper import scrape_chapter
from discovery import discover_page as browse_page

legacy = Blueprint("legacy", __name__)


@legacy.before_request
def protect_legacy_writes():
    from library_api import same_origin_writes
    return same_origin_writes()


def data_path(*parts):
    return Path(current_app.config["DATA_DIR"]).joinpath(*parts)


def stored_audio_path(value):
    """Resolve historical /data paths against this instance's storage.

    Database references remain unchanged; copied-data rehearsals never need to
    rewrite production paths just to serve existing audio.
    """
    path = Path(value)
    if path.is_absolute() and Path('/data') in path.parents:
        return data_path(*path.relative_to('/data').parts)
    return path if path.is_absolute() else data_path(str(path))


KOKORO_URL = os.environ.get("KOKORO_URL", "http://kokoro-tts:8880/v1/audio/speech")
KOKORO_VOICES_URL = os.environ.get("KOKORO_VOICES_URL", "http://kokoro-tts:8880/v1/audio/voices")


# Piper runs inside the web app container, so there is no second TTS service to keep
# alive. "Fast" voices use the low-quality 16 kHz models; "Balanced" voices use
# the medium models. Playback speed is handled in the browser and is independent.
PIPER_VOICES = {
    "lessac_low": {
        "label": "Lessac · US English · Fast",
        "path": "en/en_US/lessac/low/en_US-lessac-low",
    },
    "ryan_low": {
        "label": "Ryan · US English · Fast",
        "path": "en/en_US/ryan/low/en_US-ryan-low",
    },
    "amy_low": {
        "label": "Amy · US English · Fast",
        "path": "en/en_US/amy/low/en_US-amy-low",
    },
    "lessac_medium": {
        "label": "Lessac · US English · Balanced",
        "path": "en/en_US/lessac/medium/en_US-lessac-medium",
    },
    "ryan_medium": {
        "label": "Ryan · US English · Balanced",
        "path": "en/en_US/ryan/medium/en_US-ryan-medium",
    },
    "amy_medium": {
        "label": "Amy · US English · Balanced",
        "path": "en/en_US/amy/medium/en_US-amy-medium",
    },
    "alan_medium": {
        "label": "Alan · UK English · Balanced",
        "path": "en/en_GB/alan/medium/en_GB-alan-medium",
    },
    "jenny_medium": {
        "label": "Jenny Dioco · UK English · Balanced",
        "path": "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium",
    },
}

ESPEAK_VOICES = {
    "en-us": "US English · Default",
    "en-us+f3": "US English · Female",
    "en-us+m3": "US English · Male",
    "en-gb": "UK English · Default",
    "en-gb+f3": "UK English · Female",
    "en-gb+m3": "UK English · Male",
}

LEGACY_PIPER_VALUES = {
    "Lessac (US, neutral)": "lessac_medium",
    "Ryan (US, male)": "ryan_medium",
    "Amy (US, female)": "amy_medium",
    "Alan (GB, male)": "alan_medium",
    "Jenny (GB, female)": "jenny_medium",
}

KOKORO_FALLBACK_VOICES = [
    "af_heart",
    "af_bella",
    "af_nicole",
    "af_sarah",
    "af_nova",
    "af_sky",
    "am_michael",
    "am_fenrir",
    "am_puck",
    "bf_emma",
    "bf_isabella",
    "bm_george",
    "bm_lewis",
]

_piper_cache = {}
_piper_lock = threading.Lock()
_piper_slots = threading.BoundedSemaphore(max(1, min(2, int(os.environ.get("PIPER_CONCURRENCY", "2") or "2"))))
_kokoro_slots = threading.BoundedSemaphore(max(1, min(2, int(os.environ.get("KOKORO_CONCURRENCY", "1") or "1"))))
_work_event = threading.Event()



def get_db():
    conn = sqlite3.connect(data_path("webnovel.db"), timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the TTS worker thread(s) write while the UI polls /api/queue at the
    # same time without throwing "database is locked". Without this, a lock error
    # thrown outside a try/except used to be able to silently kill the worker
    # thread, which is what made chapters appear stuck on "pending" forever.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_column(conn, table, name, definition):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migrate_chapters_schema(conn):
    """Make databases created by older Webnovel Audio releases compatible in place."""
    expected = {
        "novel_title": "TEXT",
        "chapter_title": "TEXT",
        "source_url": "TEXT",
        "next_url": "TEXT",
        "content": "TEXT",
        "status": "TEXT DEFAULT 'pending'",
        "audio_path": "TEXT",
        "audio_format": "TEXT DEFAULT 'mp3'",
        "queue_order": "INTEGER",
        "created_at": "TEXT",
        "engine_used": "TEXT",
        "voice_used": "TEXT",
        "generation_seconds": "REAL",
        "error_message": "TEXT",
        "generation_progress": "REAL DEFAULT 0",
        "requested_engine": "TEXT",
        "requested_voice": "TEXT",
    }
    for name, definition in expected.items():
        _ensure_column(conn, "chapters", name, definition)

    # Older versions may have NULL queue positions or no explicit audio format.
    # Backfill without deleting or re-adding any existing chapters.
    conn.execute("UPDATE chapters SET queue_order = id WHERE queue_order IS NULL")
    conn.execute("UPDATE chapters SET status = 'pending' WHERE status IS NULL OR status = ''")
    conn.execute(
        """
        UPDATE chapters
        SET audio_format = CASE
            WHEN lower(COALESCE(audio_path, '')) LIKE '%.wav' THEN 'wav'
            ELSE 'mp3'
        END
        WHERE audio_format IS NULL OR audio_format = ''
        """
    )


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chapters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            novel_title TEXT,
            chapter_title TEXT,
            source_url TEXT UNIQUE,
            next_url TEXT,
            content TEXT,
            status TEXT DEFAULT 'pending',
            audio_path TEXT,
            audio_format TEXT DEFAULT 'mp3',
            queue_order INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    _migrate_chapters_schema(conn)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audio_segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chapter_id INTEGER NOT NULL,
            segment_index INTEGER NOT NULL,
            audio_path TEXT NOT NULL,
            audio_format TEXT DEFAULT 'mp3',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chapter_id, segment_index)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audio_segments_chapter ON audio_segments(chapter_id, segment_index)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    defaults = {
        "engine": "piper",
        "piper_voice": "lessac_low",
        "kokoro_voice": "af_heart",
        "espeak_voice": "en-us",
        "engine_user_set": "0",
    }
    for key, value in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # v6.1 restores Piper as the normal listening/generation engine. v6 briefly
    # migrated untouched installs to eSpeak NG so audio could start instantly,
    # but that made the browser/device voices dominate playback. This one-time
    # migration moves the current install back to Piper and pins waiting jobs to
    # the selected Piper voice. It never runs again after the first v6.1 start,
    # so later engine changes made in Settings are respected.
    migrated = conn.execute("SELECT value FROM settings WHERE key = 'v61_piper_migrated'").fetchone()
    if not migrated:
        piper_row = conn.execute("SELECT value FROM settings WHERE key = 'piper_voice'").fetchone()
        piper_voice = piper_row["value"] if piper_row and piper_row["value"] else "lessac_low"
        piper_voice = LEGACY_PIPER_VALUES.get(piper_voice, piper_voice)
        if piper_voice not in PIPER_VOICES:
            piper_voice = "lessac_low"
        conn.execute("UPDATE settings SET value = 'piper' WHERE key = 'engine'")
        conn.execute("UPDATE settings SET value = ? WHERE key = 'piper_voice'", (piper_voice,))
        conn.execute("UPDATE settings SET value = '1' WHERE key = 'engine_user_set'")
        conn.execute(
            "UPDATE chapters SET requested_engine = 'piper', requested_voice = ? "
            "WHERE status IN ('pending','processing')",
            (piper_voice,),
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('v61_piper_migrated', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = '1'"
        )

    # A container restart should not leave a chapter permanently stuck at "processing".
    conn.execute("UPDATE chapters SET generation_progress = 100 WHERE status = 'ready'")
    conn.execute("UPDATE chapters SET generation_progress = 0 WHERE status IN ('pending', 'failed')")

    # v4.0 shipped with a missing progress helper. Re-queue only chapters that
    # failed for that specific software bug so they recover automatically after
    # upgrading, without touching unrelated failures.
    conn.execute(
        """
        UPDATE chapters
        SET status = 'pending', generation_progress = 0, error_message = NULL
        WHERE status = 'failed'
          AND error_message LIKE '%_set_generation_progress%not defined%'
        """
    )

    old_voice = conn.execute("SELECT value FROM settings WHERE key = 'piper_voice'").fetchone()
    if old_voice and old_voice["value"] in LEGACY_PIPER_VALUES:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = 'piper_voice'",
            (LEGACY_PIPER_VALUES[old_voice["value"]],),
        )

    # v6 pins an engine/voice to every queued chapter. Changing the default voice
    # no longer mutates jobs already being generated, which prevents voice changes
    # from leaving a queue in an ambiguous/stuck state.
    default_engine = conn.execute("SELECT value FROM settings WHERE key = 'engine'").fetchone()["value"]
    if default_engine == "piper":
        default_voice = conn.execute("SELECT value FROM settings WHERE key = 'piper_voice'").fetchone()["value"]
    elif default_engine == "espeak":
        default_voice = conn.execute("SELECT value FROM settings WHERE key = 'espeak_voice'").fetchone()["value"]
    else:
        default_voice = conn.execute("SELECT value FROM settings WHERE key = 'kokoro_voice'").fetchone()["value"]
    conn.execute("UPDATE chapters SET requested_engine = COALESCE(NULLIF(requested_engine,''), engine_used, ?) WHERE requested_engine IS NULL OR requested_engine = ''", (default_engine,))
    conn.execute("UPDATE chapters SET requested_voice = COALESCE(NULLIF(requested_voice,''), voice_used, ?) WHERE requested_voice IS NULL OR requested_voice = ''", (default_voice,))

    conn.commit()
    conn.close()




def get_setting(key):
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_setting(key, value):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def _set_generation_progress(chapter_id, value):
    """Persist live TTS progress for the queue/player UI."""
    try:
        progress = max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return
    conn = get_db()
    conn.execute(
        "UPDATE chapters SET generation_progress = ? WHERE id = ?",
        (progress, chapter_id),
    )
    conn.commit()
    conn.close()


def normalize_piper_voice(value):
    if value in PIPER_VOICES:
        return value
    if value in LEGACY_PIPER_VALUES:
        return LEGACY_PIPER_VALUES[value]
    return "lessac_low"


def _download_file(url, destination):
    destination = Path(destination)
    temp_path = destination.with_suffix(destination.suffix + ".part")
    with requests.get(url, stream=True, timeout=(10, 300)) as response:
        response.raise_for_status()
        with open(temp_path, "wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
    temp_path.replace(destination)


def ensure_piper_voice(voice_id):
    """Download a Piper voice once, then keep the loaded model in memory."""
    voice_id = normalize_piper_voice(voice_id)
    with _piper_lock:
        voice_info = PIPER_VOICES[voice_id]
        hf_path = voice_info["path"]
        model_name = hf_path.rsplit("/", 1)[-1]
        onnx_path = data_path("piper_voices") / f"{model_name}.onnx"
        json_path = data_path("piper_voices") / f"{model_name}.onnx.json"
        base_url = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{hf_path}"

        if not onnx_path.exists():
            _download_file(base_url + ".onnx", onnx_path)
        if not json_path.exists():
            _download_file(base_url + ".onnx.json", json_path)

        # Piper generation is serialized in v6.1, so one loaded model can be
        # safely reused by the prewarm thread and the worker. v6 keyed the cache
        # by thread name, which meant the voice was loaded twice and the first
        # real chapter paid the model-load cost again.
        cache_key = voice_id
        if cache_key not in _piper_cache:
            from piper import PiperVoice
            _piper_cache[cache_key] = PiperVoice.load(str(onnx_path))
        return _piper_cache[cache_key]


def _split_tts_chunks(text, max_chars=1400):
    chunks = []
    current = ""
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        pieces = [paragraph[i:i + max_chars] for i in range(0, len(paragraph), max_chars)] if len(paragraph) > max_chars else [paragraph]
        for piece in pieces:
            if len(current) + len(piece) + 1 > max_chars:
                if current:
                    chunks.append(current)
                current = piece
            else:
                current = f"{current}\n{piece}".strip()
    if current:
        chunks.append(current)
    return chunks or [text.strip()]


def _split_stream_chunks(text):
    """Tiny first chunk for fast-start playback, then larger chunks for throughput."""
    cleaned = re.sub(r"[ \t]+", " ", text or "").strip()
    if not cleaned:
        return [""]
    sentences = re.split(r"(?<=[.!?…])\s+", cleaned)
    chunks = []
    current = ""
    target = 140
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        # Very long sentences still need a hard split.
        pieces = [sentence[i:i + target] for i in range(0, len(sentence), target)] if len(sentence) > target * 2 else [sentence]
        for piece in pieces:
            if current and len(current) + len(piece) + 1 > target:
                chunks.append(current)
                current = piece
                target = 900
            else:
                current = f"{current} {piece}".strip()
    if current:
        chunks.append(current)
    return chunks or [cleaned]


def _clear_segments(chapter_id):
    conn = get_db()
    rows = conn.execute("SELECT audio_path FROM audio_segments WHERE chapter_id = ?", (chapter_id,)).fetchall()
    conn.execute("DELETE FROM audio_segments WHERE chapter_id = ?", (chapter_id,))
    conn.commit()
    conn.close()
    for row in rows:
        path = stored_audio_path(row["audio_path"])
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass


def _register_segment(chapter_id, segment_index, audio_path, audio_format="mp3"):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO audio_segments (chapter_id, segment_index, audio_path, audio_format) VALUES (?, ?, ?, ?)",
        (chapter_id, segment_index, str(audio_path), audio_format),
    )
    conn.commit()
    conn.close()


def _encode_wav_to_mp3(wav_path, mp3_path):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path), "-ac", "1", "-b:a", "64k", str(mp3_path)],
        check=True,
    )


def _concat_wav_segments(paths, out_path):
    """Join same-voice WAV chunks without invoking ffmpeg for every chunk."""
    if not paths:
        raise RuntimeError("No audio segments were generated.")
    params = None
    frames = []
    for path in paths:
        with wave.open(str(path), "rb") as src:
            current = src.getparams()
            signature = (current.nchannels, current.sampwidth, current.framerate, current.comptype)
            if params is None:
                params = current
                expected = signature
            elif signature != expected:
                raise RuntimeError("Audio chunk formats did not match.")
            frames.append(src.readframes(src.getnframes()))
    with wave.open(str(out_path), "wb") as dst:
        dst.setparams(params)
        for chunk in frames:
            dst.writeframes(chunk)


def _concat_mp3_segments(paths, out_path):
    if not paths:
        raise RuntimeError("No audio segments were generated.")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=str(out_path.parent)) as listing:
        list_path = Path(listing.name)
        for path in paths:
            escaped = str(Path(path).resolve()).replace("'", "'\\''")
            listing.write(f"file '{escaped}'\n")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(out_path)],
            check=True,
        )
    finally:
        list_path.unlink(missing_ok=True)


def synthesize_piper(text, out_path, voice_id, chapter_id=None, progress=None):
    """Generate WAV chunks directly for fastest startup; encode MP3 once at the end."""
    if progress:
        progress(1)
    voice = ensure_piper_voice(voice_id)
    if progress:
        progress(3)
    chunks = _split_stream_chunks(text)
    segment_paths = []
    for index, chunk in enumerate(chunks):
        segment_path = data_path("audio", "segments") / f"{chapter_id}-{index:04d}.wav"
        with wave.open(str(segment_path), "wb") as wav_file:
            if hasattr(voice, "synthesize_wav"):
                voice.synthesize_wav(chunk, wav_file)
            else:
                voice.synthesize(chunk, wav_file)
        segment_paths.append(segment_path)
        if chapter_id is not None:
            _register_segment(chapter_id, index, segment_path, "wav")
        if progress:
            progress(3 + ((index + 1) / len(chunks)) * 92)
    combined_wav = out_path.with_suffix(".combined.wav")
    try:
        _concat_wav_segments(segment_paths, combined_wav)
        if progress:
            progress(97)
        _encode_wav_to_mp3(combined_wav, out_path)
    finally:
        combined_wav.unlink(missing_ok=True)
    if progress:
        progress(99)

def synthesize_kokoro(text, out_path, voice, chapter_id=None, progress=None):
    chunks = _split_stream_chunks(text)
    segment_paths = []
    if progress:
        progress(2)
    for index, chunk in enumerate(chunks):
        response = requests.post(
            KOKORO_URL,
            json={"model": "kokoro", "input": chunk, "voice": voice, "response_format": "mp3", "speed": 1.0},
            timeout=240,
        )
        response.raise_for_status()
        segment_path = data_path("audio", "segments") / f"{chapter_id}-{index:04d}.mp3"
        segment_path.write_bytes(response.content)
        segment_paths.append(segment_path)
        if chapter_id is not None:
            _register_segment(chapter_id, index, segment_path)
        if progress:
            progress(2 + ((index + 1) / len(chunks)) * 96)
    _concat_mp3_segments(segment_paths, out_path)


def synthesize_espeak(text, out_path, voice, chapter_id=None, progress=None):
    chunks = _split_stream_chunks(text)
    segment_paths = []
    if progress:
        progress(1)
    for index, chunk in enumerate(chunks):
        segment_path = data_path("audio", "segments") / f"{chapter_id}-{index:04d}.wav"
        subprocess.run(
            ["espeak-ng", "-v", voice, "-s", "180", "-w", str(segment_path)],
            input=chunk, text=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        segment_paths.append(segment_path)
        if chapter_id is not None:
            _register_segment(chapter_id, index, segment_path, "wav")
        if progress:
            progress(1 + ((index + 1) / len(chunks)) * 94)
    combined_wav = out_path.with_suffix(".combined.wav")
    try:
        _concat_wav_segments(segment_paths, combined_wav)
        if progress:
            progress(97)
        _encode_wav_to_mp3(combined_wav, out_path)
    finally:
        combined_wav.unlink(missing_ok=True)
    if progress:
        progress(99)

def kokoro_available():
    try:
        response = requests.get(KOKORO_VOICES_URL, timeout=2)
        return response.ok
    except requests.RequestException:
        return False


def _friendly_kokoro_label(voice_id):
    language = {
        "a": "US English",
        "b": "UK English",
        "j": "Japanese",
        "z": "Mandarin",
        "e": "Spanish",
        "f": "French",
        "h": "Hindi",
    }.get(voice_id[:1], "Kokoro")
    gender = {"f": "Female", "m": "Male"}.get(voice_id[1:2], "")
    name = voice_id[3:].replace("_", " ").title() if len(voice_id) > 3 else voice_id
    parts = [name, language, gender]
    return " · ".join(part for part in parts if part)


def _normalize_kokoro_voice_response(data):
    if isinstance(data, dict):
        if isinstance(data.get("voices"), list):
            data = data["voices"]
        elif isinstance(data.get("data"), list):
            data = data["data"]
        else:
            data = []

    normalized = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                voice_id = item
                label = _friendly_kokoro_label(voice_id)
            elif isinstance(item, dict):
                voice_id = item.get("id") or item.get("name") or item.get("voice")
                label = item.get("description") or item.get("label") or item.get("name")
                if voice_id and (not label or label == voice_id):
                    label = _friendly_kokoro_label(voice_id)
            else:
                continue
            if voice_id:
                normalized.append({"id": voice_id, "label": label or voice_id})
    return normalized


def _current_generation_profile():
    engine = get_setting("engine") or "piper"
    if engine == "piper":
        voice = normalize_piper_voice(get_setting("piper_voice"))
    elif engine == "espeak":
        voice = get_setting("espeak_voice") or "en-us"
    else:
        voice = get_setting("kokoro_voice") or "af_heart"
    return engine, voice


def _wake_workers():
    _work_event.set()


def _claim_next_pending():
    """Atomically claim one pending chapter so multiple worker threads never grab
    the same row. Looks at a handful of candidates in queue order and claims the
    first one that hasn't already been taken by another worker."""
    conn = get_db()
    try:
        candidates = conn.execute(
            "SELECT id FROM chapters WHERE status = 'pending' ORDER BY queue_order ASC LIMIT 8"
        ).fetchall()
        for candidate in candidates:
            cursor = conn.execute(
                "UPDATE chapters SET status = 'processing', generation_progress = 0, error_message = NULL "
                "WHERE id = ? AND status = 'pending'",
                (candidate["id"],),
            )
            conn.commit()
            if cursor.rowcount:
                return conn.execute("SELECT * FROM chapters WHERE id = ?", (candidate["id"],)).fetchone()
        return None
    finally:
        conn.close()


def _process_one_chapter(row):
    engine = row["requested_engine"] or get_setting("engine") or "piper"
    voice = row["requested_voice"]
    if engine == "piper":
        voice = normalize_piper_voice(voice or get_setting("piper_voice"))
    elif engine == "espeak":
        voice = voice if voice in ESPEAK_VOICES else (get_setting("espeak_voice") or "en-us")
    else:
        voice = voice or get_setting("kokoro_voice") or "af_heart"
    ext = "mp3"

    audio_path = data_path("audio") / f"{row['id']}.{ext}"
    started = time.monotonic()
    try:
        _clear_segments(row["id"])
        progress = lambda value: _set_generation_progress(row["id"], value)
        if engine == "piper":
            with _piper_slots:
                synthesize_piper(row["content"], audio_path, voice, chapter_id=row["id"], progress=progress)
        elif engine == "espeak":
            synthesize_espeak(row["content"], audio_path, voice, chapter_id=row["id"], progress=progress)
        else:
            with _kokoro_slots:
                synthesize_kokoro(row["content"], audio_path, voice, chapter_id=row["id"], progress=progress)
        elapsed = round(time.monotonic() - started, 1)
        conn = get_db()
        conn.execute(
            """
            UPDATE chapters
            SET status = 'ready', audio_path = ?, audio_format = ?,
                engine_used = ?, voice_used = ?, generation_seconds = ?,
                generation_progress = 100, error_message = NULL
            WHERE id = ?
            """,
            (str(audio_path), ext, engine, voice, elapsed, row["id"]),
        )
        conn.commit()
        conn.close()
        print(f"TTS ready chapter={row['id']} engine={engine} voice={voice} seconds={elapsed}", flush=True)
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 1)
        print(f"TTS failed chapter={row['id']} engine={engine}: {exc}", flush=True)
        if audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                pass
        conn = get_db()
        conn.execute(
            """
            UPDATE chapters
            SET status = 'failed', engine_used = ?, voice_used = ?,
                generation_seconds = ?, generation_progress = 0, error_message = ?
            WHERE id = ?
            """,
            (engine, voice, elapsed, str(exc)[:500], row["id"]),
        )
        conn.commit()
        conn.close()


def worker_loop(worker_name):
    """Runs forever in a daemon thread. The whole body is wrapped in a
    try/except: previously an uncaught exception here (e.g. a transient SQLite
    lock error) would silently kill this thread, and every future chapter would
    sit at "pending" forever with nothing visibly wrong. Now any error is logged
    and the loop keeps going."""
    while True:
        try:
            row = _claim_next_pending()
            if not row:
                _work_event.wait(timeout=0.35)
                _work_event.clear()
                continue
            _process_one_chapter(row)
        except Exception:
            current_app.logger.exception(f"[{worker_name}] worker loop hit an unexpected error; continuing")
            time.sleep(2)


def _settings_payload():
    piper_voice = normalize_piper_voice(get_setting("piper_voice"))
    return {
        "engine": get_setting("engine") or "piper",
        "piper_voice": piper_voice,
        "kokoro_voice": get_setting("kokoro_voice") or "af_heart",
        "espeak_voice": get_setting("espeak_voice") or "en-us",
        "kokoro_available": kokoro_available(),
    }


def _valid_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def _insert_chapter(conn, url, refresh_duplicate_next=False):
    existing = conn.execute(
        "SELECT id, novel_title, next_url, chapter_title FROM chapters WHERE source_url = ?", (url,)
    ).fetchone()
    if existing:
        result = {"duplicate": True, **dict(existing)}
        # Older scraper versions may have saved this chapter before they could
        # detect the site's next link. Re-read it when Follow Next is requested.
        if refresh_duplicate_next and not existing["next_url"]:
            data = scrape_chapter(url)
            conn.execute(
                """
                UPDATE chapters
                SET novel_title = ?, chapter_title = ?, next_url = ?, content = ?
                WHERE id = ?
                """,
                (
                    data["novel_title"], data["chapter_title"],
                    data["next_url"], data["content"], existing["id"],
                ),
            )
            conn.commit()
            result.update({
                "novel_title": data["novel_title"],
                "chapter_title": data["chapter_title"],
                "next_url": data["next_url"],
            })
        return result

    data = scrape_chapter(url)
    max_order = conn.execute("SELECT COALESCE(MAX(queue_order), 0) AS m FROM chapters").fetchone()["m"]
    engine, voice = _current_generation_profile()
    cursor = conn.execute(
        """
        INSERT INTO chapters (novel_title, chapter_title, source_url, next_url, content, queue_order, generation_progress, requested_engine, requested_voice)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            data["novel_title"],
            data["chapter_title"],
            url,
            data["next_url"],
            data["content"],
            max_order + 1,
            engine,
            voice,
        ),
    )
    from library_store import attach_legacy
    attach_legacy(conn, cursor.lastrowid)
    conn.commit()
    return {
        "id": cursor.lastrowid,
        "chapter_title": data["chapter_title"],
        "novel_title": data["novel_title"],
        "next_url": data["next_url"],
        "duplicate": False,
    }


@legacy.route("/audio")
def index():
    return render_template("index.html")


@legacy.route("/api/health")
def health():
    return jsonify({"ok": True})


@legacy.route("/api/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        engine = data.get("engine")
        if engine is not None:
            if engine not in ("piper", "kokoro", "espeak"):
                return jsonify({"error": "Invalid engine"}), 400
            if engine == "kokoro" and not kokoro_available():
                return jsonify({
                    "error": "Kokoro is not running. Start it with: docker compose --profile kokoro up -d"
                }), 409
            set_setting("engine", engine)
            set_setting("engine_user_set", "1")

        if "piper_voice" in data:
            piper_voice = normalize_piper_voice(data["piper_voice"])
            set_setting("piper_voice", piper_voice)
        if "kokoro_voice" in data and data["kokoro_voice"]:
            set_setting("kokoro_voice", str(data["kokoro_voice"]))
        if "espeak_voice" in data and data["espeak_voice"] in ESPEAK_VOICES:
            set_setting("espeak_voice", str(data["espeak_voice"]))

        return jsonify({"ok": True, **_settings_payload()})
    return jsonify(_settings_payload())


@legacy.route("/api/voices")
def list_voices():
    engine = request.args.get("engine", "piper")
    if engine == "piper":
        return jsonify([
            {"id": voice_id, "label": voice["label"]}
            for voice_id, voice in PIPER_VOICES.items()
        ])
    if engine == "espeak":
        return jsonify([{"id": voice_id, "label": label} for voice_id, label in ESPEAK_VOICES.items()])

    try:
        response = requests.get(KOKORO_VOICES_URL, timeout=5)
        response.raise_for_status()
        voices = _normalize_kokoro_voice_response(response.json())
        if voices:
            return jsonify(voices)
    except (requests.RequestException, ValueError):
        pass

    return jsonify([
        {"id": voice_id, "label": _friendly_kokoro_label(voice_id)}
        for voice_id in KOKORO_FALLBACK_VOICES
    ])


@legacy.route("/api/add", methods=["POST"])
def add_chapter():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not _valid_url(url):
        return jsonify({"error": "Paste a valid http:// or https:// chapter URL."}), 400

    conn = get_db()
    try:
        result = _insert_chapter(conn, url)
    except Exception as exc:
        conn.close()
        return jsonify({"error": f"Failed to read that chapter: {exc}"}), 500
    conn.close()

    if result.get("duplicate"):
        return jsonify({"error": "That chapter is already in the queue.", "id": result["id"]}), 409
    _wake_workers()
    return jsonify(result)


@legacy.route("/api/add-and-follow", methods=["POST"])
def add_and_follow():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    try:
        count=int(data.get('count',1))
        if count<1: raise ValueError()
        from safe_http import public_url
        public_url(url,resolve=False)
    except (TypeError,ValueError) as exc:
        return jsonify(error=str(exc) or 'Enter a positive chapter count and public URL.'),400
    from platform_store import schedule
    identifier=schedule(data_path('webnovel.db'),'legacy-follow',None,{'count':count},[(url,{'url':url})])
    return jsonify(job_id=identifier,queued=True,requested=count,added=[],skipped=0,errors=[]),202


@legacy.route("/api/add-batch", methods=["POST"])
def add_batch():
    data=request.get_json(silent=True) or {}
    raw=data.get('urls',[])
    if isinstance(raw,str): raw=raw.splitlines()
    if not isinstance(raw,list): return jsonify(error='Expected chapter URLs.'),400
    urls=list(dict.fromkeys(str(value).strip() for value in raw if str(value).strip()))
    if not urls: return jsonify(error='Paste at least one chapter URL.'),400
    try:
        from safe_http import public_url
        for url in urls: public_url(url,resolve=False)
    except ValueError as exc:
        return jsonify(error=str(exc)),400
    from platform_store import schedule
    identifier=schedule(data_path('webnovel.db'),'legacy-batch',None,{},((url,{'url':url}) for url in urls))
    return jsonify(job_id=identifier,queued=True,requested=len(urls),added=[],skipped=0,errors=[]),202


@legacy.route("/api/queue")
def get_queue():
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, novel_title, chapter_title, source_url, status, queue_order,
                   audio_format, engine_used, voice_used, requested_engine, requested_voice, generation_seconds, generation_progress, error_message,
                   CASE WHEN content IS NOT NULL AND length(trim(content)) > 0 THEN 1 ELSE 0 END AS has_content,
                   (SELECT COUNT(*) FROM audio_segments s WHERE s.chapter_id = chapters.id) AS segment_count,
                   (SELECT s.book_id FROM book_sections s WHERE s.legacy_chapter_id=chapters.id) AS book_id,
                   (SELECT s.position FROM book_sections s WHERE s.legacy_chapter_id=chapters.id) AS book_position
            FROM chapters
            ORDER BY queue_order ASC, id ASC
            """
        ).fetchall()
        return jsonify([dict(row) for row in rows])
    except Exception as exc:
        current_app.logger.exception("Queue read failed")
        return jsonify({"error": f"Queue read failed: {exc}"}), 500
    finally:
        conn.close()


@legacy.route("/api/segments/<int:chapter_id>")
def get_segments(chapter_id):
    conn = get_db()
    chapter = conn.execute(
        "SELECT id, status, generation_progress, chapter_title, novel_title FROM chapters WHERE id = ?",
        (chapter_id,),
    ).fetchone()
    rows = conn.execute(
        "SELECT id, segment_index, audio_format FROM audio_segments WHERE chapter_id = ? ORDER BY segment_index ASC",
        (chapter_id,),
    ).fetchall()
    conn.close()
    if not chapter:
        return jsonify({"error": "Chapter not found"}), 404
    return jsonify({
        "chapter_id": chapter_id,
        "status": chapter["status"],
        "generation_progress": chapter["generation_progress"],
        "segments": [
            {"id": row["id"], "index": row["segment_index"], "url": f"/api/segment-audio/{row['id']}"}
            for row in rows
        ],
    })


@legacy.route("/api/segment-audio/<int:segment_id>")
@protect_audio_response
def get_segment_audio(segment_id):
    conn = get_db()
    row = conn.execute("SELECT audio_path, audio_format FROM audio_segments WHERE id = ?", (segment_id,)).fetchone()
    conn.close()
    if not row or not stored_audio_path(row["audio_path"]).exists():
        return jsonify({"error": "Audio segment not ready"}), 404
    mimetype = "audio/wav" if row["audio_format"] == "wav" else "audio/mpeg"
    return send_file(stored_audio_path(row["audio_path"]), mimetype=mimetype, conditional=True)


@legacy.route("/api/text/<int:chapter_id>")
def chapter_text(chapter_id):
    conn = get_db()
    row = conn.execute("SELECT id, novel_title, chapter_title, content FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Chapter not found"}), 404
    if not row["content"]:
        return jsonify({"error": "Chapter text is not available yet"}), 409
    return jsonify(dict(row))


@legacy.route("/api/apply-profile", methods=["POST"])
def apply_profile_to_queue():
    data = request.get_json(silent=True) or {}
    engine = data.get("engine") or get_setting("engine") or "piper"
    if engine not in ("piper", "kokoro", "espeak"):
        return jsonify({"error": "Invalid engine"}), 400
    voice = data.get("voice")
    if engine == "piper":
        voice = normalize_piper_voice(voice or get_setting("piper_voice"))
    elif engine == "espeak":
        voice = voice if voice in ESPEAK_VOICES else (get_setting("espeak_voice") or "en-us")
    else:
        voice = voice or get_setting("kokoro_voice") or "af_heart"
    conn = get_db()
    cursor = conn.execute(
        "UPDATE chapters SET requested_engine = ?, requested_voice = ? WHERE status IN ('pending','failed')",
        (engine, voice),
    )
    # Failed chapters are explicitly requeued when applying a new profile. Jobs
    # already processing finish with the profile they started with.
    conn.execute("UPDATE chapters SET status='pending', generation_progress=0, error_message=NULL WHERE status='failed'")
    conn.commit()
    changed = cursor.rowcount
    conn.close()
    _wake_workers()
    return jsonify({"ok": True, "changed": changed, "engine": engine, "voice": voice})


@legacy.route("/api/browse", methods=["POST"])
def browse_source():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not _valid_url(url):
        return jsonify({"error": "Paste a valid website URL."}), 400
    try:
        return jsonify(browse_page(url))
    except Exception as exc:
        return jsonify({"error": f"Could not browse that page: {exc}"}), 500


@legacy.route("/api/audio/<int:chapter_id>")
@protect_audio_response
def get_audio(chapter_id):
    conn = get_db()
    row = conn.execute(
        "SELECT audio_path, audio_format FROM chapters WHERE id = ?", (chapter_id,)
    ).fetchone()
    conn.close()
    if not row or not row["audio_path"] or not stored_audio_path(row["audio_path"]).exists():
        return jsonify({"error": "Audio not ready"}), 404
    mimetype = "audio/wav" if row["audio_format"] == "wav" else "audio/mpeg"
    return send_file(stored_audio_path(row["audio_path"]), mimetype=mimetype, conditional=True)


@legacy.route("/api/download/<int:chapter_id>")
@protect_audio_response
def download_audio(chapter_id):
    conn = get_db()
    row = conn.execute(
        "SELECT audio_path, audio_format, novel_title, chapter_title FROM chapters WHERE id = ?",
        (chapter_id,),
    ).fetchone()
    conn.close()
    if not row or not row["audio_path"] or not stored_audio_path(row["audio_path"]).exists():
        return jsonify({"error": "Audio not ready"}), 404
    ext = row["audio_format"]
    base = f"{row['novel_title']} - {row['chapter_title']}"
    safe_name = re.sub(r"[^A-Za-z0-9 ._()\-]", "", base).strip()[:180] or f"chapter-{chapter_id}"
    return send_file(stored_audio_path(row["audio_path"]), as_attachment=True, download_name=f"{safe_name}.{ext}")


@legacy.route("/api/next-in-queue/<int:chapter_id>")
def next_in_queue(chapter_id):
    conn = get_db()
    row = conn.execute("SELECT book_id, position FROM book_sections WHERE legacy_chapter_id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"next_id": None})
    next_row = conn.execute(
        """SELECT c.id, c.status, c.chapter_title, c.novel_title,
               CASE WHEN c.content IS NOT NULL AND length(trim(c.content)) > 0 THEN 1 ELSE 0 END AS has_content,
               (SELECT COUNT(*) FROM audio_segments a WHERE a.chapter_id=c.id) AS segment_count
           FROM book_sections s JOIN chapters c ON c.id=s.legacy_chapter_id
           WHERE s.book_id=? AND s.position>? AND c.status != 'failed'
           ORDER BY s.position LIMIT 1""", (row["book_id"],row["position"])
    ).fetchone()
    conn.close()
    if not next_row:
        return jsonify({"next_id": None})
    return jsonify({
        "next_id": next_row["id"],
        "status": next_row["status"],
        "chapter_title": next_row["chapter_title"],
        "novel_title": next_row["novel_title"],
        "has_content": next_row["has_content"],
        "segment_count": next_row["segment_count"],
    })


@legacy.route("/api/prioritize/<int:chapter_id>", methods=["POST"])
def prioritize_chapter(chapter_id):
    """Move a waiting chapter to the front so Play does not sit behind a long queue."""
    conn = get_db()
    row = conn.execute("SELECT id, status FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Chapter not found"}), 404
    if row["status"] == "pending":
        first = conn.execute("SELECT MIN(queue_order) AS q FROM chapters").fetchone()["q"]
        conn.execute("UPDATE chapters SET queue_order = ? WHERE id = ?", ((first or 0) - 1, chapter_id))
        conn.commit()
    conn.close()
    _wake_workers()
    return jsonify({"ok": True})


@legacy.route("/api/retry/<int:chapter_id>", methods=["POST"])
def retry_chapter(chapter_id):
    _clear_segments(chapter_id)
    conn = get_db()
    row = conn.execute("SELECT id, audio_path FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Chapter not found"}), 404
    if row["audio_path"] and stored_audio_path(row["audio_path"]).exists():
        try:
            stored_audio_path(row["audio_path"]).unlink()
        except OSError:
            pass
    conn.execute(
        """
        UPDATE chapters
        SET status = 'pending', audio_path = NULL, generation_seconds = NULL, generation_progress = 0, error_message = NULL
        WHERE id = ?
        """,
        (chapter_id,),
    )
    conn.commit()
    conn.close()
    _wake_workers()
    return jsonify({"ok": True})


@legacy.route("/api/delete/<int:chapter_id>", methods=["DELETE"])
def delete_chapter(chapter_id):
    _clear_segments(chapter_id)
    conn = get_db()
    row = conn.execute("SELECT audio_path FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
    if row and row["audio_path"] and stored_audio_path(row["audio_path"]).exists():
        try:
            stored_audio_path(row["audio_path"]).unlink()
        except OSError:
            pass
    conn.execute("DELETE FROM chapters WHERE id = ?", (chapter_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@legacy.route("/api/delete-novel", methods=["POST"])
def delete_novel():
    data = request.get_json(silent=True) or {}
    novel_title = str(data.get("novel_title", ""))
    conn = get_db()
    rows = conn.execute(
        "SELECT id, audio_path FROM chapters WHERE novel_title = ?", (novel_title,)
    ).fetchall()
    conn.close()
    for row in rows:
        _clear_segments(row["id"])
        if row["audio_path"] and stored_audio_path(row["audio_path"]).exists():
            try:
                stored_audio_path(row["audio_path"]).unlink()
            except OSError:
                pass
    conn = get_db()
    conn.execute("DELETE FROM chapters WHERE novel_title = ?", (novel_title,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@legacy.route("/api/epub/<path:novel_title>")
def download_epub(novel_title):
    conn = get_db()
    rows = conn.execute(
        "SELECT c.chapter_title, c.content FROM chapters c LEFT JOIN book_sections s ON s.legacy_chapter_id=c.id WHERE c.novel_title = ? ORDER BY COALESCE(s.position,c.id), c.id",
        (novel_title,),
    ).fetchall()
    conn.close()
    if not rows:
        return jsonify({"error": "No chapters found for this novel"}), 404

    book = epub.EpubBook()
    book.set_identifier(novel_title)
    book.set_title(novel_title)
    book.set_language("en")

    epub_chapters = []
    for index, row in enumerate(rows):
        chapter = epub.EpubHtml(
            title=row["chapter_title"], file_name=f"chap_{index}.xhtml", lang="en"
        )
        body = "".join(
            f"<p>{html.escape(line)}</p>" for line in row["content"].split("\n") if line.strip()
        )
        chapter.content = f"<h1>{html.escape(row['chapter_title'] or '')}</h1>{body}"
        book.add_item(chapter)
        epub_chapters.append(chapter)

    book.toc = epub_chapters
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + epub_chapters

    safe_title = re.sub(r"[^A-Za-z0-9 ._()\-]", "", novel_title).strip()[:160] or "webnovel"
    out_path = data_path() / f"{safe_title}.epub"
    epub.write_epub(str(out_path), book)
    return send_file(out_path, as_attachment=True, download_name=f"{safe_title}.epub")


def _prewarm_default_voice():
    # Only worth doing if Piper is actually the selected engine; eSpeak NG has no
    # model to preload, and there's no reason to spend startup bandwidth/CPU
    # downloading a Piper voice nobody is currently using.
    if (get_setting("engine") or "piper") != "piper":
        return
    try:
        ensure_piper_voice(normalize_piper_voice(get_setting("piper_voice")))
        print("Piper voice preloaded", flush=True)
    except Exception as exc:
        print(f"Piper preload skipped: {exc}", flush=True)


def start_workers(application):
    """Start legacy TTS explicitly, with an application context per thread."""
    if application.extensions.get("tts_workers"):
        return
    with application.app_context():
        conn = get_db()
        conn.execute("UPDATE chapters SET status='pending', generation_progress=0 WHERE status='processing'")
        conn.commit()
        conn.close()
    def run(target, *args):
        with application.app_context():
            target(*args)
    count = max(1,min(4,int(os.environ.get("WORKER_COUNT","1"))))
    threads = []
    for index in range(count):
        thread = threading.Thread(target=run,args=(worker_loop,f"tts-worker-{index+1}"),daemon=True)
        thread.start()
        threads.append(thread)
    threading.Thread(target=run,args=(_prewarm_default_voice,),daemon=True).start()
    application.extensions["tts_workers"] = threads


def create_app(data_dir=None, *, start_tts_workers=False):
    """Importing app.py is read-only. Storage changes occur only at explicit startup.

    Pass copied storage to this factory for migration rehearsals and tests.
    Existing databases receive a verified snapshot before the additive migration.
    """
    from datetime import datetime, timezone
    from preservation import snapshot_database
    from library_store import migrate
    from library_api import library
    from platform_api import platform
    from platform_store import migrate as migrate_platform
    import fcntl
    application = Flask(__name__)
    directory = Path(data_dir or os.environ.get("DATA_DIR","/data")).resolve()
    application.config.update(DATA_DIR=str(directory), MAX_CONTENT_LENGTH=256*1024*1024)
    application.register_blueprint(legacy)
    application.register_blueprint(library)
    application.register_blueprint(platform)
    @application.after_request
    def fresh_api_state(response):
        if request.path.startswith('/api/') and response.is_json:
            response.headers['Cache-Control']='private, no-store'
        return response

    with application.app_context():
        directory.mkdir(parents=True,exist_ok=True)
        with (directory/".startup.lock").open("a") as startup_lock:
            fcntl.flock(startup_lock, fcntl.LOCK_EX)
            database = directory/"webnovel.db"
            # Keep snapshots outside data so normal library/media inventories remain clean.
            if database.exists():
                backup_dir = Path(os.environ.get("BACKUP_DIR",str(directory.parent/(directory.name+"-backups"))))
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                snapshot_database(database,backup_dir/(stamp+"-pre-startup.db"),allow_live_sidecars=True)
            for subdir in ("audio/segments","piper_voices","books","tmp"):
                (directory/subdir).mkdir(parents=True,exist_ok=True)
            init_db()
            migrate(database)
            migrate_platform(database)
            from chapter_updates import migrate as migrate_updates
            migrate_updates(database)
    if start_tts_workers:
        start_workers(application)
    from audio_cache import start_scheduler
    start_scheduler(application)
    from chapter_updates import start_scheduler as start_chapter_scheduler
    start_chapter_scheduler(application)
    return application


if __name__ == "__main__":
    application = create_app(start_tts_workers=os.environ.get("START_TTS_WORKERS","1") == "1")
    application.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")),threaded=True)
