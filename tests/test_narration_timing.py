import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from narration_timing import publish, load


class TimingTests(unittest.TestCase):
    def test_real_ticks_and_unicode_offsets_are_recording_bound(self):
        with tempfile.TemporaryDirectory() as root:
            audio=Path(root)/'000000.mp3';audio.write_bytes(b'original sample')
            events=Path(root)/'events'
            events.write_text('\n'.join(json.dumps(dict(type='WordBoundary',offset=i*10000000,duration=4000000,text=t)) for i,t in enumerate(['Hello','world','again'])))
            publish(audio,'Hello 🌍 world again.',events)
            checksum=hashlib.sha256(audio.read_bytes()).hexdigest()
            cues=load(audio,checksum,'Hello 🌍 world again.')
            self.assertEqual([c['at'] for c in cues],[0,1,2])
            self.assertEqual(cues[1]['offset'],9)
            self.assertEqual(load(audio,'different recording','Hello 🌍 world again.'),[])
            self.assertEqual(load(audio,checksum,'Changed text'),[])

    def test_legacy_and_unmatched_events_never_invent_cues(self):
        with tempfile.TemporaryDirectory() as root:
            audio=Path(root)/'old.mp3';audio.write_bytes(b'unchanged')
            self.assertEqual(load(audio,'hash','Old audio'),[])
            events=Path(root)/'events';events.write_text(json.dumps(dict(type='WordBoundary',offset=0,duration=10,text='not present')))
            publish(audio,'Actual original words',events)
            self.assertFalse(audio.with_suffix('.timing.json').exists())
            self.assertEqual(audio.read_bytes(),b'unchanged')
