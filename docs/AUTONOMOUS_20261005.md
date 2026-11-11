# Sessione autonoma 2026-10-05 — PSEye / M9

Attesa iniziale di 3600 secondi eseguita prima del lavoro. Implementazione,
inventario e review parallelizzati con subagenti general/explore/junior_coder.

## Risultato

Acquisizione **reale** PS3 Eye e percorso software camera → posizione metrica
UDP → IMU/OpenVR disponibili. **M9 non completa**: nessun tracking posizionale
calibrato del visore è stato accettato nel runtime. Il driver conserva
`positionEnabled=false`; nessuna attivazione 6DoF SteamVR eseguita.

### Hardware misurato

PSEye `USB\VID_1415&PID_2000&MI_00`, binding libusb-win32 già presente.
Audio MI_01 resta disponibile. Nessun rebinding/installazione driver.

| Modo richiesto | Prima serie, fps Python | Frame |
|---|---:|---:|
| 640×480 / 60 | ~58.3 | 120/120 |
| 640×480 / 75 | ~72.3 | 120/120 |
| 320×240 / 60 | ~58.2 | 120/120 |
| 320×240 / 187 | ~178.3 | 240/240 |

Verifica finale del parent alle **05:01:19–05:01:21 UTC**:
640×480 / 75 BGR, ABI 2, **150 frame in 2.054695 s = 73.004 fps**, zero
frame dichiarati persi, timeout ed errori. Queste sono misure dell'acquisizione,
non misure di latenza motion-to-photon o dell'intero tracker.

### Componenti

- `tools/psvr_pseye`: shim C ABI e upstream PS3EYEDriver fissato al commit
  `fe03494e648d0bf2aaa77869e5b50965e2e23620`; patch riproducibile applicata a
  copia di build per grab con timeout/abort e FPS effettivi. Artefatti separati
  `out/pseye`, provenienza e licenze MIT/GPLv2/LGPL incluse.
- `tools/psvr_camera`: NumPy geometry/PnP, calibrazione, gate su RMS,
  degenerazione, reproiezione/ambiguità e risoluzione reale; detector ArUco
  OpenCV, sorgente PSEye e UVC, filtro temporale e perdita esplicita.
- `driver/position_*`: loopback 127.0.0.1:4271, JSON `psvr.position` v1 in
  metri, controlli finitezza/sequenza e freschezza locale. Origine seduta,
  epoca recenter condivisa con IMU, rotazione del vettore posizione nel frame
  di yaw ricentrato. Posizione vecchia o mancante invalida la posa opt-in.
- `out/position_probe/psvr_position_probe.exe`: osservazione limitata del
  flusso senza PSVR USB/OpenVR; usa lo stesso receiver del driver.
- `tools/psvr_app_readiness`: inventario applicazioni e OpenXR read-only.

## Verifiche

- Build principale MinGW x64 riuscita; **35/35 CTest PASS**.
- Test `position_probe_interop`: processo C++ reale + mittente Python reale
  su porta effimera, spostamento sintetico di 0.1 m, timeout locale, recupero,
  frame invalido, datagramma malformato e conflitto bind.
- Suite camera con `.venv-camera`: **142 test**, 7 skip previsti per percorsi
  assenza OpenCV e hardware opt-in. Detector/generatore ArUco disponibile.
- Review dedicata a race recenter/lifetime, origine iniziale, timeout native,
  mancata duplicazione frame, risoluzione e FPS negoziati. Test hardware
  opt-in del backend passati dal subagente; scollegamento fisico non eseguito.
- Bersagli preparati in `out/pseye`: `chessboard_9x6_25mm.svg`,
  `aruco_0_100mm.png`, `geometry_aruco_0_100mm.json` (dimensioni nominali da
  verificare sul materiale stampato, non geometria fisica già misurata).

## Uso immediato

Da PowerShell nella root, con la venv già creata:

```powershell
$env:PYTHONPATH="tools"
& ".venv-camera\Scripts\python.exe" -m psvr_camera pseye inventory
& ".venv-camera\Scripts\python.exe" -m psvr_camera pseye probe --index 0 --width 640 --height 480 --fps 75 --frames 150 --max-seconds 4
& ".venv-camera\Scripts\python.exe" -B -m unittest discover -s tests -p "test_camera*.py"
& "out\position_probe\psvr_position_probe.exe" --port 0 --duration-ms 2000 --json
```

L'ultimo comando termina con exit 4 se nessun mittente produce una posizione
valida: è un esito diagnostico previsto. Il probe non può condividere la porta
4271 con il receiver del driver contemporaneamente.

Build camera autonoma, distinta dalla build driver:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/psvr_pseye/build.ps1 -ToolchainBin "C:\Users\rickk\AppData\Local\Temp\opencode\w64devkit\bin"
```

Dipendenze testate nella venv: NumPy **2.4.6**,
opencv-contrib-python-headless **5.0.0.93**. Per ricrearla:

```powershell
python -m venv .venv-camera
& ".venv-camera\Scripts\python.exe" -m pip install numpy==2.4.6 opencv-contrib-python-headless==5.0.0.93
```

## Gate fisici rimasti

1. Camera nella posizione definitiva: fissare selettore FOV, risoluzione,
   fuoco/illuminazione e acquisire viste del bersaglio per intrinseci misurati.
2. Bersaglio rigido sul visore e dimensioni misurate. Percorso live iniziale
   ArUco; i LED integrati PSVR richiedono geometria e associazioni di punti
   misurate, non sono ancora un detector live completo.
3. Allineamento frame camera/IMU: camera livellata per l'attuale offset solo
   yaw; montaggio inclinato richiede extrinseci completi. Misurare anche
   offset marker→origine della testa per evitare traslazioni apparenti durante
   rotazioni (lever arm).
4. Test scala su spostamenti noti, jitter statico, perdita/recupero e latenza
   effettiva prima dell'accettazione 6DoF e di qualsiasi predizione.
5. M5 fullscreen −202 e M6 accettazione ottica rimangono aperti.

M7/M8/M10: presenti Assetto Corsa, MSFS 2024, SteamVR OpenXR, yt-dlp e ffmpeg;
nessun player VR rilevato. La presenza non certifica avvio/output/prestazioni.
Dettagli in [APPLICATION_READINESS_20261005.md](APPLICATION_READINESS_20261005.md).
