"""Real HTTP smoke for an isolated, disposable CI container; no external services."""
import json
from pathlib import Path
import sys
import time

import requests
from tests.fixtures import epub_bytes

BASE = 'http://127.0.0.1:5000'
RECEIPT = Path('/tmp/source-smoke.json')
session = requests.Session()
session.trust_env = False
session.headers['Origin'] = BASE


def get(path):
    response = session.get(BASE + path, timeout=10)
    response.raise_for_status()
    return response


for attempt in range(40):
    try:
        get('/api/health')
        break
    except requests.RequestException:
        time.sleep(0.5)
else:
    raise RuntimeError('Container did not become healthy')

if sys.argv[1:] == ['verify']:
    receipt = json.loads(RECEIPT.read_text())
else:
    response = session.post(BASE + '/api/books/upload',
                            files={'file': ('ci-original.epub', epub_bytes(2), 'application/epub+zip')},
                            timeout=30)
    assert response.status_code == 201, (response.status_code, response.text)
    book_id = response.json()['id']
    detail = get('/api/books/' + book_id).json()
    assert detail['section_count'] == 2, detail
    section_id = detail['first_section_id']
    response = session.put(BASE + '/api/books/' + book_id + '/progress', json={
        'section_id': section_id, 'version': 0, 'fraction': 0.25,
        'anchor': '', 'preferences': {}}, timeout=10)
    response.raise_for_status()
    receipt = {'book_id': book_id, 'section_id': section_id}
    RECEIPT.write_text(json.dumps(receipt))

book_path = '/api/books/' + receipt['book_id']
assert get(book_path).json()['section_count'] == 2
assert get(book_path + '/sections').json()['total'] == 2
assert get(book_path + '/sections/' + str(receipt['section_id'])).json()['id'] == receipt['section_id']
assert get(book_path + '/progress').json()['fraction'] == 0.25
assert 'text/html' in get('/reader/' + receipt['book_id']).headers['Content-Type']
print('PASS: isolated HTTP health, original EPUB, reader, chapters and persisted progress')
