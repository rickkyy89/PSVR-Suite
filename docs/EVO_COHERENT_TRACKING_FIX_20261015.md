# Correzione tracking coerente completo/parziale — 2026-10-15

**Esito della prova successiva: fallimento hardware della v1.** Il blocco
progressivo del gate a dieci gradi è descritto e corretto nella v2 in
`EVO_GRAYSCREEN_LOCKOUT_FIX_20261015.md`. I risultati sotto sono storici;
non rappresentano accettazione nel visore.

## Stato

Implementata e integrata nel worker camera della suite. Verificata offline
su tutta la registrazione EVO e con test sintetici/integrativi. **Accettazione
nel visore ancora da effettuare.** Una suite già aperta deve essere chiusa e
riaperta per caricare il nuovo codice Python.

Questa variante sostituisce, per la suite, il prototipo solo frontale descritto
in `EVO_SEATED_TRACKING_INVESTIGATION_20261015.md`. Il tracker base rimane
disponibile ai precedenti consumatori/CLI; l'estensione `matching_rotation`
mantiene il suo comportamento originale.

## Intervento

- `tools/psvr_camera/coherent_imu_led_pose.py`: nuovo `CoherentImuLedTracker`.
- I cinque LED frontali confermati ancorano la traslazione; la ricostruzione
  usa la stessa rotazione ottica anche per i pattern parziali. Si evita quindi
  di alternare due diverse geometrie di stima al cambio delle luci visibili.
- Una piccola innovazione ottico/IMU viene aggiornata lentamente: costante
  temporale 0,75 s, velocità massima 2 gradi/s, compatibilità entro 10 gradi.
  Essa corregge soltanto proiezione e geometria ottica: **`alignment` assoluto
  e quaternion del driver non vengono modificati**. Non è una correzione
  dell'orientamento mostrato da SteamVR né una calibrazione metrica del visore.
- Con pattern completo confermato ma incompatibile, viene pubblicata perdita;
  non si ripiega su una coppia arbitraria che lo contraddice.
- Nei fit parziali, un prior metrico di movimento stabilizza soprattutto la
  profondità poco osservabile. Rumore centroide 1,5 px; sigma del prior
  `0.003 + 0.5*dt` metri, centrata sulla traslazione prevista. Si applica solo
  su intervalli reali `0 < dt <= 0.15 s`. Sono sempre richiesti frame,
  IMU fresca e corrispondenze ottiche; una perdita non è promossa a misura.
- Quando sono associati tutti i cinque frontali, eventuali LED extra con
  geometria laterale/posteriore approssimata restano nella verifica del pattern
  ma non trascinano il fit frontale in profondità.
- Dopo perdita driver prolungata, il prior viene eliminato senza cambiare
  il riferimento world. Il riaggancio richiede quattro frame frontali distinti,
  evitando di contare due conferme nello stesso frame.

## Integrazione e diagnostica

`tools/psvr_suite/tracking.py` usa il nuovo tracker sia all'avvio sia dopo una
riapertura camera. Le impostazioni utente One Euro, esposizione, guadagno,
modalità e fallback restano quelle della prova per un confronto confrontabile.

Ogni riga JSONL aggiunge `optical_tracking` con:
- `algorithm: coherent_imu_led_v1`;
- innovazione ottica in gradi sugli assi camera;
- motivo del frontend e politica `world_alignment_policy: fixed`;
- parametri del prior parziale.

La covarianza ora riceve tutti i nove punti/etichette del rig realmente usato
dal tracker. Prima i LED laterali/posteriori potevano essere inviati al
calcolo insieme al solo rig frontale, generando `invalid_correspondences`.
Il nuovo motivo `imu_guided_front_anchor` è riconosciuto come fit guidato.

## Replay finale

Sorgente: `logs/suite_tracking_20261015T180512696028Z_0e696411.jsonl`.
Report: `logs/ac_evo_seated_20261015/coherent-replay-final.json`.
59.226 frame, elaborati in ordine originale. Stessi parametri One Euro e
trasformazione posizione/testa della sessione registrata.

| Metrica | Registrato | Correzione |
|---|---:|---:|
| Episodi invalidi nella finestra comune prima/ultima posa valida originale | 233 | 10 |
| Frame invalidi in tale finestra, su 57.225 | 1.898 | 31 |
| Massimo gap completato dopo il primo aggancio | 1,480 s | 0,213 s |
| Salti grezzi consecutivi >15 mm | 175 | 14 |
| Salto grezzo massimo | 40,72 mm | 36,30 mm |
| Percentile 99 del salto grezzo | 4,48 mm | 4,18 mm |
| Salti pubblicati consecutivi >15 mm | 5 | 0 |
| Salto pubblicato massimo | 27,27 mm | 12,61 mm |
| Percentile 99 del salto pubblicato | 3,49 mm | 2,99 mm |

I salti sono distanze tra campioni consecutivi validi, non misure di errore
rispetto alla testa. Gli episodi ottici invalidi non equivalgono al numero
di schermate grigie: interviene la grace del driver. Il replay usa candidati
già rilevati e non riproduce il cambiamento delle ROI sulle immagini reali.
Non comprende rendering, latenza fisica, conferma della destra anatomica o
un ground truth della posizione. Restano salti grezzi e perdite residue.

Tempo del solo `update` su questo host nel replay: mediana 0,319 ms,
p95 0,489 ms, p99 9,20 ms, massimo 53,38 ms. Il massimo include acquisizioni
e associazioni difficili; non è una misura di latenza end-to-end live.

## Test

**228 test passati, 217 sottotest passati** nei moduli di tracking coerente,
IMU/LED, driver link/Python, incertezza, pipeline/filtro posizione, suite
tracking, sorgenti camera, impostazioni consumate, preferenze iniziali,
app model e prototipo precedente.

Regressioni specifiche: rotazioni su entrambi i lati fino a posteriore,
clutter, rumore su due LED, traslazione a 1,2 m/s, gap breve, innovazione lenta,
teleport, IMU stale, cambio sessione, recenter e perdita lunga. Nel caso
sintetico di traslazione parziale l'errore resta sotto 25 mm a 1,2 m/s
(equivalente a meno di 21 ms se attribuito tutto a lag); non è latenza misurata.

## Prova nel visore

1. Chiudere e riaprire la suite tramite `run_suite.ps1`.
2. Agganciare frontalmente e mantenere la testa ferma per 20–30 secondi.
3. Eseguire rotazioni lente a destra e sinistra, tornare al centro, poi guidare
   qualche minuto nelle condizioni della prova originale.
4. Annotare eventuali grigi, scatti e ricentraggi. Nei log verificare
   `optical_tracking.algorithm = coherent_imu_led_v1` prima di confrontare.

Il fallback rimane disattivato come nella prova iniziale: eventuali riduzioni
dei grigi dovranno essere dovute al recupero del tracking, non a un cambio di
politica che nasconda la perdita della posizione.
