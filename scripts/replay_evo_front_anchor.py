"""Offline comparison of the historical front anchor and suite coherent tracker."""
import argparse
from collections import Counter
import json
from pathlib import Path
import time

import numpy as np

from tools.psvr_camera.calibration import load_intrinsics
from tools.psvr_camera.imu_led_pose import ImuLedTracker, MODEL_TO_IMU
from tools.psvr_camera.led_pose import LedPose
from tools.psvr_camera.coherent_imu_led_pose import CoherentImuLedTracker
from tools.psvr_camera.driver_position import build_driver_position_packet
from tools.psvr_camera.position_pipeline import PositionPipeline
from tools.psvr_camera.constellation_led_pose import ConstellationLedTracker
from scripts.lateral_handoff_candidates import GuidedFallbackTracker, AngleHandoffTracker


class FrontAnchoredTracker(ImuLedTracker):
    def update(self, candidates, now):
        imu_rotation = self.rotation(now)
        if self.alignment is None or imu_rotation is None:
            return super().update(candidates, now)
        optical = self.front.update(candidates, now)
        if optical.valid:
            camera_rotation = self.cv2.Rodrigues(optical.rvec)[0]
            expected_rotation = self.alignment @ imu_rotation @ MODEL_TO_IMU
            innovation = self.cv2.Rodrigues(camera_rotation @ expected_rotation.T)[0]
            # Experimental consistency check, not a recalibration of the
            # immutable world basis or of the driver's IMU orientation.
            if np.linalg.norm(innovation) < np.deg2rad(10):
                if self.translation is not None and self.last_match is not None:
                    dt = now-self.last_match
                    if np.linalg.norm(optical.tvec-self.translation) > min(.25, .015+2*max(0, dt)):
                        return LedPose(False, 'front_anchor_motion_conflict')
                    if dt > .001:
                        velocity = (optical.tvec-self.translation)/dt
                        speed = np.linalg.norm(velocity)
                        if speed > 2:
                            velocity *= 2/speed
                        self.velocity = self.velocity*.75+velocity*.25
                self.translation = optical.tvec.copy()
                self.last_match = now
                optical.reason = 'experimental_front_anchor'
                optical.led_labels = self.labels[:5]
                optical.imu_age_s = now-self.imu['monotonic_s']
                return optical
        return super().update(candidates, now)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--end-s', type=float, default=float('inf'))
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--variant', choices=('front-anchor', 'coherent', 'guided-fallback', 'angle-handoff', 'constellation'), default='front-anchor')
    p.add_argument('--source', type=Path)
    p.add_argument('--side-left-rig', type=Path)
    p.add_argument('--minimum-leds', type=int, choices=(2, 3), default=2)
    p.add_argument('--stop-file', type=Path)
    args = p.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = args.source or root / 'logs/suite_tracking_20261015T180512696028Z_0e696411.jsonl'
    first_baseline_valid = None
    last_baseline_valid = None
    cutoff = args.stop_file.stat().st_mtime if args.stop_file else None
    def past_cutoff(row):
        stamp = ((row.get('estimator_input') or {}).get('packet') or {}).get('host_time_ns')
        return cutoff is not None and stamp is not None and stamp/1e9 > cutoff
    with source.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if past_cutoff(row):
                break
            if row.get('event') == 'pose' and row.get('valid'):
                if first_baseline_valid is None:
                    first_baseline_valid = row['monotonic_s']
                last_baseline_valid = row['monotonic_s']
    start_clock = time.monotonic()
    stats = {'baseline': Counter(), 'candidate': Counter()}
    active = {'baseline': Counter(), 'candidate': Counter()}
    active_previous_invalid = {'baseline': False, 'candidate': False}
    reasons = Counter()
    windows = ((796., 797.), (1439., 1442.), (1448., 1452.), (1455., 1456.), (1635., 1637.), (1715., 1717.))
    window_counts = {str(a): {'baseline': Counter(), 'candidate': Counter()} for a, b in windows}
    update_ms = []
    front_reference_errors = []
    steps = {'baseline': [], 'candidate': []}
    previous = {'baseline': None, 'candidate': None}
    previous_row = None
    published_steps = {'baseline': [], 'candidate': []}
    published_previous = {'baseline': None, 'candidate': None}
    large_candidate_steps = []
    worst_gaps = {'baseline': 0., 'candidate': 0.}
    gap_start = {'baseline': None, 'candidate': None}
    tracking_start = None
    with source.open(encoding='utf-8') as stream:
        session = json.loads(next(stream))
        rig = session['imu_rig_full']
        tracker_type = {'coherent': CoherentImuLedTracker, 'front-anchor': FrontAnchoredTracker,
                        'guided-fallback': GuidedFallbackTracker, 'angle-handoff': AngleHandoffTracker,
                        'constellation': ConstellationLedTracker}[args.variant]
        if args.side_left_rig:
            refined = json.loads(args.side_left_rig.read_text(encoding='utf-8'))
            rig['object_points_m'][rig['labels'].index('side_left')] = refined['object_points_m'][refined['labels'].index('side_left')]
        tracker = tracker_type(load_intrinsics(root / session['intrinsicsPath']),
                                       np.asarray(rig['object_points_m']), np.asarray(rig['outward_normals']), rig['labels'])
        if hasattr(tracker, 'allow_two_led'):
            tracker.allow_two_led = args.minimum_leds == 2
        pipeline = PositionPipeline('one_euro', one_euro=session['position_config']['one_euro'])
        first = None
        for line in stream:
            row = json.loads(line)
            if past_cutoff(row):
                break
            if row.get('event') != 'pose':
                continue
            now = row['monotonic_s']
            if first is None:
                first = now
            if now-first > args.end_s:
                break
            tracker.set_imu(row['imu'])
            update_start = time.perf_counter()
            pose = tracker.update(row['candidates'], now)
            update_ms.append((time.perf_counter()-update_start)*1000)
            packet = build_driver_position_packet(pose, row['imu'], tracker.alignment,
                                                  stats['candidate']['frames'], now=now,
                                                  scale=session['position_scale'],
                                                  head_offset_model_m=session['head_offset_model_m'])
            outgoing = pipeline.filter_packet(packet, tracker.alignment, now)
            if (pose.valid and row.get('valid') and len(row.get('led_labels') or []) == 5
                    and set(row['led_labels']) == set(rig['labels'][:5])
                    and (row.get('rms_px') or 0) <= 1.5):
                front_reference_errors.append(float(np.linalg.norm(pose.tvec.reshape(3)-np.asarray(row['position_camera_m']))*1000))
            for mode, published in [('baseline', row.get('position_output')), ('candidate', outgoing)]:
                if not published or not published.get('valid'):
                    published_previous[mode] = None
                else:
                    point = np.asarray(published['position_m'])
                    if published_previous[mode] is not None:
                        published_steps[mode].append(float(np.linalg.norm(point-published_previous[mode])*1000))
                    published_previous[mode] = point
            reasons[pose.reason.split(':')[0]] += 1
            if tracking_start is None and row['valid']:
                tracking_start = now
            for mode, valid, position in [('baseline', row['valid'], row['position_camera_m']),
                                           ('candidate', pose.valid, None if pose.tvec is None else pose.tvec.reshape(3))]:
                stats[mode]['frames'] += 1
                stats[mode]['valid'] += bool(valid)
                for a, b in windows:
                    if a <= now-first <= b:
                        window_counts[str(a)][mode]['frames'] += 1
                        window_counts[str(a)][mode]['invalid'] += not valid
                if first_baseline_valid <= now <= last_baseline_valid:
                    active[mode]['frames'] += 1
                    active[mode]['invalid'] += not valid
                    if not valid and not active_previous_invalid[mode]:
                        active[mode]['invalid_runs'] += 1
                    active_previous_invalid[mode] = not valid
                if tracking_start is not None:
                    stats[mode]['frames_after_initial_acquisition'] += 1
                    stats[mode]['invalid_after_initial_acquisition'] += not valid
                if not valid:
                    if gap_start[mode] is None:
                        gap_start[mode] = now
                        stats[mode]['invalid_runs'] += 1
                    previous[mode] = None
                    continue
                if gap_start[mode] is not None:
                    if tracking_start is not None and gap_start[mode] >= tracking_start:
                        worst_gaps[mode] = max(worst_gaps[mode], now-gap_start[mode])
                    gap_start[mode] = None
                point = np.asarray(position)
                if previous[mode] is not None:
                    step = float(np.linalg.norm(point-previous[mode])*1000)
                    steps[mode].append(step)
                    if mode == 'candidate' and step > 15:
                        large_candidate_steps.append(dict(time_s=now, step_mm=step, reason=pose.reason,
                                                          candidates=row['candidate_count'], labels=pose.led_labels,
                                                          previous=previous_row, position_m=point.tolist(), rms_px=pose.rms_px))
                previous[mode] = point
            previous_row = dict(time_s=now, valid=pose.valid, reason=pose.reason, labels=pose.led_labels,
                                candidates=row['candidate_count'])
    report = dict(experimental_only=(args.variant not in ('coherent', 'constellation')
                                    or bool(args.side_left_rig)
                                    or (args.variant == 'constellation' and args.minimum_leds == 2)), offline_replay=True,
                  minimum_matched_leds=args.minimum_leds, angular_driver_policy_replayed=False,
                  hardware_accepted=False, variant=args.variant, source=str(source), seconds_processed=now-first,
                  wall_seconds=time.monotonic()-start_clock, candidate_reasons=reasons,
                  trusted_recorded_front_disagreement_mm=dict(samples=len(front_reference_errors),
                      p99=float(np.percentile(front_reference_errors, 99)) if front_reference_errors else None,
                      maximum=max(front_reference_errors) if front_reference_errors else None),
                  tracker_update_ms=dict(p50=float(np.percentile(update_ms, 50)),
                                         p95=float(np.percentile(update_ms, 95)),
                                         p99=float(np.percentile(update_ms, 99)), maximum=max(update_ms)),
                  limitations='Same logged detections, not images; changed ROI feedback is not replayed. No ground truth or live acceptance.',
                  modes={}, critical_windows=window_counts,
                  experiment_counts=dict(getattr(tracker, 'experiment_counts', {})),
                  side_left_override=str(args.side_left_rig) if args.side_left_rig else None,
                  largest_candidate_steps=sorted(large_candidate_steps, key=lambda d: d['step_mm'], reverse=True)[:12])
    for mode in stats:
        report['modes'][mode] = dict(stats[mode], common_active_window=dict(active[mode]), adjacent_raw_step_p99_mm=float(np.percentile(steps[mode], 99)),
                                     adjacent_raw_step_max_mm=max(steps[mode]), steps_over_15mm=sum(s > 15 for s in steps[mode]),
                                     published_step_p99_mm=float(np.percentile(published_steps[mode], 99)),
                                     published_step_max_mm=max(published_steps[mode]),
                                     published_steps_over_15mm=sum(s > 15 for s in published_steps[mode]),
                                     max_completed_gap_after_initial_acquisition_s=worst_gaps[mode])
    args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'largest_candidate_steps'}))


if __name__ == '__main__':
    main()
