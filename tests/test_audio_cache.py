"""Destructive maintenance cases use only generated synthetic temporary data."""
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from audio_cache import cache_lock, cleanup, configuration, metadata, touch
from audio_providers import cache_key, settings
from library_store import connect
from platform_store import schedule
from platform_worker import PlatformWorker


class CacheTests(unittest.TestCase):
    def test_optional_word_cues_do_not_disable_audio_eviction(self):
        key,chunk,final=self.asset()
        cues=chunk.with_suffix('.timing.json');cues.write_text('{"schema":1}')
        report=cleanup(self.root,False,self.config)
        self.assertEqual(len(report['removed']),2)
        self.assertTrue(cues.exists())
        self.assertFalse(chunk.exists());self.assertFalse(final.exists())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'data'
        self.app = create_app(self.root, start_tts_workers=False)
        self.db = self.root / 'webnovel.db';self.client = self.app.test_client()
        self.config = {**configuration(), 'budget': 1, 'recent_seconds': 100, 'keep_chapters': 10}
        with connect(self.db) as conn, conn:
            for book in ('one', 'two'):
                conn.execute("INSERT INTO books(id,title,source_kind) VALUES(?,?,'webnovel')", (book, book))
                for number in range(1, 15):
                    identifier = number if book == 'one' else number + 100
                    conn.execute('INSERT INTO book_sections(id,book_id,position,title,href,content_html,text_content) VALUES(?,?,?,?,?,?,?)', (identifier, book, number - 1, f'Synthetic {number}', f'{number}.html', '<p>Original test sentence.</p>', 'Original test sentence.'))
        self.calls = []

    def asset(self, section=1, book='one', voice='en-US-AndrewNeural'):
        config = settings(voice);key, digest = cache_key(book, section, 'Original test sentence.', config)
        folder = self.root / 'audio' / 'edge' / book / key;folder.mkdir(parents=True, exist_ok=True)
        chunk = folder / '000000.mp3';chunk.write_bytes(b'Synthetic MP3 bytes')
        final = folder / 'chapter.mp3';final.write_bytes(chunk.read_bytes())
        with connect(self.db) as conn, conn:
            conn.execute("INSERT INTO audio_variants(id,book_id,section_id,provider,voice,settings,content_hash,path,status,chunk_count) VALUES(?,?,?,'edge',?,?,?,?,'ready',1)", (key, book, section, voice, json.dumps(config, sort_keys=True), digest, str(final)))
            conn.execute('INSERT INTO audio_chunks(asset_id,position,path,checksum,duration) VALUES(?,0,?,?,3)', (key, str(chunk), hashlib.sha256(chunk.read_bytes()).hexdigest()))
        return key, chunk, final

    def row(self, key):
        with connect(self.db) as conn: return dict(conn.execute('SELECT * FROM audio_variants WHERE id=?', (key,)).fetchone())

    def test_below_budget_deletes_nothing(self):
        key, chunk, final = self.asset()
        report = cleanup(self.root, False, {**self.config, 'budget': 100000})
        self.assertTrue(report['complete']);self.assertEqual(report['removed'], []);self.assertTrue(final.exists())

    def test_old_verified_audio_removed_metadata_ready_for_regeneration(self):
        key, chunk, final = self.asset()
        before = self.row(key)
        report = cleanup(self.root, False, self.config)
        self.assertEqual(len(report['removed']), 2);self.assertFalse(final.exists());self.assertFalse(chunk.exists())
        after = self.row(key);self.assertEqual(after['status'], 'pending');self.assertIsNone(after['path'])
        for field in ('voice', 'settings', 'content_hash', 'chunk_plan', 'first_chunk_bytes', 'section_id'): self.assertEqual(after[field], before[field])
        self.assertEqual(self.client.get('/api/books/one/audio?section_id=1').json['status'], 'missing')

    def test_dry_run_does_not_delete_or_invalidate(self):
        key, chunk, final = self.asset();report = cleanup(self.root, True, self.config)
        self.assertEqual(len(report['planned']), 2);self.assertFalse(report['removed']);self.assertTrue(final.exists());self.assertEqual(self.row(key)['status'], 'ready')

    def test_recent_access_protected(self):
        key, chunk, final = self.asset();touch(self.root, key, 'one', 1)
        report = cleanup(self.root, False, self.config);self.assertTrue(final.exists());self.assertTrue(report['protected']);self.assertGreater(report['over_budget_bytes'], 0)

    def test_current_listening_all_voice_variants_protected(self):
        key, _, final = self.asset();other, _, second = self.asset(voice='en-GB-SoniaNeural')
        with connect(self.db) as conn, conn:
            conn.execute('INSERT INTO listening_progress(book_id,section_id,asset_id,voice) VALUES(?,?,?,?)', ('one', 1, key, 'en-US-AndrewNeural'))
        report = cleanup(self.root, False, self.config);self.assertFalse(report['removed']);self.assertTrue(final.exists() and second.exists())

    def test_running_generation_protects_entire_cache(self):
        key, _, final = self.asset();job = schedule(self.db, 'audio', 'one', settings(), [(2, {'section_id': 2})])
        with connect(self.db) as conn, conn: conn.execute("UPDATE platform_items SET status='running' WHERE job_id=?", (job,))
        report = cleanup(self.root, False, self.config);self.assertFalse(report['removed']);self.assertTrue(final.exists())

    def test_queued_paused_failed_generation_preserved(self):
        key, _, final = self.asset();job = schedule(self.db, 'audio', 'one', settings(), [(1, {'section_id': 1})])
        for state in ('queued', 'paused', 'failed'):
            with connect(self.db) as conn, conn: conn.execute('UPDATE platform_jobs SET status=? WHERE id=?', (state, job))
            self.assertFalse(cleanup(self.root, False, self.config)['removed']);self.assertTrue(final.exists())

    def test_partial_and_orphan_audio_counted_but_preserved(self):
        key, chunk, final = self.asset();partial = final.parent / 'chapter.part';partial.write_bytes(b'partial')
        orphan = self.root / 'audio' / 'unregistered.wav';orphan.write_bytes(b'orphan')
        report = cleanup(self.root, False, self.config);self.assertFalse(report['removed']);self.assertTrue(partial.exists() and orphan.exists());self.assertGreater(report['before_bytes'], 36)

    def test_checksum_corruption_fails_closed(self):
        key, chunk, final = self.asset();chunk.write_bytes(b'corrupted same size')
        report = cleanup(self.root, False, self.config);self.assertFalse(report['removed']);self.assertTrue(report['errors']);self.assertTrue(final.exists())

    def test_real_assembler_diagnostics_retained_but_not_blocking_audio_eviction(self):
        key, chunk, final = self.asset();listing=final.parent/'parts.ffconcat';listing.write_text("ffconcat version 1.0\nfile '000000.mp3'\n")
        log=final.parent/'assembly.log';log.write_text('')
        report=cleanup(self.root, False, self.config)
        self.assertEqual(len(report['removed']),2);self.assertTrue(listing.exists() and log.exists());self.assertFalse(final.exists())

    def test_streaming_and_download_close_release_lock(self):
        key, chunk, final = self.asset()
        for url in (f'/api/audio/{key}', f'/api/audio/{key}/segments/0'):
            response = self.client.get(url, headers={'Range': 'bytes=0-8'}, buffered=False)
            self.assertEqual(response.status_code, 206)
            report = cleanup(self.root, False, self.config);self.assertFalse(report['removed']);self.assertIn('HTTP', report['protected'][0]['reason'])
            response.close()
            with cache_lock(self.root, exclusive=True): pass
        self.assertTrue(final.exists())

    def test_cache_hits_and_progress_not_changed(self):
        key, chunk, final = self.asset()
        with connect(self.db) as conn:
            before = {t: list(map(tuple, conn.execute('SELECT * FROM '+t))) for t in ('reading_progress', 'listening_progress', 'platform_settings', 'book_bookmarks', 'book_sections', 'books')}
        response = self.client.get(f'/api/audio/{key}');self.assertEqual(response.data, b'Synthetic MP3 bytes');response.close()
        self.assertEqual(self.client.get('/api/books/one/audio?section_id=1').json['status'], 'ready')
        cleanup(self.root, False, self.config)
        with connect(self.db) as conn:
            after = {t: list(map(tuple, conn.execute('SELECT * FROM '+t))) for t in before}
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM platform_jobs').fetchone()[0], 0)
        self.assertEqual(before, after)

    def test_multiple_books_and_voice_variants(self):
        first, _, old = self.asset();second, _, recent = self.asset(101, 'two', 'en-GB-SoniaNeural');touch(self.root, second, 'two', 101)
        report = cleanup(self.root, False, self.config);self.assertFalse(old.exists());self.assertTrue(recent.exists())

    def test_last_ten_chapters_preferred_before_older_chapter(self):
        old, _, old_file = self.asset(1);new, _, new_file = self.asset(2)
        with metadata(self.root) as db: db.execute('INSERT INTO access VALUES(?,?,?,?,?)', (new, 'one', 2, 1, 5))
        report = cleanup(self.root, True, {**self.config, 'budget': old_file.stat().st_size * 2})
        self.assertIn(str(old_file.relative_to(self.root)), report['planned']);self.assertNotIn(str(new_file.relative_to(self.root)), report['planned'])

    def test_bounds_and_no_audio_reads_below_budget(self):
        self.asset();self.asset(2)
        report = cleanup(self.root, True, {**self.config, 'max_files': 2})
        self.assertEqual(len(report['planned']), 2);self.assertFalse(report['complete'])
        with patch('hashlib.sha256', side_effect=AssertionError('No full audio hashing below budget')):
            self.assertTrue(cleanup(self.root, True, {**self.config, 'budget': 100000})['complete'])

    def test_outside_path_and_symlink_never_deleted(self):
        key, chunk, final = self.asset();outside = self.root.parent / 'private.mp3';outside.write_bytes(b'private')
        with connect(self.db) as conn, conn: conn.execute('UPDATE audio_variants SET path=? WHERE id=?', (str(outside), key))
        self.assertFalse(cleanup(self.root, False, self.config)['removed']);self.assertTrue(outside.exists())
        with connect(self.db) as conn, conn: conn.execute('UPDATE audio_variants SET path=? WHERE id=?', (str(final), key))
        chunk.unlink();chunk.symlink_to(outside)
        self.assertFalse(cleanup(self.root, False, self.config)['removed']);self.assertTrue(outside.exists() and final.exists())

    def test_interrupted_unlink_resumes_durable_intent(self):
        key, chunk, final = self.asset();real_unlink = os.unlink;calls = []
        def interrupt(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2: raise OSError('Simulated power interruption')
            return real_unlink(*args, **kwargs)
        with patch('audio_cache.os.unlink', side_effect=interrupt):
            report = cleanup(self.root, False, self.config)
        self.assertTrue(report['errors'])
        self.assertEqual(report['reclaimed_bytes'],19);self.assertEqual(report['after_bytes'],19)
        cleanup(self.root, False, self.config)
        self.assertFalse(final.exists() or chunk.exists());self.assertEqual(self.row(key)['status'], 'pending')
        with metadata(self.root) as db: self.assertFalse(db.execute('SELECT * FROM eviction').fetchall())

    def test_concurrent_new_stream_waits_for_collector(self):
        key, chunk, final = self.asset();started = threading.Event();acquired = threading.Event()
        def stream():
            started.set()
            with cache_lock(self.root): acquired.set()
        with cache_lock(self.root, exclusive=True):
            thread = threading.Thread(target=stream);thread.start();self.assertTrue(started.wait(2));self.assertFalse(acquired.wait(.05))
        thread.join(2);self.assertTrue(acquired.is_set())

    def test_deleted_audio_regenerates_through_unchanged_worker(self):
        key, chunk, final = self.asset();cleanup(self.root, False, self.config)
        from tests.test_platform import MockSpeech, mock_assemble
        provider = MockSpeech();worker = PlatformWorker(self.root, provider=provider, assembler=mock_assemble, duration_probe=lambda path: 3)
        schedule(self.db, 'audio', 'one', settings(), [(1, {'section_id': 1})])
        for _ in range(8): worker.step()
        self.assertTrue(provider.calls);self.assertTrue(final.exists());self.assertEqual(self.row(key)['status'], 'ready')
        count = len(provider.calls);schedule(self.db, 'audio', 'one', settings(), [(1, {'section_id': 1})])
        worker.step();self.assertEqual(len(provider.calls), count)

    def test_default_budget_and_disabled_initial_scheduler(self):
        self.assertEqual(configuration()['budget'], 20 * 1024 ** 3)
        self.assertNotIn('audio_cache_scheduler', self.app.extensions)
        for invalid in ('nan', 'inf', '0', '-1'):
            with patch.dict(os.environ, {'AUDIO_CACHE_LIMIT_GIB': invalid}): self.assertRaises(ValueError, configuration)
