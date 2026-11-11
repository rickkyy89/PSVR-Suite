# PS4 front-five: blob, corrispondenze e prova Suite, 2026-11-01

## Evidenza storica e contratto geometrico

`logs/ps4_camera/ps4-stereo-points-20261028T173528721315Z.json` riportava
1.687 coppie, sempre 5/5 candidati, 1.671 con cinque punti, RMS di
riproiezione 0,1236 px. Usava la canonica `1990fae7…`, crop grezzo 0 come
riferimento. È una prova di punti, **non di posa**.

La Suite usa da prima di questa diagnosi la calibrazione swapped `b7c937f9…`,
crop `right,left` e gli stessi gate: epipolare 1 px, riproiezione 1 px,
angolo 0,2°, ambiguità 0,25, massimo 16 candidati, budget 20.000. Il relabel
è coerente con `Rsw≈Rcan.T` (differenza ≈1,63e−6),
`Tsw≈−Rcan.T@Tcan` (≈3,25e−6), intrinseche scambiate entro ≈0,041 px.
Non sono stati scambiati gli occhi né cambiati intrinseche o rig per passare.
Il crop conserva l'offset di 48 pixel e gli array raw 640×400 del medesimo
frame UVC. F viene applicata a pixel **undistorti**, triangolazione a raggi
normalizzati, riproiezione nuovamente in pixel raw distorti: nessuna evidenza
di un errore di spazio o di F in riferimento sbagliato.

La riproiezione dei cinque punti 3D storici attraverso le due calibrazioni,
nel relabel fisso già dichiarato, differisce al massimo **0,04142 px**;
il matcher Suite ricostruisce cinque match, epipolare massimo **0,000198 px**.
È un controllo di contratto sulle **proiezioni ricostruite**, non il recupero
dei centroidi originali misurati. Dati: `ps4-geometry-historical-contract.json`.

Tutte le cartelle `temp/opencode` sotto indicano
`C:\Users\rickk\AppData\Local\Temp\opencode`.

## Perché 5/5 blob non davano cinque match

Sono state salvate **al massimo 16 coppie PNG sincrone** in
`temp/opencode/ps4-geometry-recording`, con sequence/timestamp, hash
calibrazione, controlli, candidati/aree, matrice epipolare completa, costi,
assegnazione, coppie withheld e primo reject reason di ogni edge.
`ps4_geometry_diagnostics.diagnose_pair` usa il matcher di produzione per
ogni edge, senza configurazione alternativa.

Nel frame `exposure-6-pair46` il riferimento aveva i cinque LED reali, ma
l'altro occhio aveva **quattro LED e un riflesso sul muro**:

| LED riferimento (x,y), area | Altro occhio (x,y), area | Epipolare |
| --- | --- | ---: |
| (279;184,288), 104 | (237,652;186,364), 66 | 0,753 px |
| (351,155;185,876), 129 | (309,474;187,244), 135 | 0,051 px |
| (313,183;211,780), 109 | (271,859;213,063), 64 | 0,018 px |
| (283,042;228,375), 24 | segnale a circa (243;230), sotto area 20 | non candidato |
| (347,361;230,111), 36 | (307,595;230,784), 37 | 0,541 px |

Il falso quinto candidato era **(625,219;183,094), area 64**, visibilmente sul
muro, con errore epipolare **50,128 px** rispetto al LED inferiore sinistro.
Nella ROI raw dell'altro occhio `[x235:250,y224:235]` il LED debole produceva
componenti da **1 e 7 pixel**, non area ≥20. È dentro il campo: non si deduce
occlusione o guasto. In `exposure-5-pair15` il falso quinto era invece una
frammentazione del LED centrale. Allargare il gate epipolare non avrebbe
trasformato questi blob nei cinque LED corretti.

## Causa software e correzione senza allargare i gate

Un'altra registrazione aveva **7/9 blob ma cinque match stereo univoci**.
Il vecchio tracker li rifiutava solo perché richiedeva anche esattamente
5/5 blob totali. Passando gli stessi cinque match al solver invariato:
posa valida, RMS stereo **0,7434 px**, epipolare massimo **0,5821 px**.
Il nuovo adapter conserva gli indici raw e costruisce i cinque centroidi
del solver nell'ordine dei match, senza mutare gli input.

Con ulteriore clutter stereo si richiede **un solo** sottoinsieme di cinque
punti compatibile con la costellazione rigida. È un prefilter necessario:
se ogni residuo 3D inlier è ≤r, ogni distanza tra due punti differisce di
≤2r. Si usa r=15 mm **già richiesto dal RANSAC originale**, non una tolleranza
aggiunta. Si confrontano le dieci distanze, senza assegnare identità per
ordinamento x/y. Più sottoinsiemi plausibili ⇒ perdita esplicita, non scelta
del migliore. Al massimo C(16,5)=4.368 sottoinsiemi, valutati vettorialmente;
un solo solver di posa. Il fast path IMU mantiene tutti i gate precedenti.

Rimangono invariati: cinque inlier, soglie colore/area, epipolare,
triangolazione, reproiezione, residui 3D, orientamento, incertezza, ambiguità,
freshness originale e identità sessione/epoca. `partial`/`ambiguous` stereo
restano rifiutati. Il primo solve lento può scadere e non viene pubblicato;
solo i frame nuovi e freschi possono usare l'allineamento già validato.

## Controlli e prima prova reale riuscita

Nella luce corrente, un piccolo sweep entro i range letti ha trovato il
compromesso **exposure −7 / gain 8 / brightness 1**, con Manual=2 e readback
verificato. Shutter nominale 7,8125 ms (unità API, non misura hardware).
Brightness 1 rende utilizzabile il LED debole senza ereditare la luminosità
4 della scacchiera; non è un nuovo threshold. Il profilo PS4 separato ora
contiene questi valori espliciti. Tutti i controlli condividono un solo
filtro COM e le sue interfacce, senza rebind per proprietà.

Prova autorizzata `RuntimeController` + `TrackingWorker` 6DoF, con override
sperimentale dei soli controlli prima di consolidare il profilo:

- **1.037 frame, 731 pose valide**, 1.037 campioni IMU freschi, UDP attivo;
- probe osservato **mode=6dof, position_valid=true, pose_valid=true**;
- **valid_published=1620** in 30 finestre driver;
- 305 frame senza cinque match non ambigui, un primo solve scaduto;
- ultimo probe in `position_grace`, non fix continuo: i dropout restano reali.

Evidenza: `temp/opencode/ps4-suite-live-20261101T135608-summary.json`, relativo
JSONL e `ps4-suite-live-check-tuned-unique-front5.log`. La prova non dimostra
scala metrologica, tracking posteriore/laterale o robustezza in movimento.
Ogni run ha fermato worker e runtime graceful, confermato cinema e power-off
tramite F0, senza processi VR residui. Nessun buio, replug, binding o install.

### Conferma senza override, con il profilo normale della Suite

`ps4-suite-live-20261101T140024-summary.json` e
`ps4-suite-live-check-production-front5.log`:

- **954 frame, 629 pose valide, 954 IMU fresche**, UDP attivo;
- ultimo probe: **mode=6dof, pose_valid=true, position_valid=true**,
  IMU age **0,236 ms**;
- **valid_published=1231**, 30 finestre driver;
- controlli richiesti e confermati: **−7/8/1, Manual=2**, tutti dopo quattro
  frame; nessun override nel wrapper di apertura;
- 246 frame in attesa di acquisizione/allineamento, 72 senza match sufficienti,
  sette scaduti: non sono stati pubblicati come fix nuovi;
- stop worker/runtime, cinema, power-off F0 e assenza processi VR confermati.

Il criterio live richiesto è soddisfatto, ma avvio e dropout non sono risolti
in ogni condizione. `valid_published` conta pose driver valide, anche durante
grace: la conferma specifica di posizione 6DoF è il probe sopra.

## Regressioni registrate

`tests/fixtures/ps4_front5_unmatched_2d_20261101.json` conserva i centroidi
7/9, hash del PNG raw, calibrazione e indici dei cinque match certificati.
`ps4_front5_stereo_clutter_20261101.json` conserva 12/15 blob, sette match
stereo, un'unica costellazione rigida di cinque e posa RMS **1,0443 px**.
I PNG originali e le matrici restano nella cartella temporanea bounded.
Test sintetici coprono duplicati/ambiguità, due costellazioni plausibili,
clutter non abbinato, clutter stereo e mancata mutazione degli input.

Prima verifica completa dopo la correzione: **185 passed, 141 subtests passed**
su tutti i test PS4 e il pannello PS4. Log: `ps4-geometry-all-tests.log`.
