# Tracking seduto EVO: associazioni parziali e profondità — 2026-10-15

Aggiornamento successivo: `EVO_COHERENT_TRACKING_FIX_20261015.md` descrive la
variante coerente ora integrata nella suite. Il prototipo e i risultati sotto
restano il resoconto storico dell'esperimento precedente.

## Esito

La prova evidenzia due problemi collegati: perdita della posizione ottica e
posizioni errate ancora dichiarate valide. L'IMU è rimasta valida nel log suite.
Il semplice aumento dello smoothing non affronta l'origine di questi errori.

Il prototipo descritto sotto è **esclusivamente offline**, non è collegato alla
suite e non è stato accettato nel visore. I salti residui impediscono di
considerarlo una soluzione completa.

## Evidenze nel percorso attuale

Sorgente: `logs/suite_tracking_20261015T180512696028Z_0e696411.jsonl`,
59.226 frame, durata dichiarata 799,718 s. Riepiloghi in
`logs/ac_evo_seated_20261015/`.

1. `ImuLedTracker.match_pattern` genera ipotesi da coppie di LED e le valuta
   rispetto alla costellazione e alla posizione precedente. Se restano soltanto
   due associazioni, può comunque restituire una posizione valida. La quantità
   di candidati rilevati e il numero di LED effettivamente associati sono diversi.
2. **42.470 delle 55.327 pose valide usano solo due LED**. La diagnostica di
   covarianza riporta `insufficient_leds` in 42.316 frame: è una valutazione di
   incertezza, non una prova che ogni singola posa a due LED sia falsa.
3. `PositionPipeline` in modalità One Euro registra tale qualità ma non la usa
   per rifiutare la misura: tutte le 55.327 pose ottiche valide sono pubblicate.
   Questa è la semantica attuale del filtro, non un errore del trasporto UDP.
4. Il gate di continuità ammette circa 4,1 cm per frame a 75 Hz
   (`0.015 + 2 * dt`). Limita il singolo salto, non impedisce una deriva
   progressiva con corrispondenze errate.
5. Una volta avvelenato il prior, la soluzione globale coerente con tutti i
   LED può trovarsi fuori dal gate rispetto alla posizione precedente. Il
   tracker rimane in un sottoinsieme sbagliato o alterna sottoinsiemi e perdite.
6. Il recupero assoluto si attiva dopo due secondi senza un match. Una catena
   di match parziali sbagliati rinnova `last_match` e non attiva tale recupero.

### Esempi riproducibili

- A `monotonic_s=119710.6414321`, ci sono cinque candidati, ma la posa valida
  precedente usa `centre` e `upper_right`. Un fit indipendente dei cinque LED
  con la rotazione IMU corrente dà RMS 3,36 px e profondità circa 0,900 m;
  dista **376 mm** dal prior. Non è una misura di ground truth, ma mostra
  l'incompatibilità interna tra la soluzione a due punti e il pattern completo.
- A `119741.9892349`, analogo disaccordo di **412 mm**, mentre il fit a cinque
  punti ha RMS **2,39 px** e profondità circa 0,921 m.
- Il salto grezzo massimo fra frame consecutivi validi è **40,72 mm**;
  il salto massimo nell'output effettivamente filtrato/pubblicato è **27,27 mm**.
- Sono presenti 84 transizioni driver `position_missing` con orientamento
  ancora valido; la mancanza di fallback 3DoF rende invalida la posa SteamVR.

## Coerenza tra modello ottico e rotazione IMU

È stata ricostruita l'identità dei cinque LED frontali anche nei frame in cui
il tracker live ne usava meno, tramite PnP indipendente: campionamento ogni
quattro secondi, RMS ottico massimo 1,5 px e rifiuto delle associazioni ambigue.
Per ciascun minuto, una piccola correzione di rotazione è stimata su metà
dei campioni e valutata sull'altra metà, rifittando le traslazioni.

- Minuto 0: RMS held-out della rotazione corrente circa **1,11 px**, corretto
  circa **1,07 px**.
- Minuto 12: circa **3,01 px**, corretto circa **1,05 px**.

Questo mostra un'incoerenza crescente della rotazione vincolata rispetto al
pattern ottico. Non distingue da solo deriva IMU, errori del modello, bias
dell'allineamento iniziale o errori dipendenti dall'angolo. Non giustifica una
correzione fissa dei gradi stimati né una ricalibrazione del riferimento world.
Il segno dello yaw non è stato verificato rispetto alla destra dell'utente.

## Esperimento: ancoraggio ottico frontale

`scripts/replay_evo_front_anchor.py` contiene una sottoclasse sperimentale:
quando il tracker frontale riconosce i cinque LED con le sue conferme e i
controlli PnP, ne usa la posizione, verificando la compatibilità con IMU e gate
di movimento. Per i pattern parziali usa il tracker guidato esistente.
Non modifica `alignment`, quaternion del driver o impostazioni della suite.

Replay sugli stessi candidati e timestamp dell'intera registrazione:

| Metrica | Registrato | Prototipo |
|---|---:|---:|
| Frame totali | 59.226 | 59.226 |
| Episodi di invalidità, inclusi estremi sessione | 235 | 25 |
| Episodi nella finestra comune tra prima e ultima posa valida originale | 233 | 23 |
| Frame invalidi in tale finestra comune, su 57.225 | 1.898 | 90 |
| Salti grezzi consecutivi >15 mm | 175 | 50 |
| Massimo salto grezzo | 40,72 mm | 40,72 mm |
| Percentile 99 del salto grezzo | 4,48 mm | 4,71 mm |
| Massimo gap completato dopo l'aggancio iniziale | 1,480 s | 0,627 s |

Il numero di invalidità ottiche non è il numero di schermate grigie: il driver
applica una grace. Non è stato riprodotto il feedback del detector alle nuove
ROI: sono disponibili candidati già rilevati, non immagini. Non c'è ground
truth della testa e non è una verifica del comportamento live.

I salti residui coinvolgono sia pattern parziali sia i passaggi fra soluzione
frontale libera e soluzione vincolata all'IMU. Il prototipo non viene promosso
alla suite: il massimo salto non migliora e il percentile 99 globale aumenta
leggermente. Il confronto non dimostra un miglioramento uniforme del jitter.

## Verifiche software e riproduzione

`35 passed, 93 subtests passed` su:
- `tests/test_camera_front_anchor_candidate.py`;
- `tests/test_camera_imu_leds.py`;
- `tests/test_camera_driver_link.py`.

Il candidato verifica anche IMU stale, cambio sessione, recenter senza modifica
di alignment, rifiuto di un teleport e mantenimento dei pattern laterali/posteriori.

Da root con `PYTHONPATH` impostato alla root:

```powershell
.\.venv-camera\Scripts\python.exe scripts\investigate_evo_tracking.py --root .
.\.venv-camera\Scripts\python.exe scripts\investigate_evo_alignment.py
.\.venv-camera\Scripts\python.exe scripts\replay_evo_front_anchor.py --out logs\ac_evo_seated_20261015\front-anchor-replay-full.json
```

## Intervento successivo individuato

Rendere coerenti le traslazioni fra pattern completi e parziali, impedire che
un match a due punti contraddetto dal pattern completo rinnovi indefinitamente
il prior, e distinguere misura affidabile da mera posa geometricamente valida.
Valutare le innovazioni ottico/IMU senza cambiare il riferimento assoluto del
driver. Il fallback 3DoF rimane una mitigazione separata delle schermate grigie.
