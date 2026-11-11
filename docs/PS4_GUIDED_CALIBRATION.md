# PS4 Camera — calibrazione guidata sul monitor

La finestra riusa il layout e il conto alla rovescia della calibrazione PS Eye
già utilizzata: `guided_calibration.build_guided_window` e `CaptureGuide`.
La versione PS4 aggiunge acquisizione accoppiata, controlli per entrambi gli
occhi e calibrazione stereo. **Non occorre indossare il visore.**

## Avvio

Suite → Camera → PS4 Camera → **Calibrazione guidata stereo**.
Se la Suite era già aperta prima dell'aggiornamento, riavviarla per vedere il
nuovo pulsante. Indice verificato su questo PC: `0`, backend `dshow`, modalità
iniziale consigliata `640x400`, `60 FPS`.

Oppure dalla cartella del progetto:

```powershell
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.ps4_guided_calibration --index 0
```

La camera deve essere già `USB Camera-OV580` in modalità UVC; non vengono
installati driver né caricati firmware dalla procedura guidata.

## Cosa fare

1. Usa la stessa scacchiera della PS Eye, se ancora disponibile:
   `out/pseye/chessboard_9x6_20mm_A4.svg`, **9×6 angoli interni**, quadrati
   dichiarati da **20 mm**. Non confonderla con il diverso file da 25 mm.
   Conferma la dimensione effettiva del foglio stampato nella casella apposita.
2. Guarda il monitor: a sinistra e a destra vedi i due flussi. Le sigle L/R
   identificano l'ordine dei flussi, non un ordine fisico già verificato.
3. Metti tutto il foglio nell'inquadratura di **entrambi** gli occhi. I vertici
   rilevati vengono sovrapposti alle immagini; il contatore deve mostrare
   **L: 54/54 e R: 54/54**.
4. Premi **AVVIA RACCOLTA** solo quando sei pronto. Non viene acquisita alcuna
   vista prima del clic. L'attesa iniziale può durare fino a **15 minuti**, non
   60 secondi; poi la raccolta ha un budget di **5 minuti**.
5. Segui la **prossima posa** indicata: centro, inclinazioni nei due assi,
   bordi, vicino/lontano. Dopo ogni scatto hai 3 secondi per muovere il foglio;
   servono poi 2 secondi di stabilità per acquisire. Un suono conferma lo scatto.
6. Si raccolgono **15 coppie**, non 15 immagini indipendenti per occhio.
   Se un occhio perde la scacchiera o si muove troppo, il conto si riavvia.
7. Al termine la camera viene rilasciata e i controlli scuri ripristinati,
   **prima** del calcolo. La finestra mostra il risultato e resta aperta finché
   la chiudi; nessun profilo di tracking viene attivato automaticamente.

Per un foglio realmente diverso sono disponibili `--cols`, `--rows` e
`--square-mm`: non modificare la misura solo per far passare il calcolo.

## Luminosità e chiusura sicura

La procedura richiede temporaneamente gain `8` e luminosità `8`, provati su
questa camera, senza cambiare l'esposizione. I valori precedenti vengono salvati
dopo la negoziazione del formato e riscritti sullo stesso proprietario della
camera alla fine. Il driver può fornire un readback immediato non aggiornato:
questa limitazione è registrata nel report, non trasformata in una verifica
hardware inesistente. I valori non sono unità universali; CLI: `--gain` e
`--brightness` permettono richieste esplicite diverse.

Usa **CHIUDI / ANNULLA**, la X della finestra oppure **Ferma** nel pannello
PS4 della Suite: la richiesta è cooperativa. Anche se il driver tarda a
terminare una lettura, finestra/processo rimangono vivi per consentire il
ripristino; viene mostrato “ripristino in corso”. Non usare Task Manager per
forzare l'uscita e non scollegare USB durante questa fase, salvo accettare che
il ripristino non possa essere garantito. Un cleanup concluso con errore è
distinto da uno ancora attivo e viene segnalato.

## Calcolo e file conservati

- **12 coppie** per il calcolo: inizializzazione monoculare, poi raffinamento
  congiunto delle due intrinseche, distorsione, rotazione e traslazione stereo
  in metri, matrici di rettificazione. Non bloccare le intrinseche alle sole
  stime monoculari iniziali.
- **3 coppie escluse da ogni fitting** per verifica epipolare e di riproiezione.
- RMS e geometria non validi, pose troppo simili, dati non finiti o ordinamenti
  incoerenti causano un rifiuto, non una calibrazione inventata.
- Le osservazioni dei vertici, con sequenza e tempo host comune, sono salvate
  come `logs/ps4_camera/calibration/ps4_stereo_<data>.views.json`, anche se la
  raccolta è parziale o il calcolo fallisce. Non viene registrato un video.
- Solo se passa i controlli viene creato il nuovo profilo stereo `.json`,
  senza sovrascrivere file esistenti. La vecchia calibrazione PS Eye rimane
  invariata. Il tempo host **non** è un timestamp hardware di esposizione.

La scacchiera fisica, il codice e il workflow PS Eye si riusano; le sue vecchie
viste e i suoi coefficienti non descrivono i due nuovi obiettivi PS4.

## Stato di verifica

La nuova UI è stata aperta sulla camera reale: anteprima di entrambi gli occhi,
70 aggiornamenti in una prova breve, **zero viste** senza AVVIA, chiusura e
cleanup completati, nessun file di intrinseche prodotto. I test sintetici
coprono inoltre conteggio/raccolta accoppiati, movimento in un solo occhio,
pause temporali, conferma/avvio, fitting e verifica separati, rifiuto di una
verifica fallita, salvataggio senza sovrascrittura e cleanup ritardato.
Regressione mirata finale: **370 test superati, 4 saltati**, più 237 subtest;
revisione indipendente della chiusura cooperativa completata senza blocker.

### Raccolta reale completata e recupero offline

L'utente ha completato **15 coppie** nella sessione
`ps4_stereo_20261023T231947385332Z`. Il primo calcolo le ha correttamente
rifiutate perché il solver bloccava parametri monoculari separati poco coerenti
fra loro. L'analisi indipendente ha escluso la necessità di invertire gli angoli
di un occhio: non è stato necessario ripetere la raccolta.

Il solver corretto raffina congiuntamente K/distorsione/R/T **solo sulle stesse
12 coppie di training**. Le altre 3 restano escluse dal fitting. Non sono stati
scartati fotogrammi, cambiati ordinamenti o allargate le soglie per far passare
il risultato. Le metriche per occhio salvate sono ricalcolate sui parametri
finali, non quelle delle inizializzazioni precedenti.

Risultato salvato e riletto con il validatore stretto:

- Profilo di riferimento: `configurations/ps4_stereo_640x400_calibrated.json`.
- RMS stereo: **0,27665 px**.
- Verifica sulle 3 coppie escluse: RMS **0,27589 px**, massimo per vista
  **0,30075 px**; epipolare massimo **0,06711 px**.
- Baseline stimata: **80,87 mm**, subordinata alla scala 20 mm confermata
  dall'operatore; non è una misura indipendente con calibro.
- Originali preservati:
  `logs/ps4_camera/calibration/ps4_stereo_20261023T231947385332Z.views.json`.
  SHA-256: `8770f9d897d01721841c737308fd7762d6598bb66134166932b59249020401fd`.
- Report di recupero:
  `logs/ps4_camera/calibration/ps4_stereo_20261023T231947385332Z.recovery-report.json`.

Il ricalcolo offline non apre la camera e non modifica le viste sorgenti:

```powershell
.\.venv-camera\Scripts\python.exe -m tools.psvr_camera.ps4_guided_calibration --solve-views logs/ps4_camera/calibration/ps4_stereo_20261023T231947385332Z.views.json --out logs/ps4_camera/calibration/nuovo_profilo_stereo.json
```

L'output deve essere un nome nuovo: nessuna sovrascrittura. La geometria della
scacchiera è letta dalle osservazioni originali, non cambiata arbitrariamente
tramite opzioni CLI. Dopo la raccolta, una riapertura indipendente ha confermato
esposizione `-3`, gain `4`, luminosità `4`: profilo LED scuro ripristinato.

Questa è una calibrazione geometrica stereo con verifica su dati esclusi, **checkpoint pre-2026-11-01**; profilo attuale -7/8/1; **6DoF statico in FRONT5** (non ancora tracciamento in PS4_STEREO_POINTS.md).

Regressione dopo il raffinamento congiunto e il replay offline: **373 test
superati, 4 saltati**, più 237 subtest. Rilettura del profilo con validazione
stretta e SHA-256 invariato delle osservazioni originali confermati.

### Prova statica punti stereo (2026-10-24, visore fermo ~30 s)

1688 coppie a 60,08 FPS di callback, 1687 stati `points` (1662 con 5 punti),
RMS di riproiezione 0,145 px, profondità 0,746–0,843 m con il profilo sopra.
Nessuna posa testa, ID anatomici, UDP o 6DoF; sole coordinate left-camera;
timestamp host, non esposizione. Dettagli e file:
[PS4_STEREO_POINTS.md](PS4_STEREO_POINTS.md).
