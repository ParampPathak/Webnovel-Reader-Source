"""Read-only visible-position mapping; synthesis is deterministic and mocked."""
import json,re,unittest
from tests.test_platform import PlatformTests,RUNTIME_AVAILABLE
from library_store import connect

@unittest.skipUnless(RUNTIME_AVAILABLE,'Existing Flask dependencies required')
class ReadingStartTests(unittest.TestCase):
 def test_native_file_cursor_has_no_false_chunk_zero_on_later_metadata_repair(self):
  book,sid,text,audio,provider,_=self.fixture()
  response=self.client.put('/api/books/'+book+'/listening',json={'section_id':sid,'asset_id':audio['asset_id'],'seconds':15,'voice':'en-US-AndrewNeural','synthesis_rate':0,'playback_rate':2,'version':0})
  self.assertEqual(response.status_code,200)
  saved=self.client.get('/api/books/'+book+'/listening').json
  self.assertEqual(saved['seconds'],15);self.assertIsNone(saved['segment_index']);self.assertIsNone(saved['segment_seconds'])

 def test_incomplete_duration_metadata_is_reported_without_fabricating_total(self):
  book,sid,text,audio,provider,_=self.fixture()
  with connect(self.db) as c,c:c.execute('UPDATE audio_chunks SET duration=NULL WHERE asset_id=? AND position=1',(audio['asset_id'],))
  result=self.client.get('/api/books/'+book+'/audio?section_id='+str(sid)+'&voice=en-US-AndrewNeural&rate=0').json
  self.assertEqual(result['status'],'ready');self.assertFalse(result['timeline_complete']);self.assertEqual(len(result['segments']),1)
 setUp=PlatformTests.setUp
 upload=PlatformTests.upload
 book=PlatformTests.book
 worker=PlatformTests.worker
 post=PlatformTests.post
 drain=PlatformTests.drain

 def fixture(self,complete=True):
  book,sections=self.book();sid=sections[0]['id'];text=('A clear sentence. Unicode café 世界 😀.\n\n'*70).strip()
  with connect(self.db) as conn:
   with conn:conn.execute('UPDATE book_sections SET text_content=? WHERE id=?',(text,sid))
  worker,provider=self.worker();base='/api/books/'+book
  self.post(base+'/audio/jobs',{'section_id':sid});worker.step()
  if complete:self.drain(worker)
  audio=self.client.get(base+'/audio?section_id='+str(sid)).json
  return book,sid,text,audio,provider,worker

 def test_visible_offset_uses_actual_cached_utf16_chunk_and_not_old_listening(self):
  book,sid,text,audio,provider,_=self.fixture();asset=audio['asset_id'];calls=len(provider.calls)
  self.assertGreater(len(audio['segments']),5)
  for index in [0,1,4,len(audio['segments'])-1]:
   location=self.client.get('/api/audio/'+asset+'/location/'+str(index)).json
   result=self.client.get('/api/audio/'+asset+'/start?offset='+str(location['normalized_offset']))
   self.assertEqual(result.status_code,200);start=result.json
   self.assertEqual(start['segment_index'],index);self.assertTrue(start['available'])
   self.assertEqual(start['seconds'],10*index);self.assertEqual(start['section_id'],sid)
   self.assertEqual(start['precision'],'chunk');self.assertEqual(start['segment_seconds'],0)
  self.assertEqual(len(provider.calls),calls)

 def test_unpublished_requested_chunk_waits_without_reverting_to_first_chunk(self):
  book,sid,text,audio,provider,worker=self.fixture(False);asset=audio['asset_id']
  result=self.client.get('/api/audio/'+asset+'/start?offset=1200').json
  self.assertFalse(result['available']);self.assertGreater(result['segment_index'],0);wanted=result['segment_index']
  self.drain(worker)
  ready=self.client.get('/api/audio/'+asset+'/start?offset=1200').json
  self.assertTrue(ready['available']);self.assertEqual(ready['segment_index'],wanted)

 def test_invalid_changed_and_unknown_positions_are_rejected(self):
  book,sid,text,audio,provider,_=self.fixture();path='/api/audio/'+audio['asset_id']+'/start?offset='
  for offset in ['-1','999999999','NaN','1.5','']:
   self.assertEqual(self.client.get(path+offset).status_code,400)
  self.assertEqual(self.client.get('/api/audio/no-such-asset/start?offset=10').status_code,404)
  with connect(self.db) as conn:
   with conn:conn.execute('UPDATE book_sections SET text_content=? WHERE id=?',('Changed text',sid))
  self.assertEqual(self.client.get(path+'10').status_code,409)

 def test_eof_unicode_and_mapping_do_not_modify_progress_or_settings(self):
  book,sid,text,audio,provider,_=self.fixture();asset=audio['asset_id']
  with connect(self.db) as conn:
   before={t:conn.execute('SELECT * FROM '+t).fetchall() for t in ['reading_progress','listening_progress','settings','platform_settings']}
  offset=len(re.sub(r'\s+',' ',text).strip().encode('utf-16-le'))//2
  result=self.client.get('/api/audio/'+asset+'/start?offset='+str(offset))
  self.assertEqual(result.status_code,200);self.assertEqual(result.json['segment_index'],len(audio['segments'])-1)
  with connect(self.db) as conn:
   after={t:conn.execute('SELECT * FROM '+t).fetchall() for t in before}
  self.assertEqual(before,after)

if __name__=='__main__':unittest.main()
