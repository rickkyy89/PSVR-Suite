# Monitor PSVR

Aggiornamento 2026-10-04: assi post-fix, confronto guidato presenza e
scollegamento/ripartenza sono stati verificati; vedere
[verifiche fisiche](M3_PHYSICAL_CHECKS_20261004.md) per evidenze e gate residui.
I risultati e le prove pendenti riportati sotto descrivono la sessione precedente.

`tools/psvr_monitor/monitor.py` è una GUI diagnostica Python 3.11/Tkinter
standard per osservare telemetria IMU e stato del PSVR durante le prove. Avvio
dalla root:

```powershell
.\scripts\run_monitor.ps1
```

La GUI avvia il diagnostico nativo `out/diag/psvr_diag.exe` in stream JSON e
offre i controlli **Start**, **Stop**, **Calibrate** e **Recenter**. Per
calibrare, appoggiare il visore su un tavolo stabile, non toccarlo per 8 s
(warmup 200 ms + calibrazione 8000 ms); poi indossarlo e premere **Recenter**
nella posa neutra desiderata. Recenter imposta solo il riferimento di posa: non
ricalibra e non modifica il bias gyro. Alimentazione e modalità provengono solo da
`control_status` (F0). La durata statica configurata deve essere almeno
`calibration_ms + startup_ms + 30000 + 1000` ms: con i valori predefiniti sono
39.200 ms, mentre una cattura da 42.000 ms è sufficiente. L'avvio normale resta
passivo e non invia comandi di alimentazione.
Nei dati sensore, `latest_data.sensor.worn` usa il bit 0 del byte 8;
`latest_data.sensor.display_flag_raw` espone senza interpretazione il bit 1;
`latest_data.sensor.ir_raw` è il valore grezzo little-endian a 16 bit del byte
55. Verificare sperimentalmente eventuali differenze tra visore indossato e non
indossato: non è noto se la prossimità condizioni l'uscita del gyro. La soglia
gyro STD in calibrazione è 0,75 dps per asse; lo scarto tra la media gyro della
prima e della seconda metà deve essere ≤0,05 dps. Non c'è adattamento automatico
del bias o soppressione dei movimenti lenti: la calibrazione richiede immobilità.
Il superamento di questi gate non garantisce i limiti di drift durante il
tracking; anche la prova di drift deve essere fisicamente stazionaria.

## API locale read-only

All'avvio il processo pubblica `logs/monitor_api.json`, contenente PID e
`base_url` dell'API HTTP su localhost e porta effimera. Il servizio espone
`/health`, `/state`, `/samples?limit=N` e `/logs?limit=N`, solo in lettura. Lo
stato distingue hardware e worker e include freschezza dei dati; con dati stale
o EOF la posa non è valida. L'API non richiede segreti perché è limitata al
loopback e non offre controllo remoto. `/state` include inoltre `pose_valid`,
`latest_pose`, `phase`, `read_api_ready` e `pending_stop`. Il watchdog invalida
la posa se warmup/calibrazione non avanzano per 1 s, se non arrivano dati per 3 s,
oppure in caso di dati stale o EOF; un tick duplicato rifiutato non aggiorna le
statistiche. La terminazione JSON best-effort invalida lo stato anche in caso di
eccezione; lo stop pendente ha un limite di 8 s e fallback gestito dal processo
owner. Interrogare campioni e log con i limiti richiesti.
Esempi PowerShell:

```powershell
$discovery = Get-Content .\logs\monitor_api.json -Raw | ConvertFrom-Json
$base = $discovery.base_url.TrimEnd('/')
Invoke-RestMethod "$base/health"
Invoke-RestMethod "$base/state"
Invoke-RestMethod "$base/samples?limit=20"
Invoke-RestMethod "$base/logs?limit=50"
# Campi principali dello stato strutturato
$state = Invoke-RestMethod "$base/state"
$state.latest_data.sensor.worn
$state.latest_data.sensor.ir_raw
$state.pose_valid
$state.latest_pose
$state.phase
```

I campioni sono orientamento rotazionale diagnostico; il disegno 3D è uno
schizzo di monitoraggio, non rendering SteamVR e non calibrazione delle lenti.
Non sono forniti tracking posizionale, camera o controller. Per convenzione degli
assi, unità, CSV, criteri di prova e misure hardware vedere
[IMU_TESTING.md](IMU_TESTING.md).

Il recenter full corretto applica `worldYawReference.inverse * absolute *
neutralHeadOffset`, con l'offset della posa neutra a destra, così il riferimento
non inclina la verticale mondo né accoppia yaw e roll. La posa neutra è scelta
dall'utente e non costituisce calibrazione ottica/head di fabbrica. Grafici:
yaw (Y) rosso, pitch (X) verde, roll (Z) blu.

## Verifica e limiti

La build software finale passa 15/15 test CTest, inclusi 71 unit test Python
del monitor. L'integrazione manuale hardware
`python -B tests/manual_monitor_integration.py --hardware-check` è passata:
enumerazione read-only, startup e stop immediato, readiness IPC nativa, contratto
DATA/calibrazione/posa, Recenter, ricalibrazione con invalidazione della posa,
Stop/EOF senza posa stale valida; nessun comando power/mode.
Una prova GUI precedente alla
correzione del recenter ha mostrato yaw-right −28,44° e roll +13,99° con
`worn=true`, IR raw 1023; è un dato della versione precedente, non una misura
valida di cross-axis dopo la correzione. Restano da eseguire test fisici degli
assi. La nuova prova statica sul tavolo è PASS (+0,420216°/min), ma M3 non è
completa finché i movimenti fisici e il confronto di presenza non sono verificati.

Il codice di uscita nativo 7 indica calibrazione/qualità o capture IMU
incompleta; 0 indica che la capture finita ha superato i gate software, non che
M3 sia stata accettata fisicamente. Restano da verificare su hardware le tre
prove degli assi con segno e cross-axis, il confronto indossato/non indossato
e gli effetti del reale scollegamento USB durante tracking. I comandi IPC
Start/Stop/Ricalibra/Ricentra e l'invalidazione su EOF sono stati verificati.
I publisher `--recenter PID`, `--recalibrate PID` e `--stop-imu PID`
inoltrano il relativo evento e non aprono USB.
