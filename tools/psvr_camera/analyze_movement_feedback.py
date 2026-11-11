"""Read-only summary of actual live filter phases, bounded to a log snapshot."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np


def summarize(path,snapshot_bytes=None):
    groups={}
    segments=[]
    current=None
    with Path(path).open('rb') as stream:
        size=Path(path).stat().st_size
        limit=size if snapshot_bytes is None else snapshot_bytes
        if limit<0 or limit>size:
            raise ValueError('snapshot boundary outside current file')
        while stream.tell()<limit:
            line=stream.readline()
            if stream.tell()>limit or not line.endswith(b'\n'):
                break
            row=json.loads(line)
            if row.get('event')=='position_reset':
                current=None
                for group in groups.values():
                    group['previous']=None;group['current_gap']=None
            if row.get('event')!='pose':
                continue
            diag=row.get('position_filter') or {}
            mode=diag.get('type','unknown')
            imu=row.get('imu') or {}
            sid=imu.get('session_id')
            key=(mode,sid,imu.get('recenter_epoch'))
            if key!=current:
                for group in groups.values():
                    group['previous']=None;group['current_gap']=None
                segments.append(dict(filter=mode,session_id=sid,start_s=row['monotonic_s'],end_s=row['monotonic_s'],
                    frames=0,optical_valid=0,imu_valid=0,published_valid=0))
                current=key
            seg=segments[-1];seg['end_s']=row['monotonic_s'];seg['frames']+=1
            optical=bool(row.get('valid'))
            imu_valid=imu.get('valid') is True
            outgoing=row.get('position_output') or {}
            published=outgoing.get('valid') is True
            seg['optical_valid']+=optical;seg['imu_valid']+=imu_valid;seg['published_valid']+=published
            # Startup/restarts/calibration and no optical alignment are reported
            # separately, not treated as comparable motion tracking observations.
            if not imu_valid or not row.get('estimator_input',{}).get('alignment'):
                continue
            g=groups.setdefault(mode,dict(frames=0,optical_valid=0,published_valid=0,
                optical_valid_but_not_published=0,predicted=0,states=Counter(),quality=Counter(),
                gating=Counter(),led_counts=Counter(),led_when_withheld=Counter(),
                points=[],timestamps=[],steps=[],speeds=[],gaps=[],current_gap=None,
                previous=None,last_good=None))
            g['frames']+=1;g['optical_valid']+=optical;g['published_valid']+=published
            g['predicted']+=diag.get('tracking_state')=='predicting'
            g['states'][diag.get('tracking_state','legacy')]+=1
            quality=diag.get('quality') or {}
            g['quality'][quality.get('reason','unknown')]+=1
            if diag.get('rejection_reason'):
                g['gating'][diag['rejection_reason']]+=1
            n=len(row.get('led_labels') or [])
            if optical:
                g['led_counts'][n]+=1
            if optical and not published:
                g['optical_valid_but_not_published']+=1
                g['led_when_withheld'][n]+=1
            now=row['monotonic_s']
            if not published:
                if g['current_gap'] is None:
                    g['current_gap']=now
                g['previous']=None
                continue
            if g['current_gap'] is not None:
                g['gaps'].append(now-g['current_gap']);g['current_gap']=None
            point=np.asarray(outgoing['position_m'],float)
            if g['previous'] is not None:
                dt=now-g['previous'][0]
                if dt>0:
                    step=float(np.linalg.norm(point-g['previous'][1]))
                    g['steps'].append(step);g['speeds'].append(step/dt)
            g['previous']=(now,point)
    result={}
    for name,g in groups.items():
        result[name]={k:v for k,v in g.items() if k not in
            ('points','timestamps','steps','speeds','gaps','current_gap','previous','last_good')}
        result[name].update(published_valid_fraction=g['published_valid']/g['frames'],
            optical_valid_fraction=g['optical_valid']/g['frames'],
            optical_valid_withheld_fraction=g['optical_valid_but_not_published']/g['optical_valid'] if g['optical_valid'] else None,
            step_p99_mm=float(np.percentile(g['steps'],99)*1000) if g['steps'] else None,
            step_max_mm=max(g['steps'],default=0)*1000,
            published_speed_max_m_s=max(g['speeds'],default=None),
            completed_invalid_gaps=len(g['gaps']),invalid_gap_max_s=max(g['gaps'],default=None))
    return dict(schema='psvr.live_movement_feedback_summary',source=str(path),snapshot_bytes=limit,
        snapshot_utc=datetime.now(timezone.utc).isoformat(),ground_truth=None,hardware_accepted=False,
        interpretation='different live movements/intervals; not matched A/B, wire validity is not driver grace validity',
        segments=segments,eligible_motion_groups=result)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('log');p.add_argument('--out',required=True)
    p.add_argument('--snapshot-bytes',type=int)
    a=p.parse_args(argv)
    report=summarize(a.log,a.snapshot_bytes)
    with Path(a.out).open('x',encoding='utf-8') as stream:
        json.dump(report,stream,indent=2,allow_nan=False)
    print(json.dumps(report['eligible_motion_groups'],indent=2))


if __name__=='__main__':
    main()
