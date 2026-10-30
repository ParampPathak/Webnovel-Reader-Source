"""Resolve immutable originals within the instance's configured data storage."""
import re
from pathlib import Path


def original_path(root,book):
    root=Path(root).resolve()
    keys=book.keys()
    file_hash=book['file_hash'] if 'file_hash' in keys else None
    if file_hash and re.fullmatch('[0-9a-f]{64}',file_hash):
        candidate=root/'books'/(file_hash+'.epub')
        if candidate.is_file() and candidate.resolve().is_relative_to(root): return candidate
    value=book['original_path'] if 'original_path' in keys else None
    if not value: return None
    candidate=Path(value)
    if str(candidate).startswith('/data/'): candidate=root/str(candidate)[6:]
    if not candidate.is_absolute(): candidate=root/candidate
    candidate=candidate.resolve()
    return candidate if candidate.is_relative_to(root) and candidate.is_file() else None
