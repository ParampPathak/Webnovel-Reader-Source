"""WebKit parent-handler policy while untrusted publication scripts stay blocked."""
import unittest
from pathlib import Path
from epub_library import ReadingHTML
class ReaderPolicy(unittest.TestCase):
 def test_parent_handlers_allowed_but_publication_execution_denied(self):
  template=Path('templates/reader.html').read_text();reader=Path('static/reader.js').read_text()
  self.assertIn('sandbox="allow-same-origin allow-scripts"',template)
  self.assertIn("script-src 'none'",reader)
  self.assertIn("form-action 'none'",reader)
  parser=ReadingHTML('chapter.xhtml','book',{})
  parser.feed('<script>parent.hacked=true</script><p onclick="parent.hacked=true">Safe text</p><iframe src="javascript:alert(1)"></iframe>')
  cleaned=''.join(parser.output)
  self.assertNotIn('script',cleaned);self.assertNotIn('onclick',cleaned);self.assertNotIn('iframe',cleaned)
