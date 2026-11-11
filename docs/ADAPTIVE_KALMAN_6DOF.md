# Kalman adattivo 6DoF — implementazione sperimentale, 2026-10-13

**Stato: implementato e verificato offline, NON accettato nel visore.** One Euro
rimane il default. La configurazione iniziale Kalman privilegia la risposta e il
rifiuto degli outlier; i risultati non dimostrano ancora un miglioramento del
jitter rispetto alla baseline accettata. Occorre il confronto fisico seguente.

## Architettura e contratto

Il punto d'inserimento, identificato prima delle modifiche, è in
`tools/psvr_camera/led_tracking.py`, dopo `build_driver_position_packet` e prima
del sender UDP: posizione HEAD `A.T @ (scale*t + R*offset)` nel frame
`psvr_imu_unrecentered`. Tracker LED, associazione/PnP, calibrazione PS Eye,
geometria, preset 60/6/13/0, fusione IMU e compositor restano quelli esistenti.

- `pose_estimator.py`: `PoseEstimator`, stato `[x,y,z,vx,vy,vz]`, modello CV,
  `F=[I,dt*I;0,I]`, dt dai timestamp di cattura. Q white-acceleration continua:
  `q*[dt³/3*I,dt²/2*I;dt²/2*I,dt*I]`. Aggiornamento Joseph della covarianza.
- `pose_uncertainty.py`: Jacobiano OpenCV della riproiezione e SVD. Nel PnP
  libero viene marginalizzata la rotazione; nel tracking guidato si propaga
  anche un nuisance angolare IMU di 1°. Rotazioni ricevute/pubblicate inalterate.
  Covarianza propagata al punto HEAD, includendo scala e offset.
- `position_pipeline.py`: selezione **esclusiva** `one_euro`/`kalman`.
  `position_filter.py` originale conservato integralmente, mai in cascata.
- Il wire v2 conserva sessione/epoca/seq/timestamp IMU. Per una predizione serve
  un **frame camera nuovo**, binding e IMU freschi e allineamento valido.
  Nessun dato IMU invalido, sessione assente o errore camera diventa posa valida.

Il gating usa `innovation.T @ inv(Ppos+Rmisura) @ innovation`; una misura
rifiutata non corregge né stato né covarianza. Sono controllate anche velocità
implicita della misura, velocità posteriore e velocità tra posizioni pubblicate.
La predizione può coprire brevi perdite ottiche/outlier, con incertezza crescente.
L'ambiguità ottica invalida la pubblicazione, preservando per breve tempo la
storia; un errore del contesto IMU la azzera. Dopo perdita lunga servono nuove
conferme affidabili. Il recovery mantiene un candidato separato e richiede un
intervallo pubblicato invalido prima di un reseed distante: nessun teletrasporto
tra due pose valide contigue. I salti **attraverso intervalli invalidi** vengono
misurati separatamente e non sono occultati.

Cambio sessione, epoca di recenter o allineamento azzera il Kalman. Lo azzerano
anche RIALLINEA, cambio filtro, controlli/soglie camera e read failure. Timestamp
non finiti, negativi, duplicati o regressivi invalidano senza seed; gap >250 ms
richiede riacquisizione. AZZERA SPOSTAMENTO RELATIVO conserva il suo contratto:
agisce solo sull'origine diagnostica camera, non equivale al recenter SteamVR.

**Interazione con il driver:** la predizione valida è uno stato esplicito
dell'estimatore, codificato come `tracking` nel wire esistente e come `predicting`
nei log. Il driver non distingue ottica da predizione. Il suo grace preesistente
può quindi prolungare il mantenimento dopo l'ultima predizione: con grace 80 ms,
il budget ottico complessivo può arrivare a circa 160 ms, entro i limiti hard del
bridge e di freschezza IMU. Gli 80 ms sotto sono il massimo **dell'estimatore**, non
una promessa end-to-end. Nessun parametro driver è stato cambiato; misurare anche
questa interazione nel visore, senza confondere la continuità con validità ottica.

## Parametri iniziali

| Parametro | Valore |
|---|---:|
| Intensità Q, radice della densità white-acceleration | 3,0 (q=9) |
| Gate Mahalanobis², 3 dimensioni | 16,27 |
| Predizione massima / gap di reset | 80 / 250 ms |
| Velocità massima stato e publish contigui | 2 m/s |
| Massimo std principale posizione | 80 mm |
| Acquisizione/recovery | 4 misure consecutive affidabili |
| Affidabilità per acquisizione | ≥4 LED, std principale ≤40 mm |
| Floor pixel / posizione | 0,75 px / 1,5 mm |
| Nuisance angolare IMU guidato | 1° |
| Span minimo / condizionamento informazione massimo | 12 px / 10⁶ |

R cresce col residuo di riproiezione (`sqrt(SSE/dof)`, floor 0,75 px) e con la
debolezza geometrica del Jacobiano. Due LED **non** aggiornano il Kalman. Tre LED
possono aggiornarlo se osservabili, ma non inizializzarlo/riagganciarlo. Rango
insufficiente, span troppo piccolo o std >80 mm rifiutano l'update, senza R
fallback ad alta confidenza. La covarianza locale non copre errori sistematici di
rig/calibrazione o corrispondenze sbagliate: per queste ultime resta il gating.
L'incertezza può esaurire la predizione **prima** di 80 ms, specialmente subito
dopo l'acquisizione con std iniziale della velocità 1 m/s. Le quattro conferme
introducono ~40 ms dal primo al quarto campione a 75 Hz, oltre l'acquisizione
ottica già esistente. Non sono parametri validati fisicamente.

## Logging e replay

I nuovi JSONL conservano log e chiavi precedenti e aggiungono:
`estimator_input` (pacchetto grezzo, A, R, qualità, permessi/contesto),
`position_output`, posizione stimata, velocità, P 6×6, innovazione, Mahalanobis²,
LED associati, rango/condizionamento/RMS/std, contatori, stato, segmenti di
continuità e ragioni di reset/rifiuto. Eventi `position_reset` rendono riproducibili
reset manuali, cambi filtro/controlli/soglie e read failure. I timestamp dell'input
sono quelli reali di cattura. Con logging, anche One Euro calcola la qualità per
avere identici input di confronto; questo aggiunge costo diagnostico. Senza log,
One Euro non esegue il calcolo del Jacobiano.

`replay_position_log.py` costruisce gli input una sola volta e li fornisce a
entrambi i filtri, senza resampling o clock live. Nei log storici R e allineamento
sono ricostruiti dai punti salvati, intrinseci, rig e IMU; scale=1 e offset=0 sono
assunti dove assenti, e i reset non registrati non possono essere ricostruiti.
La provenienza è esplicita e dati mancanti non ricevono confidenza inventata.
Nel log esaminato la differenza massima della baseline ricostruita rispetto a
quella registrata è **3,412 mm** su 46.624 frame confrontabili: il replay storico
è deterministico, ma non una riproduzione bit-identica del runtime originale.
Per confronti rigorosi preferire i nuovi log `estimator_input`.

Le metriche separano passi contigui, velocità, predizioni e salti al riaggancio.
La std su un trial in movimento è dispersione, **non jitter statico**; selezionare
un intervallo realmente fermo. La profondità camera è proiettata lungo l'asse
ottico tramite A, distinta dalla componente Z del frame IMU. Il ritardo è stimato
solo con una reference nota, nello stesso frame/clock; altrimenti è `null`.

## Verifiche e risultati

- `pytest tests -k camera`: **329 passati, 7 saltati** (336 selezionati).
- CTest mirati: **8/8 passati** — camera, nuovi scenari Kalman, UDP posizione,
  export IMU, interoperabilità camera/driver loopback reale, runtime mock,
  transizioni di validità e script driver.
- Scenari automatici: quiete, moto lineare/rapido/profondità, outlier isolati e
  ripetuti, R adattiva, dropout breve/lungo, riaggancio, recenter/sessioni/reset,
  timestamp anomali, SPD, esclusione cascata e conservazione metadati.

Benchmark sintetico ripetibile (`benchmark_position_filters`, seed 6130, 75 Hz,
rumore XYZ 1,5/1,5/5 mm; baseline launcher 2,5 Hz/β8):

| Misura sintetica | One Euro | Kalman iniziale |
|---|---:|---:|
| Quiete, std XYZ (mm) | 0,574 / 0,519 / 1,852 | 1,394 / 1,453 / 4,037 |
| Moto lineare, lag reference | 44 ms | 0 ms sulla griglia 1 ms |
| Rampa rapida 10 cm, lag | 18 ms | 0 ms sulla griglia 1 ms |
| Rampa profondità, lag | 19 ms | 9 ms |
| Rampa profondità, RMSE Z | 5,975 mm | 7,986 mm |
| Tre outlier, passo massimo | 492,43 mm | 16,65 mm |
| Tre outlier, misure scartate | 0 | 3 |

Il caso profondità comporta anche due frame invalidi e un massimo salto al
riaggancio di 131 mm per il Kalman: una regressione da verificare/tarare. Il test
dropout copre 4 e 20 frame assenti: 10 frame predetti, perdita lunga invalida e
riacquisizione confermata. Il default non batte la baseline sulla quiete: non
promuoverlo sulla sola capacità di scartare gli outlier o sul lag sintetico.

Replay storico completo `logs/driver_camera_20261012_234154_50e426.jsonl`,
**62.283 frame**, dati dettagliati in [kalman_replay_20261013.json](kalman_replay_20261013.json):

| Misura replay | One Euro storico (8 Hz/β15) | Kalman |
|---|---:|---:|
| Frame validi | 46.624 | 44.807 |
| Passo p99 / massimo contiguo | 11,413 / 34,393 mm | 9,684 / 27,882 mm |
| Velocità pubblicata massima contigua | 2,811 m/s | 1,992 m/s |
| Misure scartate dal gating | 0 | 344 |
| Frame predetti | 0 | 826 |
| Riagganci dopo invalidità | 516 | 84 |
| Salto massimo attraverso invalidità | 879,577 mm | 864,924 mm |

764 pose con due LED e 58 con incertezza eccessiva non aggiornano il Kalman.
Il trial include movimenti/perdite e non contiene una reference fisica né un
intervallo statico certificato: nessuna conclusione su jitter/deriva statici o
ritardo reale. Il grande salto dopo perdita rimane visibile come discontinuità.
Replay e benchmark sono risultati software, non accettazione hardware.

## Avvio, rollback e confronto nel visore

Dalla root repository, con il setup SteamVR/UDP già usato per la baseline:

```powershell
# Baseline One Euro: selezionare one_euro nel Tracking tab della Suite
# (oppure --start-vr / --attach per il trial verso VR esistente).
# Rollback immediato: riselezionare one_euro.

# Kalman sperimentale, stesso preset camera e geometria: selezionare
# position_filter=kalman_initial + position_config
# configurations\position_kalman_experimental.json nelle preferenze Suite.
```

Anche la GUI permette `one_euro`/`kalman`; il cambio azzera lo stato selezionato
e conserva la sequenza sender. Per rollback durante il trial scegliere One Euro.
I parametri numerici JSON, se presenti, vincono sui cutoff delle preferenze;
il file sperimentale fornito contiene solo parametri Kalman. La Suite mantiene
2,5 Hz/β8 di default.

1. Registrare tre coppie A/B, ordine alternato, stesso luogo/luci/distanza camera,
   stesso preset e stesso profilo One Euro. Annotare filtro e momenti di switch.
2. Per ogni filtro: 30 s fermo frontale e 30 s fermo ruotato, a due distanze;
   segnare i timestamp degli intervalli statici. Misurare std XYZ, std profondità
   camera, pendenza e picco-picco, distinguendo drift da movimento reale.
3. Eseguire spostamenti noti di 10 cm su XYZ, lenti e rapidi, andata/ritorno;
   fermarsi agli estremi. Registrare una reference fisica temporizzata per il lag
   (il solo rumore PnP non è ground truth). Confrontare ritardo percepito, scala,
   RMSE, passi e velocità; annotare oscillazioni di profondità.
4. Provocare occlusioni brevi 2–6 frame e lunghe >0,5 s, pose con due/tre LED,
   riaggancio frontale, recenter driver e riattivazione/sessione nuova. Contare
   scarti e flash; verificare che perdita lunga resti invalida, che non sia
   mascherata da grace driver e che il reseed non produca un salto valido→valido.
5. Accettare solo se migliorano jitter/profondità nel visore senza peggiorare lag,
   stabilità, falsi riagganci o flash. Conservare log/annotazioni anche dei fallimenti.

Analisi e test ripetibili (file `--out` deve essere nuovo):

```powershell
$env:PYTHONPATH = 'tools'
.\.venv-camera\Scripts\python.exe -m psvr_camera.replay_position_log logs\TRIAL.jsonl `
  --out logs\TRIAL_comparison.json
# Aggiungere --start-s / --end-s con timestamp assoluti del log per la quiete.
# --reference REF.jsonl: monotonic_s, position_m, stesso clock/frame.
.\.venv-camera\Scripts\python.exe -m psvr_camera.benchmark_position_filters
.\.venv-camera\Scripts\python.exe -m pytest tests -k camera
```

Dipendenze test locali: `tools/psvr_camera/requirements-test.txt`. CMake registra
`camera_kalman_pytest` quando pytest è disponibile. Nessuna prova nuova nel visore
è stata eseguita durante questa implementazione.

## Evoluzione successiva — risultati iniziali conservati

L'ottimizzazione del 2026-10-13 è documentata separatamente in
[KALMAN_OPTIMIZATION_20261013.md](KALMAN_OPTIMIZATION_20261013.md).
Questo documento e le sue tabelle descrivono la baseline iniziale. Il nuovo
profilo aggiunge policy opt-in e confronto A/B/C, senza promuovere l'algoritmo
come accettato nel visore o sovrascrivere le evidenze precedenti.
