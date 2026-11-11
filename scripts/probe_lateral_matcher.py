"""Explain rejection stages in the existing matcher without changing its policy."""
from collections import Counter
from itertools import combinations
import json
from pathlib import Path

import numpy as np

from tools.psvr_camera.calibration import load_intrinsics
from tools.psvr_camera.coherent_imu_led_pose import CoherentImuLedTracker


def probe(tracker, centers, now):
    rotation = tracker.matching_rotation(tracker.rotation(now))
    forecast = tracker.translation+tracker.velocity*min(.1, max(0., now-tracker.last_match))
    visible, projected = tracker.predicted(rotation, forecast, relaxed=True)
    width = float(np.max(np.ptp(projected[visible], axis=0))) if len(visible) else 0
    gate = max(20., min(80., width*.4))
    limit = max(3., tracker.front.reprojection_limit(float(np.max(np.ptp(centers, axis=0)))))
    maximum_jump = min(.25, .015+2*max(0., now-tracker.last_match))
    seeds = []
    for i in visible:
        distances = np.linalg.norm(centers-projected[i], axis=1)
        seeds.extend((int(i), int(j)) for j in np.argsort(distances)[:2] if distances[j] <= gate)
    counts = Counter()
    solutions = {}
    rvec = tracker.cv2.Rodrigues(rotation)[0]
    tracker._partial_match_at = now
    try:
        for first, second in combinations(seeds, 2):
            if first[0] == second[0] or first[1] == second[1]:
                continue
            counts['seed_pairs'] += 1
            ids = np.array([first[0], second[0]])
            image = centers[[first[1], second[1]]]
            translation = tracker.solve_translation(rotation, ids, image)
            if translation is None:
                counts['seed_invalid_geometry'] += 1
                continue
            if np.linalg.norm(translation-tracker.translation) > maximum_jump:
                counts['seed_motion_gate'] += 1
                continue
            for _ in range(2):
                projected = tracker.cv2.projectPoints(tracker.points, rvec, translation, tracker.k, tracker.dist)[0].reshape(-1, 2)
                edges = sorted((float(np.linalg.norm(projected[i]-center)), int(i), j)
                               for i in visible for j, center in enumerate(centers))
                assignment, used_ids, used_candidates = [], set(), set()
                for distance, i, j in edges:
                    if distance > limit*2:
                        break
                    if i not in used_ids and j not in used_candidates:
                        assignment.append((i, j))
                        used_ids.add(i)
                        used_candidates.add(j)
                if len(assignment) < 2:
                    counts['too_few_associations'] += 1
                    break
                ids = np.array([a[0] for a in assignment])
                image = centers[[a[1] for a in assignment]]
                fitted = tracker.solve_translation(rotation, ids, image)
                if fitted is None:
                    counts['refit_invalid_geometry'] += 1
                    break
                translation = fitted
            else:
                if np.linalg.norm(translation-tracker.translation) > maximum_jump:
                    counts['final_motion_gate'] += 1
                    continue
                projected = tracker.cv2.projectPoints(tracker.points[ids], rvec, translation, tracker.k, tracker.dist)[0].reshape(-1, 2)
                rms = float(np.sqrt(np.mean(np.sum((projected-image)**2, axis=1))))
                if rms > limit:
                    counts['reprojection_gate'] += 1
                    continue
                cost = rms+float(np.linalg.norm(translation-forecast))/.01*.25
                key = tuple(sorted(assignment))
                if key not in solutions or cost < solutions[key][1]:
                    solutions[key] = (len(ids), cost, translation)
                counts['accepted_hypotheses'] += 1
    finally:
        tracker._partial_match_at = None
    ordered = sorted(solutions.values(), key=lambda s: (-s[0], s[1]))
    counts['unique_solutions'] = len(ordered)
    ambiguous = (len(ordered) > 1 and ordered[0][0] == ordered[1][0]
                 and ordered[1][1]-ordered[0][1] < .2
                 and np.linalg.norm(ordered[1][2]-ordered[0][2]) > .03)
    return dict(stages=counts, visible_labels=[tracker.labels[i] for i in visible], gate_px=gate,
                reprojection_limit_px=limit, maximum_jump_mm=maximum_jump*1000,
                ambiguity=bool(ambiguous), has_solution=bool(ordered))


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / 'logs/suite_tracking_20261015T195257117578Z_e9208322.jsonl'
    counts = Counter()
    failures = []
    valid_mismatches = 0
    with source.open('rb') as stream:
        session = json.loads(next(stream))
        rig = session['imu_rig_full']
        tracker = CoherentImuLedTracker(load_intrinsics(root / session['intrinsicsPath']),
                                       np.asarray(rig['object_points_m']), np.asarray(rig['outward_normals']), rig['labels'])
        first = None
        for line in stream:
            row = json.loads(line)
            if row.get('event') != 'pose':
                continue
            now = row['monotonic_s']
            if first is None:
                first = now
            tracker.set_imu(row['imu'])
            pose = tracker.update(row['candidates'], now)
            valid_mismatches += bool(pose.valid) != bool(row['valid'])
            if not pose.reason.startswith(('Pattern non associato', 'Pattern ambiguo')):
                continue
            result = probe(tracker, np.asarray([c['center_px'] for c in row['candidates']]), now)
            counts.update(result['stages'])
            counts['failed_frames'] += 1
            counts['frames_with_any_solution'] += result['has_solution']
            counts['ambiguous_frames'] += result['ambiguity']
            result.update(elapsed_s=now-first, reason=pose.reason, candidates=len(row['candidates']))
            failures.append(result)
    report = dict(validity_mismatches=valid_mismatches, aggregate=counts, failed_frames=failures,
                  interpretation='Stage counts are hypotheses, not frame counts; normal seed pruning is not proof of root cause.')
    (root / 'logs/ac_evo_coherent_v2_20261015/lateral-matcher-rejection-stages.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(dict(validity_mismatches=valid_mismatches, aggregate=counts, examples=failures[:3])))


if __name__ == '__main__':
    main()
