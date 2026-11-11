"""Offline side-left triangulation using only front LEDs for translation.

Selected correspondences inherit the live matcher's selection bias. This
tests compatibility with held-out rays, not physical calibration or identity.
"""
from collections import Counter
import json
from pathlib import Path

import numpy as np

from tools.psvr_camera.calibration import load_intrinsics
from tools.psvr_camera.imu_led_pose import ImuLedTracker
from tools.psvr_camera.refine_rotation_rig import ray_system


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / 'logs/suite_tracking_20261015T195257117578Z_e9208322.jsonl'
    output = root / 'logs/ac_evo_coherent_v2_20261015'
    counts = Counter()
    views = []
    seen = set()
    first = None
    with source.open('rb') as stream:
        session = json.loads(next(stream))
        rig = session['imu_rig_full']
        points = np.asarray(rig['object_points_m'])
        labels = rig['labels']
        index = labels.index('side_left')
        intrinsics = load_intrinsics(root / session['intrinsicsPath'])
        tracker = ImuLedTracker(intrinsics, points.copy(), np.asarray(rig['outward_normals']), labels)
        limit_bytes = source.stat().st_size
        for line in stream:
            if stream.tell() > limit_bytes:
                break
            row = json.loads(line)
            if row.get('event') != 'pose':
                continue
            if first is None:
                first = row['monotonic_s']
            observed_labels = row.get('led_labels') or []
            if not row.get('valid') or 'side_left' not in observed_labels:
                continue
            counts['valid_frames_with_side_left'] += 1
            front = [i for i, label in enumerate(observed_labels) if labels.index(label) < 5]
            if len(front) < 3:
                counts['fewer_than_three_front_anchors'] += 1
                continue
            image = np.asarray(row['image_points'])
            rotation = tracker.cv2.Rodrigues(np.asarray(row['rvec']))[0]
            ids = np.asarray([labels.index(observed_labels[i]) for i in front])
            translation = tracker.solve_translation(rotation, ids, image[front])
            if translation is None:
                counts['invalid_front_translation'] += 1
                continue
            projected = tracker.cv2.projectPoints(points[ids], np.asarray(row['rvec']), translation,
                                                  tracker.k, tracker.dist)[0].reshape(-1, 2)
            rms = float(np.sqrt(np.mean(np.sum((projected-image[front])**2, axis=1))))
            if rms > 1.5:
                counts['front_rms_above_1_5px'] += 1
                continue
            elapsed = row['monotonic_s']-first
            # At most one observation per second AND per 1.5-degree orientation
            # cell within a temporal block. Adjacent 75Hz frames are not views.
            block = int(elapsed//5)
            bucket = (block, *np.round(np.rad2deg(row['rvec'])/1.5).astype(int))
            second = ('second', int(elapsed))
            if bucket in seen or second in seen:
                continue
            seen.add(bucket)
            seen.add(second)
            pixel = image[observed_labels.index('side_left')]
            ray = tracker.cv2.undistortPoints(pixel.reshape(1, 1, 2), tracker.k, tracker.dist).reshape(2)
            a = ray_system(ray)
            views.append(dict(time=elapsed, block=block, matrix=a@rotation,
                              values=-a@translation.reshape(3), rotation=rotation,
                              translation=translation, pixel=pixel))

    training = [v for v in views if v['block'] % 3 != 0]
    heldout = [v for v in views if v['block'] % 3 == 0]
    report = dict(counts=counts, independent_views=len(views), training_views=len(training),
                  heldout_views=len(heldout), training_blocks=sorted({v['block'] for v in training}),
                  heldout_blocks=sorted({v['block'] for v in heldout}), accepted=False,
                  limitations='Live-selected identities and rotation; no ground truth. Temporal blocks disjoint, same session.')
    if len(training) < 12 or len(heldout) < 6:
        report['reason'] = 'insufficient temporally separated front-anchored views'
    else:
        matrix = np.vstack([v['matrix'] for v in training])
        values = np.hstack([v['values'] for v in training])
        condition = float(np.linalg.cond(matrix))
        point = np.linalg.lstsq(matrix, values, rcond=None)[0]
        residual = np.linalg.norm((matrix@point-values).reshape(-1, 2), axis=1)
        keep = [v for v, error in zip(training, residual) if error <= max(.002, float(np.median(residual))*2.5)]
        if len(keep) >= 12:
            matrix = np.vstack([v['matrix'] for v in keep])
            point = np.linalg.lstsq(matrix, np.hstack([v['values'] for v in keep]), rcond=None)[0]
            condition = float(np.linalg.cond(matrix))

        def errors(candidate):
            errors = []
            for v in heldout:
                projected = tracker.cv2.projectPoints(candidate.reshape(1, 3), tracker.cv2.Rodrigues(v['rotation'])[0],
                                                      v['translation'], tracker.k, tracker.dist)[0].reshape(2)
                errors.append(float(np.linalg.norm(projected-v['pixel'])))
            return dict(median_px=float(np.median(errors)), p90_px=float(np.percentile(errors, 90)), maximum_px=max(errors))

        before, after = errors(points[index]), errors(point)
        accepted = (condition <= 100 and len(keep) >= 12 and np.linalg.norm(point-points[index]) <= .06
                    and after['median_px'] < before['median_px']*.8 and after['p90_px'] < 4)
        report.update(condition=condition, retained_training_views=len(keep), original_point_m=points[index].tolist(),
                      proposed_point_m=point.tolist(), change_mm=float(np.linalg.norm(point-points[index])*1000),
                      heldout_before=before, heldout_after=after, accepted=bool(accepted))
        if not accepted:
            report['reason'] = 'ray diversity, plausibility or held-out precision gate failed'
        # A failed validation may still be replayed as a labelled hypothesis;
        # never convert the proposal into an accepted calibration by rounding.
        rig['object_points_m'][index] = point.tolist()
        rig['offline_side_left_validation'] = report
        rig['approved_for_production'] = False
        (output / 'side-left-offline-proposal.json').write_text(json.dumps(rig, indent=2), encoding='utf-8')
    (output / 'side-left-independent-validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
