# SPDX-License-Identifier: MIT
"""Correlate saved video flashes with continuous telemetry and driver transitions."""
import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from analyze_recording import analyze


def micros(text):
    return int(datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp() * 1_000_000)


def driver_events(path):
    events = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        if 'pose_validity_transition' not in line:
            continue
        try:
            event = json.loads(line[line.index('{'):])
        except (ValueError, json.JSONDecodeError):
            continue
        if event.get('event') == 'pose_validity_transition':
            events.append(event)
    return events


def correlate(video, ffmpeg):
    video = Path(video).resolve(strict=True)
    stem = video.with_suffix('')
    metadata = json.loads(stem.with_suffix('.metadata.json').read_text(encoding='utf-8-sig'))
    analysis = analyze(video, ffmpeg)
    start = micros(metadata['video_started_utc'])
    end = micros(metadata['video_ended_utc'])
    rows = [json.loads(line) for line in stem.with_suffix('.telemetry.jsonl').read_text().splitlines()]
    samples = [r for r in rows if r['event'] == 'sample' and start <= r['utc_unix_us'] <= end]
    frames = [r for r in rows if r['event'] == 'frame_timing' and start <= r['utc_unix_us'] <= end]
    transitions = [e for e in driver_events(stem.with_suffix('.vrserver-after.txt')) if start <= e['utc_unix_us'] <= end]
    invalid_transitions = [e for e in transitions if not e['valid']]
    correlations = []
    for flash in analysis['bright_intervals_above_median_plus_8']:
        approximate_time = start + int(flash['start_seconds'] * 1_000_000)
        nearby = [e for e in transitions if abs(e['utc_unix_us'] - approximate_time) <= 2_000_000]
        nearby_samples = [s for s in samples if abs(s['utc_unix_us'] - approximate_time) <= 2_000_000]
        nearby_frames = [f for f in frames if abs(f['utc_unix_us'] - approximate_time) <= 2_000_000]
        correlations.append({
            'video_interval': flash, 'approximate_utc_unix_us': approximate_time,
            'driver_transitions': [dict(e, relative_to_flash_ms=(e['utc_unix_us'] - approximate_time) / 1000) for e in nearby],
            'raw_invalid_samples_nearby': sum(not s['raw_pose']['valid'] for s in nearby_samples),
            'compositor_invalid_samples_nearby': sum(s.get('compositor_pose') is not None and not s['compositor_pose']['valid'] for s in nearby_samples),
            'max_fade_alpha_nearby': max((s['fade_rgba'][3] for s in nearby_samples), default=None),
            'max_grid_alpha_nearby': max((s['grid_alpha'] for s in nearby_samples), default=None),
            'timing_frames_dropped_nearby': sum(f['dropped'] > 0 for f in nearby_frames),
            'timing_frames_mispresented_nearby': sum(f['mispresents'] > 0 for f in nearby_frames),
            'max_compositor_gpu_ms_nearby': max((f['compositor_gpu_ms'] for f in nearby_frames), default=None),
        })
    summary = next((r for r in rows if r['event'] == 'telemetry_summary'), None)
    return {
        'schema_version': 1, 'video': str(video), 'recording_completed': metadata.get('recording_completed'),
        'video_analysis': analysis, 'telemetry_summary': summary,
        'samples_during_video': len(samples),
        'raw_invalid_during_video': sum(not s['raw_pose']['valid'] for s in samples),
        'fullscreen_false_during_video': sum(not s['fullscreen'] for s in samples),
        'dashboard_states_during_video': dict(Counter(str(s['dashboard_visible']) for s in samples)),
        'invalid_transition_reasons': dict(Counter(e['reason'] for e in invalid_transitions)),
        'driver_invalid_transitions': invalid_transitions,
        'flash_correlations': correlations,
        'timing_frames_during_video': len(frames),
        'limitation': 'Video times are relative to encoder process launch, with uncertain capture startup latency. '
                      'Nearby transitions use a +/-2s window, not exact GPU frame synchronization. '
                      'Frame timings are received historical app records, not necessarily current driver pose.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--ffmpeg', type=Path, default=Path(r'C:\Program Files\ffmpeg\bin\ffmpeg.exe'))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() or not args.out.parent.is_dir():
        parser.error('new output in existing directory required')
    try:
        report = correlate(args.video, args.ffmpeg)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, f'Flash correlation failed: {error}\n')
    with args.out.open('x', encoding='utf-8') as output:
        json.dump(report, output, indent=2)
    print(json.dumps({
        'video': report['video'], 'flashes': report['video_analysis']['candidate_broad_flash_count'],
        'invalid_transition_reasons': report['invalid_transition_reasons'],
        'raw_invalid': report['raw_invalid_during_video'],
        'correlations': report['flash_correlations'],
        'report': str(args.out),
    }, indent=2))


if __name__ == '__main__':
    main()
