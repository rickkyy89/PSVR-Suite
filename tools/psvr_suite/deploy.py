"""Back up an installed driver/probe before controlled suite integration.

Nota B12-09: i default di --driver/--probe sono relativi alla root del repo
(out/...): eseguire questo script dalla root del repository. I default non
vengono risolti rispetto al cwd (nessuna modifica ai default, solo docs/help).
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backup-dir',type=Path,required=True,
        help='Directory di backup da creare (deve essere nuova).')
    p.add_argument('--driver',type=Path,default=Path('out/driver_psvrpc_6dof/psvrpc/bin/win64/driver_psvrpc.dll'),
        help='DLL driver installata (default relativo alla root repo: eseguire dalla root; flusso backup/integrazione suite col package 6DoF, mentre la registrazione SteamVR usa out/driver_psvrpc/psvrpc via driver_registration.ps1; default invariati, B14-09).')
    p.add_argument('--probe',type=Path,default=Path('out/runtime_probe/psvr_runtime_probe.exe'),
        help='Probe runtime installata (default relativo alla root repo: eseguire dalla root; flusso backup/integrazione suite, default invariato, B14-09).')
    a=p.parse_args(argv)
    run=subprocess.run(['powershell','-NoProfile','-Command',
        'Get-Process -Name vrserver,vrcompositor -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }; exit 0'],
        text=True,capture_output=True,timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if run.returncode or run.stdout.strip():
        raise RuntimeError('runtime must be stopped before deployment backup')
    if not a.backup_dir.parent.is_dir():
        p.error('backup parent missing')
    # B12-09: backup-dir esistente -> errore chiaro su stderr (exit 2),
    # mai traceback grezzo da FileExistsError.
    if a.backup_dir.exists():
        p.error('backup-dir already exists: %s (usare una directory nuova)' % a.backup_dir)
    try:
        a.backup_dir.mkdir(exist_ok=False)
    except FileExistsError:
        p.error('backup-dir already exists: %s (usare una directory nuova)' % a.backup_dir)
    manifest={'schema':'psvr.suite.install_backup','files':[]}
    for path in (a.driver,a.probe):
        if not path.is_file():
            raise FileNotFoundError(str(path))
        target=a.backup_dir/path.name
        shutil.copy2(path,target)
        manifest['files'].append({'installed':str(path.resolve()),'backup':str(target.resolve()),
                                  'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
    (a.backup_dir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(manifest,indent=2))


if __name__=='__main__':
    main()
