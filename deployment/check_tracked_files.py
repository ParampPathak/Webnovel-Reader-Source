#!/usr/bin/env python3
"""Fail CI on tracked private state/build outputs and common credential formats."""
import re
import subprocess
from pathlib import Path

root=Path(__file__).resolve().parents[1]
paths=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
errors=[]
for name in filter(None,paths):
    path=Path(name)
    if any(part.endswith('.private') or part.endswith('.private.md') for part in path.parts) or any(part in {'data','backups','DerivedData','xcuserdata','.build','.aws','.codex','.ssh','SideStoreMigration','SideStoreAutomaticRenewal'} for part in path.parts) or path.suffix.lower() in {'.epub','.mp3','.wav','.db','.sqlite','.sqlite3','.ipa','.mobileprovision','.p12','.pfx','.key','.pem'}:
        errors.append(name+' is private state or a build/signing artifact')
    # Inspect the exact staged release, avoiding hydration of iCloud-evicted
    # working files and accidental differences between reviewed/committed bytes.
    size=int(subprocess.check_output(['git','cat-file','-s',':'+name],cwd=root))
    if size>2*1024*1024:continue
    data=subprocess.check_output(['git','show',':'+name],cwd=root)
    if b'\0' in data:continue
    for pattern in [rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',rb'gh[pousr]_[A-Za-z0-9]{30,}',rb'github_pat_[A-Za-z0-9_]{30,}',rb'AKIA[0-9A-Z]{16}']:
        if re.search(pattern,data):errors.append(name+' contains a possible credential')
if errors:raise SystemExit('\n'.join(errors))
print('Tracked-file checks passed (not a substitute for reviewing the staged diff).')
