import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from epub_library import InvalidEpub, ReadingHTML, parse_epub, resolve, xml
from library_store import attach_legacy, connect, import_book, migrate, save_progress
from preservation import backup_tree, digest, snapshot_database, verify_backup
from tests.fixtures import change_entry, epub_bytes, write_epub


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db = self.root/'webnovel.db'

    def epub(self,payload=None):
        path = self.root/'fixture.epub'
        path.write_bytes(payload if payload is not None else epub_bytes())
        return path

    def legacy(self):
        with connect(self.db) as conn:
            conn.execute('''CREATE TABLE chapters(id INTEGER PRIMARY KEY,novel_title TEXT,chapter_title TEXT,
                           source_url TEXT, next_url TEXT,content TEXT,queue_order INTEGER,status TEXT,
                           audio_path TEXT,engine_used TEXT,voice_used TEXT,audio_format TEXT)''')
            for i,priority in [(10,2),(11,-1),(12,1)]:
                conn.execute('INSERT INTO chapters VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             (i,'Novel','Chapter '+str(i),'https://example/chapter/'+str(i),None,
                              'A < B & C',priority,'ready','/data/audio/'+str(i)+'.mp3','piper','ryan_medium','mp3'))
            conn.execute('CREATE TABLE audio_segments(id INTEGER PRIMARY KEY,chapter_id INTEGER,audio_path TEXT)')
            conn.execute("INSERT INTO audio_segments VALUES(1,999,'/data/audio/orphan.wav')")
            conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            conn.execute("INSERT INTO settings VALUES('engine','piper')")
            conn.commit()

    def imported(self,count=3,paragraph_bytes=0):
        migrate(self.db)
        path = self.root/'fixture.epub'
        write_epub(path,count,paragraph_bytes=paragraph_bytes)
        parsed = parse_epub(path,'book-a',stream=count>100)
        import_book(self.db,parsed,path,digest(path))
        return parsed


class EpubTests(Workspace):
    def test_epub3_metadata_cover_fragments_and_nested_navigation(self):
        parsed = parse_epub(self.epub(epub_bytes(nested=True)),'book')
        self.assertEqual(parsed['metadata'],{'title':'A & B','author':'Test Author','language':'en','description':'A test book'})
        self.assertEqual(parsed['cover'],'OPS/images/cover.png')
        self.assertEqual(parsed['navigation'][0]['depth'],1)
        self.assertEqual(parsed['navigation'][0]['href'],'OPS/chapter0.xhtml#heading')
        self.assertIn('data-book-target="OPS/chapter0.xhtml#note"',parsed['sections'][0]['html'])
        self.assertIn('/api/books/book/resources/OPS/images/cover.png',parsed['sections'][0]['html'])
        self.assertNotIn('Hidden title',parsed['sections'][0]['html'])

    def test_epub2_ncx(self):
        parsed = parse_epub(self.epub(epub_bytes(epub2=True)),'book')
        self.assertEqual(len(parsed['navigation']),3)
        self.assertEqual(parsed['sections'][2]['title'],'Chapter 3')

    def test_1501_sections_and_full_navigation(self):
        parsed = self.imported(1501,paragraph_bytes=9000)
        self.assertEqual(parsed['section_count'],1501)
        self.assertFalse(isinstance(parsed['sections'],list))
        self.assertEqual(len(parsed['navigation']),1501)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM book_sections').fetchone()[0],1501)
            self.assertEqual(conn.execute('SELECT title FROM book_sections WHERE position=1500').fetchone()[0],'Chapter 1501')

    def test_scripts_remote_resources_and_unsafe_markup_removed(self):
        parsed = parse_epub(self.epub(epub_bytes(hostile=True)),'book')
        content = parsed['sections'][0]['html']
        for forbidden in ['<script','onerror','evil.invalid','javascript:','<iframe','<svg','<form','<input','@import']:
            self.assertNotIn(forbidden,content)
        self.assertIn('Text &amp; meaning',content)
        self.assertIn('id="note"',content)

    def test_form_controls_do_not_suppress_following_text(self):
        parser = ReadingHTML('OPS/ch.xhtml','book',{})
        parser.feed('<form><input value="secret"><embed src="bad"></form><p>Visible after form</p>')
        content,text = parser.finish()
        self.assertIn('Visible after form',content)
        self.assertNotIn('secret',content)

    def test_lazy_section_error_rolls_back_all_imported_rows(self):
        migrate(self.db)
        path = self.epub(change_entry(epub_bytes(),'OPS/chapter1.xhtml',b'\xffinvalid UTF8'))
        parsed = parse_epub(path,'bad-book',stream=True)
        self.assertEqual(parsed['section_count'],3)
        self.assertRaises(InvalidEpub,import_book,self.db,parsed,path,digest(path))
        with connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM books').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM book_sections').fetchone()[0],0)

    def test_archive_traversal_symlink_and_duplicate_rejected(self):
        self.assertRaises(InvalidEpub,parse_epub,self.epub(change_entry(epub_bytes(),'../escape','bad')),'book')
        payload = io.BytesIO(epub_bytes())
        with zipfile.ZipFile(payload,'a') as z:
            entry = zipfile.ZipInfo('link');entry.external_attr = 0o120777 << 16
            z.writestr(entry,'/etc/passwd')
        self.assertRaises(InvalidEpub,parse_epub,self.epub(payload.getvalue()),'book')
        payload = io.BytesIO(epub_bytes())
        with zipfile.ZipFile(payload,'a') as z:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('ignore');z.writestr('mimetype','application/epub+zip')
        self.assertRaises(InvalidEpub,parse_epub,self.epub(payload.getvalue()),'book')

    def test_xml_entities_missing_resources_and_size_budget_rejected(self):
        self.assertRaises(InvalidEpub,xml,b'<!DOCTYPE x [<!ENTITY bomb "bad">]><x>&bomb;</x>')
        self.assertRaises(InvalidEpub,xml,'<!DOCTYPE x [<!ENTITY bad "payload">]><x>&bad;</x>'.encode('utf-16'))
        payload = change_entry(epub_bytes(),'META-INF/container.xml','<container><rootfile full-path="../../outside"/></container>')
        self.assertRaises(InvalidEpub,parse_epub,self.epub(payload),'book')
        with patch('epub_library.MAX_EXPANDED_BYTES',100):
            self.assertRaises(InvalidEpub,parse_epub,self.epub(),'book')
        self.assertRaises(InvalidEpub,parse_epub,self.epub(b'not a zip'),'book')

    def test_common_epub_doctypes_and_utf16_documents(self):
        payload = epub_bytes()
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            nav = z.read('OPS/nav.xhtml').decode()
            chapter = z.read('OPS/chapter0.xhtml').decode()
        payload = change_entry(payload,'OPS/nav.xhtml','<!DOCTYPE html>'+nav)
        payload = change_entry(payload,'OPS/chapter0.xhtml',chapter.encode('utf-16'))
        parsed = parse_epub(self.epub(payload),'book')
        self.assertEqual(len(parsed['navigation']),3)
        self.assertIn('Chapter 1',parsed['sections'][0]['html'])
        self.assertEqual(xml(b'<!DOCTYPE html PUBLIC "fake" "https://evil.invalid/dtd"><html/>').tag,'html')

    def test_path_resolution(self):
        self.assertEqual(resolve('OPS/text/ch.xhtml','../images/a.png'),'OPS/images/a.png')
        self.assertEqual(resolve('OPS/ch.xhtml','#note'),'OPS/ch.xhtml#note')
        for value in ['../../../escape','%2fetc/passwd','https://evil.invalid/','javascript:bad','//evil.invalid/a','%2e%2e/%2e%2e/outside','a\\b']:
            self.assertRaises(InvalidEpub,resolve,'OPS/ch.xhtml',value)

    def test_duplicate_upload_is_idempotent(self):
        self.imported()
        # Reuse the exact upload bytes. Rebuilding a ZIP changes timestamps and
        # therefore its content hash when a slow test crosses a clock boundary.
        path = self.root/'fixture.epub'
        other = parse_epub(path,'different-id')
        self.assertEqual(import_book(self.db,other,path,digest(path)),('book-a',False))
        with connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM books').fetchone()[0],1)

    def test_import_transaction_rolls_back(self):
        migrate(self.db)
        path = self.epub();parsed = parse_epub(path,'book')
        parsed['sections'][1]['position']=0
        self.assertRaises(sqlite3.IntegrityError,import_book,self.db,parsed,path,digest(path))
        with connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM books').fetchone()[0],0)


class MigrationTests(Workspace):
    def test_additive_idempotent_preserves_legacy_rows_and_paths(self):
        self.legacy()
        with connect(self.db) as conn:
            original = [tuple(r) for r in conn.execute('SELECT * FROM chapters ORDER BY id')]
        migrate(self.db)
        with connect(self.db) as conn:
            first_ids = [tuple(r) for r in conn.execute('SELECT id,book_id,position,legacy_chapter_id FROM book_sections ORDER BY position')]
            self.assertEqual([r[-1] for r in first_ids],[10,11,12])
            self.assertEqual([tuple(r) for r in conn.execute('SELECT * FROM chapters ORDER BY id')],original)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM library_audio_assets').fetchone()[0],3)
            self.assertEqual(conn.execute('SELECT chapter_id FROM audio_segments').fetchone()[0],999)
        migrate(self.db)
        with connect(self.db) as conn:
            self.assertEqual([tuple(r) for r in conn.execute('SELECT id,book_id,position,legacy_chapter_id FROM book_sections ORDER BY position')],first_ids)
            conn.execute('UPDATE chapters SET queue_order=-99 WHERE id=12');conn.commit()
            self.assertEqual([r[0] for r in conn.execute('SELECT legacy_chapter_id FROM book_sections ORDER BY position')],[10,11,12])

    def test_next_links_recover_out_of_order_insertions(self):
        self.legacy()
        with connect(self.db) as conn:
            conn.execute("UPDATE chapters SET next_url='https://example/chapter/10' WHERE id=12")
            conn.execute("UPDATE chapters SET next_url='https://example/chapter/11' WHERE id=10")
            conn.commit()
        migrate(self.db)
        with connect(self.db) as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT legacy_chapter_id FROM book_sections ORDER BY position')],[12,10,11])
            self.assertEqual(conn.execute('SELECT ordering_basis FROM library_migration_notes').fetchone()[0],'next-links')

    def test_next_link_cycle_preserves_insertion_order_with_warning(self):
        self.legacy()
        with connect(self.db) as conn:
            conn.execute("UPDATE chapters SET next_url='https://example/chapter/11' WHERE id=10")
            conn.execute("UPDATE chapters SET next_url='https://example/chapter/10' WHERE id=11")
            conn.commit()
        migrate(self.db)
        with connect(self.db) as conn:
            self.assertEqual([r[0] for r in conn.execute('SELECT legacy_chapter_id FROM book_sections ORDER BY position')],[10,11,12])
            self.assertIn('cycle',conn.execute('SELECT warning FROM library_migration_notes').fetchone()[0])

    def test_new_legacy_chapters_append_without_tts_priority_affecting_order(self):
        self.legacy();migrate(self.db)
        with connect(self.db) as conn:
            conn.execute("INSERT INTO chapters(id,novel_title,chapter_title,content,queue_order,status) VALUES(20,'Novel','New','text',-99,'pending')")
            attach_legacy(conn,20);conn.commit()
            self.assertEqual(conn.execute('SELECT position FROM book_sections WHERE legacy_chapter_id=20').fetchone()[0],3)

    def test_blank_titles_and_html_escaped(self):
        self.legacy()
        with connect(self.db) as conn:
            conn.execute('UPDATE chapters SET novel_title=NULL WHERE id=10');conn.commit()
        migrate(self.db)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM books').fetchone()[0],2)
            self.assertIn('A &lt; B &amp; C',conn.execute('SELECT content_html FROM book_sections WHERE legacy_chapter_id=10').fetchone()[0])

    def test_progress_restart_and_conflict_and_book_boundary(self):
        self.imported()
        with connect(self.db) as conn:
            section = conn.execute('SELECT id FROM book_sections WHERE position=2').fetchone()[0]
        self.assertEqual(save_progress(self.db,'book-a',section,.63,'note',{'theme':'dark'},0),1)
        self.assertIsNone(save_progress(self.db,'book-a',section,.1,'',{},0))
        self.assertRaises(ValueError,save_progress,self.db,'different-book',section,.1,'',{},0)
        with connect(self.db) as conn:
            row = conn.execute('SELECT * FROM reading_progress').fetchone()
            self.assertEqual(row['fraction'],.63)
            self.assertEqual(json.loads(row['preferences']),{'theme':'dark'})
        self.assertEqual(save_progress(self.db,'book-a',section,.8,'',{},1),2)


class BackupTests(Workspace):
    def test_verified_snapshot_and_media_copy_source_unchanged(self):
        self.legacy()
        (self.root/'audio').mkdir();(self.root/'audio/10.mp3').write_bytes(b'precious-audio')
        before = digest(self.db)
        destination = self.root.parent/(self.root.name+'-backup')
        self.addCleanup(lambda: __import__('shutil').rmtree(destination,ignore_errors=True))
        manifest = backup_tree(self.root,destination)
        self.assertTrue(manifest['complete'])
        self.assertEqual(verify_backup(destination),[])
        self.assertEqual(digest(self.db),before)
        migrate(destination/'webnovel.db')
        with connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='books'").fetchone())
        self.assertEqual((destination/'audio/10.mp3').read_bytes(),b'precious-audio')
        (destination/'audio/10.mp3').write_bytes(b'changed')
        self.assertTrue(verify_backup(destination))

    def test_snapshot_existing_destination_and_nested_backup_refused(self):
        self.legacy()
        self.assertRaises(ValueError,snapshot_database,self.db,self.db)
        self.assertRaises(ValueError,backup_tree,self.root,self.root/'nested')

    def test_symlink_is_reported_never_followed(self):
        self.legacy();(self.root/'link').symlink_to('/etc/passwd')
        destination = self.root.parent/(self.root.name+'-backup')
        self.addCleanup(lambda: __import__('shutil').rmtree(destination,ignore_errors=True))
        result = backup_tree(self.root,destination)
        self.assertFalse(result['complete'])
        self.assertTrue(any(e['path']=='link' for e in result['errors']))

    def test_live_wal_without_shm_refused(self):
        self.legacy();self.db.with_name('webnovel.db-wal').write_bytes(b'active')
        self.assertRaises(ValueError,snapshot_database,self.db,self.root/'snapshot.db')
