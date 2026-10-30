#!/usr/bin/env python3
"""Usage: python tools/backup_data.py SOURCE NEW_DESTINATION [--verify]."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from preservation import backup_tree, verify_backup

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source')
    parser.add_argument('destination')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.verify:
        failures = verify_backup(args.destination)
        print(json.dumps({'verified':not failures,'errors':failures},indent=2))
        sys.exit(bool(failures))
    result = backup_tree(args.source,args.destination)
    print(json.dumps({'complete':result['complete'],'files':len(result['files']),
                      'errors':result['errors'],'manifest':str(Path(args.destination)/'manifest.json')},indent=2))
    sys.exit(not result['complete'])
