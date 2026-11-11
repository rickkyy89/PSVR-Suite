# Collegamento e verifica milestone 1/2

## Collegare PSVR1

1. Usare il **processor unit Sony** del PSVR1 con il suo alimentatore.
2. Collegare i cavi del visore alle porte anteriori del processor unit,
   utilizzando l'eventuale prolunga/adattatore originale della propria revisione.
3. Collegare la porta micro-USB posteriore del processor unit a una porta USB
   del PC con un cavo **dati**, preferibilmente senza hub per la prima prova.
4. Per M1 HDMI non è necessario. Se già collegato, GPU HDMI OUT → porta
   **HDMI PS4** del processor unit; la porta **HDMI TV** è uscita opzionale verso
   TV/monitor, non ingresso dal PC. Non collegare il visore direttamente alla GPU.
5. M1 originaria: HID su 4/5. Da M2: **HID su 4, WinUSB su 5**
   ([procedura](docs/WINUSB_CONTROL.md)). Il tool supporta entrambe le combinazioni
   per capture passiva; comandi M2 richiedono WinUSB. Chiudere Trinus/iVRy,
   altri tool PSVR e SteamVR per evitare letture concorrenti.
6. Se il visore è in standby, provare prima il diagnostico; se non arrivano
   report, accenderlo con il suo pulsante fisico e ripetere. Il tool M1 non
   invia power-on/VR mode e non richiede PS4 o PS Camera.

Identificazione verificata dal sorgente: Sony VID **054C**, PID **09AF**,
MI_04 sensori, MI_05 controllo. Non distingue con certezza CUH-ZVR1/ZVR2
dal solo VID/PID: stampare stringa prodotto e USB release, non inventare modello.

## Esecuzione

Dopo la build, dalla root:

```powershell
.\out\diag\psvr_diag.exe --config .\config.json
$LASTEXITCODE
```

Per una capture più lunga e raw limitati:

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --duration-ms 5000 --raw-packets 3
```

Percorsi relativi di log risolti rispetto alla directory corrente. Il tool
senza `--config` usa default integrati, non carica silenziosamente altri file.
Configurazione/CLI non valida si ferma prima di aprire hardware.

## Output atteso (esempio, non risultato presunto)

```text
PSVR detected VID=054c PID=09af
USB interface OK (PnP enumeration; no raw USB claim)
sensor interface OK interface=4
control interface OK interface=5
sensor packet OK length=64 ...
sensor summary reports=... valid64=... report_hz=... samples_hz_estimate=...
control summary reports=0 ... (passive timeout is allowed)
MILESTONE 1 PASS ...
shutdown exit_code=0
```

I metadati PnP/HID completi precedono il riepilogo. Il controllo viene aperto;
se il descriptor espone input, viene letto passivamente con timeout. Sul PSVR
testato **non espone input**, quindi la lettura viene saltata e loggata.
Zero report controllo non è errore. Il tool non afferma
che power/mode siano funzionanti. Ogni log ha nome unico timestamp/PID.

Exit code passivo: **0** report sensore 64 byte + sensori 4/controllo 5 aperti; **2** PSVR assente;
**3** interfacce mancanti/open/lettura fallita o più PSVR ambigui;
**4** interfacce aperte ma nessun report sensore valido; **1** configurazione,
CLI, inizializzazione, enumerazione o log falliti; **130** Ctrl+C.
`--enumerate-only` termina
0 se PSVR presente, 2 se assente, e non prova l'apertura/lettura. Con più
MI_05 completa comunque i metadati e i riepiloghi PnP/HID, senza acquisire
ownership. Capture/comandi ambigui terminano invece 3 prima dell'apertura.

## Checklist hardware

- [x] Eseguire collegato e conservare log, exit code e output console (sessione 2026-10-03).
- [x] Scollegare **solo USB**, eseguire: deve terminare 2 senza crash (verificato).
- [x] Ricollegare USB, ripetere: apertura/lettura devono riprendersi (verificato).
- [ ] Scollegare USB durante capture di 5 s: errore reale o fine capture con
  dato disponibile, nessun crash/blocco; non avanzare se shutdown è problematico.
- [ ] Dopo le correzioni del blocco 2, ricollegare USB e ripetere sul nuovo
  binario: sensore e controllo devono riaprirsi e produrre report validi.

Le prove che richiedono un gesto fisico devono essere annotate dall'utente.
I test software non sostituiscono scollegamento/replug reale.

Per queste ultime due voci usare una sessione senza altri owner PSVR e il
binario appena compilato: capture passiva con `--duration-ms 5000`,
`--config .\config.json` e un nuovo `--log-dir`. Scollegare solo USB dopo
l'avvio della capture; conservare log ed exit code. Dopo replug rieseguire
la stessa capture in una nuova directory di log e verificare report validi
e chiusura degli handle. Queste azioni fisiche non sono state eseguite dai
test automatici del blocco 2.

## Test software

`build.ps1` esegue CTest. Test automatici: fixture binarie del decoder
(64 byte, due timestamp, int16 signed, sequenza, rifiuto lunghezze errate),
help, CLI sconosciuta/config mancante, configurazione non valida,
fixture di selezione interfacce (assenza, incompletezza, ambiguità).
Nessuno di questi test invia comandi o necessita SteamVR.

### Regressioni blocco 2 — solo software

| Test CTest | Copertura |
|---|---|
| `diag_consumer_offline` | 41 scenari sul `main.cpp` reale: inventario completo e guard pre-open, sette azioni M2 senza sensore, capture/IMU con sensore obbligatorio, correlazione A0/F0, drain/stale, errore I/O, interruzione, CLI/dry-run |
| `diag_windows_backends_offline` | 53 scenari sui sorgenti PnP/WinUSB reali con API Windows simulate: registry/GUID/path, selezione non ambigua, descriptor/pipe, timeout/errori/scrittura parziale e cleanup dopo errori del costruttore |
| `device_session_offline` | Quattro casi del guard reale su nomi mutex isolati per processo: concorrenza, nomi distinti, riacquisizione, proprietario abbandonato |

I primi due test intercettano le chiamate operative ai device. Non inviano
byte all'hardware. Il terzo usa mutex Windows reali ma non il nome di
produzione. I fake del backend sono confinati alle translation unit dei test,
con tipi e header SDK reali; non vengono linkati nel diagnostico.

```powershell
cmake --preset vs2022
cmake --build build/vs2022 --config Release --target psvr_diag psvr_diag_tests psvr_diag_consumer_tests psvr_diag_windows_backend_tests psvr_device_session_tests
ctest --test-dir build/vs2022 -C Release -R "^(diag_core|diag_consumer_offline|diag_windows_backends_offline|device_session_offline)$" --output-on-failure
```

La suite simulata non sostituisce la prova fisica di scollegamento durante
capture e successiva riapertura dopo replug della checklist hardware.

### Regressioni blocco 3 — IMU e prediction

| Test CTest | Copertura |
|---|---|
| `imu_capture_offline` | 21 scenari sulla capture reale con clock monotono deterministico e HID simulato: jitter ripetuto, limiti inclusivi 100/250 ms e scadenza oltre soglia, buffered report, startup/calibrazione, duplicati/malformati isolati o persistenti, brevi letture vuote, polling lento e baseline/recenter del profilo assi |
| `imu_velocity_geometry` | 16 traiettorie: neutro pitchato o pitch/roll, full/yaw, smoothing 0/50 ms, yaw puro/moto misto, tre recenter ripetuti; confronto con derivata mondo del quaternion e trasporto EMA |

La capture usa il clock reale in produzione; l'overload con clock fornito
permette di verificare esattamente i confini senza sleep o flakiness da
scheduler. Il test non apre HID/WinUSB; gli eventi IPC sono del solo processo
di test e i CSV vanno in una sottodirectory univoca della build.
I test geometrici richiedono `PSVR_BUILD_DRIVER=ON` per gli header OpenVR,
senza SteamVR attivo o hardware. Non eseguire target package/install per
queste verifiche.

```powershell
cmake --preset vs2022
cmake --build build/vs2022 --config Release --target psvr_imu_capture_tests psvr_imu_velocity_tests psvr_imu_tests psvr_driver_imu_export_tests
ctest --test-dir build/vs2022 -C Release -R "^(imu_capture_offline|imu_velocity_geometry|imu_math_fusion|driver_imu_export_contract)$" --output-on-failure
```

I timeout rimangono configurabili: con 100 ms, 99 e 100 ms sono ammessi,
101 ms scade; con 250 ms, 180 e 250 ms sono ammessi, 251 ms scade.
Un singolo duplicato/malformato o due letture vuote non fanno perdere il
tracking entro la finestra. Le prove fisiche B3-HW-01/02 restano salvate in
`docs/MANUAL_VALIDATION_QUEUE.md` §2 in attesa dell'utente.

### Regressioni blocco 4 — camera e backend

- Python: `test_camera_bounded_calls.py` verifica open/read/close OpenCV
  bloccati, cleanup sul thread proprietario, proprietà timeout non supportate
  e callback sul chiamante. La deadline della capture limita le operazioni
  backend; un callback utente deve restituire cooperativamente. È previsto
  al massimo un ulteriore wait di cleanup di 50 ms. `cleanup_pending=true`
  significa che non è stata confermata la chiusura entro quel limite; non
  viene liberato un handle sotto una read ancora viva.
- `test_camera_pseye_failures.py`: errori di allocazione/startup e close,
  configurazione invalida prima dell'open, eccezioni grab/conversione,
  consumer svegliato allo stop e grab transitorie ancora tollerate.
- `test_camera_latest_frame.py`: tutti i frame consumati → zero drop;
  backlog non consumato → conteggio delle sole sostituzioni effettive.
- CTest standalone `pseye_transfer_offline`: 8 casi sui corpi reali della
  patch, libusb simulata, lifetime/contatori/slot e cancellazione concorrente.
- CTest standalone `pseye_inventory_offline`: contesto C API reale estratto,
  enumerazione simulata, transizioni plug/unplug, riapertura a riposo e
  identità/indici conservati durante una sessione attiva.
- `pseye_upstream_patch_guard`: loader dello shim reale contro una DLL legacy
  di test; rifiuto verificato prima di init, senza enumerazione USB.

```powershell
$env:PSVR_PSEYE_HARDWARE = "0"
python -m pytest tests/test_camera_bounded_calls.py tests/test_camera_capture.py tests/test_camera_latest_frame.py tests/test_camera_pseye.py tests/test_camera_pseye_failures.py tests/test_camera_pseye_controls.py tests/test_suite_camera_source.py tests/test_camera_cli.py tests/test_suite_tracking.py -q
cmake -S tools/psvr_pseye -B build/pseye-review -DBUILD_TESTING=ON
cmake --build build/pseye-review --config Release --target psvr_pseye_transfer_tests psvr_pseye_inventory_tests psvr_pseye psvr_pseye_legacy_fixture
ctest --test-dir build/pseye-review -C Release --output-on-failure
```

Coperti anche in CI dal job `pseye-offline` (`.github/workflows/ci.yml`),
stessa configure standalone del sotto-progetto (B14-02).

I test nativi richiedono Python 3.11+ e il sorgente pinned (recuperato da
CMake o fornito via `FETCHCONTENT_SOURCE_DIR_PS3EYEDRIVER`), ma non una camera
né libusb per il link: sono test di API/lifetime con dipendenze simulate.
Per costruire la DLL upstream reale servono anche gli input libusb indicati
in `tools/psvr_pseye/README.md`. Lo shim rifiuta una DLL upstream vecchia,
prima dell'inizializzazione, tramite il marker patch-level 3.
Le prove fisiche B4-HW-01/02 restano in `docs/MANUAL_VALIDATION_QUEUE.md` §8.

### Regressioni blocco 5 — geometria e riconoscimento ottico

La baseline laterale accettata è `constellation_led_v1`: conservare minimo
tre LED, riferimento mondo fisso e correzione ottica incrementale. Un
controllo di ambiguità deve coprire sia `match_pattern` sia l'`update` esterno
con frontale debole; non basta testare il solo fallback legacy.

Il gruppo numerico deve includere calibrazioni con viste incompatibili,
dimensioni delle immagini scartate/utili, K non triangolare/singolare,
ambiguità fisica planare e round-trip Rodrigues vicino a 180°. La validazione
planare è distinta dal riconoscimento LED non planare della suite: non
trasformare il disaccordo tra ipotesi in una nuova soglia di rotazione testa.

Per il detector white-core verificare risultati equivalenti e costo delle
dilatazioni indipendente dal numero di nuclei; i tempi locali non sono un
criterio portabile di PASS. Per il matcher preservare inoltre gli sweep
frontale/laterale, clutter, IMU stale, sessione/recenter e rifiuto di prior
incoerenti, senza ridurre la tolleranza alle perdite brevi già prevista.

```powershell
$env:PSVR_PSEYE_HARDWARE = "0"
python -B -m pytest tests -k "camera or suite or lateral_handoff" -q --tb=short
python -B -m tools.psvr_suite.app --headless-check
```

I test offline e i replay dei candidati non verificano le ROI sul detector
live, la latenza end-to-end né gli scatti nelle lenti. La prova B5-HW-01
resta salvata in `docs/MANUAL_VALIDATION_QUEUE.md` §9; non richiede una nuova
scacchiera come prerequisito per la correzione software.

### Regressioni blocco 6 — stima e filtraggio posizione

Copre loader/config (`ValueError` su sezioni non-dict e chiavi ignote),
gate di contesto One Euro, default `context_valid` non circolare, `_last`
su `invalid`, diagnostica `missing_covariance`/anti-alias, `ValueError` su
pacchetti incompleti, reseed Kalman senza `reason` stantio né doppio
conteggio, validazione covarianza/`det==1`, `scale`/label non validi.
Nessun cambio a soglie, profili o tolleranze di tracking.

```powershell
$env:PSVR_PSEYE_HARDWARE = "0"
python -B -m pytest tests/test_camera_position_filter.py tests/test_camera_pipeline.py tests/test_camera_position_pipeline.py tests/test_camera_position_pipeline_b6.py tests/test_camera_position_replay.py tests/test_camera_pose_uncertainty.py tests/test_camera_pose_uncertainty_b6.py tests/test_camera_pose_estimator.py tests/test_camera_pose_estimator_b6.py tests/test_camera_kalman_adaptive.py tests/test_camera_kalman_budget.py tests/test_camera_kalman_onset.py tests/test_camera_kalman_recovery_fit.py tests/test_camera_estimator.py -q --tb=short
```

I replay registrati e le fault injection non misurano jitter statico
certificato né latenza end-to-end. B6 non richiede alcuna prova fisica:
sono difetti di contratto e diagnostica riproducibili offline.

### Regressioni blocco 7 — ponte camera–IMU–driver

Copre `latest()` diagnostico e validazione quaternioni/argomenti,
interpolazione solo stessa sessione/epoca, reset identità anche v1 legacy,
`inliers` unsigned senza eccezioni, except morto rimosso, probe dichiarato
v1-only. Build/check nativi solo in directory temporanea, mai nel repo.

```powershell
$env:PSVR_PSEYE_HARDWARE = "0"
python -B -m pytest tests/test_camera_driver_python.py tests/test_camera_driver_link.py tests/test_camera_position_replay.py tests/test_camera_driver_imu_b7.py tests/test_camera_imu_diagnostic_b7.py tests/test_suite_tracking.py tests/test_camera_protocol.py -q --tb=short
```

B7 non richiede alcuna prova fisica: il P0 era confinato al trial
diagnostico in modo driver (suite live immune), il resto è offline.

### Regressioni blocco 8 — ciclo di vita e pubblicazione HMD

Copre macchina a stati lifecycle + gate in-flight Hmd e frame Provider,
mailbox worn con ripristino diretto, latch display selettivo, rollback prima
del log con catch-all ai confini SDK, `stop_` sotto mutex, 3 assert package.
Percorso pose byte-identico: nessuna misura di posa è cambiata. Build/check
nativi solo in directory temporanea, mai installazione/registrazione reale.

```powershell
powershell -File tests\test_driver_scripts.ps1
cmake --preset vs2022
cmake --build build/vs2022 --config Release
ctest --test-dir build/vs2022 -C Release --output-on-failure
```

B8 non richiede alcuna prova fisica: interleaving concorrenti verificati
con mock/harness offline; nessuna garanzia SteamVR aggiuntiva affermata.

### Regressioni tasti visore — decoder + forwarding (2026-10-18)

Decoder e forwarding implementati e verificati (dettagli in
`docs/HEADSET_BUTTONS_20261018.md`).

Decoder (`tools/psvr_diag/sensor_packet.hpp`) e forwarding
(`driver/headset_buttons.hpp`, `driver/headset_audio.hpp`,
wiring in `driver/driver_psvrpc.cpp`) implementati il 2026-10-18. Casi su
frame sintetici (byte 0: 2/4/8, byte 2, byte 8 bit3) in `diag_core` e
sequenze edge/debounce in `headset_buttons_offline`:

```powershell
cmake --preset vs2022
cmake --build build/vs2022 --config Release --target psvr_diag_tests psvr_headset_buttons_tests driver_psvrpc
ctest --test-dir build/vs2022 -C Release -R "^(diag_core|headset_buttons_offline)$" --output-on-failure
```

Verificato 2026-10-18 in build temp: `diag_core` PASS,
`headset_buttons_offline` PASS (12 asserzioni), `driver_psvrpc` compila.
La prova fisica resta in `docs/MANUAL_VALIDATION_QUEUE.md` §13 e non è
sostituita da alcun test offline.

### Regressioni blocco 9 — display e presentazione video

Copre helper `native_desktop_absence_verified` + contratto gate nativo,
reason device-removed nel catch presenter, gate/lease Redirect e indice
dopo proprietà, snapshot atomico, writer con fingerprint, stale esplicito,
guard disarmato nel boundary test. Esecuzioni native solo in directory
temporanea; script solo su fixture temp, mai display/settings reali.

```powershell
powershell -File tests\test_capture_refresh_baseline_snapshot.ps1
powershell -File tests\test_set_trial_refresh_merge.ps1
powershell -File tests\test_display_core_stale_unknown.ps1
cmake --preset vs2022
cmake --build build/vs2022 --config Release
ctest --test-dir build/vs2022 -C Release --output-on-failure
```

Validazione finale su HMD reale per direct nativa e device-lost rinviata a
futura sessione hardware (non bloccante); tutto il resto è offline.

### Regressioni blocco 10 — ottica e proiezione

Copre validazione tipi restore, readiness con pose, timeout grid, journal
atomico, certificazione forward e bordo strict-only, ciclo apply/restore,
grid PNG deterministica, boundary EDID, minori pattern. Nativi solo in
directory temporanea; fixture sintetiche, mai HMD reale.

```powershell
$env:PSVR_PSEYE_HARDWARE = "0"
python -B -m pytest tests/test_reference_edid.py tests/test_optics_trial.py tests/test_optics_trial_types.py tests/test_optics_trial_journal_b10.py tests/test_optics_trial_cycle.py tests/test_optics_grid.py tests/test_optics_visual_trial.py tests/test_patterns.py -q
ctest --test-dir build/vs2022 -C Release -R "distortion|optical|optics" --output-on-failure
```

Validazione ottica su HMD resta alla sessione hardware già in coda (§12
B9); i fix qui sono tutti offline.

### Regressioni blocco 11 — orchestrazione sessioni

Copre reap handle e registry worker con shutdown bounded, abort
preemptivo, wrap eccezioni e flag waiting, race `_thread`/start, setup
con dry-run, guard shortcut, lock produttori, timeout validato, fail-fast
smoke. Solo mock e fixture temp, mai hardware/SteamVR/sistema.

```powershell
$env:PSVR_PSEYE_HARDWARE = "0"
python -B -m pytest tests -k suite -q
```

Nessuna prova hardware richiesta (smoke resta manuale per design, senza
nuove voci di coda).

### Regressioni blocco 12 — interfaccia utente e launcher

Copre wrapper seriale non bloccante, destroy sul main, pin console posix,
azioni UI e matrice prefs (solo test), drain in `finally`, coalescing
ultimo frame, catalogo senza timestamp, alias tab, minori. Tutto headless
con mock, mai Tk/display reale.

```powershell
$env:PSVR_PSEYE_HARDWARE = "0"
python -B -m pytest tests -k suite -q
```

Nessuna prova hardware richiesta (Tk reale solo manuale futura, non
bloccante).

### Regressioni blocco 13 — diagnostica, telemetria e analisi

Copre streaming con skip, exit 2 puliti, root validata, cap VDF/depth,
sanitize inventory, `--out` atomico, bound PE, cap frames. Solo mock e
fixture temp, mai hardware/sistema/rete.

```powershell
$env:PSVR_PSEYE_HARDWARE = "0"
python -B -m pytest tests/test_monitor.py tests/test_native_transition_summary.py tests/test_runtime_probe_flash_pacing_cli.py tests/test_app_readiness.py tests/test_app_readiness_b13_06.py tests/test_audio_diag.py tests/test_inspect_visibility_call.py tests/test_camera_kalman_benchmark.py tests/test_camera_position_replay.py -q
```

Nessuna prova hardware richiesta (tutto offline con mock).

### Regressioni blocco 14 — build, distribuzione e manutenzione

Coperto in CI dai job `pseye-offline` (sotto-progetto PS Eye, test
`pseye_*`) e `build-ps1-offline` (`.github/workflows/ci.yml`): quest'ultimo
lancia `scripts/build.ps1` end-to-end con i default (VS2022/RelWithDebInfo),
quindi configure, build, intero CTest e `cmake --install` con prefix dentro
il repo (`out/diag`), più verifica read-only del layout installato
(`psvr_diag.exe`, `config.json`, `licenses/`). Fallisce se i test falliscono
(ordinamento test-prima-di-install) o se l'install non produce il layout;
nessuna registrazione driver, nessun deploy/publish.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1
python -B -m pytest tests/test_license_collection.py tests/test_release_preparation.py -q
powershell -File tests\test_driver_scripts.ps1
```

Stessi default del wrapper `.\build.ps1` di root e del comando in
`BUILD.md`. La variante MinGW (`-Toolchain MinGW -ToolchainRoot <path>`)
resta manuale: richiede un toolchain locale non presente su `windows-2022`.
I 4 PS1 ex-orfani sono registrati in CTest; coerenza dict↔requirements
pinnata da `test_runtime_versions_match_requirements_pins`; SKIP espliciti
se numpy/pytest assenti; primi run dei nuovi job CI da osservare.

## Milestone 2 — comandi e verifica

La sessione M2 è stata autorizzata dopo M1. Con WinUSB solo su MI_05:

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --control-info
.\out\diag\psvr_diag.exe --config .\config.json --power-off
.\out\diag\psvr_diag.exe --config .\config.json --power-on
.\out\diag\psvr_diag.exe --config .\config.json --vr-mode
.\out\diag\psvr_diag.exe --config .\config.json --cinema-mode
```

Un comando alla volta, controllando exit code/log. L'apertura non invia comandi.
Le azioni M2 richiedono MI_05 WinUSB sana e non ambigua; non richiedono e
non aprono MI_04. Capture passiva e IMU richiedono invece il sensore MI_04 HID.
Power-on non abilita automaticamente VR; mode non accende il visore.
Per la prima sequenza partire dal visore acceso, spegnerlo, accenderlo e
cambiare modalità: queste transizioni sono state verificate sul PC reale.

Output atteso dopo un cambio VR:

```text
command requested action=vr-mode full_usb_packet=23 00 aa 04 01 00 00 00
command transfer OK bytes=8 state_verified=false (waiting for fresh F0 status)
command response result_code=0 (0=accepted; not a display-state verification)
control status power=on mode=vr ... vr_active=1 ...
MILESTONE 2 action verified: vr-mode (accepted A0 response + fresh F0 state after successful transfer)
shutdown exit_code=0
```

Controlli reali: power-off mask 0x64; power-on 0x15; VR 0x51/active=1;
cinema 0x55/active=0. Altri bit cambiano con cuffie/mute/prossimità:
verificare bit documentati, non l'uguaglianza del mask intero.

**5** = WinUSB MI_05 necessario, comando non inviato. **6** = ACK/stato non
confermati entro `control_timeout_ms` (default 3000, override CLI fino a 15000).
**3** = errore di interfaccia/I/O o risposta A0 negativa. Nessun retry automatico.

`--status --control-timeout-ms 10000` aspetta passivamente un evento F0;
indossare/togliere il visore durante l'osservazione può generarne uno.
Senza eventi ritorna 6 con stato sconosciuto. Un comando già soddisfatto può
restituire ACK senza nuovo F0: non è successo di verifica dello stato.
`--power-on --dry-run` stampa il pacchetto senza aprire USB. Azioni multiple
o abbinate a `--enumerate-only` sono rifiutate prima dell'hardware.

CTest ora esegue **9 test**: aggiunti wire packets, F0 reale/malformato, A0,
criterio ACK+stato, conflitti CLI e dry-run senza USB.

- [x] Cambio driver limitato a MI_05, controllato via PnP.
- [x] Power-off/on e VR/cinema con ACK e F0 dopo il comando.
- [x] Capture passiva dopo i cambi: 2002 report validi in 2 s.
- [ ] Ripristino fisico MI_05 a HID (procedura documentata).

**Fermarsi dopo M2.** Prossima sessione: M3 IMU/orientamento e recenter.

## Milestone 3 — IMU/fusion + monitor (IN_PROGRESS, rinvio docs)

M3 non è completa. Procedure, assi, gate e comandi non duplicati qui;
riferimenti canonici (altro agent):

- [docs/IMU_TESTING.md](IMU_TESTING.md) — capture `--imu`, `--axis-test`,
  `--stationary-test`, `--json-stream`, IPC `--recenter/--recalibrate/--stop-imu PID.
- [docs/MONITOR_APP.md](MONITOR_APP.md) — GUI, API readonly, grafici/state.
- [docs/MILESTONE_3_REPORT.md](MILESTONE_3_REPORT.md) — file/comandi/risultati/
  fallimenti/fix/gate di questo agent.

Sintesi: cal 8 s tavolo hands-off dopo 200 ms; build MinGW 14/14 CTest;
statico 60 s originale PASS pre-gate; statico post-fix 42000 ms PENDING.
