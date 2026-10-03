# Stato del progetto

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
