# Stato del progetto

## Sessione M3 — 2026-10-03 — IN_PROGRESS (non completa)

- **M3 IN_PROGRESS, non COMPLETE; M4 non iniziata.** Fusion propria C++17
  (`orientation_math.hpp` + `imu_fusion.hpp` + `imu_capture.*`): quaternione
  complementare gravità, cal 8 s tavolo hands-off dopo 200 ms warm-up,
  scale gyro 0.00105 / accel 9.81/16384, gate STD 0.75 dps + meanShift 0.05 dps,
  bias congelato, gravityGain 1, smoothing 0, recenter full/yaw. Full corretto
  two-sided; regressione tilt-30° passa.
- GUI `tools/psvr_monitor/monitor.py` (Py 3.11 stdlib Tk 8.6) via
  `scripts/run_monitor.ps1` / root `run_monitor.ps1`; API readonly loopback
  effimera riscoperta via `logs/monitor_api.json`. Dettagli operativi in
  `docs/IMU_TESTING.md`, `docs/MONITOR_APP.md`.
- Build MinGW x64 OK **15/15 CTest**, inclusi **71 test Python** del monitor;
  statico 60 s originale PASS
  (drift -0.16°/min, pre-nuovi gate); due capture asse precoci fallite
  conservate; drift-reclamo spiegato da cal in movimento (+0.648 dps Y).
  Statico post-fix nella GUI **PASS**: 42000 report, 33.799532 s di tracking,
  yaw +0.236718° = **+0.420216°/min**, pitch/roll in escursioni ~0.060°/~0.034°,
  2 rollover, nessuna perdita/discontinuità, exit 0.
- Verificata anche l'integrazione reale del backend GUI con **Stop immediato
  all'avvio**, recenter, ricalibrazione e Stop/EOF: nessuna posa obsoleta valida.
  Rafforzati watchdog di startup/progressione, rifiuto di duplicati,
  invalidazione JSON sulle eccezioni e disponibilità IPC durante l'avvio.
- Restano da verificare i **segni fisici di pitch/roll e il confronto controllato
  indossato/non indossato**. Non sono state applicate inversioni arbitrarie.
- Report: [docs/MILESTONE_3_REPORT.md](docs/MILESTONE_3_REPORT.md).
  Non dichiarate stereo/display/SteamVR; Win11 reale, VS 2022/Win10 non verificati.

## Sessione M2 — 2026-10-03

- **M2 completata, verificata sul PSVR reale.** Comandi espliciti `--power-on`,
  `--power-off`, `--vr-mode`, `--cinema-mode`; pacchetti documentati di 8 byte,
  verifica con risposta A0 accettata e nuovo F0.
- Autorizzazione dell'utente e installazione manuale Zadig: **solo MI_05** da
  HidUsb/input.inf a **WinUSB/oem88.inf**. MI_04 resta HidUsb/input.inf;
  composite e audio invariati. Inventario originale in `logs/`, ripristino
  documentato in `docs/WINUSB_CONTROL.md`.
- Endpoint rilevati: **IN 0x84 / OUT 0x04**, interrupt, max packet 64.
  Backend WinUSB nativo, nessuna dipendenza libusb.
- Power-off: mask **0x64**; power-on **0x15**;
  VR **0x51, vr_active=1**; cinema **0x55, vr_active=0**; exit 0 per ogni cambio.
- Stato finale: **visore acceso in cinema mode**.
- Regressione passiva M1 col binding misto: **2002 report validi in 2 s**, 0
  discontinuità/rifiuti, exit 0. Build x64 MinGW riuscita, **9/9 test passati**.
- **M3–M10 non iniziate.** Prossimo passo: IMU/fusion 3DoF e recenter.

Report: [docs/MILESTONE_2_REPORT.md](docs/MILESTONE_2_REPORT.md).
`--status` osserva passivamente eventi, non fa polling: senza nuovo F0
termina **6 / stato sconosciuto**. Anche un comando già soddisfatto può non
produrre F0: ACK da solo non diventa successo di verifica.
Ripristino del binding HID, hot-unplug durante I/O e Ctrl+C non provati
fisicamente nella sessione M2. VS 2022 e Windows 10 ancora non verificati.
Verifica display/SteamVR resta alle sue milestone. **Sessione conclusa a M2.**

## Sessione M0/M1 — 2026-10-03 (storico)

- **M0: analisi completata.** Revisioni, sorgenti, compatibilità e rischi in
  `ARCHITECTURE.md`. Ambiente iniziale: repository vuoto, Windows 11 Pro x64
  10.0.26200, Git/Python disponibili; VS 2022 non rilevato nei percorsi standard.
- **M1: completata e verificata sul PSVR reale.** `out/diag/psvr_diag.exe`
  compilato Windows x64, 5/5 test CTest passati. Rilevato `054C:09AF`,
  USB release `0107`, HID sensori 4 e controllo 5 aperte.
  Dopo accensione fisica confermata dall'utente: **5002 report sensore da 64 byte
  in 5 s**, 0 rifiutati, 0 discontinuità, ~1000.20 report/s e ~2000.41 campioni/s.
  Exit **0**. USB scollegato fisicamente: exit **2**, nessun crash.
  Ricollegato: **2001 report in 2 s**, exit **0**, nessuna discontinuità.
- Al termine della prima sessione **M2–M10 non iniziate**.

## Limiti verificati e gate successivo

- In standby iniziale le HID si aprivano ma non arrivavano report: exit 4,
  nessun comando software aggiunto. L'accensione fisica ha sbloccato il flusso.
- Sensori: input HID Windows 65 byte (prefisso zero rimosso da HIDAPI).
- Controllo: input **0**, output **0**, feature **5** byte dal descriptor Windows.
  Non leggibile come flusso input: la lettura viene saltata esplicitamente.
  Apertura riuscita NON verifica invio comandi power/VR mode. Prima di M2
  verificare il trasporto per questa interfaccia senza indovinare feature report.
- MI_00 3D Audio, MI_06 H.264 e MI_07 BulkIn: problem code 28, driver assente;
  non hanno impedito la lettura sensori e non sono stati modificati.
- Build verificata con toolchain portabile MinGW; percorso VS 2022 documentato,
  non eseguito perché VS non disponibile. Windows 10 non testato in questa sessione.
- Scollegamento **durante** una capture e Ctrl+C restano prove manuali aggiuntive.
- Nessuna verifica di orientamento/drift, display, distortion o SteamVR in M1.

Report con comandi/output e log: [docs/MILESTONE_1_REPORT.md](docs/MILESTONE_1_REPORT.md).
Commit M0: `80debcb`. M1 ha un commit dedicato nella cronologia Git.
La prima sessione si è conclusa a M1; la successiva istruzione "procedi" ha
avviato M2, riportata sopra. Il commit M2 dedicato è nella cronologia Git.
