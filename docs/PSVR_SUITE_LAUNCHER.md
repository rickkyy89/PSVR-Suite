# PSVR Suite: icona e collegamento Start Menu (utente corrente)

## File (solo nuovi asset launcher/install + docs; `app.py`/`tracking.py` non toccati)
- `tools/psvr_suite/psvr_suite_launcher.py` — launcher GUI senza console.
- `tools/psvr_suite/install_psvr_suite_shortcut.ps1` — installer collegamento.
- `tools/psvr_suite/assets/psvr_suite.svg` — sorgente vettoriale piccola.
- `tools/psvr_suite/assets/generate_psvr_suite_icon.py` — generatore riproducibile (solo stdlib).
- `tools/psvr_suite/assets/psvr_suite.png` — 256x256 RGBA (generato).
- `tools/psvr_suite/assets/psvr_suite.ico` — 16/32/48/256 PNG-compressi (generato).
- `tests/test_suite_launcher.py` — 13 test read-only (fake Win32, niente HW/GUI).

## Collegamento installato
- Link: `C:\Users\rickk\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\PSVR Suite.lnk`
- TargetPath: `C:\Users\rickk\Desktop\PSVR\.venv-camera\Scripts\pythonw.exe` (senza console)
- Arguments: `"C:\Users\rickk\Desktop\PSVR\tools\psvr_suite\psvr_suite_launcher.py"` (senza args: GUI standard)
- WorkingDirectory: `C:\Users\rickk\Desktop\PSVR`
- IconLocation: `C:\Users\rickk\Desktop\PSVR\tools\psvr_suite\assets\psvr_suite.ico,0`
- Icona: `C:\Users\rickk\Desktop\PSVR\tools\psvr_suite\assets\psvr_suite.ico`

## Comportamento reale
- Doppio click a suite aperta: il launcher cerca la finestra esatta
  `"PSVR · Suite utente"` via `FindWindowW`; se minimizzata invia
  `ShowWindow(hwnd, 9)` (SW_RESTORE), poi `SetForegroundWindow` (best-effort,
  senza garanzie per policy foreground Windows) e ritorna 0 senza toccare il
  lock: niente doppia istanza, niente fallimento invisibile sotto pythonw.
- Altrimenti avvia `tools.psvr_suite.app.main(["--root", repo] + argv CLI)`:
  senza args nessun `--start-vr`/`--attach` (nessun avvio VR implicito).
  Argv espliciti ammessi, es. launcher `--attach --tab Camera` per test.
- Race: se un'altra istanza parte nel frattempo, resta autorità il lock
  singleton in `app.py` (`logs/psvr_suite.lock`, exit 3). In quel caso il
  launcher registra una riga in `logs/psvr_suite_launcher.log` e mostra un
  avviso breve invece di fallire in silenzio.
- Errori di startup sotto pythonw: append troncato (2000 char) in
  `logs/psvr_suite_launcher.log` + messagebox (prima Win32 `MessageBoxW`,
  fallback Tk solo-errore); nessun blob su stdout.

## Comandi
```powershell
# Rigenera icona (format-aware, niente editor testo su binari)
.venv-camera\Scripts\python.exe tools\psvr_suite\assets\generate_psvr_suite_icon.py

# Test launcher (read-only, nessuna GUI/HW)
.venv-camera\Scripts\python.exe -m pytest tests/test_suite_launcher.py -q

# (Ri)installa collegamento utente
powershell -NoProfile -ExecutionPolicy Bypass -File tools\psvr_suite\install_psvr_suite_shortcut.ps1
```

Nota: `run_suite.ps1` lasciato invariato; il launcher standard ne replica il
default (GUI senza VR automatico, `--root` = repo).
