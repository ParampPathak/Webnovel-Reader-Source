"""Bounded Contents around a canonical chapter, including sparse imports."""
import unittest
from tests import test_platform as fixtures
from library_store import connect

@unittest.skipUnless(fixtures.RUNTIME_AVAILABLE,'Flask dependencies required')
class ContentsTests(unittest.TestCase):
    setUp=fixtures.PlatformTests.setUp
    upload=fixtures.PlatformTests.upload
    book=fixtures.PlatformTests.book

    def test_sparse_positions_use_imported_row_rank_not_canonical_number(self):
        book,sections=self.book()
        with connect(self.db) as conn:
            with conn:
                for item,position in zip(sections,[10,3201,9000]):
                    conn.execute('UPDATE book_sections SET position=? WHERE id=?',(position,item['id']))
        result=self.client.get(f'/api/books/{book}/sections?around={sections[1]["id"]}&limit=1').json
        self.assertEqual(result['offset'],1)
        self.assertEqual(result['items'][0]['id'],sections[1]['id'])
        self.assertEqual(result['total'],3)

    def test_current_lookup_is_book_scoped_and_validated(self):
        book,sections=self.book()
        for value in ['-1','0','NaN','1.5','999999999999999999999999']:
            self.assertEqual(self.client.get(f'/api/books/{book}/sections?around={value}').status_code,400)
        self.assertEqual(self.client.get(f'/api/books/{book}/sections?around=9999999').status_code,404)

    def test_lookup_does_not_write_progress_or_preferences(self):
        book,sections=self.book()
        with connect(self.db) as conn:
            before={t:[tuple(row) for row in conn.execute('SELECT * FROM '+t)] for t in ['reading_progress','listening_progress','book_preferences','reading_follow']}
        self.assertEqual(self.client.get(f'/api/books/{book}/sections?around={sections[2]["id"]}').status_code,200)
        with connect(self.db) as conn:
            after={t:[tuple(row) for row in conn.execute('SELECT * FROM '+t)] for t in before}
        self.assertEqual(before,after)
