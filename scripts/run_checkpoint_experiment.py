"""Bootstrap an isolated tool overlay against the existing platform package."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

PAYLOADS = {
    'backend/experiments/__init__.py', 'backend/experiments/checkpoint.py',
    'backend/experiments/resources.py', 'backend/experiments/process.py',
    'backend/experiments/preflight.py', 'backend/experiments/runner.py',
    'scripts/run_checkpoint_experiment.py', 'config/resume-config.json',
    'config/performance-policy.json',
}


def verify_overlay(root):
    manifest_path = root / 'tool-manifest.json'
    if not manifest_path.is_file() or manifest_path.stat().st_size > 32768:
        raise ValueError('EXPERIMENT_TOOL_MANIFEST_INVALID')
    value = json.loads(manifest_path.read_bytes())
    entries = value['files']
    if not isinstance(entries, dict) or set(entries) != PAYLOADS:
        raise ValueError('EXPERIMENT_TOOL_INVENTORY_INVALID')
    for name, expected in entries.items():
        path = root / name
        for part in (path, *path.parents):
            if part.is_symlink() or (getattr(part.stat(), 'st_file_attributes', 0) & 1024):
                raise ValueError('EXPERIMENT_TOOL_REPARSE')
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected:
            raise ValueError('EXPERIMENT_TOOL_HASH_MISMATCH')


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--platform-root", type=Path, default=Path(r"D:\Re3D-platform"))
    parsed, _ = parser.parse_known_args()
    try:
        verify_overlay(Path(__file__).resolve().parents[1])
    except Exception:
        print('{"status":"not_completed","error_code":"EXPERIMENT_TOOL_VERIFICATION_FAILED"}', flush=True)
        return 1
    sys.path.insert(0, str(parsed.platform_root))
    import backend
    overlay = Path(__file__).resolve().parents[1] / "backend"
    if str(overlay) not in backend.__path__:
        backend.__path__.append(str(overlay))
    from backend.experiments.runner import main as run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
