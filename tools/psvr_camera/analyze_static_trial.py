"""Static measurements from real captures: no invented position ground truth."""
import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np

from .imu_led_pose import rotation_from_quaternion
from .replay_position_log import load_inputs, replay, metrics, innovation_statistics


def depth_spectrum(times, depth):
    if len(times)<20 or times[-1]-times[0]<2:
        return None
    rate=min(75.,1./np.median(np.diff(times)))
    uniform=np.arange(times[0],times[-1],1/rate)
    signal=np.interp(uniform,times,depth)
    fit=np.polyfit(uniform-uniform[0],signal,1)
    signal=signal-np.polyval(fit,uniform-uniform[0])
    # Parseval-correct band RMS; interpolation is ONLY for spectral diagnostics.
    fft=np.fft.rfft(signal)
    power=np.abs(fft)**2/len(signal)**2
    if len(power)>2:
        power[1:-1 if len(signal)%2==0 else None]*=2
    frequency=np.fft.rfftfreq(len(signal),1/rate)
    return dict(metric_only_resampling_hz=rate,
        rms_below_0_3_hz_m=float(np.sqrt(power[(frequency>0)&(frequency<.3)].sum())),
        rms_0_3_to_3_hz_m=float(np.sqrt(power[(frequency>=.3)&(frequency<3)].sum())),
        rms_above_3_hz_m=float(np.sqrt(power[frequency>=3].sum())))


def position_statistics(rows):
    valid=[r for r in rows if r.get('packet') is not None and r['packet']['valid']]
    result=metrics(rows)
    result.update(jitter_interpretation='real static capture declared stationary by user; no measured position ground truth',
        invalid_frames=len(rows)-len(valid),nis=innovation_statistics(rows),
        quiet_active_fraction=float(np.mean([bool(r['diagnostics'].get('quiet_active')) for r in rows])) if rows else None)
    if len(valid)<2:
        return result
    times=np.asarray([r['monotonic_s'] for r in valid])
    xyz=np.asarray([r['packet']['position_m'] for r in valid])
    depth=np.asarray([np.dot(r['packet']['position_m'],r['optical_axis_world_m']) for r in valid])
    result.update(xyz_peak_to_peak_m=np.ptp(xyz,axis=0).tolist(),depth_peak_to_peak_m=float(np.ptp(depth)),
        mean_position_m=xyz.mean(axis=0).tolist(),depth_spectrum=depth_spectrum(times,depth))
    return result


def analyze(path,variants,warmup_s=10.):
    path=Path(path)
    rows=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    poses=[r for r in rows if r.get('event')=='pose']
    if not poses:
        raise ValueError('no camera observations')
    start=poses[0]['monotonic_s']+warmup_s
    observations=[r for r in poses if r['monotonic_s']>=start]
    inputs,provenance=load_inputs(path)
    output={}
    data_by_variant={}
    for name,cfg in variants.items():
        mode='one_euro' if cfg.get('one_euro') is not None else 'kalman'
        filtered=replay(inputs,modes=(mode,),kalman=cfg.get('kalman',{}),one_euro=cfg.get('one_euro'),
            uncertainty=cfg.get('uncertainty',{}),prediction_output=cfg.get('prediction_output','camera'))[mode]
        selected=[r for r in filtered if r['monotonic_s']>=start]
        data_by_variant[name]=selected
        output[name]=position_statistics(selected)
    raw=[]
    for row in observations:
        inp=row['estimator_input']
        a=inp.get('alignment')
        raw.append(dict(monotonic_s=row['monotonic_s'],packet=inp['packet'],diagnostics={},
            optical_axis_world_m=None if a is None else (np.asarray(a).T@[0.,0.,1.]).tolist()))
    output['raw']=position_statistics(raw)
    common=set(r['monotonic_s'] for r in raw if r['packet'] is not None and r['packet']['valid'])
    for filtered in data_by_variant.values():
        common.intersection_update(r['monotonic_s'] for r in filtered if r['packet'] is not None and r['packet']['valid'])
    common_stats={name:position_statistics([r for r in data if r['monotonic_s'] in common])
                  for name,data in {'raw':raw,**data_by_variant}.items()}
    imu=[r for r in observations if r.get('imu') and r['imu'].get('valid') and r['imu'].get('quaternion_sensor_wxyz')]
    imu_stats=dict(states=dict(Counter((r.get('imu') or {}).get('state','absent') for r in observations)))
    if imu:
        import cv2
        ref=rotation_from_quaternion(imu[0]['imu']['quaternion_sensor_wxyz'])
        angles=np.asarray([cv2.Rodrigues(ref.T@rotation_from_quaternion(r['imu']['quaternion_sensor_wxyz']))[0].reshape(3)
                           for r in imu])*180/np.pi
        t=np.asarray([r['monotonic_s'] for r in imu]);design=np.c_[t-t.mean(),np.ones(len(t))]
        trend=np.linalg.lstsq(design,angles,rcond=None)[0]
        imu_stats.update(relative_rotation_std_deg=angles.std(axis=0).tolist(),
            relative_rotation_peak_to_peak_deg=np.ptp(angles,axis=0).tolist(),
            axis_drift_deg_min=(trend[0]*60).tolist(),
            gyro_present=any('corrected_gyro_rad_s' in r['imu'] for r in imu),
            interpolated_fraction=float(np.mean([bool(r['imu'].get('interpolated')) for r in imu])))
    return dict(schema='psvr.real_static_analysis',log=str(path),input_provenance=provenance,
        warmup_s=warmup_s,selected_frames=len(observations),start_s=start,
        end_s=observations[-1]['monotonic_s'] if observations else None,
        stationary_evidence='user declaration and sensor/image consistency, not motion capture',
        ground_truth_position=None,hardware_accepted=False,
        note='filter comparisons are replays; actual OpenVR poses, when collected, are reported separately',
        variants=output,common_valid_frames=len(common),common_valid_statistics=common_stats,
        led_counts=dict(Counter(len(r.get('led_labels') or []) for r in observations)),
        imu=imu_stats)


def analyze_runtime(camera_log,poses_path=None,telemetry_path=None):
    camera=[json.loads(line) for line in Path(camera_log).read_text(encoding='utf-8').splitlines()]
    camera=[r for r in camera if r.get('event')=='pose' and r.get('live_variant')
            and r.get('estimator_input',{}).get('packet') is not None]
    if not camera:
        raise ValueError('no genuine bound live driver phases to correlate')
    times=np.asarray([r['monotonic_s'] for r in camera])
    utc=np.asarray([r['estimator_input']['packet']['host_time_ns']/1e9 for r in camera])
    starts={}
    for r in camera:
        starts.setdefault(r['live_variant'],r['monotonic_s'])
    result=dict(camera_log=str(camera_log),phase_warmup_s=5.,pose_prediction_seconds=0,
                runtime_pose_sampling_hz=10,hardware_accepted=False)
    if poses_path:
        groups={}
        for line in Path(poses_path).read_text(encoding='utf-8').splitlines():
            r=json.loads(line)
            if r.get('event')!='snapshot':
                continue
            t=r['received_monotonic_s'];idx=np.searchsorted(times,t,side='right')-1
            if idx<0 or t>times[-1]:
                continue
            c=camera[idx];name=c['live_variant']
            if t-starts[name]<5:
                continue
            groups.setdefault(name,[]).append((r,c))
        summary={}
        for name,pairs in groups.items():
            valid=[(r,c) for r,c in pairs if r.get('accepted')]
            s=dict(samples=len(pairs),valid=len(valid),invalid=len(pairs)-len(valid))
            if len(valid)>1:
                points=np.asarray([r['position_m'] for r,c in valid])
                axes=[]
                for r,c in valid:
                    a=np.asarray(c['estimator_input']['alignment'])
                    q=c['imu'].get('world_rotation_wxyz')
                    axes.append(rotation_from_quaternion(q)@(a.T@[0.,0.,1.]))
                depth=np.einsum('ij,ij->i',points,np.asarray(axes))
                s.update(xyz_std_mm=(points.std(axis=0)*1000).tolist(),
                    camera_axis_std_mm=float(depth.std()*1000),camera_axis_ptp_mm=float(np.ptp(depth)*1000))
            summary[name]=s
        result['actual_openvr_poses']=summary
        result['pose_time_alignment']='stdout receipt time, not exact GPU/sensor synchronization'
    if telemetry_path:
        groups={}
        for line in Path(telemetry_path).read_text(encoding='utf-8').splitlines():
            r=json.loads(line)
            if r.get('event')!='sample':
                continue
            t=r['utc_unix_us']/1e6;idx=np.searchsorted(utc,t,side='right')-1
            if idx<0 or t>utc[-1]:
                continue
            c=camera[idx];name=c['live_variant']
            if c['monotonic_s']-starts[name]<5:
                continue
            groups.setdefault(name,[]).append(r)
        result['telemetry_active_phases']={name:dict(samples=len(rows),
            raw_valid=sum(r['raw_pose']['valid'] for r in rows),
            compositor_pose_valid=sum(r['compositor_pose']['valid'] for r in rows),
            fullscreen=sum(bool(r.get('fullscreen')) for r in rows),
            can_render=sum(bool(r.get('can_render_scene')) for r in rows),
            get_last_poses_errors=dict(Counter(r.get('get_last_poses_error') for r in rows)),
            fade_alpha_changes_over_0_1=sum(abs(b['fade_rgba'][3]-a['fade_rgba'][3])>.1 for a,b in zip(rows,rows[1:])),
            grid_alpha_max=max(r.get('grid_alpha',0) for r in rows)) for name,rows in groups.items()}
        result['flash_interpretation']='telemetry transitions only; not physical OLED/lens flash detection'
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('logs',nargs='+')
    parser.add_argument('--variants-config',default='configurations/kalman_comparison_20261013.json')
    parser.add_argument('--warmup-s',type=float,default=10.)
    parser.add_argument('--out',required=True)
    parser.add_argument('--runtime-poses')
    parser.add_argument('--telemetry')
    args=parser.parse_args(argv)
    variants=json.loads(Path(args.variants_config).read_text(encoding='utf-8'))
    result=dict(schema='psvr.real_static_series',variants=variants,
        hardware_accepted=False,logs=[analyze(p,variants,args.warmup_s) for p in args.logs])
    if args.runtime_poses or args.telemetry:
        if len(args.logs)!=1:
            parser.error('runtime correlation requires one camera log')
        result['runtime']=analyze_runtime(args.logs[0],args.runtime_poses,args.telemetry)
    with Path(args.out).open('x',encoding='utf-8') as stream:
        json.dump(result,stream,indent=2,allow_nan=False)
    for trial in result['logs']:
        print(trial['log'])
        for name,stat in trial['variants'].items():
            print(json.dumps(dict(variant=name,valid=stat.get('valid_frames'),
                depth_std_mm=None if stat.get('camera_depth_std_m') is None else stat['camera_depth_std_m']*1000,
                depth_drift_mm_s=None if stat.get('camera_depth_drift_m_s') is None else stat['camera_depth_drift_m_s']*1000,
                depth_ptp_mm=None if stat.get('depth_peak_to_peak_m') is None else stat['depth_peak_to_peak_m']*1000,
                step_max_mm=None if stat.get('step_max_m') is None else stat['step_max_m']*1000,
                quiet=stat.get('quiet_active_fraction'))))
    if 'runtime' in result:
        print(json.dumps(result['runtime'],indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
