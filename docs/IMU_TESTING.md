# Test IMU e orientamento 3DoF

Procedura di misura per il PSVR collegato via USB e alimentato. Il diagnostico
usa HID sull'interfaccia sensori 4 e WinUSB sull'interfaccia controllo 5
(MI_05); MI_04 resta HID. Per il binding WinUSB, installazione e ripristino
consultare [WINUSB_CONTROL.md](WINUSB_CONTROL.md). Il diagnostico non cambia
alimentazione o modalità durante una capture IMU.

## Preparazione e cattura

Prima verificare che `out/diag/psvr_diag.exe` sia stato compilato con le opzioni
IMU descritte qui. Eseguire dalla root del progetto:

```powershell
.\out\diag\psvr_diag.exe --imu --duration-ms 0 --output-interval-ms 100 --json-stream
```

Questi flag selezionano la capture continua JSON per il monitor; per una misura
finita usare invece:

```powershell
.\out\diag\psvr_diag.exe --imu --duration-ms 60000 --stationary-test
```

Per il test interattivo usare `scripts/run_monitor.ps1`, l'unico launcher della
GUI, che avvia `tools/psvr_monitor/monitor.py` con Python 3.11 e Tkinter standard:

```powershell
.\scripts\run_monitor.ps1
```

Nel monitor usare **Start** con il visore appoggiato su un tavolo stabile e
completamente fermo, senza toccarlo per 8 s (warmup 200 ms + calibrazione di
8000 ms). Solo dopo la calibrazione indossarlo e premere **Recenter** nella posa
neutra desiderata; iniziare i movimenti quando le tracce sono visibili. **Stop**
termina la capture e **Calibrate** riavvia la calibrazione da fermo. **Recenter**
imposta solo il riferimento di posa: non ricalibra né modifica il bias gyro.

L'interfaccia mostra alimentazione e modalità solo da `control_status` (report
F0). Nei dati sensore `latest_data.sensor.worn` corrisponde al bit 0 del byte 8;
`latest_data.sensor.display_flag_raw` conserva senza interpretazione il bit 1.
`latest_data.sensor.ir_raw` è il valore grezzo little-endian a 16 bit del byte
55. L'ipotesi che la prossimità/il non-indossato sopprima il giroscopio resta da
verificare: confrontare acquisizioni indossato/non indossato mostrando flag e
tracce raw, senza inferire la causa dai soli flag.

## Test CLI di movimento e recenter

Per isolare gli assi, eseguire una prova per volta:

```powershell
.\out\diag\psvr_diag.exe --axis-test yaw-right --duration-ms 15000
.\out\diag\psvr_diag.exe --axis-test pitch-up --duration-ms 15000
.\out\diag\psvr_diag.exe --axis-test roll-right --duration-ms 15000
```

Prima di muovere il visore eseguire la calibrazione su tavolo descritta sopra,
poi indossarlo e recenter nella posa neutra. Ogni prova richiede escursione con
segno corretto di almeno 10° sull'asse
primario; l'escursione di ciascun asse incrociato deve restare sotto
`max(10°, 60% dell'escursione primaria)`. I nomi degli assi e i limiti sono
criteri di prova, non risultati garantiti. Per recenter programmato in una
capture IMU usare `--recenter-at-ms N`; durante una capture interattiva `R`
richiede il recenter e `C` riavvia la calibrazione. La modalità è `full` oppure
`yaw` tramite `tracking.recenter_mode` in `config.json`.

Per richiedere un recenter a un processo già attivo, passare il suo PID:

```powershell
.\out\diag\psvr_diag.exe --recenter 1234
```

I publisher `--recenter PID`, `--recalibrate PID` e `--stop-imu PID` inoltrano
rispettivamente recenter, ricalibrazione e stop al processo target senza aprire
USB.

## Dati, assi e interpretazione

La capture scrive un log `psvr_diag_*.log` e un CSV omonimo con estensione
`.csv`. Le grandezze sono gyro in rad/s, accelerazione in m/s², quaternion in
ordine WXYZ ed Euler in ordine YXZ. Convenzione assi: +X destra, +Y alto, -Z
avanti; yaw-destra è negativo, pitch-su positivo, roll-destra negativo.
I tick sensore sono a 24 bit, unità 1 µs, con rollover ogni 16,777216 s; ogni
report contiene nominalmente due campioni a circa 2 kHz.

Questo è tracking rotazionale 3DoF: non produce posizione, tracking camera o
controller. L'accelerometro può correggere la direzione della gravità, ma non
rende osservabile lo yaw assoluto; non c'è magnetometro. Valutare drift solo a
visore fisicamente fermo. La deviazione standard dei campioni gyro descrive
rumore/instabilità, non è di per sé una misura di drift. La soglia di
calibrazione per la deviazione standard gyro è 0,75 dps per asse e il controllo
aggiunge un limite allo spostamento della media tra prima e seconda metà della
finestra di calibrazione: massimo 0,05 dps. Non c'è adattamento automatico del
bias né soppressione dei movimenti lenti. Questi gate non garantiscono un limite
di drift durante il tracking: la verifica del drift richiede immobilità fisica.

## Misure hardware disponibili

La capture storica statica di 60 s `logs/psvr_diag_20261003T190001.965Z_18636.log`
e il CSV omonimo, effettuata con la precedente calibrazione da 2 s, ha riportato:
60.003 report, 120.006 campioni, 115.605 campioni
integrati per 57,802552 s, frequenza 1999,998201 campioni/s; `dt` 420–579 µs,
3 rollover, zero gap, duplicati o discontinuità di sequenza. STD gyro
`(0,551529; 0,583171; 0,619393)` dps; STD accelerometro
`(0,041820; 0,047420; 0,052851)` m/s². Delta yaw −0,155225°, drift yaw
−0,161126°/min; yaw range [−0,166808°, 0,024095°], pitch
[−0,038162°, 0,014161°], roll [−0,008277°, 0,056916°]. Calibrati 4001
campioni; prova statica PASS, exit 0. È un dato preliminare precedente ai nuovi
gate di calibrazione, non una verifica aggiornata del drift.

La build software corrente passa 15/15 test CTest, inclusi 71 unit test Python
del monitor. Una calibrazione
anticipata durante movimento di 2 s ha stimato bias gyro
(-0,398759; -0,539445; 0,203057) dps; con la procedura ferma su tavolo la media
grezza successiva era (-0,036096; 0,108590; 0,076103) dps. La differenza residua
sull'asse Y, +0,648035 dps (circa 38,88°/min), spiega il drift elevato osservato
dopo quella calibrazione in movimento.

Durante una prova GUI con il recenter precedente è stato osservato yaw-right
−28,44° con roll accoppiato +13,99°, `worn=true` e IR raw 1023. La correzione del
recenter full evita di lasciare l'inversa del riferimento in modo da inclinare
la verticale del mondo e accoppiare yaw/roll. La trasformazione attuale usa
`worldYawReference.inverse * absolute * neutralHeadOffset`, con offset neutro a
destra; la regressione sintetica da posa neutra pitchata di 30° e yaw mondo puro
passa senza pitch/roll. L'offset neutro definisce la posa utente, non una
calibrazione ottica o della geometria di fabbrica.

La nuova cattura statica post-fix da 42 s è registrata in
`logs/psvr_diag_20261003T220901.855Z_23728.log`: 42.000 report, 84.000 campioni,
67.599 integrati (33,799532 s), 1999,998106 campioni/s, `dt` 479–522 µs, 2
rollover, zero gap, duplicati, report malformati o discontinuità di sequenza.
Delta yaw +0,236718° e drift +0,420216°/min; pitch range
[−0,022635°, 0,037219°], roll [−0,018449°, 0,015977°]. La capture sul visore
lasciato fermo sul tavolo è PASS. Non è una garanzia di drift nullo in altre
temperature, posture o sessioni.

Le prove fisiche degli assi dopo la correzione del recenter e il confronto
indossato/non indossato restano da eseguire; M3 non è dichiarata completa. Non
invertire assi sulla base dei dati precedenti e non attribuire la telemetria a un
gate di prossimità senza misure comparative: firmware e hardware restano da
verificare.

## Criteri ancora da chiudere

- Calibrare 8 s immobile su tavolo, indossare, recenter nella posa neutra e
  ripetere yaw-right, pitch-up e roll-right con escursione primaria ≥10° e
  cross-axis entro il limite sopra, controllando segni e tracce visibili.
- Eseguire confronto indossato/non indossato annotando `wearing`, flag raw,
  campioni grezzi e stato F0; stabilire solo da dati se cambia la telemetria.
- Verificare calibrazione, perdita/staleness, stop, ricalibrazione e recenter
  locale/remoto, inclusa la modalità yaw.
- Ripetere la misura dopo cambi di temperatura e nelle condizioni d'uso
  indossato, con immobilità realmente verificata; conservare tempi, CSV e log.
- Exit 7 significa calibrazione/qualità/capture IMU incompleta. Exit 0 certifica
  la capture finita secondo i gate software, non l'accettazione fisica completa M3.
