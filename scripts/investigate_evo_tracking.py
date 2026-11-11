"""Offline geometry investigation; never opens camera, HID or OpenVR."""
import argparse
from collections import Counter
from itertools import permutations
import json
from pathlib import Path

import numpy as np

from tools.psvr_camera.calibration import load_intrinsics
from tools.psvr_camera.imu_led_pose import ImuLedTracker, MODEL_TO_IMU, rotation_from_quaternion


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, required=True)
    args = p.parse_args()
    root = args.root
    source = root / 'logs/suite_tracking_20261015T180512696028Z_0e696411.jsonl'
    destination = root / 'logs/ac_evo_seated_20261015/geometry-investigation.json'
    previous = None
    last_valid = None
    gap = None
    gaps = []
    jumps = []
    probes = []
    last_probe = -float('inf')
    reasons = Counter()
    with source.open(encoding='utf-8') as stream:
        session = json.loads(next(stream))
        rig = session['imu_rig_full']
        tracker = ImuLedTracker(load_intrinsics(root / session['intrinsicsPath']),
                                np.asarray(rig['object_points_m']), np.asarray(rig['outward_normals']), rig['labels'])
        for line in stream:
            row = json.loads(line)
            if row.get('event') != 'pose':
                continue
            now = row['monotonic_s']
            if row['valid']:
                if gap:
                    gap['duration_s'] = now-gap['start_s']
                    if last_valid:
                        gap['reacquire_step_mm'] = float(np.linalg.norm(np.asarray(row['position_camera_m'])-np.asarray(last_valid['position_camera_m']))*1000)
                    gaps.append(gap)
                    gap = None
                if previous and previous['valid']:
                    step = float(np.linalg.norm(np.asarray(row['position_camera_m'])-np.asarray(previous['position_camera_m']))*1000)
                    if step > 15:
                        jumps.append(dict(time_s=now, step_mm=step, dt_s=now-previous['monotonic_s'],
                                          labels_before=previous['led_labels'], labels_after=row['led_labels'],
                                          rms_px=row['rms_px'], before_m=previous['position_camera_m'], after_m=row['position_camera_m']))
                last_valid = row
            else:
                if gap is None:
                    gap = dict(start_s=now, frames=0, reasons=Counter(), candidates=Counter())
                gap['frames'] += 1
                reason = row['reason'].split(':')[0]
                gap['reasons'][reason] += 1
                gap['candidates'][row['candidate_count']] += 1
                reasons[reason] += 1
                if (last_valid and row['candidate_count'] == 5 and reason == 'Pattern non associato'
                        and now-last_probe >= 8):
                    last_probe = now
                    alignment = np.asarray(row['estimator_input']['alignment'])
                    rotation = alignment @ rotation_from_quaternion(row['imu']['quaternion_sensor_wxyz']) @ MODEL_TO_IMU
                    centers = np.asarray([c['center_px'] for c in row['candidates']])
                    rvec = tracker.cv2.Rodrigues(rotation)[0]
                    best = None
                    for order in permutations(range(5)):
                        image = centers[list(order)]
                        translation = tracker.solve_translation(rotation, np.arange(5), image)
                        if translation is None:
                            continue
                        projected = tracker.cv2.projectPoints(tracker.points[:5], rvec, translation, tracker.k, tracker.dist)[0].reshape(-1, 2)
                        rms = float(np.sqrt(np.mean(np.sum((projected-image)**2, axis=1))))
                        if best is None or rms < best['rms_px']:
                            best = dict(rms_px=rms, translation_m=translation.reshape(3).tolist(),
                                        jump_from_last_valid_mm=float(np.linalg.norm(translation.reshape(3)-np.asarray(last_valid['position_camera_m']))*1000))
                    last_t = np.asarray(last_valid['position_camera_m']).reshape(3, 1)
                    visible, projected = tracker.predicted(rotation, last_t, relaxed=True)
                    distances = np.linalg.norm(projected[visible, None]-centers[None], axis=2)
                    nearest = distances.min(axis=1)
                    probes.append(dict(time_s=now, seconds_since_valid=now-last_valid['monotonic_s'],
                                       previous_labels=last_valid['led_labels'],
                                       expected_labels=[tracker.labels[i] for i in visible],
                                       nearest_projected_distances_px=nearest.tolist(), front_fit=best,
                                       candidate_centers=centers.tolist()))
            previous = row
    report = dict(source=str(source), invalid_reasons=reasons, gap_count=len(gaps),
                  gaps_largest=sorted(gaps, key=lambda g: g['duration_s'], reverse=True)[:15],
                  adjacent_raw_steps_over_15mm=len(jumps),
                  largest_raw_steps=sorted(jumps, key=lambda j: j['step_mm'], reverse=True)[:15],
                  failed_five_candidate_probes=probes)
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(dict(gap_count=len(gaps), gaps_largest=report['gaps_largest'][:5],
                          adjacent_raw_steps_over_15mm=len(jumps), largest_raw_steps=report['largest_raw_steps'][:5],
                          failed_five_candidate_probes=probes[:6])))


if __name__ == '__main__':
    main()
