import io
import zipfile
from xml.sax.saxutils import escape


def _build_epub(output, count=3, *, epub2=False, hostile=False, nested=False, paragraph_bytes=0):
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype','application/epub+zip',compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml','''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>''')
        manifest = ''.join(f'<item id="c{i}" href="chapter{i}.xhtml" media-type="application/xhtml+xml"/>' for i in range(count))
        spine = ''.join(f'<itemref idref="c{i}"/>' for i in range(count))
        nav_item = '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>' if epub2 else '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        z.writestr('OPS/book.opf',f'''<package xmlns="http://www.idpf.org/2007/opf" version="{'2.0' if epub2 else '3.0'}" unique-identifier="uid"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">fixture</dc:identifier><dc:title>A &amp; B</dc:title><dc:creator>Test Author</dc:creator><dc:language>en</dc:language><dc:description>A test book</dc:description><meta name="cover" content="cover"/></metadata><manifest>{manifest}{nav_item}<item id="cover" href="images/cover.png" media-type="image/png" properties="cover-image"/></manifest><spine toc="ncx">{spine}</spine></package>''')
        # A valid 1x1 PNG, rather than a media-type-only fixture.
        import base64
        z.writestr('OPS/images/cover.png',base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='))
        for i in range(count):
            attack = '''<script>window.top.pwned=true</script><img src="https://evil.invalid/tracker" onerror="alert(1)"><a href="javascript:alert(1)">bad</a><iframe src="/api/delete/1"></iframe><svg onload="alert(1)"></svg><form action="/api/delete/1"><input value="bad"></form>''' if hostile else ''
            long_text = '<p>' + ('A quiet paragraph with words and punctuation. ' * (paragraph_bytes // 45)) + '</p>' if paragraph_bytes else ''
            z.writestr(f'OPS/chapter{i}.xhtml',f'''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Hidden title</title><style>@import 'https://evil.invalid/';</style></head><body><h1 id="heading">Chapter {i+1}</h1><p>Text &amp; meaning {i}.</p>{long_text}<p id="note">A footnote</p><a href="#note">See note</a><img src="images/cover.png" alt="Cover"/>{attack}</body></html>''')
        if epub2:
            points = ''.join(f'<navPoint id="n{i}"><navLabel><text>Chapter {i+1}</text></navLabel><content src="chapter{i}.xhtml#heading"/></navPoint>' for i in range(count))
            z.writestr('OPS/toc.ncx',f'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>{points}</navMap></ncx>')
        else:
            links = ''.join(f'<li><a href="chapter{i}.xhtml#heading">Chapter {i+1}</a></li>' for i in range(count))
            if nested:
                links = '<li><span>Part One</span><ol>'+links+'</ol></li>'
            z.writestr('OPS/nav.xhtml',f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>{links}</ol></nav></body></html>')


def change_entry(payload,name,data):
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(result,'w') as target:
        for item in source.infolist():
            target.writestr(item, data if item.filename==name else source.read(item))
        if name not in source.namelist():
            target.writestr(name,data)
    return result.getvalue()


def epub_bytes(count=3, **options):
    output = io.BytesIO()
    _build_epub(output,count,**options)
    return output.getvalue()


def write_epub(path, count=3, **options):
    """Large test fixtures write directly to disk, with one chapter in memory."""
    _build_epub(str(path),count,**options)
    return path
