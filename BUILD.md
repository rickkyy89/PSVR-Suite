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
Build MSVC verificata il 2026-10-06 con Build Tools 2022 17.14.41,
MSVC 19.44.35229 e Windows SDK 10.0.26100.0.
Il driver MSVC è staged separatamente in `out/driver_psvrpc_msvc/psvrpc`;
MinGW conserva `out/driver_psvrpc/psvrpc`. Anche il preset `vs2022` seleziona
lo staging separato. `-DriverPackageRoot <path>` permette uno staging esplicito.
La registrazione SteamVR deve indicare il pacchetto che si vuole realmente provare.
Risultati runtime e presenza diagnostica: [prova MSVC](docs/MSVC_RUNTIME_20261006.md).

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

## Driver OpenVR M4

La build include il target DLL `driver_psvrpc` e staging automatico
`out/driver_psvrpc/psvrpc`. SDK OpenVR 2.15.6 fissato al commit
`0924064316de3effbcd1acf1e309182a2deb1c05`, usato solo per header driver.
`PSVR_BUILD_DRIVER=OFF` esclude driver e download SDK; per offline usare
`FETCHCONTENT_SOURCE_DIR_OPENVR_SDK` con checkout di quel commit.
La build non registra né attiva SteamVR. Dettagli e gate in
[report M4](docs/MILESTONE_4_REPORT.md).

## M5 display e test matematici M6

La stessa build produce anche `psvr_display.exe`, inventario read-only Windows,
nel package standalone `out/display/` e tramite install in `out/diag/`.
La DLL include il componente display desktop-esteso ma resta disabilitato
finché non sono forniti opt-in e settings espliciti verificabili. M6 include
matematica pura, loader/adapter e validatore `out/optics/psvr_optics.exe`.
La build genera pattern SVG stereo in `out/patterns/` e risorse driver quando
Python 3.11 è disponibile; nessun pattern apre GUI o attiva display.
Checkpoint M6: 28 CTest; nessun test automatico richiede USB, HDMI o SteamVR.
Report [M5](docs/MILESTONE_5_REPORT.md) e
[M6 software](docs/MILESTONE_6_SOFTWARE.md).

## M9 posizione e PSEye

La build principale aggiunge il bridge Winsock al driver e il probe standalone
`out/position_probe/psvr_position_probe.exe`. Suite verificata **35/35 CTest**,
inclusi UDP reale e interoperabilità Python → C++. La suite camera richiede
NumPy nel Python scelto da CMake; se manca, configure lo segnala e non registra
quel test opzionale. OpenCV resta opzionale per le prove geometriche offline.

La camera ha una build separata, con upstream fissato e patch riproducibile:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/psvr_pseye/build.ps1 -ToolchainBin "C:\Users\rickk\AppData\Local\Temp\opencode\w64devkit\bin"
```

Stage `out/pseye/`; nessun driver USB installato. `build.ps1 -NoUpstream`
produce solo lo shim, non il backend funzionante completo. Per detection e
calibrazione live usare la venv camera (già pronta su questo PC):

```powershell
python -m venv .venv-camera
& ".venv-camera\Scripts\python.exe" -m pip install -r tools/psvr_camera/requirements-optional.txt
$env:PYTHONPATH="tools"
& ".venv-camera\Scripts\python.exe" -m psvr_camera pseye inventory
& ".venv-camera\Scripts\python.exe" -B -m unittest discover -s tests -p "test_camera*.py"
```

Per usare la venv nei test CMake, scegliere esplicitamente
`-DPython3_EXECUTABLE=<root>/.venv-camera/Scripts/python.exe`.
Versioni verificate e gate fisici in [report sessione](docs/AUTONOMOUS_20261005.md).
