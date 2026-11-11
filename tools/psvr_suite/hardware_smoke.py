"""Real-hardware smoke test: REQUIRES a real rig, starts VR/LED/camera.

B11-00: this is NOT an offline test. It starts the real VR runtime via
``RuntimeController.start_vr``, binds the real tracking worker and toggles
optical publication, then queries driver tracking. Run only with the
headset/camera connected; never in CI or headless suites.

Exit codes: 0 when all real-hardware checks pass, 2 otherwise (checks
failed or any error, see ``error`` field in the JSON output).

B11-09: the preferences file must exist (no silent defaults before
touching real hardware) and the ``--out`` parent directory must exist
(clear error instead of a raw FileNotFoundError at write time). Both are
validated before starting anything.
"""
import argparse
import json
from pathlib import Path
import time

from .settings import load_preferences
from .runtime import RuntimeController
from .tracking import TrackingWorker


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2])
    a=p.parse_args(argv)
    # B11-09: fail fast prima di toccare hardware reale. Niente default
    # silenziosi: il file prefs deve esistere. Niente FileNotFoundError
    # grezzo a fine run: la parent di --out deve esistere da subito.
    prefs_path=a.root/'configurations/psvr_suite_user.json'
    if not prefs_path.is_file():
        raise ValueError(
            'preferences file not found: %s (create it via the suite '
            'first; refusing silent defaults before touching real '
            'hardware)' % prefs_path)
    if not a.out.parent.is_dir():
        raise ValueError('output parent must exist: %s' % a.out.parent)
    prefs=load_preferences(prefs_path)
    runtime=RuntimeController(a.root)
    worker=TrackingWorker(a.root,lambda event:None)
    result={'schema':'psvr.suite.real_static_smoke','lens_acceptance_verified':False,'states':[]}
    try:
        if not runtime.start_vr(prefs):
            raise RuntimeError('VR startup did not complete')
        worker.start(prefs)
        deadline=time.perf_counter()+30
        while time.perf_counter()<deadline:
            state=worker.status()
            if state.get('imu_bound') and state.get('valid_frames',0)>30:
                break
            time.sleep(.2)
        def observe(label,seconds):
            deadline=time.perf_counter()+seconds
            observations=[]
            while time.perf_counter()<deadline:
                observations.append(runtime.status(query_driver=True).get('driver_tracking',{}))
                time.sleep(.1)
            result['states'].append({'phase':label,'observations':observations})
        observe('optical_fix',2)
        # A real producer is disabled: invalid termination enters fallback.
        # The driver's positionEnabled remains TRUE and orientation remains live.
        paused=dict(prefs);paused['tracking_mode']='3dof'
        worker.update_settings(paused)
        time.sleep(.4)
        observe('optical_publication_paused',2)
        worker.update_settings(prefs)
        time.sleep(.1)
        observe('optical_fix_recovered',3)
        groups={s['phase']:s['observations'] for s in result['states']}
        normal=groups['optical_fix'];lost=groups['optical_publication_paused'];recovered=groups['optical_fix_recovered']
        result['checks']={
            'real_6dof_before':any(s.get('mode')=='6dof' and s.get('position_valid') for s in normal),
            'fallback_valid_without_optical_fix':bool(lost) and all(s.get('mode')=='3dof_fallback' and
                s.get('pose_valid') and s.get('orientation_valid') and s.get('position_valid') is False for s in lost),
            'returned_to_6dof':any(s.get('mode')=='6dof' and s.get('position_valid') for s in recovered),
            'pose_valid_through_recovery':bool(recovered) and all(s.get('pose_valid') for s in recovered)}
        result['ok']=all(result['checks'].values())
        result['tracking_log']=worker.status().get('log')
    except Exception as exc:
        result.update(ok=False,error=str(exc))
    finally:
        result['worker_stopped']=worker.stop()
        runtime.close()
        with a.out.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,indent=2,allow_nan=False)
    print(json.dumps(result,indent=2))
    return 0 if result.get('ok') else 2


if __name__=='__main__':
    raise SystemExit(main())
