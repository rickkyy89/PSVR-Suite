# Ottimizzazione Kalman 6DoF — 2026-10-13

## Esito

**Ottimizzato il Kalman esistente; nessun nuovo estimatore da zero. Profilo
sperimentale pronto, non accettato nel visore. One Euro resta il default.**

Profondità statica sintetica: **4,037 → 1,812 mm std, −55,1%**, vicino a One Euro
2,5 Hz/β8 (**1,852 mm**). Reattività promettente, ma regressioni di disponibilità
e riaggancio rimangono. Replay reale senza ground truth o quiete certificata:
nessuna conclusione su jitter statico, latenza fisica o flash risolti.

Analizzati [ADAPTIVE_KALMAN_6DOF](ADAPTIVE_KALMAN_6DOF.md),
[SIXDOF_DRIVER_BRIDGE](SIXDOF_DRIVER_BRIDGE.md),
[DRIVER_CAMERA_CONTRACT](DRIVER_CAMERA_CONTRACT.md), implementazione, log e test.
Nessuna modifica al tracker ottico/PnP, rig, intrinseci PS Eye, preset 60/6/13/0,
fusione/orientamento IMU, driver, compositor o display. Nessuna configurazione
SteamVR applicata, nessun reset/riavvio hardware e nessun nuovo trial fisico.

## 1. Architettura e modifiche

Resta il percorso `PnP → HEAD nel frame IMU non-recentered → filtro esclusivo → UDP`.
`pose_estimator.py` conserva stato `[x,y,z,vx,vy,vz]`, CV, dt reali e aggiornamento
Joseph. `position_pipeline.py` seleziona un solo filtro e gestisce R/policy wire;
`position_filter.py` e il profilo Kalman iniziale restano conservati.

### Q adattiva quasi-statica

La Q originale era discretizzata correttamente:
`q * [[dt³/3 I, dt²/2 I], [dt²/2 I, dt I]]`.
Il nome storico `acceleration_std_m_s2` resta per compatibilità, ma è la **radice
della densità white-acceleration**: q ha unità m²/s³, non è sigma dell'accelerazione
costante per frame. A root 3/75 Hz, `sqrt(Qpp)≈2,67 mm`, `sqrt(Qvv)≈0,346 m/s`
per asse: molto reattivo rispetto al rumore millimetrico di quiete.

Sweep eseguito: root **0,05 / 0,1 / 0,2 / 0,5 / 1 / 1,5 / 2 / 3**, fattori di
varianza ottica **1 / 2 / 4**. Abbassare globalmente Q migliora la quiete ma
penalizza accelerazioni e rifiuti; non basta come soluzione generale.

Modalità opt-in `adaptive_quiet`:
- fit causale pesato da R del trend ottico, velocità latente media, innovazioni
  e affidabilità; solo misure **accettate affidabili** addestrano il detector;
- dwell 250 ms, trend smussato d'ingresso <0,03 m/s, isteresi d'uscita 0,08 m/s,
  finestra 120 ms e significatività del trend 3 sigma;
- root quiet **0,03** (q=0,0009), root motion **3** (q=9);
- uscita anche per due innovazioni whitened coerenti: statistica >11,34,
  coseno >0,8. Un solo superamento della NIS al 95% non basta;
- missing/rifiuti interrompono la coppia statistica; dati parziali interrompono
  il dwell; loss/reset cancellano la storia. Nessun freeze, pseudo-misura di
  velocità zero, P artificialmente ridotta o buffer di frame.

I cinque seed hanno mostrato onset troppo lento dal prior quiet stretto.
`motion_onset_acceleration_std=8` fornisce **una sola** predizione alternativa,
dal medesimo prior, per gate/uscita sul campione corrente. Se l'uscita è accettata,
quel campione usa q=64; il successivo frame dinamico torna a q=9. Non si itera Q
finché una misura passa. Tutti i controlli fisici e statistici restano attivi.
Pulse 5 è stato provato ma recupera solo parte del divario dinamico.

Queste soglie sono candidati motivati da statistica e benchmark, non tarature
fisiche certificate. “Uscita sullo stesso campione” non significa riconoscimento
istantaneo: onset 0,1 m/s, seed 6130, uscita quiet dopo **66,7 ms**, risposta 10 mm
dopo 93,3 ms, t50 con 13,3 ms aggiuntivi e t90 senza ritardo aggiuntivo rilevato
alla griglia camera. La posizione continua a essere stimata prima dell'uscita.

### R lungo l'asse corretto

La stima Jacobiana/SVD PnP esistente non è sostituita. Aggiunta inflazione PSD:

```
u = A.T @ [0,0,1]       # asse ottico espresso nel frame IMU
R_eff = R + ((f-1)*(u.T@R@u) + sigma_extra²) * u@u.T
```

Profilo: **f=2 sulla varianza**, non sulla std; sigma extra=0. Nessuna direzione
diventa più certa; le proiezioni trasverse sono invariate. `None` resta non
osservabile. R grezza/effettiva sono loggate e limiti/affidabilità rivalutati dopo
l'inflazione. Allineamento assente/invalido fallisce chiuso senza inventare R.

**R reale sottostimata non è dimostrato:** la NIS reale è bassa. f=2 è una
regolarizzazione sperimentale sostenuta dal sintetico, non una calibrazione della
profondità. R locale non copre bias di rig/scala, calibrazione, errori correlati
dei centroidi o corrispondenze sbagliate. Il filtro non li corregge.

### Gating e recovery

Il limite raw a 2 m/s confondeva rumore e movimento: due misure indipendenti con
sigma depth 5 mm, a 75 Hz, implicano ~0,53 m/s di sigma della differenza.
Allowance opt-in sul **solo delta raw**:
`min(15 mm, 3*sqrt(lambda_max(R_cur+R_last)))`.
Il cap impedisce che R enorme ammetta teleport; velocità posteriore e velocità
delle pubblicazioni contigue restano **≤2 m/s**.

`recovery_velocity_fit` usa un cluster separato di almeno quattro conferme
affidabili. Fit GLS di posizione all'ultimo timestamp e velocità con R complete,
P 6×6 correlata e kernel temporale dell'accelerazione bianca
`q*min(a,b)²*(3*max(a,b)-min(a,b))/6`. Corretto il contributo di processo alla
seconda differenza, `q*b²*(a+b)/3`, nel percorso ottimizzato. Fit non affidabili
o oltre i limiti non fanno seed, clamp o fallback a velocità zero. La validità
prima di un reseed distante resta interrotta; salti attraverso invalidità sono
misurati separatamente, non attenuati artificialmente per occultarli.

### Rollback

Nuove policy core disattivate per default; regressioni bitwise sul comportamento
legacy. `position_kalman_experimental.json` originale invariato. Nuovo profilo
`position_kalman_optimized_20261013.json`; snapshot A/B/C
`kalman_comparison_20261013.json`. Nessuna cascata One Euro/Kalman.

## 2. Budget degradato camera/driver

Il wire v2 non distingue ottica/predizione: predizioni valide per 80 ms possono
riarmare last-good e aggiungere altri 80 ms di grace driver (~160 ms totali).

Il profilo selezionato usa **`prediction_output=driver_hold`**. Il Kalman predice
internamente entro tempo, P e velocità, ma `predicting` è inviato come
**`valid:false,state:lost,position_m:null`**. Il driver conserva il suo hold
preesistente, che gli invalidi non rinnovano. Con grace 80 ms il budget pubblicato
è il solo hold dal last-good ottico accettato, non 80+80. Con grace=0 invalida
subito: possibile regressione di flash da verificare, non compensata modificando
la configurazione SteamVR.

Nessun nuovo campo wire né modifica a session ID, epoch, seq o timestamp.
Watchdog IMU/freschezza mantengono priorità; orientation-only invariato. Policy
originale `camera` disponibile per baseline e confronto dell'algoritmo, ma conserva
il problema del doppio hold. Log distinguono validità interna, wire e predizioni
interne: un hold driver non è una misura ottica valida nuova.

## 3. Benchmark A/B/C con ground truth sintetica

`kalman_optimization_benchmark.py`: shared input/timestamp per tutte le varianti.
Primi sei scenari identici al benchmark precedente; aggiunti depth lenta,
allineamento camera inclinato, LED 2/3/4/5, 1,7 m/s rumoroso, AR(1), onset.
900 frame/75 Hz/seed 6130. Errori/std dopo 1 s warm-up (825 frame possibili);
conteggi/acquisizione su tutti i 900. RMSE/lag sono **condizionati ai frame validi**.
Lag su reference nota, griglia 1 ms: 0 ms non certifica latenza fisica nulla.
Autocorrelazione solo su frame originali adiacenti; missing/gap non concatenati.

### Quiete: std, deriva e picchi

| Indicatore | One Euro 2,5/8 | Kalman iniziale | Ottimizzato |
|---|---:|---:|---:|
| Std X (mm) | 0,574 | 1,394 | 0,799 |
| Std Y (mm) | 0,519 | 1,453 | 0,747 |
| Std depth camera (mm) | 1,852 | 4,037 | **1,812** |
| Pendenza depth (mm/s) | 0,0261 | 0,0130 | 0,0267 |
| Passo p99 / max (mm) | 3,869 /6,487 | JSON completo | 7,039 /10,625 |
| Validi /900 | 900 | 897 | 897 |

Quiet nel ~90% dei frame ottimizzati. Nessun rifiuto di gate statico: tre invalidi
sono le conferme iniziali. **X/Y e picchi statici non battono One Euro**; la
pendenza del rumore bianco non migliora uniformemente.

### Movimento/perdite — seed 6130

“Camera” isola l'estimatore; “hold” è il profilo operativo senza budget sommato.

| Misura | One Euro 2,5/8 | Iniziale | Ottimizzato camera | Ottimizzato hold |
|---|---:|---:|---:|---:|
| Rampa depth RMSE Z (mm) | 5,975 | 7,986 | 4,342 | 4,313 |
| Lag depth (ms) | 19 | 9 | 5 | 5 |
| Depth validi /900 | 900 | 895 | 894 | 891 |
| Salto depth attraverso invalidità (mm) | nessun gap | 131,0 | 65,6 | 65,6 |
| Rapido laterale RMSE X (mm) | 5,531 | 1,342 | 1,304 | 1,304 |
| Lag rapido laterale (ms) | 18 | 0 | 0 | 0 |
| Tilted depth RMSE ottico (mm) | 5,912 | 4,186 | 4,249 | 4,249 |
| Depth lenta RMSE / lag | 2,163 mm /45 ms | 4,027 mm /4 ms | 2,260 mm /8 ms | uguale |
| Tre outlier 0,5/0,4 m rifiutati | 0 | 3 | 3 | 3 |
| Dropout validi / predetti interni | 876 /0 | 880 /10 | 880 /10 | 870 /10 |
| Dropout salto max (mm) | 3,325 | 25,105 | 4,683 | 4,942 |
| Tracking parziale validi /900 | 900 | 860 | 860 | 854 |

Due LED non aggiornano il Kalman; tre LED osservabili aggiornano tracking attivo
ma non reinit. Dieci frame a due LED esauriscono il budget; i successivi tre LED
non riagganciano fino a conferme con ≥4 LED. One Euro conserva la validità legacy
anche con due LED: i suoi 900 validi non certificano la precisione di quelle pose.

### Cinque seed e risultati negativi

Seed 6130–6134, stessa sequenza A/B/C per seed, 4500 frame/scenario:

| Aggregato | Iniziale | Ottimizzato camera | Ottimizzato hold |
|---|---:|---:|---:|
| Depth RMSE medio (mm) | ~4,89 | **4,278** | ~4,26 |
| Depth lag medio (ms) | 2,4 | 4,0 | 3,8 |
| Depth validi /4500 | 4482 | 4460 | 4449 |
| Tilted RMSE medio (mm) | ~4,47 | **4,242** | ~4,23 |
| Tilted validi /4500 | 4480 | 4464 | 4456 |
| Rapido laterale validi /4500 | 4485 | 4461 | 4460 |

Std Z statica media ottimizzata ~1,908 mm. RMSE statica Z **1,44–2,28 mm** contro
One Euro **1,81–2,02 mm**: non vince in tutti i seed. Il seed 6130 era sfavorevole
all'iniziale sulla rampa; altri seed mostrano regressioni di lag/errore e validità.

Negativi conservati:
- Q globale root 0,05/R×1: quiete buona, depth ~11,75 mm RMSE/29 ms, rifiuti sul
  rapido. R×4 non risolve la dinamica a Q molto bassa.
- Root 1,5/R×1: depth ~4,58 mm/6 ms ma quiete Z ~3,60 mm e laterale rapido peggiore.
- Adattivo senza pulse: medie cinque seed depth **5,703 mm**, tilted **6,619 mm**;
  pulse 8 porta a 4,278/4,242. Pulse 5 recupera solo parte del divario.
- Pulse 8 introduce alcuni gap assenti nella baseline e peggiora singoli riagganci,
  pur migliorando le medie. Non costituisce accettazione globale.
- AR(1), rho=0,9: RMSE ottico One Euro **3,78 mm**, iniziale **4,68 mm**,
  ottimizzato **4,12 mm**. Rumore correlato ancora problematico.
- Triangolare 1,7 m/s, rumore isotropo 5 mm: iniziale **462/900**, ottimizzato
  camera **745/900**, hold **469/900** validi; RMSE Z **57,74 /21,74 /6,17 mm**.
  Il valore hold è condizionato all'esclusione delle predizioni, non superiorità
  durante gap. Salti massimi **549,7 /217,2 /241,4 mm**: scenario non risolto.

## 4. Replay reale, NIS e flash

`driver_camera_20261012_234154_50e426.jsonl`, **62.283 frame**, shared input
ricostruito una volta. Restano limiti legacy: A inferita, scala/offset assunti e
reset assenti. Il confronto precedente della baseline registrata aveva errore
max 3,412 mm. I nuovi `estimator_input` permettono replay esatto del boundary.

| Reale ricostruito | One Euro 2,5/8 | Iniziale | Ottimizzato camera | Ottimizzato hold |
|---|---:|---:|---:|---:|
| Wire validi | 46.624 | 44.807 | 44.063 | 43.778 |
| Invalidi | 15.659 | 17.476 | 18.220 | 18.505 |
| Estimatore valido | 46.624 | 44.807 | 44.063 | 44.063 |
| Predetti interni | 0 | 826 | 285 | 285 |
| Passo p99 (mm) | **8,025** | 9,684 | 9,290 | 8,822 |
| Passo max contiguo (mm) | 28,505 | 27,882 | 27,351 | 27,351 |
| Velocità contigua max (m/s) | 2,398 | 1,992 | 1,980 | 1,980 |
| Scarti di gate | 0 | 344 | 63 | 63 |
| Scarti qualità/R | 0 legacy | 822 | 987 | 987 |
| Riagganci wire | 516 | 84 | 62 | 182 |
| Salto max attraverso invalidità (mm) | 875,140 | 864,924 | 863,979 | 854,250 |

R×2 aggiunge 165 scarti per incertezza e riduce alcune possibilità di reinit,
contribuendo al peggioramento della disponibilità. Scarti di qualità separati
dal gate, non cancellati. Più gap wire con hold non equivalgono al numero di flash
nel visore: lì opera il grace. **I grandi salti dopo perdita rimangono.**
Std dell'intero trial è dispersione del movimento, non jitter; RMSE/lag reali
restano null senza reference. `quiet_active` non certifica immobilità fisica.

NIS = `nu.T @ inv(S) @ nu`; ottica `(u.T@nu)²/(u.T@S@u)`. Riferimenti gaussiani
ideali: medie 3 e 1. Statistiche accettate sono selezionate, non calibrabili
imponendo tali medie. Missing/R assente non diventano zero. S effettiva, alternativa
onset e probe quiet sono loggate; stratificazione per LED/modalità e correlazione
temporale. Selezione adattiva e R dal residuo rendono chi-quadro approssimato.

| Popolazione | NIS 3D media / mediana | NIS ottico medio |
|---|---:|---:|
| Sintetico statico iniziale | ~1,52 | JSON |
| Sintetico statico ottimizzato | 2,271 /1,756 | 0,496 |
| Reale pre-update iniziale | 0,0416 /0,00350 | 0,0302 |
| Reale pre-update ottimizzato | 0,0939 /0,00852 | 0,0234 |

**Nessuna evidenza di sottostima generalizzata di R reale.** R×2 va ricalibrata
con quiete certificata, raw/reference e autocorrelazione, non solo minimo jitter.

Flash: mantenere diagnosi separata con `pose_validity_transition`, telemetry,
capture e `tools/psvr_runtime_probe/correlate_flashes.py`. Distinguere IMU lost,
stale/future, position missing/incoherent, display invalid, fade/fullscreen/focus
ed errori compositor −202. Il filtro non risolve gap IMU o compositor; nessuna
nuova diagnosi fisica di flash è stata eseguita.

## 5. Verifica, costo e artefatti

- **Python completo: 723 passati, 7 saltati, 250 subtest passati.**
- **CTest completo: 57/57 passati**, artifacts MSVC RelWithDebInfo; include
  orientation-only/IMU, wire/UDP, loopback camera/driver, validità e runtime mock.
- Regressioni originali mantenute: bitwise default, SPD, dt/R variabili,
  quiet/onset, outlier, rapido rumoroso, GLS/covarianza, dropout/recovery, LED
  parziali, recenter/sessioni/timestamp, PSD/NIS e metadati v2 invariati.
- Nessuna esclusione di test nel risultato finale. Un errore intermedio del
  digest log è stato corretto escludendo solo due campi diagnostici aggiunti,
  conservando il digest congelato di tutti i campi preesistenti/stato/P.

Profiling locale, 900 frame statici, filtro+diagnostics, sotto carico offline:
mediana/p99 µs One Euro **10/23**, iniziale **189/413**, ottimizzato hold **545/937**
(camera p99 ~1077). Ben sotto 13,33 ms/frame ma esclude camera, PnP, attesa IMU,
trasporto/render: non è latenza nel visore. Nessuna dipendenza pesante nuova;
NumPy/OpenCV esistenti, pytest test; matrici GLS piccole risolte solo nel recovery.

Artefatti quantitativi:
- [sweep Q/R](kalman_optimization_qr_sweep_20261013.json);
- [confronto finale 12 scenari](kalman_optimization_results_20261013.json);
- [cinque seed finali](kalman_optimization_robustness_20261013.json);
- [replay reale finale](kalman_optimization_real_selected_20261013.json).

Stadi intermedi/negativi conservati nei file `kalman_optimization_synthetic*`,
`kalman_optimization_multiseed*`, `kalman_optimization_real_20261013.json` e
`kalman_optimization_selected_20261013.json`. I profili pre-pulse non sono i numeri
della configurazione finale. Le tabelle iniziali in ADAPTIVE_KALMAN restano intatte.

```powershell
# Setup bridge già operativo: nuovo profilo opt-in via preferenze Suite
# (position_filter=kalman_optimized + position_config
# configurations\position_kalman_optimized_20261013.json).
# Rollback Kalman iniziale: position_filter=kalman_initial +
# configurations\position_kalman_experimental.json.
# Rollback One Euro 2,5/8: position_filter=one_euro.

$env:PYTHONPATH = 'tools'
.\.venv-camera\Scripts\python.exe -m psvr_camera.kalman_optimization_benchmark `
  --sweep-q 0.05 0.1 0.2 0.5 1 1.5 2 3 --depth-factors 1 2 4 --frames 900 --out logs\sweep_NEW.json
.\.venv-camera\Scripts\python.exe -m psvr_camera.kalman_optimization_benchmark `
  --compare-only --frames 900 --out logs\abc_NEW.json
.\.venv-camera\Scripts\python.exe -m psvr_camera.kalman_optimization_benchmark `
  --variants-config configurations\kalman_comparison_20261013.json --scenarios stationary depth tilted_depth onset rapid `
  --seeds 6130 6131 6132 6133 6134 --out logs\seeds_NEW.json
.\.venv-camera\Scripts\python.exe -m psvr_camera.replay_position_log logs\TRIAL.jsonl `
  --variants-config configurations\kalman_comparison_20261013.json --out logs\replay_NEW.json
.\.venv-camera\Scripts\python.exe -m pytest tests -q
```

`--out` richiede file nuovo. Numerica deterministica a versioni NumPy/OpenCV
fissate; `--measure-timing` separato e non inserito nei JSON.

## 6. Piano di validazione fisica e accettazione

1. Tre serie A/B/C, ordine alternato, stessa distanza/luci/preset/rig. Annotare
   profilo/timestamp/stato. Nessun cambio compositor durante il confronto.
2. Quiete **certificata** su supporto e quasi-quiete indossata: ≥30 s frontale,
   ruotato e a due distanze. XYZ/asse ottico, deriva, p99/picchi; raw/R/NIS e
   transizioni. `quiet_active` non sostituisce la certificazione dell'operatore.
3. Spostamenti noti XYZ 10 cm, partenze/arresti/inversioni e depth lenta/rapida;
   reference fisica temporizzata nello stesso clock/frame. Lag, RMSE, overshoot,
   invalidità e comportamento anche nei frame esclusi.
4. Occlusioni 2–6 frame e >0,5 s; 2/3/4 LED, reaggancio in movimento, recenter,
   nuova sessione. Verificare grace effettivo read-only, nessun cumulo in hold,
   perdita lunga invalida e salti al reseed. Non promuovere invalidi per evitare flash.
5. Correlare ogni flash con IMU/posa/fade/compositor; non attribuirli tutti al
   filtro. Conservare anche registrazioni fallite e annotazioni negative.

Accettazione solo se migliora realmente la profondità nel visore, preservando
reattività e senza regressioni significative di tracking/riaggancio. Oggi:
statico **sintetico** migliorato, dinamica promettente con regressioni documentate,
disponibilità reale ricostruita non migliorata, rumore correlato e salti lunghi
non risolti. **Nessuna accettazione hardware dichiarata.**

## Follow-up statico reale, senza sovrascrivere i risultati offline

Successivamente l'utente ha autorizzato test statici con il PSVR fermo.
Acquisizioni fisiche, replay esatto e driver/OpenVR effettivo sono documentati in
[STATIC_TRACKING_VALIDATION_20261013.md](STATIC_TRACKING_VALIDATION_20261013.md).
La quiete frontale a cinque LED migliora anche sui dati reali; il consumer
OpenVR ha registrato std depth ~0,080 mm per l'ottimizzato. Scope limitato:
restano movimento, altre geometrie/occlusioni e valutazione nel visore. Le
tabelle offline precedenti e i risultati negativi restano evidenze separate.

## Esito dinamico successivo — non accettato

Nel test di movimento del 2026-10-14 l'utente ha giudicato One Euro nettamente
più solido e continuo, pur con scatti occasionalmente più grandi. Il Kalman
ottimizzato è quindi **non accettato per l'uso reale**. Il miglioramento statico
non prevale sulla regressione di disponibilità. Dettagli e dati parziali:
[MOVEMENT_TRIAL_FEEDBACK_20261014.md](MOVEMENT_TRIAL_FEEDBACK_20261014.md).
