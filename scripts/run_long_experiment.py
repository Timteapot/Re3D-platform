"""Opt-in tool-overlay bootstrap. No execution without --execute-long-test."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def verify_before_import(root):
    """Only stdlib executes before the explicit execution overlay hash gate."""
    required = {f'backend/experiments/{name}.py' for name in ('__init__','checkpoint','resources','process','preflight',
        'runner','high_profile','stages','snapshot_store','evidence','supervision','long_runner')}
    required |= {'scripts/run_long_experiment.py','scripts/run_profiled_colmap.py'}
    path = root / 'tool-manifest.json'
    for part in (path, *path.parents):
        if not part.exists() or part.is_symlink() or getattr(part.stat(), 'st_file_attributes', 0) & 1024:
            raise ValueError('LONG_TOOL_MANIFEST_REQUIRED')
    if path.stat().st_size > 128*1024: raise ValueError('LONG_TOOL_MANIFEST_REQUIRED')
    entries = json.loads(path.read_bytes()).get('files')
    if not isinstance(entries, dict) or set(entries) != required: raise ValueError('LONG_TOOL_MANIFEST_REQUIRED')
    for name in required:
        file = root / name
        for part in (file, *file.parents):
            if not part.exists() or part.is_symlink() or getattr(part.stat(), 'st_file_attributes', 0) & 1024:
                raise ValueError('LONG_TOOL_HASH_MISMATCH')
        if not isinstance(entries[name], str) or not re.fullmatch(r'[0-9a-f]{64}', entries[name]):
            raise ValueError('LONG_TOOL_HASH_MISMATCH')
        with file.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != entries[name]:
                raise ValueError('LONG_TOOL_HASH_MISMATCH')


def main():
    parser=argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--platform-root',type=Path,default=Path(r'D:\Re3D-platform'))
    parser.add_argument('--execute-long-test', action='store_true')
    args,unknown=parser.parse_known_args()
    if any(item.startswith('--execute') for item in unknown):
        parser.error('LONG_EXPLICIT_EXECUTION_OPTION_REQUIRED')
    if args.execute_long_test:
        try: verify_before_import(Path(__file__).resolve().parents[1])
        except BaseException:
            print(json.dumps({'status':'not_completed','error_code':'LONG_TOOL_HASH_GATE_FAILED'}))
            return 1
    sys.path.insert(0,str(args.platform_root))
    import backend
    overlay=Path(__file__).resolve().parents[1]/'backend'
    # Prefer this tool overlay even when the host checkout also has experiments.
    # Non-experiment backend dependencies still resolve from the platform root.
    if str(overlay) in backend.__path__: backend.__path__.remove(str(overlay))
    backend.__path__.insert(0,str(overlay))
    from backend.experiments.long_runner import main as run
    return run(Path(__file__).resolve().parents[1])


if __name__=='__main__':
    raise SystemExit(main())
