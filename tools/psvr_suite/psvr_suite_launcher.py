"""Launcher GUI PSVR Suite per collegamento Start (pythonw, senza console).

Usato dal collegamento "PSVR Suite.lnk" con:
  TargetPath: <repo>\\.venv-camera\\Scripts\\pythonw.exe
  Arguments: "<repo>\\tools\\psvr_suite\\psvr_suite_launcher.py"
  WorkingDirectory: <repo>

Comportamento:
- Se la finestra "PSVR Suite" esiste già, la riporta davanti
  (restore se minimizzata + SetForegroundWindow, senza garanzie) e ritorna 0
  senza toccare il lock: niente doppia istanza, niente fallimento invisibile.
- Altrimenti avvia la GUI via tools.psvr_suite.app.main con
  ["--root", repo] + argv CLI. Senza args non aggiunge --start-vr/--attach:
  nessun avvio VR implicito. Argv espliciti (es. --attach --tab Camera)
  sono ammessi per test/scorciatoie dedicate.
- In caso di race (istanza partita nel frattempo) resta autorità il lock
  singleton in app.py (exit 3): il launcher mostra solo un avviso breve.
- Errori di startup sotto pythonw (niente console): coda in
  logs/psvr_suite_launcher.log (append, troncato) + messagebox solo errore.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

WINDOW_TITLE = "PSVR Suite"
LEGACY_WINDOW_TITLE = "PSVR · Suite utente"
SW_RESTORE = 9
MB_ICONERROR = 0x10
LOG_REL = Path("logs") / "psvr_suite_launcher.log"
_MAX_LOG_CHARS = 2000


def _repo_root() -> Path:
    # .../tools/psvr_suite/psvr_suite_launcher.py -> repo (parents[2]).
    return Path(__file__).resolve().parents[2]


def build_suite_argv(repo: Path, argv=None) -> list:
    """Costruisce argv per app.main: --root repo + passthrough CLI."""
    extra = list(argv) if argv is not None else []
    # B12-09: --root presente sia come "--root <dir>" sia come "--root=<dir>".
    for item in extra:
        if item == "--root" or (isinstance(item, str)
                                and item.startswith("--root=")):
            return extra
    return ["--root", str(repo)] + extra


def _real_user32():
    """Carica user32 con argtypes tipati (HWND 64bit via wintypes)."""
    import ctypes
    from ctypes import wintypes as w

    user = ctypes.WinDLL("user32", use_last_error=True)
    user.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
    user.FindWindowW.restype = w.HWND
    user.IsIconic.argtypes = [w.HWND]
    user.IsIconic.restype = w.BOOL
    user.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
    user.ShowWindow.restype = w.BOOL
    user.SetForegroundWindow.argtypes = [w.HWND]
    user.SetForegroundWindow.restype = w.BOOL
    return user


def bring_existing_to_front(win32=None) -> bool:
    """True se la finestra esisteva (riportata davanti, best-effort).

    win32: modulo iniettabile per test (attesi FindWindowW/IsIconic/
    ShowWindow/SetForegroundWindow). Se None, usa user32 reale solo su
    Windows, altrimenti ritorna False.
    """
    if win32 is None:
        if os.name != "nt":
            return False
        try:
            win32 = _real_user32()
        except Exception:
            return False
    try:
        hwnd = win32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            hwnd = win32.FindWindowW(None, LEGACY_WINDOW_TITLE)
    except Exception:
        return False
    if not hwnd:
        return False
    try:
        if win32.IsIconic(hwnd):
            win32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pass
    try:
        win32.SetForegroundWindow(hwnd)  # senza garanzie (policy foreground).
    except Exception:
        pass
    return True


def log_error(repo: Path, text: str) -> None:
    """Appende errore troncato al log launcher; mai solleva."""
    try:
        path = Path(repo) / LOG_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text[:_MAX_LOG_CHARS] + "\n")
    except Exception:
        pass


def show_error(title: str, message: str) -> None:
    """Messagebox solo-errore: prima Win32, poi Tk; mai solleva, mai stdout."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes as w

            user = ctypes.WinDLL("user32", use_last_error=True)
            user.MessageBoxW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT]
            user.MessageBoxW.restype = ctypes.c_int
            user.MessageBoxW(None, message, title, MB_ICONERROR)
            return
    except Exception:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        try:
            messagebox.showerror(title, message)
        finally:
            root.destroy()
    except Exception:
        pass


def main(argv=None, _bring=None, _suite_main=None, _repo=None,
         _log=None, _show=None) -> int:
    """Entrypoint launcher. Parametri _* solo per test (default reali)."""
    repo = Path(_repo) if _repo is not None else _repo_root()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    extra = list(argv) if argv is not None else sys.argv[1:]
    suite_argv = build_suite_argv(repo, extra)
    bring = _bring if _bring is not None else bring_existing_to_front
    log = _log if _log is not None else (lambda text: log_error(repo, text))
    show = _show if _show is not None else show_error
    try:
        if bring():
            return 0
    except Exception:
        pass  # il lock singleton in app resta l'autorità in caso di race.
    try:
        if _suite_main is None:
            from tools.psvr_suite.app import main as suite_main
        else:
            suite_main = _suite_main
        code = suite_main(suite_argv)
    except SystemExit as exc:
        text = f"exit-arg: {exc.code!r} argv={suite_argv!r}"[:_MAX_LOG_CHARS]
        log(text)
        show("PSVR Suite", f"Avvio non riuscito (argomenti?). Codice {exc.code!r}.")
        try:
            return int(exc.code) if isinstance(exc.code, int) else 2
        except Exception:
            return 2
    except BaseException as exc:  # noqa: BLE001 - pythonw non ha console.
        tb = traceback.format_exc(limit=5)
        log(f"avvio fallito argv={suite_argv!r}: {exc!r}\n{tb}")
        show("PSVR Suite", f"Avvio non riuscito: {exc!r}")
        return 1
    if code == 3:
        # Lock singleton: altra istanza attiva ma finestra non trovata
        # (startup in corso o titolo diverso) -> avviso visibile, non silent.
        log("suite già attiva (lock singleton, finestra non trovata)")
        show("PSVR Suite già attiva",
             "La suite risulta già in esecuzione: riporta in primo piano "
             "la finestra esistente.")
        return 3
    try:
        return int(code)
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
