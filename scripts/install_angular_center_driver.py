"""Install the verified DLL into this workspace's registered driver, with backup."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / 'build/vs2022/RelWithDebInfo/driver_psvrpc.dll'
    package = root / 'out/driver_psvrpc_6dof/psvrpc'
    target = package / 'bin/win64/driver_psvrpc.dll'
    registry = Path(r'C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64\vrpathreg.exe')
    flags = dict(capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    registered = subprocess.run([str(registry), 'show'], **flags)
    expected = 'psvrpc : ' + str(package)
    if registered.returncode or expected.casefold() not in registered.stdout.casefold():
        raise RuntimeError('Expected workspace driver is not registered')
    processes = subprocess.run(['powershell', '-NoProfile', '-Command',
                                'Get-Process -Name vrserver,vrcompositor -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }; exit 0'], **flags)
    if processes.returncode or processes.stdout.strip():
        raise RuntimeError('Stop SteamVR before replacing the driver')
    if not source.is_file() or not target.is_file() or not (root / 'logs').is_dir():
        raise RuntimeError('Build, installed driver or backup parent missing')
    backup_dir = root / 'logs' / ('angular_center_install_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup_dir.mkdir(exist_ok=False)
    backup = backup_dir / target.name
    shutil.copy2(target, backup)
    before = digest(backup)
    expected_hash = digest(source)
    temporary = target.with_name(target.name + '.angular-install.tmp')
    if temporary.exists():
        raise RuntimeError('Unexpected deployment temporary file exists')
    try:
        shutil.copy2(source, temporary)
        if digest(temporary) != expected_hash:
            raise RuntimeError('Deployment copy hash mismatch')
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    report = dict(source=str(source), installed=str(target), backup=str(backup),
                  before_sha256=before, installed_sha256=digest(target),
                  positionAngularCenterEnabled='Applied by suite on next Start VR',
                  hardware_accepted=False)
    (backup_dir / 'manifest.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
