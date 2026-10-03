# PSVR-PC

Progetto open source per PSVR1 (visore PS4) su Windows 10/11 x64.
Prima fase: diagnostica USB/HID indipendente; evoluzione prevista verso
HMD-only SteamVR per video VR e Assetto Corsa, poi OpenXR/MSFS.

**Milestone 1 e 2 verificate sul PSVR reale:** rilevamento, ~1000 report sensore/s,
power on/off e VR/cinema mode con conferma di stato dal dispositivo.
Binding attuale: **sensori 4 = HID, controllo 5 = WinUSB**.
Vedere [STATUS.md](STATUS.md), [report M1](docs/MILESTONE_1_REPORT.md) e
[report M2](docs/MILESTONE_2_REPORT.md).

Eseguibile pronto: `out/diag/psvr_diag.exe`. Dalla root:

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --duration-ms 5000 --raw-packets 3
$LASTEXITCODE
```

Comandi espliciti M2 (uno per esecuzione):

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --power-on
.\out\diag\psvr_diag.exe --config .\config.json --vr-mode
.\out\diag\psvr_diag.exe --config .\config.json --cinema-mode
.\out\diag\psvr_diag.exe --config .\config.json --power-off
```

Controllare `$LASTEXITCODE` dopo ciascuno: 0 = comando accettato **e** stato
confermato; 6 = stato non verificato. `--control-info` legge solo descrittori;
`--status` osserva un nuovo F0 e può scadere senza eventi. `--power-on --dry-run`
mostra i byte senza aprire hardware. Il default continua a essere passivo.

- [Architettura e analisi del codice](ARCHITECTURE.md)
- [Build Windows](BUILD.md)
- [Collegamenti e test](TESTING.md)
- [Diagnosi problemi](TROUBLESHOOTING.md)
- [Dipendenze e provenienza](third_party/README.md)
- [Configurazione](docs/CONFIGURATION.md)
- [WinUSB controllo e ripristino](docs/WINUSB_CONTROL.md)

Struttura: `tools/psvr_diag` diagnostico, `driver/` riservato al futuro driver,
`scripts/` build, `tests/` verifiche, `docs/` report, `logs/` output locali,
`third_party/` descrizione/pin delle dipendenze. Questa cartella è già la root
del progetto: non serve aggiungere una seconda root `PSVR-PC/`.
