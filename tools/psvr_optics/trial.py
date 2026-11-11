"""Apply/restore only frozen optical settings, with exact backup and validation."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

KEYS=('distortionMode','distortionProfilePath')
MODES=('identity','profile')


def require_section(settings):
    if not isinstance(settings,dict):
        raise ValueError('driver_psvrpc section missing/invalid')
    section=settings.get('driver_psvrpc')
    if not isinstance(section,dict):
        raise ValueError('driver_psvrpc section missing/invalid')
    return section


def _checked(values,label):
    if not isinstance(values,dict):
        raise TypeError(label+' must be a dict with optical keys')
    if set(values)!=set(KEYS):
        raise ValueError(label+' must contain exactly optical keys')
    for key in KEYS:
        if not isinstance(values[key],str):
            raise TypeError(label+'.'+key+' must be a string')
    if values['distortionMode'] not in MODES:
        raise ValueError(label+'.distortionMode must be identity or profile')
    return values


def runtime_stopped():
    result=subprocess.run(['powershell','-NoProfile','-Command',
        "Get-Process -Name vrserver,vrcompositor -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }; exit 0"],
        capture_output=True,text=True,timeout=15)
    if result.returncode or result.stdout.strip():
        raise RuntimeError('SteamVR must be closed before changing frozen optical settings')


def preserved(settings):
    require_section(settings)
    data=json.loads(json.dumps(settings))
    for key in KEYS:
        data['driver_psvrpc'].pop(key,None)
    return data


def update(settings,values):
    _checked(values,'optical values')
    require_section(settings)
    result=json.loads(json.dumps(settings))
    for key in KEYS:
        if key not in values:
            raise ValueError('missing optical setting: '+key)
        result['driver_psvrpc'][key]=values[key]
    if preserved(result)!=preserved(settings):
        raise RuntimeError('unrelated settings changed')
    return result


def write_journal(path,journal):
    """Atomic journal write: readers never see a truncated journal (B10-06)."""
    fd,name=tempfile.mkstemp(prefix='psvr-optics-journal-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            stream.write(json.dumps(journal,indent=2)+'\n')
        os.replace(name,path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_write(path,data,original):
    if path.read_bytes()!=original:
        raise RuntimeError('settings changed concurrently; refusing overwrite')
    fd,name=tempfile.mkstemp(prefix='psvr-optics-',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as stream:
            json.dump(data,stream,indent=3,ensure_ascii=False,allow_nan=False)
            stream.write('\n')
        runtime_stopped()
        if path.read_bytes()!=original:
            raise RuntimeError('settings changed concurrently; refusing overwrite')
        os.replace(name,path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    action=p.add_mutually_exclusive_group(required=True)
    action.add_argument('--apply',type=Path)
    action.add_argument('--restore',type=Path)
    p.add_argument('--settings',type=Path,default=Path('C:/Program Files (x86)/Steam/config/steamvr.vrsettings'))
    p.add_argument('--backup-dir',type=Path,required=True)
    p.add_argument('--validator',type=Path,default=Path('out/optics/psvr_optics.exe'))
    a=p.parse_args(argv)
    runtime_stopped()
    if not a.backup_dir.parent.is_dir() or a.backup_dir.exists():
        p.error('backup directory must be new, in an existing parent')
    raw=a.settings.read_bytes();settings=json.loads(raw)
    section=require_section(settings)
    before={key:section.get(key) for key in KEYS}
    if any(value is None for value in before.values()):
        raise ValueError('explicit optical baseline keys required')
    validation=None
    if a.apply:
        profile=a.apply.resolve(strict=True)
        run=subprocess.run([str(a.validator.resolve(strict=True)),'--validate',str(profile),'--json'],
            capture_output=True,text=True,timeout=15)
        if run.returncode:
            raise RuntimeError('profile validation failed: '+run.stdout+run.stderr)
        validation=json.loads(run.stdout)
        after=dict(distortionMode='profile',distortionProfilePath=str(profile))
    else:
        previous=json.loads(a.restore.read_text(encoding='utf-8'))
        if not isinstance(previous,dict) or 'before' not in previous or 'after' not in previous:
            raise ValueError('restore file must contain before/after optical settings')
        _checked(previous['before'],'previous before')
        _checked(previous['after'],'previous after')
        if before!=previous['after']:
            raise RuntimeError('optical settings no longer match this trial; refusing stale rollback')
        after=previous['before']
    result=update(settings,after)
    a.backup_dir.mkdir()
    backup=a.backup_dir/'settings_before.json'
    backup.write_bytes(raw)
    journal=dict(schema='psvr.optics.controlled_trial',utc=datetime.now(timezone.utc).isoformat(),
        settings=str(a.settings.resolve()),backup=str(backup.resolve()),before=before,after=after,
        changed_keys=list(KEYS),validation=validation,backup_sha256=hashlib.sha256(raw).hexdigest(),
        unrelated_settings_preserved=True,optical_acceptance_verified=False,applied=False)
    jp=a.backup_dir/'journal.json'
    write_journal(jp,journal)
    atomic_write(a.settings,result,raw)
    loaded=json.loads(a.settings.read_text(encoding='utf-8'))
    if loaded!=result:
        raise RuntimeError('settings readback mismatch; inspect backup/journal')
    journal['applied']=True
    write_journal(jp,journal)
    print(json.dumps(journal,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
