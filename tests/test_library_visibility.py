"""Both clients use the same library contract; pending imports remain recoverable."""
import unittest
from app import create_app
from library_store import connect
from tests import test_http


class LibraryVisibilityTests(unittest.TestCase):
    setUp = test_http.HttpTests.setUp

    def seed(self, name, text=None, cover=False):
        with connect(self.db) as c, c:
            c.execute("INSERT INTO books(id,title,source_kind,cover_path) VALUES(?,?,'webnovel',?)",
                      (name, name, 'cover-only.png' if cover else None))
            if text is not None:
                c.execute('INSERT INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES(?,0,?,?,?,?)',
                          (name, 'Chapter 1', 'https://public.example/'+name+'/1', '<p>'+text+'</p>', text))

    def test_metadata_cover_zero_and_empty_placeholders_are_pending(self):
        for name, text, cover in [('metadata',None,False),('cover',None,True),('zero','',False),('whitespace',' \n\t ',False)]:
            self.seed(name,text,cover)
        self.assertEqual(self.client.get('/api/books').json['total'],0)
        pending = self.client.get('/api/books?pending=1').json
        self.assertEqual(pending['total'],4)
        for row in pending['items']:
            self.assertEqual(self.client.get('/api/books/'+row['id']).status_code,200)

    def test_one_real_chapter_and_partial_import_remain_visible(self):
        self.seed('one','Persisted chapter text.')
        self.seed('partial','First chapter remains safe.',True)
        with connect(self.db) as c,c:
            c.execute("INSERT INTO import_sources(book_id,url) VALUES('partial','https://public.example/partial')")
            c.execute("INSERT INTO import_chapters(book_id,url,position,title,status,error) VALUES('partial','https://public.example/partial/2',1,'Second','failed','Fixture timeout')")
        result=self.client.get('/api/books').json
        self.assertEqual({r['id'] for r in result['items']},{'one','partial'})
        self.assertEqual(result['total'],2)

    def test_failed_import_retry_duplicate_and_restart_preserve_state(self):
        first=self.client.post('/api/imports',json={'url':'https://public.example/retry'}).json
        book=first['book_id']
        self.assertEqual(self.client.get('/api/books').json['total'],0)
        second=self.client.post('/api/imports',json={'url':'https://public.example/retry'}).json
        self.assertEqual(second['book_id'],book)
        restarted=create_app(self.root/'data').test_client()
        self.assertEqual(restarted.get('/api/books').json['total'],0)
        self.assertEqual(restarted.get('/api/books?pending=1').json['total'],1)
        with connect(self.db) as c,c:
            c.execute('INSERT INTO book_sections(book_id,position,title,href,content_html,text_content) VALUES(?,0,?,?,?,?)',
                      (book,'Recovered','https://public.example/retry/1','<p>Recovered text</p>','Recovered text'))
        self.assertEqual(restarted.get('/api/books').json['total'],1)
        self.assertEqual(restarted.get('/api/books?pending=1').json['total'],0)

    def test_filter_totals_and_pagination_apply_after_real_content_rule(self):
        self.seed('pending',cover=True)
        self.seed('visible','Real text')
        result=self.client.get('/api/books?limit=1&q=visible').json
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['id'],'visible')
        self.assertEqual(self.client.get('/api/books?q=pending').json['total'],0)

    def test_backend_contract_remains_compatible_with_native_v1(self):
        result=self.client.get('/api/compatibility').json
        self.assertEqual(result['api_contract'],1)
        self.assertEqual(result['minimum_native_contract'],1)
        self.assertIn('reader-text-start',result['capabilities'])
