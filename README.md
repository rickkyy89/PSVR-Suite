# PSVR-PC

Progetto open source per PSVR1 (visore PS4) su Windows 10/11 x64.
Prima fase: diagnostica USB/HID indipendente; evoluzione prevista verso
HMD-only SteamVR per video VR e Assetto Corsa, poi OpenXR/MSFS.

**Sessione iniziale limitata alla milestone 1.** Vedere [STATUS.md](STATUS.md).

- [Architettura e analisi del codice](ARCHITECTURE.md)
- [Build Windows](BUILD.md)
- [Collegamenti e test](TESTING.md)
- [Diagnosi problemi](TROUBLESHOOTING.md)
- [Dipendenze e provenienza](third_party/README.md)

Struttura: `tools/psvr_diag` diagnostico, `driver/` riservato al futuro driver,
`scripts/` build, `tests/` verifiche, `docs/` report, `logs/` output locali,
`third_party/` descrizione/pin delle dipendenze. Questa cartella è già la root
del progetto: non serve aggiungere una seconda root `PSVR-PC/`.
