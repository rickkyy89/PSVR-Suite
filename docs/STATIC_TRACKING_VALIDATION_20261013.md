# PSVR — prove statiche reali del 2026-10-13

## Esito e ambito

**Eseguite acquisizioni reali ripetute, confronto sui medesimi input e prova
6DoF nel driver SteamVR già configurato. Risultati statici positivi nell'attuale
posa frontale con cinque LED; nessuna accettazione generale del tracking.**

Staticità dichiarata dall'utente («PSVR acceso e fermo davanti a te»), coerente
con immagini e segnali acquisiti. Nessuna reference esterna di posizione,
movimento controllato, cambio di distanza/angolo, occlusione fisica o osservazione
diretta dell'OLED nel visore. Millimetri nel modello geometrico esistente:
scala assoluta e pivot anatomico rimangono non validati.

Calibrazione PS Eye, rig, preset **60/6/13/0**, fusione/orientamento IMU e
parametri dei filtri non modificati. Aggiunti soltanto strumenti di acquisizione
e analisi statica. One Euro resta il default del launcher.

## Operazioni e risorse

- Preflight: nessun tracker, diagnostico o SteamVR in esecuzione. Camera
  disponibile; 150 frame iniziali senza LED rilevati.
- La prima lettura passiva F0 riportava `power=off`, ma il successivo drenaggio
  degli eventi accodati ha riportato `power=on`. Il comando power-on ha ricevuto
  A0 accepted senza nuova F0 di conferma (exit 6); **non è stato ripetuto** né
  interpretato come prova di cambiamento dello stato. Nessun reset hardware.
- Comando LED già previsto dal progetto: mask 0x1ff, intensità 100, accepted.
  Effetto verificato dalla camera: cinque LED frontali tracciati. Prova breve
  292/297 pose valide, ~74,3 fps, nessun errore di lettura.
- Tre acquisizioni camera+IMU diagnostica da 60 s, con una sola calibrazione
  iniziale di 8 s; tre acquisizioni puramente ottiche da 60 s.
- Verificate **impostazioni live**, non le sole proposal: driver registrato
  `out/driver_psvrpc_6dof/psvrpc`, posizione/export IMU abilitati, grace 80 ms,
  timeout 200 ms, display configurato 90 Hz. SteamVR avviato normalmente senza
  cambiare impostazioni, registrazione, display o compositor.
- Due acquisizioni driver di 95 e 65 s; tre filtri selezionati nello stesso
  processo/capture, mantenendo A, sessione/epoca e orientamento: One Euro,
  Kalman iniziale, Kalman ottimizzato. Nessun recenter o ricalibrazione manuale.
- Camera rilasciata senza abbandono del reader, diagnostico chiuso con exit 0,
  collector nativo terminato; SteamVR chiuso normalmente con `vrmonitor://quit`.
  Nessun processo camera/diagnostico/runtime/probe rimasto attivo al controllo.
  Nessun comando power-off o interruzione dell'alimentazione USB.
- Blocchi live `driver_psvrpc` e `power` verificati invariati prima/dopo:
  posizione/export/grace/display e `powerOffOnExit=false` conservati.

I nomi delle directory sono identificatori di prova: per gli orari usare
`started_utc/ended_utc` dei relativi `capture_summary.json`.

## Acquisizioni

| Serie | Frame | Pose ottiche valide | Letture fallite | Frequenza osservata |
|---|---:|---:|---:|---:|
| Camera + IMU diagnostica, 3×60 s | 11.945 | 11.424 | 0 | 66,2–66,5 fps |
| Solo ottica, 3×60 s | 13.504 | 13.498 | 0 | ~75 fps |
| Driver, 95 s | 7.121 | 7.116 | 0 | 74,97 fps |
| Driver + pose OpenVR, 65 s | 4.873 | 4.870 | 0 | 74,98 fps |

37.443 frame nelle serie principali; nessun hash immagine consecutivo ripetuto.
Gli invalidi iniziali sono warming/calibrazione/aggancio, non perdite statiche
successive. Dopo calibrazione, le serie diagnostiche hanno IMU `tracking`;
le due serie driver hanno IMU `tracking` per tutti i frame acquisiti.

La pipeline diagnostica usa output IMU 50 Hz e attesa di interpolazione; ha
campionato meno frame (p99 intervallo ~40 ms), senza timeout camera. Con export
driver a 100 Hz, camera e pipeline raggiungono ~75 fps, p99 ~14,2 ms. Non
attribuire il minor campionamento diagnostico a errori USB o alla Q del filtro.

## Confronto deterministico: stessa sequenza per tutti i filtri

Filtri applicati in replay ai **medesimi input esatti e timestamp reali**. Esclusi
i primi 10 s di ogni log dall'analisi, ma conservata la storia precedente per
inizializzare i filtri. Misurate std XYZ/depth ottica, std detrended, deriva,
picco-picco, passi, NIS, disponibilità e RMS in bande temporali. Resampling usato
solo per la diagnostica FFT, mai per l'input dei filtri. Confronto aggiuntivo sul
medesimo insieme di frame validi per evitare vantaggi da esclusione di dati.

### Profondità camera — deviazione standard in mm

| Acquisizione | Grezza | One Euro 2,5/8 | Kalman iniziale | Ottimizzato |
|---|---:|---:|---:|---:|
| Guidata 1 | 0,573 | 0,294 | 0,444 | **0,269** |
| Guidata 2 | 0,396 | 0,174 | 0,298 | **0,152** |
| Guidata 3 | 0,255 | 0,091 | 0,179 | **0,072** |
| Solo ottica 1 | 0,245 | 0,079 | 0,157 | **0,055** |
| Solo ottica 2 | 0,265 | 0,084 | 0,168 | **0,059** |
| Solo ottica 3 | 0,374 | 0,119 | 0,242 | **0,081** |
| Driver 95 s | 0,623 | 0,266 | 0,451 | **0,237** |
| Driver 65 s | 0,266 | 0,105 | 0,188 | **0,090** |

Ottimizzato `camera` e `driver_hold` coincidono nei tratti senza predizione.
Nelle tre serie guidate le disponibilità post-warm-up coincidono fra filtri.
Nei log live, i marcatori di cambio filtro resettano anche i replay: sei frame
aggiuntivi invalidi nei Kalman corrispondono alle conferme ai due switch, non a
misure scartate durante quiete continua. Gli snapshot nuovi preservano la
provenienza reale, senza ricostruzioni legacy.

### Rumore rapido vs deriva

| Guidata | Std detrended One Euro | Iniziale | Ottimizzato | Deriva ottimizzata |
|---|---:|---:|---:|---:|
| 1 | 0,187 mm | 0,381 mm | **0,138 mm** | +0,0160 mm/s |
| 2 | 0,138 mm | 0,278 mm | **0,105 mm** | +0,00768 mm/s |
| 3 | 0,083 mm | 0,175 mm | **0,061 mm** | +0,00259 mm/s |

La deriva lenta non viene azzerata: andamento simile nei tre filtri. Nella
prima serie ~0,96 mm/min ottimizzato, vs ~0,95 mm/min iniziale. Questo è un
risultato negativo da conservare: il miglioramento del rumore non risolve bias
geometrico, deriva IMU o eventuale movimento minimo non misurato.

Rotazione IMU relativa (vettore Rodrigues nel riferimento iniziale, non una
nuova fusione): deriva sull'asse dominante ~**0,287 /0,292 /0,154 °/min** nelle
tre serie, std ~0,071 /0,071 /0,039°. Altri assi ~0,006–0,041 °/min. Dati coerenti
con il gate statico precedente di 1 °/min, ma senza reference non separano
perfettamente deriva del sensore e micromovimento. Nessun filtro orientamento
aggiunto. L'accoppiamento orientamento/solver di traslazione resta diagnosticabile.

NIS, distribuzioni per LED/modalità e spettri completi nei JSON: l'incertezza
locale risulta conservativa rispetto al rumore breve osservato; non è provata
una sottostima generalizzata di R né una precisione assoluta pari alla std.

## Driver/OpenVR effettivo — non solo replay

Probe C++ esistente, Utility read-only, HMD esatto `PSVRPC-HMD-0`, universo raw
and uncalibrated, prediction=0, circa 10 Hz. Nessuna presenza sintetica, modifica
dashboard/standby o comando di recenter. Scartati 5 s dopo ogni cambio filtro.
Le fasi sono successive, quindi **non** hanno gli stessi campioni: il replay
sopra è il confronto rigoroso; queste letture verificano il consumer reale.

| Filtro live | Pose valide/campionate | Std XYZ OpenVR (mm) | Std asse camera | Picco-picco depth |
|---|---:|---:|---:|---:|
| One Euro 2,5/8 | 148/148 | 0,019 /0,049 /0,133 | 0,141 mm | 0,722 mm |
| Kalman iniziale | 149/149 | 0,036 /0,049 /0,164 | 0,169 mm | 0,740 mm |
| Ottimizzato hold | 197/197 | 0,020 /0,032 /0,075 | **0,080 mm** | **0,347 mm** |

Asse ottico trasformato tramite A e `world_rotation_wxyz`, non assunto uguale
a Z OpenVR. Tempo del probe: ricezione stdout, non sincronizzazione esatta
GPU/sensore. Nessuna misura di latenza motion-to-photon ricavabile dalla quiete.

### Telemetria compositor nelle stesse finestre stabili

| Filtro | Campioni | Raw/compositor validi | Fullscreen/can-render | Cambi fade alpha >0,1 |
|---|---:|---:|---:|---:|
| One Euro | 1.415 | 1.415/1.415 | 1.415/1.415 | 0 |
| Iniziale | 1.426 | 1.426/1.426 | 1.426/1.426 | 0 |
| Ottimizzato hold | 1.906 | 1.906/1.906 | 1.906/1.906 | 0 |

GetLastPoses error=0, grid alpha=0 nelle finestre. Startup senza camera e
shutdown/invalidazione intenzionale sono registrati ma esclusi dal conteggio
di perdite spontanee. **Nessun indicatore di flash in queste finestre**; non
equivale a ispezione dell'OLED, assenza di flash locali/sub-frame o soluzione
dei problemi durante movimento/dropout. Il grace non è stato stressato con
occlusioni fisiche: durante la quiete non si sono verificate perdite spontanee.

## Artefatti e riproduzione

Strumenti nuovi, usando detector/tracker/fusion/probe già esistenti:
`tools/psvr_camera/static_tracking_trial.py`, `analyze_static_trial.py`.
Standalone: pacchetti interni locali dichiarati **non v2** e nessun UDP.
Modalità driver esplicita: metadata bound genuini, sender/validator esistenti.

- [confronto sei serie sensori](../logs/static_sensor_comparison_20261013.json)
- [confronto driver 95 s](../logs/static_driver_comparison_20261013.json)
- [pose OpenVR e telemetria](../logs/static_runtime_pose_analysis_20261013.json)
- Raw/snapshot/summary: `logs/static_guided_20261013_1846/`,
  `logs/static_optical_20261013_1853/`, `logs/static_driver_20261013_2000/`,
  `logs/static_driver_pose_20261013_2010/`.
- Telemetry raw: `static_runtime_telemetry_20261013_2000.jsonl` e `_2010.jsonl`.
  Log dei comandi stato/LED: `psvr_diag_20261013T163710.047Z_23560.log`,
  `psvr_diag_20261013T163810.427Z_3600.log`, `psvr_diag_20261013T163859.075Z_9020.log`.

Gli hash di preset/intrinseci/rig sono identici tra le acquisizioni e riportati
in ogni summary. Default/ottimizzato preservati, nessuna promozione automatica.
Regression mirate: **121 PASS, 105 subtest PASS**, inclusi cinque nuovi test degli
strumenti statici (spettro/deriva, determinismo, asse inclinato, assenza di GT
inventata e divieto di pubblicazione v2 dai modi standalone).

```powershell
# SteamVR deve essere chiuso: diagnostico è owner IMU esclusivo.
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.static_tracking_trial `
  --mode diagnostic --seconds 60 --repeats 3 --output-dir logs\NEW_static_diag
# Solo camera, nessun HID/UDP:
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.static_tracking_trial `
  --mode optical --seconds 60 --repeats 3 --output-dir logs\NEW_static_optical
# Solo quando il driver reale è già attivo/configurato; nessun cambio settings:
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.static_tracking_trial `
  --mode driver --publish-position --capture-runtime-poses --seconds 65 `
  --live-phase-seconds 20 --repeats 1 --output-dir logs\NEW_static_driver
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.analyze_static_trial `
  logs\NEW_static_driver\trial_01.jsonl --runtime-poses logs\NEW_static_driver\runtime_poses.jsonl `
  --out logs\NEW_static_analysis.json
```

Output nuovi/esclusivi, nessun overwrite. Prove ancora necessarie: quasi-quiete
indossata, altri angoli/distanze, LED parziali, dropout/riaggancio fisici, risposta
al movimento noto e osservazione nel visore. **Statico nell'attuale posa
verificato; non dichiarati risolti jitter durante uso, salti o flash generali.**
