#!/usr/bin/env python3
"""Transfer a committed release through existing SSH pairing, then deploy safely.

No GitHub token is stored on the server. The Git bundle preserves commit history.
Run local checks and commit/push first; this command also reruns Linux tests in the
built image and verifies the requested recovery backup before changing services.
"""
import argparse
import shlex
import subprocess
import tempfile
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',required=True)
    p.add_argument('--root',required=True)
    p.add_argument('--verified-backup',required=True)
    p.add_argument('--compose',action='append',required=True)
    args=p.parse_args();repo=Path(__file__).resolve().parents[1]
    def run(*command,**kwargs):return subprocess.run(command,check=True,**kwargs)
    if subprocess.check_output(['git','status','--porcelain'],cwd=repo).strip():raise SystemExit('Commit reviewed changes before publishing.')
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    with tempfile.TemporaryDirectory(prefix='webnovel-publish-') as directory:
        bundle=Path(directory)/'source.bundle'
        run('git','bundle','create',str(bundle),'HEAD',cwd=repo)
        remote='/tmp/webnovel-source-'+commit+'.bundle'
        run('scp',str(bundle),args.host+':'+remote)
        command='cd '+shlex.quote(args.root)+' && git fetch '+shlex.quote(remote)+' HEAD && git show '+commit+':deployment/safe_deploy.py > /tmp/webnovel-safe-deploy-'+commit+'.py'
        run('ssh',args.host,command)
        command=['python3','/tmp/webnovel-safe-deploy-'+commit+'.py','--root',args.root,'--commit',commit,'--verified-backup',args.verified_backup]
        for compose in args.compose:command+=['--compose',compose]
        run('ssh',args.host,shlex.join(command))


if __name__=='__main__':main()
