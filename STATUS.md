# Stato del progetto

## Sessione 2026-10-03

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
- **M2–M10: non iniziate.** Nessun comando power/VR, driver SteamVR o tracking.

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
**Sessione conclusa alla milestone 1; M2 solo su nuova istruzione.**
