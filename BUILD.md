# Build Windows x64

## Visual Studio 2022 (preferita)

Installare Visual Studio 2022 o Build Tools 2022 con workload **Desktop
development with C++**, MSVC v143, SDK Windows 10/11 e CMake tools for Windows.
Git deve essere nel PATH. Lo script trova CMake anche nell'installazione VS
tramite `vswhere`; non occorre aprire una Developer Shell con il generator VS.

Da PowerShell nella root del progetto:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

Lo script previsto verifica toolchain e piattaforma, configura CMake,
compila, esegue CTest e installa il diagnostico in `out/diag/`.
Il generator è `Visual Studio 17 2022`, architettura `-A x64`.
La build MSVC sarà verificata solo quando VS 2022 è disponibile.

## Toolchain portabile utilizzata in questa sessione

Scaricare [w64devkit x64 2.10.0](https://github.com/skeeto/w64devkit/releases/tag/v2.10.0),
asset `w64devkit-x64-2.10.0.7z.exe`, verificare:

```powershell
Get-FileHash .\w64devkit-x64-2.10.0.7z.exe -Algorithm SHA256
```

SHA-256 atteso:
`18d0a4c71a166f8401ab6305781bec5882b40b5e06ba9807c61cb5f3b3c6325e`.
Estrarre l'archivio in una cartella esistente: niente installazione globale
né modifiche permanenti al PATH. Poi:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -Toolchain MinGW -ToolchainRoot "C:\percorso\w64devkit"
```

Su questo PC il percorso estratto nella prima sessione è:
`C:\Users\rickk\AppData\Local\Temp\opencode\w64devkit`.
È temporaneo: conservarlo altrove o installare VS per build future.
GCC 16.2.0, CMake 4.4.3 e Ninja inclusi. Target x64 imposto/verificato da CMake.

## Dipendenze e riproducibilità

HIDAPI 0.15.0 e nlohmann/json 3.12.0 scaricati via Git al primo configure,
con commit completi fissati in CMake, compilati staticamente.
Nessuna DLL libusb/OpenHMD/OpenVR necessaria per M1/M2. M2 collega WinUSB,
SetupAPI e altre API di sistema Windows; nessuna nuova dipendenza scaricata.
MinGW usa runtime statico;
MSVC usa CRT statico. Restano le DLL di sistema Windows.
Il pacchetto conserva config e licenze; nessuna struttura SteamVR attiva prima di M4.
Comandi M2: WinUSB su sola MI_05, procedura manuale in `docs/WINUSB_CONTROL.md`.
Compilazione ed esecuzione del tool non cambiano driver.

Build directory distinte: `build/vs2022` e `build/mingw`.
Configurazione default `RelWithDebInfo`, simboli utili al debug.
Per offline, CMake accetta `FETCHCONTENT_SOURCE_DIR_HIDAPI` e
`FETCHCONTENT_SOURCE_DIR_NLOHMANN_JSON` indicando checkout delle revisioni fissate.
Non aggiungere binari/cache di terzi a Git.

Se configure/build/test fallisce, lo script si ferma con codice errore e
non installa un pacchetto apparentemente riuscito. Leggere l'errore prima di riprovare.

## GUI monitor (opzionale, M3)

Il driver/diagnostico resta standalone C++ con dipendenze fissate sopra.
La GUI `tools/psvr_monitor/monitor.py` richiede solo **Python 3.11 + Tk 8.6
stdlib** (launcher `py.exe`), nessuna USB in Python:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run_monitor.ps1
.\run_monitor.ps1 -StartImu -ImuDurationMs 42000 -StationaryTest
```

`scripts/run_monitor.ps1` verifica `out/diag/psvr_diag.exe`, `config.json`
e Tk prima di avviare; senza Python la build/test C++ resta valida.
