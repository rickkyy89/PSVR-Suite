"""Bounded real static capture using the accepted optical/IMU implementations.

Never starts SteamVR. Standalone modes never send poses and their local replay
packets are NOT driver-v2. Explicit driver mode uses genuine bound IMU metadata.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import threading
import time

import numpy as np

from .blue_leds import BlueLedConfig, BlueLedDetector
from .calibration import load_intrinsics
from .camera_source import open_camera_source
from .geometry import rodrigues
from .led_pose import FrontLedTracker, load_led_rig
from .pose_uncertainty import estimate_pose_covariance


def _owners():
    if not hasattr(subprocess, 'CREATE_NO_WINDOW'):
        return []
    result = subprocess.run(['powershell', '-NoProfile', '-Command',
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(vrserver|vrcompositor|psvr_diag)\\.exe$' } | Select-Object Name,ProcessId | ConvertTo-Json -Compress"],
        text=True, capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('cannot verify sensor ownership: ' + result.stderr)
    values = json.loads(result.stdout) if result.stdout.strip() else []
    return values if isinstance(values, list) else [values]


def collect_runtime_poses(path,stop,executable,dll):
    """Existing read-only Utility probe, no USB/presence/recenter/display requests."""
    with Path(path).open('x',encoding='utf-8') as stream:
        while not stop.is_set():
            process=subprocess.Popen([str(Path(executable).resolve()),'--openvr-dll',dll,'--duration-ms','10000'],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            deadline=threading.Timer(15,lambda p=process:p.terminate() if p.poll() is None else None)
            deadline.daemon=True
            deadline.start()
            for line in process.stdout:
                try:
                    row=json.loads(line)
                except json.JSONDecodeError:
                    continue
                # elapsed_ms is local to each probe invocation. Record receipt
                # times as such, not as a fabricated sensor acquisition stamp.
                row['received_monotonic_s']=time.perf_counter()
                row['received_utc_ns']=time.time_ns()
                stream.write(json.dumps(row,allow_nan=False)+'\n')
            stream.flush()
            process.wait(timeout=3)
            deadline.cancel()
            process.stdout.close()
            if process.returncode not in (0,4):
                stream.write(json.dumps(dict(event='collector_error',exit_code=process.returncode))+'\n')
                break


def capture_trial(args):
    out = Path(args.output_dir)
    if not out.parent.is_dir():
        raise ValueError('output parent must already exist')
    out.mkdir(exist_ok=False)
    preset_path = Path(args.preset)
    preset = json.loads(preset_path.read_text(encoding='utf-8'))
    intrinsic_path = Path(preset['intrinsics'])
    intrinsics = load_intrinsics(intrinsic_path, expected_image_size=tuple(preset['image_size']))
    detector = BlueLedDetector(BlueLedConfig(**preset['detector']))
    front_rig, front_points = load_led_rig(args.rig)
    if args.mode in ('diagnostic','driver'):
        owners = _owners()
        if args.mode == 'diagnostic' and owners:
            raise RuntimeError('SteamVR/diagnostic already running; diagnostic sensor ownership refused')
        if args.mode == 'driver' and (not any(o['Name'].lower()=='vrserver.exe' for o in owners)
                                    or any(o['Name'].lower()=='psvr_diag.exe' for o in owners)):
            raise RuntimeError('driver mode requires vrserver and no diagnostic sensor owner')
        from .imu_led_pose import ImuLedTracker, load_rotation_rig
        rig, points, normals = load_rotation_rig(preset['rotation_rig'])
        tracker = ImuLedTracker(intrinsics, points, normals, rig['labels'])
    else:
        rig, points = front_rig, front_points
        tracker = FrontLedTracker(intrinsics, points)
    source = cap = sender = pipeline = None
    runtime_stop=threading.Event()
    runtime_thread=None
    live_phase = None
    variants = json.loads(Path(args.variants_config).read_text(encoding='utf-8'))
    sequence = ('one_euro_2.5_8','initial_Q3_camera','optimized_driver_hold')
    started = time.perf_counter()
    summary = dict(schema='psvr.camera.real_static_trial', mode=args.mode, requested_seconds=args.seconds,
        repeats=args.repeats, udp_enabled=args.mode=='driver' and args.publish_position,
        steamvr_started=False, hardware_accepted=False,
        stationary_declaration='user states headset stationary; no measured position ground truth',
        camera_preset=preset, run_id=out.name, trials=[], started_utc=datetime.now(timezone.utc).isoformat(),
        hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
                (preset_path, intrinsic_path, Path(args.rig), Path(preset['rotation_rig']))})
    try:
        if args.mode == 'diagnostic':
            from .imu_source import DiagnosticImuSource
            source = DiagnosticImuSource(args.imu_diag, args.imu_config)
        elif args.mode == 'driver':
            from .driver_imu_source import DriverImuSource
            source = DriverImuSource()
            if args.publish_position:
                from .driver_position import DriverPositionSender
                sender = DriverPositionSender()
        cap, descriptor = open_camera_source(preset['index'], backend=preset['backend'],
            width=preset['image_size'][0], height=preset['image_size'][1], fps=preset['requested_fps'],
            read_timeout_s=.5, max_consecutive_failures=10)
        cap.set_manual_controls(preset['exposure'], preset['gain'])
        if args.capture_runtime_poses:
            runtime_thread=threading.Thread(target=collect_runtime_poses,
                args=(out/'runtime_poses.jsonl',runtime_stop,args.runtime_probe,args.openvr_dll),daemon=True)
            runtime_thread.start()
        summary['negotiated'] = {k:getattr(descriptor,k,None) for k in ('width','height','fps','backend')}
        for trial in range(args.repeats):
            path = out / ('trial_%02d.jsonl' % (trial+1))
            trial_started = time.perf_counter()
            counts, reasons, imu_states = Counter(), Counter(), Counter()
            captured = valid = failures = repeated = 0
            times, image_ranges = [], []
            previous_hash = None
            best_candidates = -1
            with path.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(event='session', intrinsics=str(intrinsic_path), rig=rig,
                    image_size=preset['image_size'], udp_enabled=sender is not None, diagnostic_only=args.mode!='driver',
                    capture_mode=args.mode, frame=('camera_xyz' if args.mode=='optical' else
                        'psvr_imu_unrecentered' if args.mode=='driver' else 'diagnostic_imu_absolute'),
                    stationary_declaration=summary['stationary_declaration'], input_time_policy='original camera host delivery timestamps',
                    hardware_accepted=False, position_config={}, preset=preset, run_id=out.name))+'\n')
                while time.perf_counter()-trial_started < args.seconds:
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        failures += 1
                        stream.write(json.dumps(dict(event='read_failure', monotonic_s=time.perf_counter(), error=getattr(cap,'error',None)))+'\n')
                        if getattr(cap,'error',None) or failures >= 10:
                            raise RuntimeError('camera read failure: '+str(getattr(cap,'error',None)))
                        continue
                    stamp = cap.last_frame_monotonic
                    sample = None if source is None else source.at(stamp)
                    if source is not None:
                        tracker.set_imu(sample)
                    roi, ratio = tracker.search_region(*preset['image_size'], stamp)
                    detector.config = BlueLedConfig(**{**preset['detector'],
                        'min_area':preset['detector']['min_area'] if roi is None else
                            max(3,int(preset['detector']['min_area']*min(1.,ratio**2)*.5))})
                    if roi is None:
                        candidates = detector.detect(frame)
                    else:
                        x0,y0,x1,y1 = roi
                        candidates = detector.detect(frame[y0:y1,x0:x1])
                        for c in candidates:
                            c['center_px'][0] += x0; c['center_px'][1] += y0
                            c['bbox_px'][0] += x0; c['bbox_px'][1] += y0
                    pose = tracker.update(candidates, stamp)
                    alignment = np.eye(3) if source is None else tracker.alignment
                    context = source is None or (sample is not None and sample.get('valid') is True
                        and sample.get('monotonic_s') is not None and 0 <= stamp-sample['monotonic_s'] <= .15
                        and alignment is not None)
                    position = None
                    if pose.valid and context:
                        position = (alignment.T @ pose.tvec.reshape(3)).tolist()
                    covariance, quality = estimate_pose_covariance(pose, intrinsics, points, rig['labels'], alignment,
                        guided=pose.reason in ('imu_guided_pattern','imu_guided_front_reacquired'))
                    # Local identity only for the estimators. This dictionary is
                    # NOT a psvr.position v2 and is never transmitted to a driver.
                    internal = dict(session_id='local-diagnostic:'+out.name,
                        recenter_epoch=0 if sample is None else sample.get('recenter_count',0),
                        valid=position is not None, state='tracking' if position is not None else 'lost',
                        position_m=position)
                    if args.mode=='driver':
                        from .driver_position import build_driver_position_packet
                        binding = sample if sample is not None else source.latest()
                        seq = captured if sender is None else max(captured,(sender.last_packet or {}).get('seq',-1)+1)
                        internal = build_driver_position_packet(pose if sample is not None else None,
                            binding, alignment, seq, now=stamp)
                        context = bool(internal is not None and context and sample is not None
                                       and sample.get('connected',True) is True)
                    can_predict = bool(context and (pose.valid or pose.reason.startswith('Pattern parziale:')
                        or pose.reason.startswith('Pattern non associato:')))
                    frame_hash = hashlib.blake2b(frame.tobytes(),digest_size=8).hexdigest()
                    repeated += int(frame_hash==previous_hash)
                    previous_hash = frame_hash
                    image_range = [int(frame.min()),int(frame.max()),float(frame.mean())]
                    captured += 1; valid += int(position is not None)
                    times.append(stamp); image_ranges.append(image_range)
                    counts[len(candidates)] += 1; reasons[pose.reason] += 1
                    if sample is not None:
                        imu_states[sample.get('state','unknown')] += 1
                    row = dict(event='pose',monotonic_s=stamp,**pose.to_dict(),
                        candidates=candidates,candidate_count=len(candidates),search_roi=roi,
                        detector=vars(detector.config),camera_controls=cap.controls_status(),
                        frame_hash=frame_hash,frame_intensity=image_range,
                        diagnostic_only=args.mode!='driver',imu=sample,
                        estimator_input=dict(packet=internal,alignment=None if alignment is None else alignment.tolist(),
                            covariance=None if covariance is None else covariance.tolist(),quality=quality,
                            prediction_allowed=can_predict,context_valid=bool(context)))
                    if sender is not None:
                        from .position_pipeline import PositionPipeline
                        phase = sequence[min(2,int((time.perf_counter()-trial_started)/args.live_phase_seconds))]
                        if phase != live_phase:
                            cfg = variants[phase]
                            pipeline = PositionPipeline('one_euro' if cfg.get('one_euro') is not None else 'kalman',
                                one_euro=cfg.get('one_euro'),kalman=cfg.get('kalman'),
                                uncertainty=cfg.get('uncertainty'),prediction_output=cfg.get('prediction_output','camera'))
                            live_phase = phase
                            stream.write(json.dumps(dict(event='position_reset',reason='filter_switch',
                                variant=phase,monotonic_s=stamp))+'\n')
                        outgoing = pipeline.filter_packet(internal,alignment,stamp,covariance=covariance,
                            quality=quality,prediction_allowed=can_predict,context_valid=bool(context))
                        if outgoing is not None:
                            sender.send(outgoing)
                        else:
                            sender.invalidate()
                        row.update(live_variant=phase,position_output=outgoing,position_filter=pipeline.diagnostics())
                    stream.write(json.dumps(row, allow_nan=False)+'\n')
                    if len(candidates)>best_candidates and cap.controls_status() is not None:
                        best_candidates=len(candidates)
                        detector.cv2.imwrite(str(out/('snapshot_%02d.png'%(trial+1))),detector.annotate(frame,candidates))
                    if captured % 75==0:
                        stream.flush()
                    if args.mode=='diagnostic' and source.process.poll() is not None:
                        raise RuntimeError('diagnostic IMU exited: '+str(source.process.returncode))
                dt = np.diff(times)
                result=dict(log=str(path),captured=captured,valid_positions=valid,read_failures=failures,
                    duration_s=time.perf_counter()-trial_started,first_frame_s=times[0] if times else None,
                    last_frame_s=times[-1] if times else None,
                    delivered_fps=(len(times)-1)/(times[-1]-times[0]) if len(times)>1 else None,
                    dt_median_ms=float(np.median(dt)*1000) if len(dt) else None,
                    dt_p99_ms=float(np.percentile(dt,99)*1000) if len(dt) else None,
                    frame_hash_repeats=repeated,candidate_counts=dict(counts),pose_reasons=dict(reasons),
                    imu_states=dict(imu_states),controls=cap.controls_status())
                stream.write(json.dumps(dict(event='end',**result))+'\n')
                summary['trials'].append(result)
                print(json.dumps(result,indent=2),flush=True)
        summary['ok']=True
    except Exception as exc:
        summary.update(ok=False,error=str(exc))
    finally:
        runtime_stop.set()
        if sender is not None:
            sender.close()
        if cap is not None:
            cap.release()
            summary['camera_release_abandoned']=getattr(cap,'_abandoned',False)
        if source is not None:
            source.close()
            if args.mode=='diagnostic':
                summary['imu_process_exit_code']=source.process.returncode
        if runtime_thread is not None:
            runtime_thread.join(timeout=16)
            summary['runtime_collector_joined']=not runtime_thread.is_alive()
        summary['duration_s']=time.perf_counter()-started
        summary['ended_utc']=datetime.now(timezone.utc).isoformat()
        with (out/'capture_summary.json').open('x',encoding='utf-8') as stream:
            json.dump(summary,stream,indent=2,allow_nan=False)
    print(json.dumps(summary,indent=2),flush=True)
    return 0 if summary.get('ok') else 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--mode',choices=('optical','diagnostic','driver'),default='diagnostic')
    parser.add_argument('--publish-position',action='store_true')
    parser.add_argument('--live-phase-seconds',type=float,default=30.)
    parser.add_argument('--variants-config',default='configurations/kalman_comparison_20261013.json')
    parser.add_argument('--capture-runtime-poses',action='store_true')
    parser.add_argument('--runtime-probe',default='out/runtime_probe/psvr_runtime_probe.exe')
    parser.add_argument('--openvr-dll',default='C:/Program Files (x86)/Steam/steamapps/common/SteamVR/bin/win64/openvr_api.dll')
    parser.add_argument('--seconds',type=float,default=60.)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--preset',default='configurations/pseye_led_capture_reference.json')
    parser.add_argument('--rig',default='configurations/psvr_front_leds_refined_20261011.json')
    parser.add_argument('--imu-diag',default='build/vs2022/RelWithDebInfo/psvr_diag.exe')
    parser.add_argument('--imu-config',default='out/diag/config.json')
    args=parser.parse_args(argv)
    if not math.isfinite(args.seconds) or args.seconds<=0 or args.repeats<1:
        parser.error('duration/repeats must be positive')
    if args.publish_position and args.mode!='driver':
        parser.error('position transmission requires the real driver mode')
    if args.capture_runtime_poses and args.mode!='driver':
        parser.error('runtime poses require the real driver mode')
    if not math.isfinite(args.live_phase_seconds) or args.live_phase_seconds<=0:
        parser.error('phase duration must be positive')
    return capture_trial(args)


if __name__=='__main__':
    raise SystemExit(main())
