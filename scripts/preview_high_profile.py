"""Read-only high-profile preview; never start a local/remote reconstruction."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.experiments.high_profile import BASE_COMMIT, BASE_CONFIG_SHA, build_pipeline_overlay, capture_stage_plan, file_sha
from backend.experiments.preflight import verify_git_identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--pipeline-root', type=Path, required=True)
    parser.add_argument('--mode', choices=('overlay', 'stages'), required=True)
    parser.add_argument('--overlay', type=Path)
    parser.add_argument('--prepared-manifest', type=Path)
    args = parser.parse_args()
    try:
        verify_git_identity(args.pipeline_root, BASE_COMMIT)
        baseline_path = args.pipeline_root / 'configs/pipeline.json'
        if file_sha(baseline_path) != BASE_CONFIG_SHA:
            raise ValueError()
        baseline = json.loads(baseline_path.read_bytes())
        profile = json.loads(args.profile.read_bytes())
        overlay, diff = build_pipeline_overlay(baseline, profile)
        if args.mode == 'overlay':
            output = {'status': 'offline_overlay_preview', 'server_contacted': False, 'stage_executed': False,
                      'baseline_config_sha256': BASE_CONFIG_SHA, 'overlay': overlay, 'diff': diff}
        else:
            if args.overlay is None or args.prepared_manifest is None:
                raise ValueError()
            if json.loads(args.overlay.read_bytes()) != overlay:
                raise ValueError()
            prepared = json.loads(args.prepared_manifest.read_bytes())
            if prepared.get('count') != 300:
                raise ValueError()
            stages = capture_stage_plan(args.pipeline_root, args.overlay, args.pipeline_root / 'configs/paths.local.json', profile, prepared['uniform_size'])
            output = {'status': 'offline_local_argv_preview_not_a_server_launch_plan', 'server_contacted': False,
                      'stage_executed': False, 'runtime_paths_need_server_regeneration': True,
                      'overlay_config_sha256': file_sha(args.overlay), 'profile_sha256': file_sha(args.profile),
                      'native_final_output_validation_not_recorded_in_dry_run': True,
                      'mapper_budget_adapter_in_full_supervisor_integrated': True,
                      'current_short_benchmark_runner_accepts_this_profile': False, 'stages': stages}
        print(json.dumps(output, ensure_ascii=True, separators=(',', ':'), allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'preview_failed', 'error_code': getattr(exc, 'code', 'HIGH_PROFILE_PREVIEW_FAILED')}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
