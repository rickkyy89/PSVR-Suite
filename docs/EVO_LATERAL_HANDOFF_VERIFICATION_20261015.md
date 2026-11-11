# Verifica diretta delle tre ipotesi sul passaggio laterale

## Esito

Verifica eseguita direttamente, senza subagenti. Le tre ipotesi sono state
provate in replay; **nessuna variante è promossa alla suite**. Il fallback
guidato recupera alcuni frame, ma frammenta i gap; il cambio a soglia angolare
e la nuova coordinata laterale introducono regressioni. Restano attivi il
tracker v2 e il rig precedenti a questi esperimenti.

La precedente spiegazione attribuiva troppo direttamente gli errori alla
coordinata di `side_left`: il nuovo replay non conferma tale causalità isolata.
Inoltre l'RMS massimo dei 193 `front_reprojection_conflict` è 4,108 px, non
3,80 px. Gli zeri del vecchio ispettore per `guided_would_succeed` erano campi
non calcolati: la verifica effettiva del fallback è quella riportata qui.

## Metodo e fedeltà

Sorgente principale: `logs/suite_tracking_20261015T195257117578Z_e9208322.jsonl`.
135.114 frame, circa 30 minuti e 44 secondi. Il replay della v2 riproduce
esattamente conteggi di validità, gap e statistiche dei salti registrati;
il probe per-frame rileva **zero differenze di validità**.

La finestra attiva comune, dal primo all'ultimo frame valido registrato,
contiene 128.948 frame. Esclude acquisizione iniziale e coda finale senza
luci. Ogni variante ha storia indipendente, stessi candidati/timestamp,
stessa trasformazione world/testa e stesso One Euro della sessione.

Non ci sono immagini o ground truth: ROI/detector non vengono ricalcolati.
I salti sono distanze tra campioni consecutivi validi, non errori misurati
della testa. Il compositor e la grace del driver non sono riprodotti:
gli episodi ottici invalidi **non sono conteggi di gray screen**.

## 1. Fallback guidato dopo conflitto frontale

Variante `guided-fallback`: dopo il normale aggiornamento della correzione
ottica, `front_reprojection_conflict` prova il matcher guidato. Accetta almeno
tre LED; ripristina traslazione, velocità e `last_match` se il tentativo è
respinto, così una coppia scartata non avvelena lo stato nascosto. Non esegue
un secondo riaggancio frontale dopo una perdita lunga.

- 193 tentativi, **79 frame recuperati**.
- 104 tentativi senza pattern coerente.
- 10 ipotesi a meno di tre LED respinte.
- Frame invalidi attivi: 276 → 197.
- Episodi invalidi attivi: **85 → 105**.
- Gap massimo completato: 0,440 → 0,199 s.

Le perdite lunghe vengono spezzate da recuperi intermittenti; non è dimostrata
una riduzione dei grigi. Il percentile 99 pubblicato peggiora leggermente.

## 2. Priorità guidata per vista laterale

Variante `angle-handoff`: il ramo guidato ha priorità quando l'asse frontale
del modello dista almeno 25 gradi dalla direzione della camera; ritorna alla
politica frontale sotto 20 gradi (isteresi). Richiede almeno tre LED prima di
accettare la priorità; se non riesce, ripiega sulla variante precedente.
Gli angoli sono geometrici camera/modello, non yaw anatomico calibrato.

18.200 tentativi guidati accettati su 19.026. I gap diminuiscono, ma il massimo
salto pubblicato cresce a **148,13 mm** e il percentile 99 a 3,906 mm.
Questa politica è respinta. Non è una prova contro ogni possibile confronto
di qualità tra ipotesi: testa specificamente la priorità angolare 25/20 gradi.

## 3. Verifica indipendente di `side_left`

1.053 frame validi contengono l'etichetta `side_left`. Per triangolare il punto
sono richiesti almeno tre frontali, traslazione stimata dai soli frontali e
RMS dei frontali <=1,5 px. Il punto laterale non contribuisce alla traslazione.
Le viste sono diradate temporalmente e per orientamento, per non contare
centinaia di frame quasi identici come campioni indipendenti.

- 26 viste selezionate: 14 training, 12 validazione.
- Training e validazione in blocchi temporali disgiunti di cinque secondi.
- 13 viste training mantenute dopo scarto robusto; condizionamento 3,95.
- Punto originale: `[-0.090000, 0.000000, 0.100000] m`.
- Proposta: `[-0.098892, -0.002935, 0.102411] m`, spostamento 9,67 mm.
- Mediana errore held-out del LED: **6,277 → 2,506 px**.
- Percentile 90 held-out: **7,017 → 4,002853 px**.

Il gate predefinito richiede p90 <4 px: la proposta **non lo supera**, anche
se di pochissimo. Non viene arrotondata per farla accettare. Le corrispondenze
provengono comunque dal tracker live e hanno bias di selezione; non è una
calibrazione fisica indipendente del LED.

La proposta respinta è stata ugualmente provata come ipotesi offline, senza
modificare il rig di produzione. Peggiora il replay completo e proprio la
finestra laterale a 1448–1452 s: 26 → 44 frame invalidi.

## Tabella globale: tutti gli esperimenti sul log v2

I frame/episodi invalidi usano la finestra attiva comune. I salti usano l'intera
sequenza di campioni consecutivi validi, come lo script precedente.

| Variante | Frame invalidi | Episodi invalidi | Gap max s | Salto grezzo max mm | p99 grezzo mm | Salto pubblicato max mm | p99 pubblicato mm | Salti pubblicati >15 mm |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| V2 attuale | 276 | 85 | 0,440 | 68,21 | 4,845 | 71,76 | 3,708 | 61 |
| Fallback >=3 LED | 197 | 105 | 0,199 | 68,21 | 4,857 | 71,76 | 3,734 | 61 |
| Priorità angolare + fallback | 180 | 106 | 0,107 | 169,83 | 4,816 | 148,13 | 3,906 | 67 |
| Solo nuova proposta side_left | 300 | 84 | 0,440 | 93,73 | 4,737 | 96,41 | 3,631 | 65 |
| Fallback + proposta side_left | 218 | 102 | 0,361 | 93,73 | 4,749 | 96,41 | 3,649 | 65 |

### Finestre critiche: frame invalidi

| Secondi dalla partenza tracking | V2 | Fallback | Priorità angolare | Solo side_left | Fallback + side_left |
|---|---:|---:|---:|---:|---:|
| 796–797 | 16 | 12 | 2 | 16 | 12 |
| 1439–1442 | 51 | 23 | 29 | 51 | 23 |
| 1448–1452 | 26 | 26 | 8 | 44 | 44 |
| 1455–1456 | 33 | 14 | 6 | 33 | 12 |
| 1635–1637 | 26 | 21 | 0 | 26 | 21 |
| 1715–1717 | 24 | 13 | 10 | 27 | 11 |

## Perché fallisce il matcher nei 92 frame non associati e 2 ambigui

Un probe replica gli stessi seed, assegnazione greedy, due refit e gate del
matcher usando lo stato ricostruito della v2. Conteggi sulle **ipotesi**, non
sui frame:

- 2.253 coppie seed valide come combinazione;
- 651 scartate dal gate di movimento prima del refit;
- 1.003 scartate per riproiezione finale;
- 483 con meno di due associazioni utilizzabili;
- 109 con geometria seed non risolvibile;
- 7 ipotesi sopravvivono nei due frame ambigui: i due migliori risultati hanno
  profondità diverse e costo troppo simile, quindi il rifiuto è intenzionale.

Nei 92 frame non associati non resta alcuna soluzione. Il gate di movimento
è una potatura ordinaria di ipotesi anche sbagliate: questi conteggi non
dimostrano che allentarlo risolva il tracking. Non c'è una singola coordinata
laterale o una soglia isolata dimostrata come causa esclusiva.

## Controllo sulla prima prova

Sul log iniziale `suite_tracking_20261015T180512696028Z_0e696411.jsonl`, rispetto
alla v2 ricostruita il fallback passa da 31 a 20 frame invalidi attivi, ma da
10 a 12 episodi. Gap massimo 0,213 → 0,040 s; massimo salto pubblicato invariato
a 12,61 mm; p99 pubblicato 2,989 → 2,997 mm. Anche qui il miglioramento non è
uniforme. Le varianti angolare/geometrica già regredite sulla v2 non vengono
candidate all'integrazione e non sono qualificate come non-regressioni.

Tre test specifici passati sul rollback del prior, accettazione del triplo e
assenza di doppia riacquisizione nello stesso frame. I test di geometria/IMU
correlati sono passati durante la verifica; nessuna modifica al tracker attivo.

## File e riproduzione

Varianti: `scripts/lateral_handoff_candidates.py` (mai importato dalla suite).
Runner: `scripts/replay_evo_front_anchor.py`, opzioni `--variant coherent`,
`guided-fallback`, `angle-handoff`, `--source`, `--side-left-rig`.

Verifica punti: `scripts/verify_side_left_geometry.py`.
Probe matcher: `scripts/probe_lateral_matcher.py`.
Report nella cartella `logs/ac_evo_coherent_v2_20261015/`:
- `handoff-baseline-replay.json`;
- `handoff-guided-fallback.json`;
- `handoff-angle-priority.json`;
- `side-left-independent-validation.json`;
- `side-left-offline-proposal.json` (`approved_for_production: false`);
- `handoff-side-left-only.json`;
- `handoff-fallback-side-left.json`;
- `handoff-first-trial-regression.json`;
- `lateral-matcher-rejection-stages.json`.

Usare il Python della `.venv-camera` e `PYTHONPATH` alla root. Log grandi letti
in streaming, report piccoli e output limitato ai riepiloghi. I tempi CPU dei
replay paralleli non sono comparabili a latenza end-to-end live.
