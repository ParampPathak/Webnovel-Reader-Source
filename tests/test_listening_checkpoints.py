"""Ordered listening cursors: isolated SQLite/Flask fixtures, never real books/audio."""
import unittest
from tests import test_http
from library_store import connect


@unittest.skipUnless(test_http.RUNTIME_AVAILABLE, 'Existing Flask dependencies required')
class ListeningCheckpointTests(unittest.TestCase):
    setUp = test_http.HttpTests.setUp
    upload = test_http.HttpTests.upload

    def fixture(self):
        book = self.upload().json['id']
        base = '/api/books/' + book
        sections = self.client.get(base + '/sections').json['items']
        return book, base + '/listening', [s['id'] for s in sections]

    def checkpoint(self, sid, version=0, sequence=1, session='current-player-session', generation=1, claim=False, owner=''):
        return dict(section_id=sid, asset_id='legacy-' + str(sid), seconds=12,
                    playback_rate=2.5, voice='legacy', synthesis_rate=0, version=version,
                    session_id=session, session_generation=generation, event_sequence=sequence,
                    claim_session=claim, expected_session_id=owner, checkpoint_intent='playback',
                    playback_state='playing', text_anchor='', normalized_offset=None)

    def test_older_logical_event_cannot_rollback_with_a_fresh_database_version(self):
        _, url, ids = self.fixture()
        first = self.checkpoint(ids[0], claim=True)
        self.assertEqual(self.client.put(url, json=first).status_code, 200)
        latest = self.checkpoint(ids[2], version=1, sequence=5)
        self.assertEqual(self.client.put(url, json=latest).status_code, 200)
        delayed = self.checkpoint(ids[1], version=2, sequence=2)
        self.assertEqual(self.client.put(url, json=delayed).status_code, 409)
        self.assertEqual(self.client.get(url).json['section_id'], ids[2])
        # Backward movement is valid when it is a newer explicit seek event.
        backward = self.checkpoint(ids[0], version=2, sequence=6)
        backward['checkpoint_intent'] = 'seek'
        self.assertEqual(self.client.put(url, json=backward).status_code, 200)
        self.assertEqual(self.client.get(url).json['section_id'], ids[0])

    def test_new_explicit_session_fences_an_old_tab_even_with_the_latest_version(self):
        _, url, ids = self.fixture()
        self.assertEqual(self.client.put(url, json=self.checkpoint(ids[0], claim=True)).status_code, 200)
        new = self.checkpoint(ids[2], version=1, session='replacement-player-session', generation=2,
                              claim=True, owner='current-player-session')
        self.assertEqual(self.client.put(url, json=new).status_code, 200)
        old = self.checkpoint(ids[1], version=2, sequence=99)
        self.assertEqual(self.client.put(url, json=old).status_code, 409)
        self.assertEqual(self.client.get(url).json['section_id'], ids[2])
        legacy = {k: new[k] for k in ['section_id', 'asset_id', 'seconds', 'playback_rate', 'voice', 'synthesis_rate']}
        legacy.update(version=2, section_id=ids[0], asset_id='legacy-' + str(ids[0]))
        self.assertEqual(self.client.put(url, json=legacy).status_code, 409)

    def test_acknowledgement_loss_retry_is_idempotent_but_reused_sequence_is_not_mutable(self):
        _, url, ids = self.fixture()
        data = self.checkpoint(ids[0], claim=True)
        first = self.client.put(url, json=data)
        self.assertEqual(first.status_code, 200)
        retry = self.client.put(url, json=data)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json['version'], 1)
        changed = {**data, 'seconds': 2, 'version': 1}
        self.assertEqual(self.client.put(url, json=changed).status_code, 409)
        self.assertEqual(self.client.get(url).json['seconds'], 12)

    def test_manual_reading_location_and_preferences_are_independent(self):
        book, url, ids = self.fixture()
        manual = dict(section_id=ids[0], fraction=.4, anchor='text:2', preferences={}, version=0)
        self.assertEqual(self.client.put('/api/books/' + book + '/progress', json=manual).status_code, 200)
        with connect(self.db) as conn:
            before = dict(conn.execute('SELECT * FROM reading_progress WHERE book_id=?', (book,)).fetchone())
        self.assertEqual(self.client.put(url, json=self.checkpoint(ids[2], claim=True)).status_code, 200)
        with connect(self.db) as conn:
            after = dict(conn.execute('SELECT * FROM reading_progress WHERE book_id=?', (book,)).fetchone())
        self.assertEqual(before, after)

    def test_legacy_rows_are_not_promoted_to_new_sessions_by_migration(self):
        from platform_store import migrate
        _, url, ids = self.fixture()
        legacy = dict(section_id=ids[1], asset_id='legacy-' + str(ids[1]), seconds=7,
                      voice='legacy', synthesis_rate=0, playback_rate=2, version=0)
        self.assertEqual(self.client.put(url, json=legacy).status_code, 200)
        before = self.client.get(url).json
        migrate(self.db); migrate(self.db)
        after = self.client.get(url).json
        self.assertEqual(before, after)
        self.assertFalse(after.get('session_id'))
        self.assertFalse(after.get('session_generation'))
        self.assertFalse(after.get('event_sequence'))

    def test_invalid_partial_or_noninteger_session_fields_never_write(self):
        _, url, ids = self.fixture()
        data = self.checkpoint(ids[0], claim=True)
        for patch in [dict(event_sequence=True), dict(event_sequence=0), dict(session_generation=-1),
                      dict(session_id=''), dict(text_anchor='<script>'), dict(normalized_offset=-2)]:
            response = self.client.put(url, json={**data, **patch})
            self.assertEqual(response.status_code, 400, patch)
        self.assertEqual(self.client.get(url).json['version'], 0)
