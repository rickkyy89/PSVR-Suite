# PSVR-PC

Progetto open source per PSVR1 (visore PS4) su Windows 10/11 x64.
Prima fase: diagnostica USB/HID indipendente; evoluzione prevista verso
HMD-only SteamVR per video VR e Assetto Corsa, poi OpenXR/MSFS.

**Milestone 1 verificata sul PSVR reale:** rilevamento, apertura HID sensori/controllo
e ~1000 report sensore/s; test assenza e ricollegamento riusciti.
Vedere [STATUS.md](STATUS.md) e [report M1](docs/MILESTONE_1_REPORT.md).

Eseguibile pronto: `out/diag/psvr_diag.exe`. Dalla root:

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --duration-ms 5000 --raw-packets 3
$LASTEXITCODE
```

- [Architettura e analisi del codice](ARCHITECTURE.md)
- [Build Windows](BUILD.md)
- [Collegamenti e test](TESTING.md)
- [Diagnosi problemi](TROUBLESHOOTING.md)
- [Dipendenze e provenienza](third_party/README.md)
- [Configurazione](docs/CONFIGURATION.md)

Struttura: `tools/psvr_diag` diagnostico, `driver/` riservato al futuro driver,
`scripts/` build, `tests/` verifiche, `docs/` report, `logs/` output locali,
`third_party/` descrizione/pin delle dipendenze. Questa cartella è già la root
del progetto: non serve aggiungere una seconda root `PSVR-PC/`.
