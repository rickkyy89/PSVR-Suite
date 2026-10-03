# Report milestone 3 — 2026-10-03 — IN_PROGRESS (non completa)

Stato: **M3 IN_PROGRESS, non COMPLETE. M4 non iniziata.**
Pendenti: verifica fisica post-fix Yaw/Pitch/Roll e confronto controllato di
presenza. Statico post-fix e IPC reale sono verificati. Nessuna affermazione su
stereo/display/SteamVR.

## 1. Cosa implementa M3

- Matematica/fusion C++17 originale: `tools/psvr_diag/orientation_math.hpp`,
  `tools/psvr_diag/imu_fusion.hpp`, `tools/psvr_diag/imu_capture.{hpp,cpp}`,
  CLI in `tools/psvr_diag/main.cpp`.
- Fusion propria: piccolo quaternione complementare con correzione gravità.
  Fatti asse/unità riusati da OpenHMD + decoder BSL originale; matematica/fusion
  nuova **MIT indipendente**, nessuna dipendenza OpenHMD-lib/Madgwick.
- Default calibrazione: **8 s immobile su TAVOLO, mani staccate**, dopo
  **200 ms warm-up** scartati. Poi indossare + Recenter. Non tenere in mano
  per minuti: basta tavolo 8 s, poi wear/Rec.
- Scale: `gyroScale 0.00105 rad/count`, `accel 9.81/16384`.
  Gate calibrazione: `gyroSTD ≤ 0.75 dps/asse` (da noto σ tavolo 0.62),
  `meanShift prima/seconda metà ≤ 0.05 dps`. Bias congelato, nessuna
  auto-adattamento zero, nessuna prediction; `gravityGain 1` configurabile,
  `smoothing 0`; recenter `full`/`yaw`.
- Recenter `full` corretto attuale **two-sided**:
  `worldYawReference.inverse * absolute * neutralHeadOffset`, offset locale
  a destra così head-neutral = identità senza inclinare world-up.
  Vecchio `full` left-inverse accoppiava yaw globale in roll quando neutral
  era tiltato. Regressione unitaria: sensore tiltato 30° + puro world-yaw
  passa senza coupling pitch/roll. Head-neutral = posa utente, non
  mount di fabbrica/calibrazione lenti.
- IPC esterno senza USB: `--recenter PID`, `--recalibrate PID`, `--stop-imu PID`.
  `--imu --duration-ms 0` = indefinito. `--json-stream` = envelope macchina
  DATA: per update gyro/accel, tick24, quaternione WXYZ, Eulero YXZ, validity,
  wear bit0 byte 8 sensore e IR LE16 byte 55; bit1 display solo RAW per
  riferimenti conflittuali. F0 power/wear/mode da lettura WinUSB IN, no query.
  Mask byte4: bit0 power, bit1 wear, bit2 cinema; `vr_active` byte11.
- Tick24: periodo 1 µs, rollover 16.777 s; robusto a duplicati/gap,
  invalidazione gap > max 5 ms, stale 100 ms. CSV omonimo del log con colonne
  wear/status/IR.
- Convenzione: RHand `+X destra, +Y up, -Z forward` ⇒ yaw-destra negativo,
  pitch-su positivo, roll-destra negativo. Grafici: yaw(Y) ROSSO, pitch(X)
  VERDE, roll(Z) BLU. Non dichiarare inversioni-asse risolte senza test
  fisico misurato.

## 2. Monitor GUI

- `tools/psvr_monitor/monitor.py` Python 3.11 stdlib Tk 8.6;
  `scripts/run_monitor.ps1` e root `run_monitor.ps1`.
  Hardware-detect, power/mode, Start/Stop, Ricalibra bias, Ricentra,
  schematico 3D, grafici, console. GUI = un solo NativeWorker, no USB in Python.
- Default avvio readonly enumeration; controlli USB-power disabilitati durante IMU.
  Launcher esplicito opzionale:
  `-StartImu -ImuDurationMs 42000 -StationaryTest`; profili statici non
  permettono recenter/recal/durata indefinita.
- API readonly 127.0.0.1 porta effimera `GET /health,/state,/samples?limit`
  (max 200),`/logs?limit` (max 200); discovery `logs/monitor_api.json`
  pid/base_url + lock owned; no CORS wildcard, no comandi HTTP write.
- Porte osservate (non hardcodare, riscoperte a ogni avvio): GUI PID 23724
  API 63573 prima; restart PID 8508 API 52448; ultima PID 24036 API 57725
  con statico esplicito 42000 completato. L'API è stata interrogata live durante
  l'uso; le porte sono effimere e vengono riscoperte dal file di discovery.
- API reale: worn `true` IR 1023 confermato F0; non indossato `false` IR 183.
  Gyro-motion corporea vista solo da indossato. Prossimità come CAUSA di
  motion mancante precedente: **non provata**.

## 3. Verifiche: comandi, risultati, fallimenti

Build/toolchain MinGW x64: **build OK, 15/15 CTest**, includendo **71 test Python
del monitor**. I test hardware opt-in non fanno parte di CTest.
Unit: Euler/assi traiettorie note, bias, qnorm, wrapping, cal-rejection, gap,
modi Rec, tilt-regression, meanShift.
VS 2022 non installato; Win11 reale, Win10 non testato.

Statico nativo originale 60 s (cal pre-fix loose 2 s):

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --imu --duration-ms 60000 --stationary-test
# log logs/psvr_diag_20261003T190001.965Z_18636.log (+ .csv omonimo)
```

60003 report, 120006 campioni, 115605 integrati 57.802552 s ~2 kHz,
dt 420..579 µs, 3 rollover, 0 gap/duplicati/seqd;
gyro σ (0.551529, 0.583171, 0.619393) dps,
accel σ (0.041820, 0.047420, 0.052851) m/s²;
yaw-delta -0.155225°, drift -0.161126°/min, PASS exit 0.
Dato preliminare pre-nuovi gate, non verifica drift aggiornata.

Fallimenti conservati (non cancellare):

- `logs/psvr_diag_20261003T190750.168Z_1560.log` (15 s) e
  `logs/psvr_diag_20261003T191129.525Z_9496.log` (90 s): due capture CLI
  asse precoci quasi statiche, nessuna escursione ≥10° nonostante movimento
  utente; timings evitati poi via GUI.
- Prova GUI pre-fix: yaw-right -28.44149° con roll +13.98942° (wear F0+sensore
  true, IR 1023, recenter count 3): segno consistente ma coupling → fix sopra.
- Drift "tutti gli assi su tavolo": cal in MOVIMENTO 2 s
  gyroMean (-0.398759, -0.539445, 0.203057) dps
  σ (0.844211, 0.950668, 0.783110); successivo statico 20 s
  mean (-0.036096, 0.108590, 0.076103) ⇒ residuo Y +0.648035 dps ≈38.88 dpm,
  spiega il reclamo. Soglia/finestra migliorate come sopra, ma non garantiscono
  magia: stazionarietà richiesta, yaw costante non osservabile.

Post-fix statico nella GUI — **PASS**:

```powershell
.\run_monitor.ps1 -StartImu -ImuDurationMs 42000 -StationaryTest
```

Tavolo hands-off 8 s cal + 33.799532 s tracking; API live `tracking true`,
bias mean (-0.0345698, 0.0423267, 0.0262338) dps
σ (0.5566455, 0.5805659, 0.6246319),
halfshift (0.0083559, -0.0002888, 0.0075511) entro limiti;
a 21.9 s Yaw +0.1163 Pitch -0.0022 Roll +0.0032.
Esito finale: **42000 report / 84000 campioni**, 67599 integrazioni,
~1999.998106 campioni/s, dt 479–522 µs, **2 rollover**, 0 gap/duplicati/
malformati/discontinuità. Yaw-delta **+0.236718°**, drift **+0.420216°/min**,
entro il gate configurato 1°/min. Range pitch [-0.022635,0.037219],
roll [-0.018449,0.015977]. **Exit 0**.
Log/CSV: `logs/psvr_diag_20261003T220901.855Z_23728.log` e `.csv` omonimo.

## 4. Verifiche indipendenti aggiuntive

La revisione ha individuato e corretto casi che il test statico non copriva:

- Stream malformato/duplicato oppure calibrazione interrotta non possono restare
  in warming/calibrating indefinitamente: watchdog basato su campioni che
  avanzano, timeout startup senza report 3 s, stallo pre-tracking 1 s.
- Un campione con timestamp duplicato non cambia misure accettate, posa o
  statistiche del rumore. I contatori distinguono ricevuti e integrati.
- Ogni uscita tenta una sola invalidazione JSON terminale; sulle eccezioni
  l'errore originale è preservato, anche se il sink è guasto.
- La GUI distingue processo avviato da IPC pronto; Stop durante startup è
  pendente e limitato, mai inviato ciecamente a processi non posseduti.
- API inattiva/EOF rimuovono la validità della posa e le transizioni pendenti.
- Argomento asse vuoto, telemetria non finita e valori raw fuori range sono
  rifiutati; i gate non diventano una capture generica apparentemente riuscita.

Test reale opt-in, eseguito a visore fermo senza power/mode writes:

```powershell
python -B .\tests\manual_monitor_integration.py --hardware-check
```

Esiti: **PASS** startup → Stop immediato (exit 0), DATA reale accettata,
recenter del worker verificato, ricalibrazione con invalidazione e Stop/EOF.
Nessun processo estraneo terminato; nessuna posa obsoleta rimasta valida.

## 5. Gate rimanenti M3

- Verifica fisica post-fix: yaw-right, pitch-up, roll-right (≥10° primaria,
  cross-axis entro `max(10°, 60% primaria)`), segni + tracce; recenter full/yaw.
- Confronto worn/unworn con flag raw + F0. Nei sorgenti consultati non è
  documentato un gate software che sopprima il gyro quando non indossato;
  l'eventuale comportamento del firmware rimane da misurare.
- Segni e possibili correzioni di basis: vedere `docs/AXIS_CONVENTIONS.md`.
- Dettagli operativi: `docs/IMU_TESTING.md` e `docs/MONITOR_APP.md`.
