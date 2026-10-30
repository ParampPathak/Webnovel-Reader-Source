#!/usr/bin/env python3
"""Run on the existing Linux checkout. Additive releases only; never restores/deletes data.

Usage: python deployment/safe_deploy.py --root /path/to/project --commit FULL_SHA
       --verified-backup /backups/pre-release --compose compose.yaml --compose compose.release.yaml
"""
import argparse
import datetime
import json
import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--verified-backup',required=True,help='Existing full recovery copy inside the app backup mount.')
    parser.add_argument('--compose',action='append',required=True)
    args=parser.parse_args();root=args.root.resolve()
    os.chdir(root)
    def run(*command,capture=False,input=None):
        return subprocess.run(command,check=True,text=True,input=input,stdout=subprocess.PIPE if capture else None).stdout
    def git(*a):return run('git',*a,capture=True).strip()
    if git('status','--porcelain'):raise SystemExit('Checkout has uncommitted tracked changes. Preserve them before deployment.')
    commit=git('rev-parse',args.commit+'^{commit}');old_commit=git('rev-parse','HEAD')
    run('git','merge-base','--is-ancestor',old_commit,commit)
    base=['docker','compose']
    for name in args.compose:
        if not (root/name).is_file():raise SystemExit('Missing existing compose file: '+name)
        base+=['-f',str(root/name)]
    config=json.loads(run(*base,'config','--format','json',capture=True))
    selected=['webnovel-app','webnovel-platform-worker']
    if any(name not in config['services'] for name in selected):raise SystemExit('Expected application services were not found.')
    container='webnovel-app'
    info=json.loads(run('docker','inspect',container,capture=True))[0]
    if Path(info['Config']['Labels']['com.docker.compose.project.working_dir']).resolve()!=root:
        raise SystemExit('Container belongs to another checkout; refusing to alter it.')
    project=info['Config']['Labels']['com.docker.compose.project']
    base[2:2]=['--project-name',project]
    backup_mount=next(m['Source'] for m in info['Mounts'] if m['Destination']=='/backups')
    original={}
    for name in selected:
        identifier=run(*base,'ps','-q',name,capture=True).strip()
        if not identifier:raise SystemExit('Expected running service: '+name)
        original[name]=json.loads(run('docker','inspect',identifier,capture=True))[0]['Image']
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    private=root/'deployment'/('release-'+stamp+'.local')
    private.mkdir(parents=True,exist_ok=False)
    receipt={'commit':commit,'previous_commit':old_commit,'previous_images':original,'full_backup':args.verified_backup,'status':'preflight'}
    def save():
        (private/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    save()
    # Verify the full recovery copy before any schema or running-image change.
    backup_code='from preservation import verify_backup;import json; errors=verify_backup('+repr(args.verified_backup)+',bytes_per_second=64*1024*1024,monitor=False);print(json.dumps({"verified":not errors,"errors":errors}));raise SystemExit(bool(errors))'
    run('docker','exec',container,'python','-c',backup_code)
    run('git','checkout','--detach',commit)
    image='webnovel-unified:'+commit[:12]
    # A long-lived deployment checkout contains ignored operational files. Build
    # only reviewed Git bytes, never that host's private/untracked working tree.
    with tempfile.TemporaryDirectory(prefix='webnovel-release-context-') as temporary:
        archive=Path(temporary)/'source.tar';context=Path(temporary)/'source';context.mkdir()
        run('git','archive','--format=tar','--output',str(archive),commit)
        with tarfile.open(archive) as source:source.extractall(context,filter='data')
        run('docker','build','--label','org.opencontainers.image.revision='+commit,'-t',image,str(context))
    test=['docker','run','--rm','--network','none','--read-only','--cpus','1','--memory','1g','--tmpfs','/tmp:rw,size=768m,mode=1777','--tmpfs','/data:rw,size=128m,mode=1777']
    for key in ['START_TTS_WORKERS','AUDIO_CACHE_CLEANUP_ENABLED','CHAPTER_UPDATES_ENABLED']:
        test+=['--env',key+'=0']
    test+=['--env','PYTHONDONTWRITEBYTECODE=1','--entrypoint','python',image,'-m','unittest','discover','-s','tests','-t','.']
    run(*test)
    override=private/'images.yaml';rollback=private/'rollback.yaml'
    def images(path,mapping):path.write_text('services:\n'+''.join('  '+name+':\n    image: '+value+'\n' for name,value in mapping.items()))
    images(override,{name:image for name in selected});images(rollback,original)
    compose=base+['-f',str(override)];old_compose=base+['-f',str(rollback)]
    # A fresh, consistent SQLite recovery point immediately before the additive
    # startup migration. The full immutable-media recovery copy is retained.
    db_backup='/backups/unified-db-'+stamp+'.sqlite'
    run('docker','exec',container,'python','-c','from preservation import snapshot_database;snapshot_database("/data/webnovel.db",'+repr(db_backup)+',allow_live_sidecars=True)')
    receipt['database_backup']=str(Path(backup_mount)/Path(db_backup).name)
    # This is a read-only API/content inventory. No test writes production progress.
    snapshot='''import sqlite3,json,os
from pathlib import Path
c=sqlite3.connect("file:/data/webnovel.db?mode=ro",uri=True)
tables=["books","book_sections","chapters","reading_progress","listening_progress","book_bookmarks","book_preferences","audio_variants","audio_chunks"]
counts={t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in tables}
media={}
for name in ["books","audio","covers","exports","piper_voices","audiobook-exports"]:
 for p in (Path('/data')/name).rglob('*'):
  if p.is_file() and not p.name.endswith(('.part','.part.events','.lock')):media[str(p.relative_to('/data'))]=[p.stat().st_size,p.stat().st_mtime_ns]
print(json.dumps({"counts":counts,"media":media}))'''
    before=json.loads(run('docker','exec',container,'python','-c',snapshot,capture=True))
    (private/'before.json').write_text(json.dumps(before))
    try:
        run(*compose,'up','-d','--no-deps','--no-build',*selected)
        health_code='import urllib.request,json;u="http://127.0.0.1:5000";a=json.load(urllib.request.urlopen(u+"/api/health",timeout=4));b=json.load(urllib.request.urlopen(u+"/api/compatibility",timeout=4));assert b["api_contract"]==1;assert urllib.request.urlopen(u+"/library",timeout=4).status==200;assert "items" in json.load(urllib.request.urlopen(u+"/api/books?limit=1",timeout=4));print(json.dumps({"health":a,"compatibility":b}))'
        health=None
        for _ in range(12):
            try:
                health=json.loads(run('docker','exec',container,'python','-c',health_code,capture=True));break
            except (subprocess.CalledProcessError,json.JSONDecodeError):time.sleep(5)
        if health is None:raise RuntimeError('Application health/API compatibility check did not pass.')
        after=json.loads(run('docker','exec',container,'python','-c',snapshot,capture=True))
        lost=[name for name,count in before['counts'].items() if after['counts'][name]<count]
        # Existing cleanup policy may legitimately evict unpinned audio; report
        # every difference and fail deployment if any preexisting file vanished.
        changed=[name for name,value in before['media'].items() if after['media'].get(name)!=value]
        if lost or changed:raise RuntimeError('Preservation check requires review: '+repr({'tables':lost,'media_files':len(changed)}))
        receipt.update(status='healthy',image=image,health=health,counts_before=before['counts'],counts_after=after['counts'],preserved_media_files=len(before['media']),rollback_command=old_compose+['up','-d','--no-deps','--no-build',*selected])
        save();print(json.dumps({'status':'healthy','commit':commit,'receipt':str(private/'receipt.json'),'preserved_media_files':len(before['media'])}))
    except BaseException:
        # Never restore the old database over newer listening/reading writes.
        run(*old_compose,'up','-d','--no-deps','--no-build',*selected)
        run('git','checkout','--detach',old_commit)
        receipt['status']='rolled-back-images-data-retained';save()
        raise


if __name__=='__main__':main()
