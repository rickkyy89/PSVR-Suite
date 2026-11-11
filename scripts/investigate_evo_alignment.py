"""Fit camera/IMU alignment innovations offline from five-front-LED frames."""
import json
from itertools import permutations
from pathlib import Path

import numpy as np

from tools.psvr_camera.calibration import load_intrinsics
from tools.psvr_camera.imu_led_pose import ImuLedTracker, MODEL_TO_IMU, rotation_from_quaternion


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / 'logs/suite_tracking_20261015T180512696028Z_0e696411.jsonl'
    groups = {}
    labels = ['upper_left', 'upper_right', 'centre', 'lower_left', 'lower_right']
    last = -float('inf')
    with source.open(encoding='utf-8') as stream:
        session = json.loads(next(stream))
        rig = session['imu_rig_full']
        tracker = ImuLedTracker(load_intrinsics(root / session['intrinsicsPath']),
                                np.asarray(rig['object_points_m']), np.asarray(rig['outward_normals']), rig['labels'])
        for line in stream:
            row = json.loads(line)
            if row.get('event') != 'pose' or row.get('candidate_count') != 5:
                continue
            now = row['monotonic_s']
            if now-last < 4:
                continue
            last = now
            alignment = row.get('estimator_input', {}).get('alignment')
            if alignment is None:
                continue
            centers = np.asarray([c['center_px'] for c in row['candidates']])
            tracker.front.reset()
            fits = []
            for order in permutations(range(5)):
                fits.extend(tracker.front.solve(centers[list(order)]))
            fits.sort(key=lambda fit: fit.rms_px)
            if not fits or fits[0].rms_px > 1.5:
                continue
            best = fits[0]
            if any(fit.rms_px-best.rms_px < .3 and
                   np.max(np.linalg.norm(fit.image_points-best.image_points, axis=1)) > 3 for fit in fits[1:]):
                continue
            image = best.image_points
            rotation = np.asarray(alignment) @ rotation_from_quaternion(row['imu']['quaternion_sensor_wxyz']) @ MODEL_TO_IMU
            groups.setdefault(int((now-119540.8267949)//60), []).append((rotation, image))

    def residual(delta, views):
        correction = tracker.cv2.Rodrigues(delta)[0]
        values = []
        for rotation, image in views:
            rotation = correction @ rotation
            translation = tracker.solve_translation(rotation, np.arange(5), image)
            if translation is None:
                raise ValueError('invalid fit')
            projected = tracker.cv2.projectPoints(tracker.points[:5], tracker.cv2.Rodrigues(rotation)[0],
                                                  translation, tracker.k, tracker.dist)[0].reshape(-1, 2)
            values.extend((projected-image).reshape(-1))
        return np.asarray(values)

    report = []
    for minute, views in groups.items():
        if len(views) < 4:
            continue
        training, heldout = views[::2], views[1::2]
        delta = np.zeros(3)
        for _ in range(12):
            r = residual(delta, training)
            eps = 1e-5
            jac = np.column_stack([(residual(delta + np.eye(3)[axis]*eps, training)-r)/eps for axis in range(3)])
            step = np.linalg.lstsq(jac, -r, rcond=None)[0]
            for scale in (1., .5, .25, .125):
                trial = delta + step*scale
                if np.linalg.norm(residual(trial, training)) < np.linalg.norm(r):
                    delta = trial
                    break
            else:
                break
            if np.linalg.norm(step) < 1e-7:
                break
        report.append(dict(minute=minute, views=len(views), correction_camera_axis_angle_deg=np.rad2deg(delta).tolist(),
                           heldout_before_rms_px=float(np.sqrt(np.mean(residual(np.zeros(3), heldout)**2)*2)),
                           heldout_after_rms_px=float(np.sqrt(np.mean(residual(delta, heldout)**2)*2))))
    destination = root / 'logs/ac_evo_seated_20261015/alignment-independent-front-investigation.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
