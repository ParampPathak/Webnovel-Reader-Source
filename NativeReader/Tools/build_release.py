#!/usr/bin/env python3
"""Build an unsigned SideStore input IPA; never installs or renews certificates."""
import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend-url',required=True)
    p.add_argument('--team',required=True,help='Existing Personal Team; no account operations are performed.')
    p.add_argument('--output',type=Path,required=True,help='Private output directory outside the Git checkout.')
    p.add_argument('--version',default='1.1')
    p.add_argument('--build',default='2')
    args=p.parse_args()
    url=urlsplit(args.backend_url)
    if url.scheme!='https' or not url.hostname or url.username or url.password or url.port or url.query or url.fragment or url.path not in ('','/'):
        p.error('Use the existing HTTPS backend origin without credentials, query, fragment, or custom port.')
    native=Path(__file__).resolve().parents[1]
    args.output=args.output.resolve()
    if native.parent==args.output or native.parent in args.output.parents:
        p.error('Release packages and logs must remain outside the Git checkout.')
    args.output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='webnovel-release-') as temporary:
        root=Path(temporary);derived=root/'DerivedData'
        command=['xcodebuild','build','-project',str(native/'WebnovelReader.xcodeproj'),'-scheme','WebnovelReader','-configuration','Release','-destination','generic/platform=iOS','-derivedDataPath',str(derived),'CODE_SIGNING_ALLOWED=NO',f'DEVELOPMENT_TEAM={args.team}',f'NATIVE_BACKEND_URL={args.backend_url}',f'MARKETING_VERSION={args.version}',f'CURRENT_PROJECT_VERSION={args.build}']
        with (args.output/'build.log').open('w') as log:subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
        app=derived/'Build/Products/Release-iphoneos/WebnovelReader.app'
        info=plistlib.loads((app/'Info.plist').read_bytes())
        assert info['CFBundleIdentifier']=='com.parampathak.WebnovelReader'
        assert info['NativeBackendURL'].rstrip('/')==args.backend_url.rstrip('/')
        assert 'audio' in info['UIBackgroundModes']
        assert info.get('CFBundleIcons',{}).get('CFBundlePrimaryIcon')
        assert (app/'Assets.car').is_file()
        assert not (app/'embedded.mobileprovision').exists()
        # A plain ZIP avoids the AppleDouble ._*.app importer failure. No private
        # profile, device pairing, app container, or signing key is packaged.
        ipa=args.output/f'WebnovelReader-{args.version}-{args.build}-SideStore.ipa'
        with zipfile.ZipFile(ipa,'w',zipfile.ZIP_DEFLATED) as archive:
            for path in app.rglob('*'):
                if not path.is_file() or path.name.startswith('._') or path.name=='.DS_Store':continue
                archive.write(path,'Payload/'+str(path.relative_to(app.parent)))
        with zipfile.ZipFile(ipa) as archive:
            assert archive.testzip() is None
            assert not any('/._' in name or name.startswith('__MACOSX') for name in archive.namelist())
            assert sum(name.count('/')==1 and name.endswith('.app/') for name in archive.namelist())<=1
        manifest={'bundle_id':info['CFBundleIdentifier'],'version':info['CFBundleShortVersionString'],'build':info['CFBundleVersion'],'signing_owner':'SideStore','input_signed':False,'api_contract':1,'sha256':hashlib.sha256(ipa.read_bytes()).hexdigest(),'bytes':ipa.stat().st_size}
        (args.output/'release-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        print(json.dumps({'ipa':str(ipa),**manifest},indent=2))


if __name__=='__main__':main()
