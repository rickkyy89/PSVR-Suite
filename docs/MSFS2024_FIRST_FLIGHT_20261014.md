# MSFS 2024 — confronto delle due sessioni VR nei log

## Feedback utente e prima sessione recuperata

L'utente precisa: inizialmente bene, poi scatti marcati e fallback 3DoF;
riavvio con fallback disattivato e identity, senza altre modifiche;
seconda sessione molto migliore, con pochi scatti. Identity migliora la
distorsione percepita nel gioco. Il report iniziale sotto si riferisce
alla **seconda** sessione, non alla prima.

I file Steam `vrserver.previous.txt` e `vrcompositor.previous.txt`
conservano la prima sessione. Intervallo scena MSFS:
17:50:21.660–18:19:48.791, durata 1767,131 s.
Aggregati: `logs/msfs2024_previous_session_review_20261014_v2.json`.

- Durate da transizioni, incluse testa e coda: 6DoF 1501,596 s,
  fallback 181,325 s, riaggancio 84,210 s. Nessun fault IMU registrato.
- Entrate fallback nei segmenti successivi di cinque minuti: 23, 73,
  102, 116, 361; ultimo segmento parziale: 176. Forte aumento delle
  alternanze negli ultimi minuti.
- Fallback continuo dalle 18:17:26.705 fino alla chiusura del runtime
  alle 18:19:48.791: 142,086 s, circa 2 minuti e 22 secondi.
- Massimo offset di correzione esposto durante riaggancio: 0,412 m,
  nella scala driver (non certificazione della scala fisica).
- Prima sessione: profilo ottico esplicito; seconda: identity.
- Riproiezione prima: 49.331 / 159.017 = circa 31,02%; seconda:
  18.752 / 60.369 = circa 31,06%. Zero dropped in entrambe.

Il peggioramento del tracking nel tempo è quindi coerente con il feedback.
Il fallback segnala l'assenza di posizione ottica utilizzabile; le numerose
alternanze con riaggancio possono contribuire agli scatti percepiti. La
causa a monte delle perdite di posizione non è ancora isolata. La quota
media di riproiezione quasi identica non supporta l'attribuzione del
miglioramento di fluidità al solo costo grafico della distorsione.

Identity è la scelta visiva preferita dall'utente nella prova MSFS.
Non ripristinare automaticamente peripheral o fallback: riavvio,
fallback e ottica sono cambiati insieme, quindi questa comparazione
non è una prova controllata delle singole cause.

### Scelta ottica successiva: identity anche nella Home SteamVR

L'utente conferma di mantenere identity anche in SteamVR: peripheral e
stronger deformano la scena, con la sensazione che lo sfondo sia stirato
rispetto al primo piano. Entrambi i profili sono quindi non accettati
visivamente; non ripristinarli automaticamente. Identity è stata verificata
nelle impostazioni live e salvata nelle preferenze persistenti della suite.
Questo feedback descrive una deformazione percepita, non dimostra una doppia
correzione del gioco né identifica da solo la causa matematica del difetto.

## Intervallo e fonti

14 ottobre 2026, ore locali 18:21:22.444–18:32:33.429 (UTC+2):
670,985 secondi, processo `FlightSimulator2024.exe` PID 27020, OpenXR.

- Steam: `vrserver.txt`, `vrcompositor.txt`.
- Camera: `logs/suite_tracking_20261014T162055476681Z_82cf559b.jsonl`.
- Applicazione impostazioni: `logs/suite_backups/20261014T161950Z-24584/journal.json`.
- Aggregati: `logs/msfs2024_tracking_review_20261014.json` e
  `logs/msfs2024_driver_review_20261014_v2.json`.

Analisi in sola lettura, streaming dei file grandi; nessuna modifica delle
impostazioni o riattivazione dell'hardware.

## Esiti

| Misura | Risultato |
|---|---|
| Frame camera nel periodo | 50.045, circa 74,585 fps |
| Pose ottiche valide | 49.444 (98,8%) |
| Pose ottiche invalide | 601 (1,2%) |
| Errori lettura camera, intera cattura | 0 |
| Massimo intervallo tra frame nel periodo | 86,1 ms |
| IMU durante gioco, stato driver | Tracking; nessun fault IMU nel periodo |
| Epoca di ricentraggio | Unica, 1 |
| Driver: 6DoF | 662,684 s (98,763%) |
| Driver: grace posizione, posa ancora valida | 2,801 s (0,417%) |
| Driver: posa invalida | 5,500 s (0,820%), 10 episodi |
| Episodio invalido più lungo | 2,034 s, ore 18:28:56.353 |
| Frame presentati dal compositor | 60.369 |
| Frame dropped registrati | 0 |
| Frame riproiettati | 18.752 (31,1%) |

Durate driver calcolate dalle transizioni, includendo lo stato antecedente
all'inizio e la coda finale fino all'uscita del gioco. Non sono conteggi di
campioni indipendenti. Tutti i 10 episodi invalidi hanno `position_missing`
con IMU `tracking` e orientamento valido: perdita della posizione ottica,
non perdita della rotazione IMU.

La mediana RMS delle pose ottiche valide è 1,716 px; p90 2,578 px e p99
2,963 px. I valori RMS misurano la coerenza della stima camera, non la
distorsione delle lenti. Il recupero ottico contiene un salto importante
nella posizione filtrata dopo circa due secondi senza soluzione ottica;
la scala fisica della stima non risulta validata nel log, quindi non va
interpretato come spostamento fisico misurato del visore.

## Impostazioni realmente usate

- One Euro 2,5 Hz / beta 8; camera 640×480 a 75 fps, preset 60/6/13/0.
- Fallback 3DoF **disabilitato**: chiave driver assente, default false.
  Nessuna transizione `3dof_fallback` o `6dof_reacquiring` nel periodo.
- Distorsione driver **identity**, profilo vuoto, confermata dal journal
  applicato alle 16:19:50 UTC e dall'attivazione del driver. Il valore
  `peripheral` nel file preferenze utente non prova che quel profilo fosse
  caricato durante il volo.

Prima del gioco, alle 18:20:02, la calibrazione fallisce con
`gyro variation exceeds stationary limit`. Il successivo avvio alle
18:20:47 recupera l'IMU, in tracking dalle 18:20:55. Il fault iniziale
non viene attribuito al volo.

## Prestazioni e limiti

Tempi medi riportati: applicazione CPU 10,951 ms, GPU 9,563 ms;
compositor CPU 0,262 ms, GPU 0,105 ms. CPU e GPU possono sovrapporsi:
non sommare questi valori per derivare il framerate. Il target 90 Hz
non dimostra 90 fps nativi del simulatore; la riproiezione al 31,1%
indica che la presentazione usa anche frame riproiettati.

Nessuna evidenza esplicita del codice -202 nel log compositor esaminato;
questo non equivale a una misura continua del fullscreen. Zero dropped
non prova assenza di qualsiasi errore. La qualità visiva della distorsione
e il comfort richiedono il feedback dell'utente.

## Ipotesi di prova proposta prima del feedback

Il report iniziale proponeva di abilitare il fallback per coprire le
perdite ottiche. Alla luce del feedback, questa proposta non equivale
alla scelta operativa accettata dall'utente: mantenere identity e la
configurazione della seconda sessione come riferimento, e indagare
prima il degrado della disponibilità della posizione nella prima.
