# Riconoscimento frontale/misto: confronto delle costellazioni

## Stato

Implementato in `tools/psvr_camera/constellation_led_pose.py` e collegato al
worker della suite. È la stessa variante verificata nei replay sotto, con
almeno tre LED di default. **Baseline attuale mantenuta per decisione utente.**

Feedback live dopo la prova nel visore: **molti meno gray screen, restano
piccoli scattini quando probabilmente cambiano le costellazioni** (ipotesi
dell'utente sulle transizioni, non causalità dimostrata). Non si dichiara
zero gray screen né scatti risolti. L'ottimizzazione delle transizioni è
rinviata; intanto si tiene questa versione così.

Feedback originale dell'utente: «meglio, qualche piccolo scattino quando
probabilmente cambiano le costellazioni, ma molti meno greyscreen. chiudi la
documentazione con junior coder spiegando come hai fatto a sistemare il
problema nel dettaglio, intanto lo teniamo così».

La suite va riaperta per caricare il modulo Python aggiornato; non serve una
nuova DLL.

L'orientamento mostrato rimane esclusivamente quello del driver IMU. Il
riferimento world `alignment` rimane fisso; la correzione ottica lenta già
presente viene usata per la geometria della posizione, non per sostituire
l'orientamento del visore. Il raccordo nativo oltre 90 gradi e la sua correzione
di regressione frontale restano quelli dell'intervento precedente.

## Riconoscimento

Sono esplicite le famiglie frontale, diamante destro/sinistro, laterale misto
destro/sinistro e coppie posteriori. Le coppie richiedono l'opzione sperimentale.
L'IMU/proiezione suggerisce le associazioni da provare, senza uno switch
obbligatorio a un dato angolo. Visibilità e ROI rimangono basate sul modello.

- Un frontale completo confermato e con RMS <=2 px è il percorso rapido:
  non è utile enumerare centinaia di ipotesi quando cinque identità sono chiare.
- Se il frontale è debole o genera `front_reprojection_conflict`, vengono
  valutate nello stesso frame anche le costellazioni miste prima della perdita.
- Ogni proposta è verificata come geometria nota, con rotazione ottica/IMU
  comune, profondità positiva, gate di movimento e residui reali dei LED.
- Un punto incoerente può essere escluso e il sottoinsieme rifittato, senza
  scendere sotto il minimo di LED consentito. I tripli devono avere geometria
  non collineare nel modello.
- Il punteggio usa supporto, residui e continuità insieme; un piccolo bonus
  mantiene la famiglia già riconosciuta quando l'evidenza è quasi equivalente.
- Due soluzioni di costo simile e posizioni 3D distanti oltre 3 cm sono
  ambigue. Il ramo di compatibilità non può scavalcare tale rifiuto.
- I tentativi non modificano il prior di movimento: traslazione, velocità e
  timestamp vengono aggiornati solo dopo la selezione della soluzione.

Il matcher precedente resta un ripiego quando la libreria non verifica alcuna
famiglia, mantenendo comunque il minimo di LED e i gate esistenti. Non viene
usato per sovrascrivere una profondità esplicitamente ambigua.

Non sono stati spostati i punti di `side_left` né allargati semplicemente i
limiti globali per far passare le ipotesi precedentemente fallite.

## Riaggancio frontale e costo

Con allineamento e prior recenti, i cinque slot frontali previsti dall'IMU
guidano le identità per il PnP. Si possono distinguere cinque frontali anche
tra sei/sette candidati, con controlli di distanza, unicità e separazione.
Il PnP verifica comunque la geometria e richiede quattro conferme reali.
Non vengono generati conferme o timestamp validi artificiali.

In questa situazione si prova una disposizione coerente, evitando di ripetere
120 permutazioni su ogni frame laterale. L'aggancio iniziale senza riferimento
e il recupero dopo perdita prolungata conservano la ricerca completa frontale.
Quando iniziano a mancare frontali, il matcher misto continua a lavorare.

## Verifica sui log conservati

La variante finale è stata riprodotta sull'intera prova v2 di 30 minuti,
sulla prima prova e sull'ultima registrazione fino alla segnalazione di fine
salvata nel suo `STOP`. Le finestre attive usano gli stessi estremi delle pose
valide registrate per ciascun confronto.

| Prova | Frame invalidi attivi prima/dopo | Episodi prima/dopo | Gap massimo prima/dopo |
|---|---:|---:|---:|
| Prima prova | 1.898 / 30 | 233 / 3 | 1,480 / 0,013 s |
| V2, 30 minuti | 276 / 32 | 85 / 12 | 0,440 / 0,093 s |
| Ultima prova, fino allo STOP | 554 / 40 | 139 / 16 | 0,587 / 0,213 s |

Le sei finestre critiche della v2 (796, 1439, 1448, 1455, 1635, 1715 secondi)
hanno zero frame invalidi con questa variante, rispetto ai precedenti
16, 51, 26, 33, 26, 24 nelle finestre definite dal runner.

### Stabilità: riportare anche gli indicatori peggiori

| Prova | p99 salto pubblicato mm prima/dopo | Massimo pubblicato mm prima/dopo | Salti pubblicati >15 mm prima/dopo |
|---|---:|---:|---:|
| Prima prova | 3,492 / 2,917 | 27,27 / 42,63 | 5 / 1 |
| V2, 30 minuti | 3,708 / 3,677 | 71,76 / 72,08 | 61 / 50 |
| Ultima prova | 4,271 / 4,297 | 51,97 / 53,84 | 77 / 83 |

Quindi **non è dimostrato un miglioramento uniforme della stabilità**. Il
massimo sulla prima prova peggiora, pur riducendo gli altri salti. Nell'ultima
prova alcuni indici pubblicati peggiorano leggermente. I salti sono spostamenti
fra campioni consecutivi validi, non errori rispetto alla testa; recuperare
campioni prima invalidi modifica anche l'insieme su cui si calcolano i salti.
Non c'è ground truth per attribuire i singoli massimi a movimento reale o stima.

Come controllo sui riferimenti frontali chiari (cinque LED, RMS registrato
<=1,5 px), nell'ultima prova il p99 della differenza grezza dalla posa
registrata è 0,087 mm, massimo 22,53 mm. Nella prima prova è 8,65 mm, massimo
10,82 mm. Questo non è un'accuratezza metrica, ma un confronto fra stimatori.

Tempo `update` nel replay finale: p99 circa 3,8 ms sulla v2 e 4,3 ms
sull'ultima prova, rispetto ai picchi p99 di circa 38 ms della prima variante
non ottimizzata sull'ultimo log. Restano massimi occasionali attorno a 40 ms:
non è una garanzia end-to-end a 75 Hz.

## Limiti e test

Sono usati i candidati già rilevati, non immagini: il cambiamento delle ROI
sul detector reale non è riprodotto. La politica angolare del driver, la sua
grace, il compositor e i gray screen nelle lenti non sono riprodotti. I numeri
di episodi sono perdite ottiche, non gray screen misurati. Restano perdite,
ambiguità e salti residui. Il rig geometrico conserva i propri limiti di calibrazione.

Test specifici: transizioni su entrambi i lati con clutter, outlier, rifiuto
delle coppie senza consenso esplicito, immutabilità del prior nei tentativi,
riaggancio senza 120 permutazioni e con conteggio reale delle conferme,
deriva lenta oltre dieci gradi, recenter, IMU stale e impossibilità di
scavalcare il rifiuto di profondità ambigua. La nuova ragione
`imu_guided_constellation` usa la covarianza guidata con l'intero rig.

Verifica finale: **336 test e 219 sottotest passati**, più controllo headless
della suite riuscito. Nessun avvio hardware effettuato durante queste verifiche.

Report finali: `constellation-final-replay.json` nelle tre cartelle
`logs/ac_evo_seated_20261015/`, `logs/ac_evo_coherent_v2_20261015/` e
`logs/ac_evo_angular_center_20261015/`.

La baseline mantenuta si identifica nel log con
`optical_tracking.algorithm=constellation_led_v1`, la famiglia selezionata e
il riepilogo delle ipotesi. L'aggancio iniziale resta frontale. Il feedback
live nella sezione Stato chiude la prova qualitativa di questa variante;
l'ottimizzazione degli scatti residui è rinviata per decisione dell'utente.

## Resoconto conclusivo delle correzioni

Resoconto di chiusura richiesto dall'utente. Non è una dichiarazione di
soluzione perfetta: restano piccoli scatti residui e perdite/ambiguità
residue documentate sopra. Il feedback live qualitativo (molti meno gray
screen) va distinto dalle metriche di replay.

1. Sintomo iniziale. Da seduti c'erano salti anche senza grandi movimenti:
false profondità stimate da coppie di LED, pattern completo contraddetto
dal sottoinsieme pubblicato e gray screen come conseguenza di posa invalida
al driver. Nei log di queste prove la IMU restava valida; non si afferma
una causa hardware unica della deriva.

2. Primo ancoraggio frontale e correzione ottica lenta. I cinque LED
frontali confermati ancorano la traslazione; il riferimento world
`alignment` resta immutabile e la rotazione mostrata nel visore resta
sempre quella IMU del driver. La correzione ottica lenta serve solo per
la geometria della posizione. Il gate originario confrontava l'innovazione
totale con l'attivazione e raggiungeva 10 gradi dopo 12–15 minuti,
bloccandosi da solo (`front_orientation_conflict`, vedi
`docs/EVO_GRAYSCREEN_LOCKOUT_FIX_20261015.md`). La correzione usa ora il
target relativo alla correzione corrente (`target @ optical_correction.T`,
innovazione incrementale): segue un disaccordo graduale oltre i dieci
gradi accumulati, ma rifiuta ancora un nuovo salto oltre dieci gradi. Non
è stato allargato un limite di rotazione della testa.

3. Riconoscimento laterale precedente e tre ablation scartate. Il vecchio
ramo dava precedenza al PnP frontale e su `front_reprojection_conflict`
non cercava sottoinsiemi misti. Le tre varianti provate in
`docs/EVO_LATERAL_HANDOFF_VERIFICATION_20261015.md` NON sono la soluzione
attivata e il rig non è stato modificato: solo fallback >=3 LED riduceva
i frame persi ma frammentava le perdite in più episodi; priorità angolare
25/20 gradi produceva salti pubblicati fino a circa 148 mm; raffinamento
di `side_left` migliorava i residui held-out (mediana 6,277→2,506 px) ma
peggiorava il replay completo e la finestra 1448–1452 s (26→44 invalidi).

4. Nuovo matcher a famiglie note. Famiglie esplicite: frontale, diamanti
dx/sx, laterali misti dx/sx, posteriori/coppie solo sperimentali. Fronte
completo forte con RMS <=2 px come fast path; fronte debole o in conflitto
confronta le alternative nello stesso frame. Ogni proposta usa fit
coerente con rotazione ottica/IMU comune, profondità positiva, gate di
movimento e residui reali; un outlier può essere escluso solo conservando
il minimo di LED; i tripli devono essere non collineari nel modello.
Score su supporto+residui+continuità con piccola isteresi per la famiglia
già riconosciuta. Posizioni 3D distanti oltre 3 cm a costo simile =
rifiuto esplicito, non scavalcabile dal legacy fallback. Il prior
(traslazione, velocità, timestamp) viene aggiornato solo dopo la scelta.
Resta il ripiego legacy quando nessun template copre il sottoinsieme, ma
non su ambiguità esplicita.

5. Riaggancio frontale con previsione da IMU e posizione recente. Con
allineamento e prior recenti, i 5 slot frontali previsti guidano le
identità per il PnP: si distinguono cinque frontali anche tra 6/7
candidati, con controlli di distanza, unicità e separazione. Il PnP
verifica comunque la geometria e richiede 4 conferme reali. Così si evita
di ripetere 120 permutazioni per ogni frame laterale. Prima acquisizione
e perdita lunga restano ricerche frontali complete. Il driver mantiene
orientamento IMU; l'ottica fornisce posizione e riferimento geometrico
lento. La nuova ragione `imu_guided_constellation` è classificata come
guided per la covarianza con l'intero rig a 9 LED.

6. Compromesso oltre 90 gradi (non è la cura dei gray a 30–40 gradi). Tra
90 e 120 gradi la posizione è raccordata verso il centro seduto con curva
smoothstep, limite 0,25 m/s; da 120 gradi verso il retro obiettivo
centrato; ritorno sotto 85 gradi con misura valida o raccordo di rientro;
IMU valida necessaria; niente pacchetti ottici inventati, il raccordo è
nel controller del driver (`driver/position_continuity.hpp`). Opzione
`two_led_experimental` spenta di default; se accesa usa coppie vincolate
da IMU e disattiva il centering. La regressione frontale del controller
(riaggancio con raccordo anche dopo semplici dropout frontali senza mai
essere entrati nel fallback angolare) è stata corretta separatamente:
con sola politica angolare attiva il frontale conserva la semantica
strict precedente (vedi `docs/ANGULAR_CENTER_3DOF_20261015.md` e
`docs/ANGULAR_TRIAL_REGRESSION_AND_CLEANUP_20261015.md`).

7. Verifiche e limiti del replay. Verifica finale: **336 test Python e
219 sottotest passati** + suite headless; i due contratti CTest nativi
precedenti sul driver includevano sweep sintetico di 40 minuti, NON un
test hardware di 40 minuti. Il replay riusa stessi candidati e timestamp,
non immagini/ROI ricalcolate né rendering/grace/driver angolare; gli
episodi ottici non equivalgono al numero di gray screen.

Numeri finali di replay (finestre attive, stessi estremi delle pose
valide registrate; stabilità NON uniformemente migliore):

- Prima prova `logs/ac_evo_seated_20261015/constellation-final-replay.json`:
invalidi attivi 1898→30; episodi 233→3; gap 1,480→0,013 s; max
pubblicato 27,27→42,63 mm (peggiora).
- V2 `logs/ac_evo_coherent_v2_20261015/constellation-final-replay.json`:
invalidi 276→32; episodi 85→12; gap 0,440→0,093 s. Sei finestre critiche
tutte a zero frame persi.
- Ultima `logs/ac_evo_angular_center_20261015/constellation-final-replay.json`:
invalidi 554→40; episodi 139→16; gap 0,587→0,213 s; p99 pubblicato
4,271→4,297 mm; max 51,97→53,84 mm; salti >15 mm 77→83 (non dichiarare
miglioramento uniforme).
- Costo `update` in replay: p99 circa 3,8 ms v2 e circa 4,3 ms ultima
prova, massimi occasionali attorno a 40 ms. Non è latenza fisica.

Pulizia già effettuata e chiusa: 42 vecchi JSONL rimossi, 15.367.015.715
byte = 14,312 GiB (circa 15,37 GB), manifest
`logs/old-trial-cleanup-manifest.json`. Non cancellare altro ora.

Decisione registrata: baseline attuale mantenuta, ottimizzazione delle
transizioni rinviata.

## Review B5-07 — 2026-10-16 (fix outer update)

Riferimento: `review_261015.md` §8 (stato attuale), rilievo B5-07.

- Fix in `ConstellationLedTracker.update()`: se `_recognize` restituisce
  `constellation_ambiguous_depth` dopo un frontale debole valido (RMS >2),
  l'update esterno ora ritorna posa invalida e ripristina il prior
  (`translation`/`velocity`/`last_match` a `before`).
- La correzione ottica valida convergente da `super().update()` è preservata
  (`optical_correction`/`correction_at` non rollbackati); `alignment` invariato.
- Invariati: fast path frontale forte (RMS <=2 px), ripiego weak front quando
  `_recognize` riporta `constellation_no_verified_pattern`, minimo LED,
  soglie e baseline `constellation_led_v1`.
- Prove (scope: solo selezione camera correlata, non intero progetto):
  `tests/test_camera_constellations.py` **11 passed**;
  selezione correlata (constellations + coherent + front-anchor +
  guided + imu-leds) **43 passed**. Nessun hardware.
- Nessun nuovo claim hardware; nessuna attribuzione certa degli scatti
  residui a questo ramo. Il feedback qualitativo in §Stato resta invariato.
