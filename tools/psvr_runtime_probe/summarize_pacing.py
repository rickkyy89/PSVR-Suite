# SPDX-License-Identifier: MIT
"""Read-only pacing summary, including a still-growing telemetry JSONL file."""
import argparse
import json
import statistics
from collections import Counter
from pathlib import Path


def summarize(path, seconds):
    rows = []
    with Path(path).open(encoding='utf-8-sig') as source:
        for line in source:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                # Writer may currently be appending the final buffered line.
                if line.endswith('\n'):
                    raise
    end = max(r['utc_unix_us'] for r in rows)
    start = end - int(seconds * 1_000_000)
    samples = [r for r in rows if r['event'] == 'sample' and r['utc_unix_us'] >= start]
    frames = sorted((r for r in rows if r['event'] == 'frame_timing' and r['utc_unix_us'] >= start),
                    key=lambda r: r['frame_index'])
    intervals = [(b['system_time_s'] - a['system_time_s']) * 1000
                 for a, b in zip(frames, frames[1:]) if b['system_time_s'] > a['system_time_s']]
    def stats(values):
        values = sorted(values)
        return None if not values else {'mean': statistics.mean(values), 'median': statistics.median(values),
                                       'p95': values[int((len(values) - 1) * .95)], 'max': max(values)}
    return {
        'window_seconds': seconds, 'last_utc_unix_us': end, 'samples': len(samples), 'frame_records': len(frames),
        'raw_invalid': sum(not r['raw_pose']['valid'] for r in samples),
        'fullscreen_false': sum(not r['fullscreen'] for r in samples),
        'frame_interval_ms': stats(intervals),
        'timing_frame_rate': (frames[-1]['frame_index'] - frames[0]['frame_index']) /
            (frames[-1]['system_time_s'] - frames[0]['system_time_s']) if len(frames) > 1 and frames[-1]['system_time_s'] > frames[0]['system_time_s'] else None,
        'frames_with_drops': sum(r['dropped'] > 0 for r in frames),
        'frames_mispresented': sum(r['mispresents'] > 0 for r in frames),
        'presents_per_frame': dict(Counter(str(r['presents']) for r in frames)),
        'reprojection_flags': dict(Counter(str(r['reprojection_flags']) for r in frames)),
        'compositor_gpu_ms': stats([r['compositor_gpu_ms'] for r in frames]),
        'compositor_cpu_ms': stats([r['compositor_cpu_ms'] for r in frames]),
        'present_cpu_ms': stats([r['present_cpu_ms'] for r in frames]),
        'poll_gap_ms': stats([r['poll_gap_ms'] for r in samples]),
        'dashboard_states': dict(Counter(str(r['dashboard_visible']) for r in samples)),
        'limitations': 'Frame timing is runtime-reported app/compositor data, not optical scanout. '
                       'Raw validity does not measure orientation update cadence or prediction quality.',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('telemetry', type=Path)
    parser.add_argument('--seconds', type=float, default=30)
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error('seconds must be positive')
    try:
        report = summarize(args.telemetry, args.seconds)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f'Pacing summary failed: {error}\n')
    print(json.dumps(report, indent=2))
