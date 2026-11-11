"""Enable the requested suite policies using the selective settings store."""
import argparse
import json
from pathlib import Path

from .settings import (
    ROOT,
    OPTICS_ALIASES,
    OPTICS_CURRENT,
    default_preferences,
    DriverSettingStore,
    driver_keys_for,
    resolve_optics_profile,
    save_preferences,
    validate_optics_profile,
    validate_preferences,
)

# Repo root calcolata da __file__ (come hardware_smoke con parents[2]),
# cosi' il default non dipende dalla CWD di invocazione.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PREFERENCES = _REPO_ROOT / "configurations" / "psvr_suite_user.json"


def argument_parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backup-dir',type=Path,required=True)
    p.add_argument('--preferences',type=Path,default=_DEFAULT_PREFERENCES)
    p.add_argument('--vrsettings',type=Path,default=None,
                   help='Percorso steamvr.vrsettings di destinazione (default: quello reale di SteamVR)')
    p.add_argument('--dry-run',action='store_true',
                   help='Valida tutto senza scrivere: no backup, no prefs, no vrsettings')
    p.add_argument('--optics',default=default_preferences()['optics_profile'],
                   help='Alias ottica (%s) o percorso profilo custom '
                        '(validato subito, prima di qualsiasi scrittura)'
                        % ", ".join(OPTICS_ALIASES))
    return p


def _validate_dry_run(store,prefs,backup_root,preferences_path):
    """Esegue i controlli di apply/save senza alcuna scrittura."""
    cleaned=validate_preferences(prefs)
    store._require_runtime_stopped()
    _raw,settings=store._read_snapshot()
    if not isinstance(settings,dict):
        raise ValueError('driver settings must hold an object')
    mapped=driver_keys_for(cleaned)
    mode,profile=resolve_optics_profile(cleaned['optics_profile'],root=store.root)
    if mode!=OPTICS_CURRENT and mode=='profile':
        target=Path(profile)
        if not target.is_absolute():
            target=store.root/target
        if not target.is_file():
            raise FileNotFoundError('optics profile missing: %s' % target)
        if callable(store.optics_validator):
            store.optics_validator(target)
        else:
            validate_optics_profile(target,validator=store.optics_validator,root=store.root)
    if backup_root.exists():
        raise FileExistsError('backup directory must be new: %s' % backup_root)
    if not backup_root.parent.is_dir():
        raise ValueError('backup parent must exist')
    preferences_path=Path(preferences_path)
    if preferences_path.resolve()==(ROOT/'config.json').resolve():
        raise ValueError('refusing to overwrite the accepted global config; save prefs elsewhere')
    if not preferences_path.parent.is_dir():
        raise ValueError('preferences parent must exist')
    return cleaned,mapped,mode


def main(argv=None):
    parser=argument_parser()
    a=parser.parse_args(argv)
    # B11-09: resolve anticipata di --optics (alias o percorso custom,
    # vedi resolve_optics_profile): errore chiaro subito, prima di
    # qualsiasi scrittura. Niente choices argparse: i percorsi custom
    # restano ammessi.
    try:
        resolve_optics_profile(a.optics, root=_REPO_ROOT)
    except ValueError as exc:
        parser.error("invalid --optics: %s" % exc)
    if a.preferences.exists():
        raise RuntimeError('user preferences already exist; apply from the suite instead')
    prefs=default_preferences()
    prefs['optics_profile']=a.optics
    store=DriverSettingStore(path=a.vrsettings,backup_root=a.backup_dir)
    if a.dry_run:
        cleaned,mapped,mode=_validate_dry_run(store,prefs,a.backup_dir,a.preferences)
        print(json.dumps({'dry_run':True,'valid':True,'preferences':str(a.preferences),
                          'vrsettings':str(store.path),
                          'optics_mode':mode,'mapped_keys':sorted(mapped),
                          'lens_acceptance_verified':False},indent=2))
        return
    journal=store.apply(prefs,apply_optics=True)
    save_preferences(a.preferences,prefs)
    print(json.dumps({'journal':journal,'preferences':str(a.preferences),'lens_acceptance_verified':False},indent=2))


if __name__=='__main__':
    main()
