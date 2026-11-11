"""Show existing flat HMD-relative grid only once real tracking is valid."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import subprocess
import time


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--grid',type=Path,default=Path('logs/flat_optics_grid_20261009.png'))
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--wait-seconds',type=float,default=180)
    p.add_argument('--duration-ms',type=int,default=120000)
    p.add_argument('--probe',type=Path,default=Path('out/runtime_probe/psvr_runtime_probe.exe'))
    p.add_argument('--dll',default='C:/Program Files (x86)/Steam/steamapps/common/SteamVR/bin/win64/openvr_api.dll')
    a=p.parse_args(argv)
    if not 0<a.wait_seconds<=300 or not 100<=a.duration_ms<=120000:
        p.error('bounded waiting/grid duration required')
    command=[str(a.probe.resolve(strict=True)),'--openvr-dll',a.dll]
    grid=str(a.grid.resolve(strict=True))
    if not a.out.parent.is_dir():
        p.error('output parent missing')
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    with a.out.open('x',encoding='utf-8') as stream:
        deadline=time.perf_counter()+a.wait_seconds
        ready=False
        while time.perf_counter()<deadline:
            run=subprocess.run(command+['--duration-ms','100','--compositor-status'],
                capture_output=True,text=True,timeout=10,creationflags=flags)
            events=[]
            for line in run.stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            summary=next((e for e in reversed(events) if isinstance(e,dict) and e.get('event')=='summary'),None)
            if isinstance(summary,dict):
                _compositor=summary.get('compositor',{})
                _fullscreen=_compositor.get('fullscreen') if isinstance(_compositor,dict) else False
                _valid=summary.get('valid_count')
                _valid_ok=isinstance(_valid,(int,float)) and not isinstance(_valid,bool) and _valid>0
                ready=bool(run.returncode==0 and _fullscreen and _valid_ok)
            else:
                ready=False
            stream.write(json.dumps(dict(event='grid_readiness',utc=datetime.now(timezone.utc).isoformat(),
                ready=ready,return_code=run.returncode,valid_count=(summary.get('valid_count') if isinstance(summary,dict) else None)))+'\n')
            stream.flush()
            if ready:
                break
            time.sleep(2)
        if not ready:
            stream.write(json.dumps(dict(event='grid_not_shown',reason='no valid real pose/fullscreen before deadline'))+'\n')
            return 4
        process=subprocess.Popen(command+['--duration-ms',str(a.duration_ms),'--optics-grid',grid],
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,creationflags=flags)
        try:
            try:
                stdout_data,_=process.communicate(timeout=a.duration_ms/1000.0+10.0)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout_data,_=process.communicate()
                if stdout_data:
                    stream.write(stdout_data);stream.flush()
                stream.write(json.dumps(dict(event='grid_timeout',reason='grid process exceeded duration + margin'))+'\n')
                stream.flush()
                return process.returncode
            if stdout_data:
                stream.write(stdout_data);stream.flush()
            return process.returncode
        finally:
            try:
                process.stdout.close()
            except Exception:
                pass


if __name__=='__main__':
    raise SystemExit(main())
