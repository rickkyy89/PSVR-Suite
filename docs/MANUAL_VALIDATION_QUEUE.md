# Controlli manuali rinviati — riprendere al ritorno dell'utente

Sezioni 1–5: stato storico 2026-10-04 della sessione autonoma. Sezione 6:
aggiornata dopo il trial live del 12/13 ottobre. Non dichiarare milestone
complete prima dei rispettivi gate.
Sezione 7: verifiche fisiche della review blocco 2, rinviate il 2026-10-15
su richiesta dell'utente. Riprenderle quando l'utente sarà disponibile.
Sezione 2: residui M3 identificati come B3-HW-01/02 dopo la review del
2026-10-15; anch'essi rinviati in attesa della disponibilità dell'utente.
Sezione 8: verifiche della camera per il blocco 4, salvate il 2026-10-15
e rinviate; riprendere dopo le correzioni software quando l'utente può.
Blocchi 6 e 7 (review + fix software 2026-10-16): **nessuna validazione
fisica richiesta** — difetti di contratto, robustezza e diagnostica
riproducibili offline; dettagli in `../review_261015.md` §9.6 e §10.5.

## 1. Preparazione

- Chiudere acquisizione/monitor diagnostico prima delle prove driver.
- Build/install con `build.ps1` dopo chiusura per aggiornare `out/diag`;
  nuova DLL e inventario sono già nei rispettivi package `out/`.
- Confermare sensori MI_04 HID, controllo MI_05 WinUSB; nessuna modifica binding.
- Conservare log/capture precedenti. Annotare versione SteamVR, Windows e GPU.

## 2. Gate M3 residui

- [ ] **B3-HW-01 — Rinviato: recenter yaw fisico.** Con tracking pronto e
  modalità yaw, verificare azzeramento della direzione orizzontale e
  conservazione del tilt pitch/roll. Richiede posizionamento/movimento del
  visore da parte dell'utente; conservare configurazione, log/CSV e confronto
  prima/dopo. Non confondere con il full recenter, che azzera la posa neutra.
- [ ] **B3-HW-02 — Rinviato: ripetizioni termiche e condizioni d'uso.**
  Ripetere calibrazione e misura statica con temperatura/condizioni diverse e
  immobilità fisica verificata; conservare tempi, condizioni, log/CSV e drift.
  Il PASS storico di una singola sessione non garantisce il drift nelle altre.
- Assi, confronto presenza, USB loss/restart sono già documentati in
  `M3_PHYSICAL_CHECKS_20261004.md`, non ripeterli come mancanti senza motivo.

Review e correzioni software del blocco 3 completate il 2026-10-15:
B3-01/02/03 risolti; 21 scenari capture, 16 traiettorie e 20/20 CTest
selezionati PASS. Soglie di invalidazione invariate e inclusive; dettagli in
`../review_261015.md` §6.6.
Questi due gate fisici sono salvati per la ripresa quando l'utente può e non
bloccano il lavoro software successivo. Alla ripresa integrare nella stessa sessione
una verifica qualitativa della risposta del visore con neutro inclinato:
il confronto numerico tra quaternion e velocità richiede comunque i test
offline e non può essere certificato dalla sola impressione nelle lenti.

## 3. Gate M4 runtime

- Individuare percorso SteamVR reale; Inspect/DryRun degli script disponibili.
- A SteamVR chiuso registrare il driver e verificare rimozione/reinstallazione.
- Opt-in `enable=true`, mantenendo `displayEnabled=false` inizialmente:
  osservare riconoscimento HMD/pose, calibrazione su tavolo, assenza/staleness,
  DebugRequest recenter/recalibrate, lifecycle e perdita USB nel driver.
- Esaminare `vrserver.txt`/`vrcompositor.txt`; i mock non provano compatibilità
  dell'host reale o necessità display per selezione HMD nella versione installata.

## 4. Gate M5 display

- Eseguire `.\out\display\psvr_display.exe --json`, identificare manualmente
  PSVR e conservare output. Non selezionare automaticamente il primo 1920x1080.
- Confermare HDMI e desktop esteso sul PSVR, unrotated/progressive/no-clone,
  scaling identity. Confrontare refresh path/signal esatti; differenze richiedono
  analisi, non arrotondamento per aggirare il gate.
- Preparare settings espliciti dal report M5; diagnostic projection/identity
  distortion non sono accettazione ottica. Fare backup degli override SteamVR.
- Verificare compositor sul pannello selezionato, L/R, dimensioni/refresh,
  vsync e frametime. Driver non cambia il modo video né invia VR-mode:
  eventuale cambio power/mode deve essere esplicito e verificato con tool M2.
- Provare perdita/cambio topologia e richiesta restart: controllare che il
  compositor si arresti/reinizializzi correttamente. La richiesta SDK non
  attesta un arresto immediato. Nessuna fallback selection automatica.

## 5. M6 ottica e milestone applicative

- Loader/adapter e pattern sono ora implementati e testati software. Scegliere/
  misurare un profilo, validarlo con `out/optics/psvr_optics.exe`, configurare
  `distortionMode=profile` e path assoluta. Identity esempio non misurata;
  exit 0 non certifica le lenti.
- Verificare pattern `out/patterns/` (griglia/cerchi/L-R/bordi/centri/colore/
  scala) attraverso la pipeline corretta. SVG da solo non prova UV o timing.
- Solo dopo display/ottica accettati: player video M7, Assetto Corsa M8,
  posizione webcam M9 e MSFS/OpenXR M10. Nessuna compatibilità gioco dichiarata
  dalle sole build software.

## 6. Bridge 6DoF driver/camera — trial parziale, ripresa successiva

Implementazione software completata: export IMU dal solo driver proprietario
HID, sorgente Python UDP, posizione v2 vincolata a sessione/epoch/timestamp,
offset/scala configurabili e invalidazione su perdita. **55/55 CTest** passati,
suite camera **213 test, 7 skip previsti, nessun fallimento** (ultimo: test
noise profondità 3mm a 75Hz con >20% riduzione std vs vecchio profilo e step
10cm >95% entro 80ms), incluso interop C++/Python reale su
loopback con sensori simulati. Non è accettazione del 6DoF nelle lenti.
Matcher con gate `min(0.25, 0.015+2*elapsed)` e costo continuity non saturato
già attivo nella prova live; nuovo profilo leggero One Euro finale 2.5Hz/8
come default Suite (`min_cutoff_hz`/`beta` nelle preferenze Tracking,
legacy 8/15 conservati per rollback) **NON ancora caricato** (camera live usa 8/15).
DLL invariata `284ECF913DF43B151F1780F6FD6962372D5BD64F1B4DF638E415D9BD83BA3ADE`
grace 80ms.

Riferimento: [SIXDOF_DRIVER_BRIDGE.md](SIXDOF_DRIVER_BRIDGE.md).
Package `out/driver_psvrpc_6dof/psvrpc` registrato e provato live, export IMU e
posizione abilitati, grace 80ms. Backup baseline:
`logs/sixdof_live_trial_20261012/`. Feedback finale: lateralità abbastanza
precisa; profondità instabile anche quasi da fermo, scatti forse diminuiti,
**un flash** nell'ultima prova. Non è accettazione finale.

Nuovo profilo anti-noise pronto e testato: la Suite (`TrackingWorker`,
Tracking tab, 6dof) usa min cutoff 2.5Hz / beta 8 (max 40Hz, derivata 4Hz),
selezionabile nelle preferenze con rollback ai legacy 8/15. **Log storico
di riferimento:** `logs/driver_camera_20261012_234154_50e426.jsonl`
(la camera live precedente usava 8Hz / beta 15; confronto fisico rinviato).
Nessun processo chiuso al termine della sessione. Ultimo probe: fullscreen true ma posizione
mancante/pose invalide; verificare aggancio prima di riprendere.

- [ ] Riavviare solo camera con log nuovo, visore frontale; verificare nel log
      profilo 2.5Hz / beta 8, IMU fresco, fullscreen e pose valide, poi recenter.
- [ ] Quiete 20–30s: misurare oscillazione profondità raw/filtrata; traslazioni
      note 10cm, prioritariamente Z. Se deriva lenta resta, verificare geometria
      e corrispondenze due-LED prima di aumentare la forza del filtro.

- [ ] Scala metrica: traslazioni note in X/Y/Z e rapporto scala misurato.
- [ ] Offset rig → centro tra occhi / origine pose HMD; validazione con pivot
      noto, senza assumere zero traslazione reale durante movimenti del collo.
- [x] Preview da IMU del driver provata: unico proprietario HID, niente diagnostica
      IMU standalone contemporanea; setup conserva display/presenter verificato.
- [ ] Trial posizionale SteamVR: direzioni, recenter yaw/full, nuova sessione,
      perdita/recupero, near/far e pattern frontali/laterali/posteriori.
      Eseguito parzialmente; centrature e pose valide verificate, lateralità
      discreta. Stabilità Z e recupero restano da accettare.
- [ ] Fluidità nelle lenti, ritardo effettivo, fullscreen, flash e -202.
- [ ] Ulteriori viste laterale sinistro/posteriori se necessarie per precisione.

Stato finale 2026-10-12 **NON accettato**: lateralità abbastanza precisa,
problema principale oscillazione profondità/distanza quasi da fermo; scatti
forse diminuiti, **UN SOLO flash** ultima prova. Log
`logs/driver_camera_20261012_234154_50e426.jsonl`: ultimi 180s 13503 frame /
13468 valid, dt median 13.3265ms p99 14.164ms, filtered max step 27.81mm p99
10.943mm, invalid gaps max 79.826ms; nessun confronto fisico controllato col
prima. Ultimo probe fullscreen true ma pose invalide `position_missing`
(IMU tracking), non dire tracking attivo verificato ora. Recovery 202 con
solo restart `vrcompositor`; nessuna garanzia persistenza. Nessun processo
chiuso a fine sessione.
Prossima sessione: visore frontale camera, avviare il tracking dalla Suite
(`.\run_suite.ps1`, Tracking tab, 6dof; nuovo `logs/suite_tracking_*.jsonl`), verificare IMU+fullscreen,
recenter, test fermo 20-30s + movimenti 10cm XYZ. Se resta drift lento,
indagare bias geometria/corrispondenze 2 LED prima di filtri più pesanti.
Nessuna calibrazione scala/head-offset validata; Assetto Corsa rinviato.

Il tracking ottico a 180° e il preset **60/6/13/0** sono già accettati
dall'utente; non richiedere nuovamente la scacchiera come prerequisito.

## 7. Review blocco 2 — verifiche fisiche rinviate (2026-10-15)

**Stato: salvate e rinviate in attesa della disponibilità dell'utente.**
Richiesta: «i punti che rimangono aperti per la necessità di un mio intervento
tienili salvati che li affrontiamo quando posso.» Non sollecitare questi
gesti come prerequisito del lavoro software successivo. Restano verifiche
aperte, non eseguite né superate dai test automatici.

Software completato: B2-01 corretto, contratto MI_04/MI_05 definito,
41 scenari consumer + 53 backend + 4 ownership PASS, 17/17 CTest PASS.
Riferimenti: `../review_261015.md` §5.7 e `../TESTING.md`, checklist hardware.

### B2-HW-01 — Scollegamento USB durante capture

- [ ] **Rinviato — serve intervento fisico dell'utente.**
- Usare il diagnostico compilato dopo le correzioni del blocco 2, senza
  altri owner PSVR; confermare MI_04 HID e MI_05 WinUSB.
- Avviare capture passiva di 5 secondi con `--config .\config.json`,
  `--duration-ms 5000` e una nuova directory `--log-dir`.
- L'utente scollega **solo USB** dopo l'avvio della capture.
- Verificare errore I/O esplicito o conclusione con dati già disponibili,
  uscita senza crash/blocco e chiusura delle risorse.
- Conservare percorso/versione del binario, comando, log, exit code e
  osservazione fisica; non segnare PASS basandosi soltanto sui fake.

### B2-HW-02 — Riapertura dopo ricollegamento USB

- [ ] **Rinviato — segue B2-HW-01 e richiede il replug dell'utente.**
- Ricollegare USB e ripetere la capture con lo stesso binario e una nuova
  directory di log, senza cambiare binding.
- Verificare riapertura di sensore e controllo, report sensore validi,
  conclusione regolare e chiusura degli handle.
- Conservare log ed exit code; aggiornare questa coda, `TESTING.md` e
  `review_261015.md` soltanto dopo la verifica reale.

Questi due punti non riaprono rilievi software già risolti. La validazione
hardware del blocco 2 rimane incompleta finché non saranno verificati.

## 8. Review blocco 4 — camera e backend, prove rinviate (2026-10-15)

**Stato: salvate, non eseguite, in attesa della disponibilità dell'utente.**
Review e correzioni software completate, B4-01..06 risolti: dettagli in
`../review_261015.md` §7.6. Verifica offline: 130 test Python + 63 subtest
PASS (7 skip previsti), 3/3 CTest nativi PASS, shim e upstream completi
compilati/linkati in temp. Le prove seguenti restano rinviate e vanno
riprese con shim/upstream ricompilati insieme, marker patch-level 3, quando
l'utente è disponibile. Nessuna verifica hardware eseguita automaticamente.
Conservare i risultati storici di `tools/psvr_pseye/README.md` e il preset
ottico già accettato; non richiedere una nuova scacchiera come prerequisito.

### B4-HW-01 — Perdita e riapertura della sola camera

- [ ] **Rinviato — scollegamento/ricollegamento fisico della PS3 Eye.**
- Usare una sessione dedicata alla camera con sorgente/indice espliciti,
  senza altri owner della stessa camera; registrare versione/hash DLL,
  binding esistente, modalità richiesta e reale.
- Durante una capture scollegare solo il cavo USB della **camera**, non
  quello del PSVR. Verificare fallimento esplicito della capture, arresto
  senza blocchi, niente frame vecchi ripubblicati come nuovi e risorse
  effettivamente rilasciate. B4-01 è coperto offline; ciò non sostituisce
  questa verifica USB reale.
- Ricollegare e richiedere inventario/nuovo open nello stesso processo:
  conteggio aggiornato, identità/indice coerenti, nuove sequenze/timestamp
  e frame validi. Verificare anche avvio processo con camera assente, poi
  collegamento e apertura esplicita. Non dichiarare auto-reconnect.
- Conservare log, errori/tempi di shutdown e riapertura; aggiornare coda
  e review solo dopo la verifica reale.

### B4-HW-02 — Sorgenti disponibili e cambio camera

- [ ] **Rinviato — conferma della camera/immagine da parte dell'utente.**
- Provare solo le sorgenti realmente disponibili e scelte esplicitamente
  (PS3 Eye nativa, OpenCV DSHOW/MSMF/ANY se applicabili); nessun cambio driver
  USB o selezione silenziosa di un altro indice.
- Registrare modalità richiesta/negoziata, immagine ricevuta, frequenza,
  timestamp/sequenze e stop/restart/cambio sorgente senza lock o owner residui.
  Nei report finiti OpenCV registrare `cleanup_pending`: se vero la chiusura
  non è stata confermata entro la deadline; non dichiarare risorse rilasciate
  finché il proprietario non è effettivamente rientrato dalla chiamata nativa.
  Le proprietà timeout OpenCV non supportate non sono da sole un fallimento
  della camera; verificare il limite esterno delle sonde dopo B4-02.
- Le intrinseche appartengono alla camera e alla modalità corretta: il cambio
  sorgente non trasferisce automaticamente la calibrazione PS3 Eye.
- Se una sorgente non è disponibile registrare il motivo, senza dichiararla
  funzionante o cambiare binding per completare la checklist.

## 9. Review blocco 5 — transizioni e scatti residui (2026-10-16)

### B5-HW-01 — Verifica differenziale delle transizioni, rinviata

- [ ] **Rinviata: serve la disponibilità dell'utente, non un prerequisito
  per proseguire le correzioni software.** È l'ottimizzazione residua già
  rinviata in `CONSTELLATION_RECOGNITION_20261016.md`, non un nuovo fallimento
  della baseline qualitativamente accettata.
- Conservare il feedback «molti meno gray screen, piccoli scattini residui».
  Prima di altri cambi identificare `optical_tracking.algorithm=constellation_led_v1`,
  rig/intrinseche e impostazioni effettive; non riattivare implicitamente le
  coppie né spostare i punti del rig o cambiare fallback per mascherare dropout.
- Dopo la correzione B5-07, quando l'utente può, confrontare rotazioni dx/sx
  attraverso 20–45°, ritorno frontale e rotazioni più ampie; annotare scatti
  percepiti e gray screen separatamente dai frame ottici invalidi.
- Conservare log con famiglia scelta, RMS, supporto LED, ragione di rifiuto,
  stato IMU e timestamp. Non associare automaticamente i 34° fra rami planari
  del repro ArUco a una soglia di rotazione testa: sono fenomeni distinti.
- I replay correnti usano candidati registrati e non riproducono il feedback
  ROI sulle immagini. Misurare separatamente il costo del detector su scene
  reali prima di attribuire gli scatti al white-core o a un cambio famiglia.
- Nessuna nuova scacchiera/calibrazione richiesta per proseguire la review;
  conservare intrinseche blue e rig accettati. Scala fisica/origine anatomica
  restano nei limiti già documentati, non validate dalla sola regolarità visiva.

Stato software (2026-10-16): blocco 5 completato, B5-01..07 risolti e
verificati 7/7 (6 P2 + 1 P3). Dettagli in `../review_261015.md` §8.6.
Resta rinviata solo la verifica fisica B5-HW-01 sopra; ciò non è
certificazione hardware né review dei restanti blocchi.

## 10. Review blocchi 6 e 7 — nessuna prova fisica richiesta (2026-10-16)

- Blocchi 6 (stima/filtraggio posizione) e 7 (ponte camera–IMU–driver):
  review + fix software completati, B6-01..09 e B7-01..09 risolti e
  verificati. Nessun cambio a soglie, profili accettati o tolleranze di
  tracking. Dettagli in `../review_261015.md` §9.6 e §10.5.
- Nessuna voce di coda fisica aggiunta di proposito: i rilievi erano difetti
  di contratto, robustezza su input non previsti e diagnostica,
  riproducibili offline. Il P0 del blocco 7 era confinato al trial
  diagnostico in modo driver (suite live immune).

## 11. Review blocco 8 — nessuna prova fisica richiesta (2026-10-16)

- Blocco 8 (driver OpenVR: ciclo di vita e pubblicazione HMD): review + fix
  software completati, B8-01..06 risolti e verificati. Percorso pose
  byte-identico, nessuna soglia o profilo toccato. Dettagli in
  `../review_261015.md` §11.4.
- Nessuna voce di coda fisica aggiunta: interleaving concorrenti verificati
  con mock/harness offline; nessuna garanzia SteamVR aggiuntiva affermata.

## 12. Review blocco 9 — validazione HMD reale rinviata (2026-10-16)

- Blocco 9 (display e presentazione video): review + fix software
  completati, B9-01..09 decisi/risolti e verificati. Dettagli in
  `../review_261015.md` §12.4.
- [ ] **Rinviata a futura sessione hardware (non bloccante): validazione su
  HMD reale di modalità direct nativa (B9-01: produttore affidabile +
  integrazione helper) e device-lost/TDR (B9-02: reason registrata,
  policy fail-closed confermata).** Richiede visore e disponibilità
  dell'utente; non è prerequisito per proseguire la review dei blocchi
  10–14 né certifica alcunché da sola.

## 13. Tasti visore (Vol+/Vol-/Mute) — prove fisiche rinviate (nota storica 2026-10-18)

**Stato: salvate e rinviate in attesa della disponibilità dell'utente.**
Riferimento: `docs/HEADSET_BUTTONS_20261018.md`. Decoder e forwarding **implementati** il 2026-10-18 (verifica di sola lettura ivi registrata); la checklist sotto resta prova fisica non sostituibile dai test offline. Richiede visore in mano all'utente; non sollecitare questi gesti come prerequisito del lavoro software successivo.

**Prerequisiti.**

- Audio VR funzionante per distinguere l'effetto: matrice A/B SIE-HMD vs
  PS VR Audio ancora da completare in VR (`docs/AUDIO_TRACKING_CAMERA_20261015.md` §1).
  A tasti funzionanti ma audio muto non si distingue l'effetto.
- Build/install con `build.ps1` dopo le modifiche di decoder + forwarding;
  annotare versione binario/driver, Windows, GPU e SteamVR.

### B13-HW-01 — Arrivo byte tasti su MI_04 (Vol+/Vol-/Mute)

- [ ] **Rinviato — serve pressione fisica dei tasti sul visore.**
- Usare il diagnostico compilato dopo le correzioni, senza altri owner
  PSVR; confermare MI_04 HID aperto per l'IMU.
- Avviare cattura raw MI_04 (`psvr_diag` capture) e premere in sequenza
  Vol+, Vol-, Mute; verificare transizioni byte 0 (2/4/8), byte 2
  (livello volume) e byte 8 bit3 (microphoneMuted).
- Se i byte non cambiano, la PU li consuma internamente: il piano cambia
  (mixer hardware USB-audio, non forwarding) e va registrato, non forzato
  via payload USB inventati (regola M2).
- Conservare comando, log/capture raw, exit code e osservazione fisica.

### B13-HW-02 — Forwarding verso endpoint Windows (OSD volume + mute mic)

- [ ] **Rinviato — segue B13-HW-01 e richiede ascolto/occhio dell'utente.**
- Con driver `psvrpc` registrato e SteamVR attivo, premere Vol+/Vol-:
  verificare OSD volume Windows sull'endpoint di rendering e variazione
  d'ascolto reale sul jack cuffie del visore.
- Premere Mute: verificare toggle `SetMute` sull'endpoint di cattura
  (stato mute Windows) e ripresa alla seconda pressione (edge-trigger,
  non livello).
- Ripetere in cinema e VR: il routing PU cambia fra i modi; annotare
  eventuali differenze (cfr. `docs/AUDIO_TRACKING_CAMERA_20261015.md`).
- Conservare log driver, endpoint usati (ID scoperti dinamicamente, mai
  hardcodati), screenshot/stato OSD, exit code; aggiornare questa coda e
  `docs/HEADSET_BUTTONS_20261018.md` soltanto dopo la verifica reale.
