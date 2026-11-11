# Stato del progetto

## Riconoscimento costellazioni — feedback live 2026-10-16

- Tracker `constellation_led_v1` (`tools/psvr_camera/constellation_led_pose.py`)
  provato nel visore: **molti meno gray screen**, restano piccoli scattini
  quando probabilmente cambiano le costellazioni (ipotesi utente, non
  causalità dimostrata). Zero gray screen / scatti risolti NON dichiarati.
- Decisione utente: baseline attuale mantenuta, ottimizzazione transizioni
  rinviata. Verifica finale 336 test + 219 sottotest PASS + headless suite;
  replay usa stessi candidati/timestamp, episodi ottici != gray screen.
- Dettagli e numeri con caveat:
  `docs/CONSTELLATION_RECOGNITION_20261016.md`.

## Consolidamento release locale 0.3.0-alpha — 2026-10-14

- Stato corrente: identity scelto dall'utente sia in SteamVR sia nei giochi;
  peripheral e stronger non accettati visivamente. One Euro 2,5/β8 resta
  riferimento; fallback 3DoF ora disattivato nei default della suite.
- Selettore Italiano/English con cambio immediato e preferenza UI separata.
  Anteprima Camera responsive, icona e collegamento Start, diagnostiche senza
  console lampeggianti, riavvio tracking ordinato dopo apply driver/ottica.
- Proiezione personale in prova: H78,267° / V84,939°, aspect tangente 8/9;
  non è una calibrazione universale e non viene imposta agli altri utenti.
- MIT © 2026 rickkyy, 36 testi licenza originali con manifest/hash, README
  inglese/italiano, istruzioni release e donazioni volontarie PayPal (URL dopo).
- Primo artefatto: sorgenti 0.3.0-alpha, senza build, runtime DLL, venv,
  log, preferenze utente o staging Drive. Preparazione locale, non pubblicata.
- Verifiche da estrazione pulita: configurazione e build MSVC PASS,
  CTest 63/63 PASS; Python 912 PASS, 8 skip, 287 subtest; 36 licenze
  verificate senza cache o rete. Dettagli: `docs/RELEASE_VERIFICATION.md`.
- Le sezioni sottostanti sono cronologia: profili attivi e default di vecchi
  trial non sostituiscono queste scelte correnti. Il -202 da resize desktop
  dashboard resta riproducibile: recupero compositor-only, non cura definitiva.

## Suite GUI persistente — fallback 3DoF/recovery e profilo peripheral 2026-10-14

- Suite Tk (`tools/psvr_suite`, `run_suite.ps1`) documentata dal codice:
  schema canonico 27 chiavi, default `6dof` + fallback attivo `150 ms/1,5 m/s`,
  One Euro `2,5/8`, camera `640×480@75`; backup/rollback selettivo con rifiuto stale.
- Smoke finale `logs/suite_hardware_smoke_final_20261014.json` **PASS**
  (exit 0; `6dof` → `3dof_fallback` valido → `6dof`; `pose_valid` ovunque;
  `lens_acceptance_verified=false`); retry precedente PASS conservato.
  Lo smoke non campiona recovery; transizioni reali `6dof_reacquiring`
  verificate in `vrserver.txt` (`16:09:19.591` e `16:12:34.367` dopo attach,
  `duration 150 / elapsed 0`, pose valide, `epoch 1`).
- Profilo `configurations/optics_radial_peripheral.json` (`a=0,2/b=0,4`)
  applicato e attivo via journal `logs/suite_settings_before_20261014`
  (da medium a peripheral, `apply_optics=true`); **NON accettato visivamente**.
  One Euro default accettato, Kalman non accettato in movimento.
- Compositor finale **fullscreen true** (`before/after_100ms/final`, `10/10`
  valide, exit 0); tracking sano (`6dof`, IMU valida, `8578/8571`, `74.8 fps`).
  Fix TOCTOU `_status_pending`: Python **94 PASS + 37 subtest**,
  CTest `suite_python` 1/1; completo **60/60**.
  **-202 recuperato nella sessione presente**; anti-recidiva NON provata
  (gap `6500` vs `5000` invariato).
  Backup `logs/suite_install_before_20261014` (DLL `284ECF…3ADE`).
- [Implementazione, fallback/recovery, peripheral e rollback](docs/SUITE_GUI_20261014.md).

## Correzione ottica — trial radiale 2026-10-14

- Applicato profilo moderato diagnostico (non calibrato), da identity a profile;
  modificate solo due chiavi ottiche con backup/rollback selettivo.
- One Euro 2,5/8, preset/rig/calibrazione, FOV/proiezioni e refresh 90 Hz preservati.
- Profilo attivo e mapping non identity verificati nei callback reali; griglia
  piatto HMD-relative mostrata con pose valide e fullscreen. Feedback visivo atteso.
- Validator 3 profili PASS, Python 3 PASS, CTest ottici/display 6/6 PASS.
- [Parametri, evidenze e rollback](docs/OPTICS_RADIAL_TRIAL_20261014.md).

## Test di movimento nel visore — feedback 2026-10-14

- L'utente preferisce nettamente One Euro per solidità e continuità; i rari
  scatti sono più violenti, ma il tracking viene perso molto meno spesso.
- **Kalman ottimizzato non accettato per uso reale**, nonostante i risultati
  statici positivi. Baseline operativa One Euro 2,5/8 conservata.
- Snapshot dei log: 1.856 pose ottiche valide non pubblicate valide dal Kalman,
  soprattutto a 2/3/4 LED; zero invalidazioni aggiuntive One Euro su ottica valida.
- [Feedback, riscontri e priorità](docs/MOVEMENT_TRIAL_FEEDBACK_20261014.md).

## Prove statiche reali — 2026-10-13

- 3×60 s camera+IMU diagnostica, 3×60 s solo ottica, 95+65 s driver reale:
  37.443 frame, nessun errore camera; preset/intrinseci/rig invariati.
- Confronti sugli stessi input e lettura OpenVR reale: std depth del consumer
  0,141 mm One Euro, 0,169 mm iniziale, 0,080 mm ottimizzato nelle fasi statiche.
- Finestre stabili: tutte le pose runtime valide, fullscreen/render disponibili,
  nessuna transizione fade rilevata. Deriva lenta e validazione dinamica/lenti
  ancora aperte; non accettazione generale.
- Camera/IMU/probe rilasciati; SteamVR chiuso normalmente, settings live invariati.
- [Report e log](docs/STATIC_TRACKING_VALIDATION_20261013.md).

## Kalman 6DoF — ottimizzazione offline 2026-10-13

- Profilo quasi-statico/Q transitoria, R sull'asse ottico, recovery GLS e policy
  `driver_hold` implementati opt-in. Baseline Kalman iniziale e One Euro conservate.
- Quiete sintetica seed 6130: std profondità 4,037→1,812 mm; dati multi-seed e
  regressioni di disponibilità/riaggancio conservati. **Non accettato nel visore.**
- Python completo 723 PASS, 7 skip, 250 subtest PASS; CTest 57/57 PASS.
- Nessun cambiamento a calibrazione/preset/rig, orientamento IMU, driver o
  compositor/configurazioni SteamVR in uso; nessun nuovo reset hardware.
- [Report e piano fisico](docs/KALMAN_OPTIMIZATION_20261013.md).

## Ultima prova locale90 —2026-10-06 00:05+02:00

- Dopo rollback compat/runtime, Sony nuovamente attivo e modo90 applicato
  dall'utente. Gateoffline positivo per1920×1080p90, path/signal90/1.
- Driver allineato90/1 con backup fresco; nessun'altra chiave modificata.
- Avvio:−202 alle00:05:45; compositorDisplayFrequency90.000000,
  GPU90 mantenuta, probe3/3 pose valide ma fullscreenfalse.
- Ridurre120→90 non risolve−202 in questa prova. Evidenze:
  `logs/refresh90_trial_20261005T213639765Z/runtime_result.md` e capture.
- Ultimo stato configurazione: GPU/driver90 coerenti, nessun overrideHMD
  sperimentale né opt-inDirectMode. SteamVR chiuso normalmente tramiteURIquit,
  assenza dei tre processi verificata. Eventuale restore120 deve allineare
  siaGPU sia driver, non ripristinare soltanto la proprietà runtime.

## Prove locali120 —2026-10-05 sera

- Ripristinati primo overrideHMD e opt-in runtime; dopo hotplug sullo stesso
  collegamento Sony torna desktop1920×1080p120, cacheEDID originale verificata.
- Modo90 testato/salvato dall'utente ma non applicato; richiesto prima120.
- Primo avvio120:−202, tracking10/10 valido, fullscreenfalse. Link UI ricevuto
  dal compositor ma senza recupero. Secondo avvio:Ready iniziale, immagine
  sdoppiata riferita;−202 circa81s dopoReady. SegnaleGPU120 mantenuto.
- Evidenze: `logs/refresh120_trial_20261005T214146116Z/`.
- Nessun successo stabile120/Direct Mode dimostrato. Seguono i risultati della
  prova compat0x27; calibrazione ottica resta non verificata.

### Esito prova compat0x27 successiva

Compat applicata con piano/backup freschi nella directory della prova120.
Avvio23:56:26: errore422, InvalidOutputDesktop/output−1/887A0002. Il server
nasconde il Sony e pubblica desktopfalse, ma compositor tenta backenddesktop;
listeNV direct vuote, nessun targetDisplayCore disponibile, probeIPC303.
Compat non risolve. Dopo chiusuraSteamVR, rollbackHMD e runtime fresco eseguiti:
journalappliedfalse/pendingnull, opt-in e sezione direct_modenull. Al controllo
immediato Sony ancora assente, hotplug richiesto per ultimo confronto90.
Nessuna misura di120inDirectMode. Dettagli `compat_result.md` nella directory
della prova.

## Prova90Hz predisposta — 48/48 CTest PASS

- Manifest90 separato e non verificato fisicamente:
  `configurations/candidates/hdmi_native_90hz_trial.json`.
- Procedura: [PSVR_90HZ_TRIAL_20261005.md](docs/PSVR_90HZ_TRIAL_20261005.md).
  Ripristino baseline, timing manuali90, lettura path/signal, allineamento delle
  sole due chiavi refresh, osservazione prolungata e rollback ai valori reali.
- Checker offline `refresh90_trial.py`,32 test, verifica identità/geometria/
  progressive e razionali90 reali nel JSON salvato. Il120 non passa come90.
- Suite completa48/48 PASS. Nessun modo90 creato o applicato; prova fisica
  pendente. Ramo desktop distinto da Direct Mode e dagli esperimenti EDID.

## Confine DLL virtual display — 47/47 CTest PASS

- Harness offline con DLL SDK002 e EXE indipendente senza header OpenVR:
  tre slot, puntatori, bool e dati64bit verificati attraverso caricamento DLL.
- Pagine host PAGE_NOACCESS dimostrano rifiuto prima della lettura e assenza
  di over-read oltre il prefisso32, oltre ai controlli tramite canarie.
- Suite completa47/47 PASS; entrambi i moduli test compilati MinGW. Nessuna
  prova MSVC/SteamVR o rendering fisico; stato del display non modificato.
- Dettagli nella sezione finale di
  [DIRECT_MODE_UNATTENDED_20261005.md](docs/DIRECT_MODE_UNATTENDED_20261005.md).

## Ulteriore fase autonoma — 46/46 CTest PASS

- Matrice EDID offline: sei varianti, sette confronti causali,15 invarianti
  veri sui dati salvati; coppia HMD07→27 isolata a byte uso + checksum.
  Artifact: `out/edid_trial_matrix_20261005.json`. Nessuna applicazione.
- Core lifecycle presenter offline: coda limitata, ticket/generazioni,
  gestione disconnessioni, fence distinto da vblank e token per rifiutare
  osservazioni della vecchia sessione. Test di overflow e cleanup verificati.
- Due nuovi target CTest integrati, suite completa46/46 PASS. Nessuna nuova
  modifica display/runtime; ultimo stato live resta il bundle12:32 precedente.
- Dettagli nella sezione finale di
  [DIRECT_MODE_UNATTENDED_20261005.md](docs/DIRECT_MODE_UNATTENDED_20261005.md).

## Implementazione delegata senza UAC — 44/44 CTest PASS

- Junior coder: probe Display Core readonly, classificatore/bundle, procedura
  locale, contratto ABI IVRVirtualDisplay e proposta120EDID offline. Revisionati
  binding, identità, stati sconosciuti e ordini di restore; integrate verifiche.
- Bundle live readonly finale5/5 sorgentiOK, outcome blocked: Sony fuori
  desktop, NVIDIA direct lista vuota e nessun target Display Core esatto.
- Il range EDID originale61Hz/150MHz è un ulteriore vincolo da testare dopo
  la disponibilità del target. Proposta120 pronta solo offline, non applicata.
- Primo override HMD ancora attivo, SteamVR chiuso. Nessuna nuova elevazione,
  scrittura registro, comando power, ownership o avvio runtime in questa fase.
- Dettagli e artefatti: [DIRECT_MODE_UNATTENDED_20261005.md](docs/DIRECT_MODE_UNATTENDED_20261005.md).

## Sviluppo senza UAC — verifiche e diagnostica Direct Mode

- Controlli EDID/cache/rollback estratti in modulo puro,13 casi offline PASS,
  restore guard validato read-only sul piano reale. Journal e salvataggio atomico.
- Logreader robusto su righe/UTF8 parziali, summary distingue IPC fallito da
  fullscreen misurato.35/35 test precedenti e4/4 nuovi CTest PASS.
- Windows Display Core interrogato senza ownership: Sony/4352 non esposto
  come target connesso nella sessione corrente; solo LG connesso. Restano
  primo override attivo e rendering diretto non stabilito. Nessuna nuova UAC.
- Analisi e test locale pronti: [DIRECT_MODE_OFFLINE_20261005.md](docs/DIRECT_MODE_OFFLINE_20261005.md).

## Aggiornamento HMD EDID — Sony fuori desktop, rendering ancora aperto

- Override Microsoft HMD minimo applicato al solo SNY6A04 UID4352:
  identità e timing originali conservati. **Sony ora fuori dal desktop Windows**.
- SteamVR resta sul backend desktop e fallisce422/output−1; Direct Mode
  NVIDIA enum0, nessuna immagine o120Hz diretto dimostrati.
- IMU ripristinata con power-on/F0 fresco,2002/2002 pacchetti in2s;
  pose valide dopo calibrazione nella nuova prova.
- Stato attuale: SteamVR chiuso, primo override HMD e runtime sperimentale
  ancora attivi. Variante compat non applicata (elevazione non completata).
  Ripristino e dati in [NATIVE_DIRECT_MODE_20261005.md](docs/NATIVE_DIRECT_MODE_20261005.md).

## Direct Mode nativa — 2026-10-05 — richiesta implementata, acquisizione non riuscita

- Percorso opt-in SteamVR `IVRDisplayComponent`, EDID reale D94D:6A04 e
  configurazione `direct_mode` selettiva con rollback.7/7 CTest interessati e
  test isolato restore PowerShell PASS.
- SteamVR chiama NVIDIA e riporta successo della richiesta, ma il compositor
  resta desktop; Windows e NVAPI confermano Sony ancora OS-visible. Non è
  Direct Mode operativa e non è una soluzione al −202.
- Prova di routing false/origine0 fallita, rimossa dalla versione finale.
  Nessun flag debug o falsa dichiarazione di successo. Override ripristinati,
  SteamVR chiuso normalmente; HDMI120 conservata. Rimane aperta la verifica
  dei prerequisiti di acquisizione/classificazione HMD lato GPU/runtime.
- Dettagli: [NATIVE_DIRECT_MODE_20261005.md](docs/NATIVE_DIRECT_MODE_20261005.md).

## Sessione autonoma — 2026-10-05 — PSEye reale e fondazione M9

- Dopo l'attesa richiesta di 60 minuti, sviluppo e review parallelizzati con
  subagenti. **M9 IN_PROGRESS**: acquisizione fisica verificata, 6DoF fisico
  nel visore ancora da calibrare/validare. M3–M6 mantengono i gate aperti,
  incluso il problema fullscreen −202.
- PSEye PS3 **1415:2000**, MI_00 già libusb-win32, MI_01 audio. Backend USB
  dedicato compilato da sorgente fissato, nessun cambio binding. Acquisizione
  reale 640×480/60 ~58 fps, /75 ~72–73 fps, 320×240/187 ~178 fps nella prima
  serie. Verifica finale: **150/150 frame BGR, 73.004 fps, zero timeout/errori**
  a 640×480/75. ABI v2 con grab limitato, abort e FPS negoziati reali.
- Tracking metrico da marker misurato, calibrazione intrinseci, bersagli
  stampabili e backend PSEye collegati alla pipeline. Non sono stati inventati
  intrinseci o geometria dei LED PSVR; LED integrati non ancora tracciati live.
- Driver: posizione UDP loopback versionata in metri, opt-in disabilitato,
  freshness/sequenze/perdita esplicita, origine seduta e recenter coerente con
  il riferimento yaw IMU. Probe posizionale indipendente da SteamVR e test
  end-to-end mittente Python → ricevitore C++ con perdita/recupero.
- **Build MinGW x64 e 35/35 CTest PASS**. Suite camera nella venv con OpenCV:
  142 test eseguiti, 7 skip previsti (assenza OpenCV/hardware opt-in).
- Ambiente `.venv-camera` pronto (NumPy 2.4.6, OpenCV contrib headless
  5.0.0.93); DLL/licenze in `out/pseye`, probe in `out/position_probe`.
- Inventario applicazioni read-only: Assetto Corsa e MSFS 2024 presenti,
  runtime OpenXR SteamVR, yt-dlp/ffmpeg presenti, player VR non rilevato.
  M7/M8/M10 restano non validati in esecuzione.
- [Report sessione](docs/AUTONOMOUS_20261005.md),
  [M9 software](docs/MILESTONE_9_SOFTWARE.md),
  [PSEye](docs/PSEYE_TRACKING.md),
  [applicazioni](docs/APPLICATION_READINESS_20261005.md).

## Stato corrente — 2026-10-04, verifica 23:49:31: −202 ricorrente, prova DP sospesa

- **M3–M6 IN_PROGRESS.** HDMI nativo 120 Hz è un candidato di avvio con
  riscontro utente positivo, **non una configurazione stabile certificata**.
  Restano validi i successi precedenti a 60/120 Hz, movimento, entrambi gli
  occhi e recenter; non annullano la nuova schermata rossa/errore −202.
- Alle 23:49:31 stessa sessione delle 23:40:19: vrserver 23376/compositor
  23424, 10/10 pose valide e connected, ma **IsFullscreen=false**.
  `power.pauseCompositorOnStandby` è ancora false: da solo non è sufficiente
  come soluzione permanente. Standby osservato, causalità con −202 non provata.
- Config refresh 120 Hz, Sony SNY6A04 UID4352 su HDMI nativo, origine
  (1920,492). Headset Window reale (1920,492), 1920x1080, DPI 96,
  PerMonitorAware, visibile/non minimizzata/non foreground: nessuna discrepanza
  di bounds osservata. Il driver resta **solo desktop esteso**, non Direct Mode.
- **Prova DP PAUSED**: l'utente ha chiesto di attendere prima dello scambio.
  Nessun cambio cavi durante la ricerca, nessuna modifica runtime in questo
  aggiornamento documentale. Il parent sta salvando il candidato e il package;
  non dichiarati qui già archiviati o ripristinati.
- [Evidenze runtime](docs/RUNTIME_CHECKS_20261004.md),
  [prova high-refresh](docs/PSVR_HIGH_REFRESH_TEST.md),
  [candidato e ripristino selettivo](docs/WORKING_CANDIDATE_HDMI_120.md).

Le sezioni seguenti sono cronologia delle prove: «ultimo», «pendente» e
«positivo» descrivono il rispettivo checkpoint, non sostituiscono lo stato corrente.

## Primo avvio SteamVR 120 Hz — positivo

- Utente conferma modo salvato e visore appoggiato. HDMI nativo SonyUID4352
  mantiene source/scanout1920x1080 progressivo e path/signal120/1 prima/dopo avvio.
- Compositor reale dichiara120.0Hz (float119.999992), avvio/distortion surfaces
  riusciti; calibrazione8s completata. Fullscreen true e10/10 pose valide.
- Nuova conferma fluidità/immagine indossato e stabilità120 prolungata ancora
  pendenti; non testata persistenza dopo rebootWindows. Nessun CRU/EDIDoverride.
  [Prova high-refresh](docs/PSVR_HIGH_REFRESH_TEST.md).

## Primo riscontro PSVR HDMI nativo 120 Hz

- Test manuale NVIDIA con timing storico120: utente «sembra funzionare».
  GPU/Windows leggono realmente source/target1920x1080 progressivo e
  path/signal120/1 Hz sul SonyUID4352. SteamVR chiuso.
- Driver preparato a120/1 dopo la misura, restore60 documentato. Persistenza
  del modo custom e nuova sessione SteamVR120 ancora da verificare; GDI
  inizialmente non elenca120. Nessun override EDID o installazione driver.
  [Prova high-refresh](docs/PSVR_HIGH_REFRESH_TEST.md).

## Focus VR su HDMI nativo — preparazione prova high-refresh

- Istanza SonyUID4352 ancora1920x108060Hz, EDIDmax61Hz150MHz; nessun override.
- Profilo storico PSVR120 analizzato in memoria: timing90/120 reali con
  clock222,750/297MHz, totali2200x1125. IdentitàSNYB403 diversa: nessun import.
- Aperto pannello NVIDIA; preparata prova manuale limitata al Sony senza
  installaredriver/CRU o cambiare monitor. Nessun modo custom ancora applicato.
- [Procedura/restore](docs/PSVR_HIGH_REFRESH_TEST.md), analyzer con9testoffline.
- Suite software aggiornata: 31/31 CTest PASS, incluso analyzer del profilo
  storico su dati sintetici; nessun test automatico applica timing o override.

## Ultimo riscontro — occhi/ricentraggio qualitativamente OK, 60 Hz reali

- Utente conferma «tutto ok» al controllo un occhio alla volta dopo recenter.
  Nota fluidità non 120 Hz: inventario conferma 60/1 Hz attivi e scanout,
  solo modi 60/59/50/24/23 Hz full-HD progressivi esposti sul percorso corrente.
- Prova video base positiva a 60 Hz; 90/120 non verificati/disponibili da
  Windows su DP→HDMI attuale. Proposta prova isolante HDMI GPU nativo,
  senza inventare refresh/EDID o attribuire causa certa all'adattatore.
  Ottica quantitativa e stabilità lunga restano gate aperti.
  [Report runtime](docs/RUNTIME_CHECKS_20261004.md).

## Ultima conferma utente — mondo e dashboard visibili nelle lenti

Successiva prova movimenti indossato: utente «sembra tutto ok» dopo rotazioni
destra/sinistra e alto/basso; esito qualitativo positivo. Recenter neutro e
controllo L/R ancora da verificare.

- Utente conferma «funziona, compare il mondo e la dashboard» dopo la prova
  con pausa compositor in standby disattivata. Output visivo di base positivo.
- M5 resta IN_PROGRESS per L/R, stabilità indossata e gate residui; ottica
  ancora identity diagnostica, 3DoF a 60 Hz, nessun Direct Mode dichiarato.
  [Report runtime](docs/RUNTIME_CHECKS_20261004.md).

## Ultima prova temporizzata — pausa standby disattivata, esito positivo

- Nuova sessione 22:07:12 con stesso display e unico override di prova
  power.pauseCompositorOnStandby=false. Calibrazione riuscita.
- Ai checkpoint 61/123/184/225 s: fullscreen true, 10/10 pose valide ciascuno,
  PID invariati. Standby realmente attivato ma nessuna perdita fullscreen
  osservata dopo; nessun nuovo −202/GPU fence nel periodo esaminato.
- Mitigazione promettente, non garanzia di lunga sessione. Nuova verifica
  nelle lenti e stabilità indossata ancora da confermare; M5 IN_PROGRESS.
  [Report runtime](docs/RUNTIME_CHECKS_20261004.md).

## Prova mirata pausa SteamVR in standby — da verificare

- Fullscreen nuovamente false dopo pochi minuti, ma 10/10 pose ancora valide.
  Finestra Headset fisica corretta sul Sony; Windows AC display/sleep disattivi.
- Log prova che SteamVR entra in standby. Modificato solo l'override utente
  power.pauseCompositorOnStandby da true a false via API, readback verificato.
  Nessun cambio Direct Mode, OpenXR o grafica/risparmio energetico Windows.
- Fullscreen non è tornato immediatamente; test di riavvio e durata ancora
  pendente. Restore solo della chiave documentato in logs. M5 non completa.
  [Report runtime](docs/RUNTIME_CHECKS_20261004.md).

## Ultimo riavvio reale — fullscreen ripristinato

- Dopo richiesta utente, nuova sessione SteamVR 19:04:31 con stessi settings.
  API IsFullscreen=true nelle tre osservazioni del probe, nuove 10/10 pose
  valide/connected, exit 0. Compositor attivo.
- Richiesta conferma visiva dell'immagine normale dopo riavvio; non ancora
  accettata stabilità prolungata o ottica. M5 resta IN_PROGRESS.
  [Report runtime](docs/RUNTIME_CHECKS_20261004.md).

## Ultimo blocco M5 — errore −202 fullscreen

- Screenshot utente chiarisce errore fullscreen −202, distinto dall'avviso
  OpenXR non predefinito. Tracking ancora 10/10 valide/connected.
- API compositor: fullscreen=false, CanRenderScene=true, focus scene PID 0.
  Una richiesta BringToFront non cambia fullscreen; nessun successo dichiarato.
- Prossimo gate: attivazione fullscreen dalla UI SteamVR sul PC e conferma
  immagine reale. Probe normale read-only Utility; flag compositor espliciti
  usano Background, senza Scene/fallback o cambi OpenXR.
  [Report runtime](docs/RUNTIME_CHECKS_20261004.md).

## Ultima verifica reale — stanza SteamVR completata, primo riscontro positivo

- Dopo il riavvio utente conferma configurazione stanza completata e
  «sembra funzionare». Stessa sessione compositor attiva, nuove 10/10 pose
  valide/connected. Nessuna nuova perdita tracking/topologia osservata in
  questa sessione al controllo.
- Verifiche dei movimenti indossato/ricentraggio, L/R e ottica restano da
  completare; nessuna dichiarazione 6DoF o stabilità di lunga sessione.
  M4–M6 restano IN_PROGRESS. [Report runtime](docs/RUNTIME_CHECKS_20261004.md).

## Prova M5 display — 2026-10-04 — compositor PASS, lenti da confermare

Aggiornamento: utente vede schermata rossa «riavvia», non immagine normale.
Probe attuale 0/10 pose valide, OutOfRange/disconnected; log tracking-lost e
invalidazione latched topologia. Config e inventario ora corrispondono, ma
occorre riavvio con calibrazione ferma. **M5 ancora IN_PROGRESS**; avvio e
presentazione iniziali non certificano stabilità. Il driver è desktop-esteso:
la precedente indicazione Direct Mode non è dimostrata.

- Bug ABI Windows x64 trovato: SteamVR (MSVC) e MinGW usano ordini diversi per
  il ritorno aggregato di `ComputeDistortion`; corrotto `this` causava crash
  `vrserver.exe` c0000005. Aggiunto ponte vtable C-shaped opt-in per MinGW.
- Dopo la correzione il compositor crea le superfici di distorsione, resta vivo
  e presenta frame (331 presents, 0 dropped); display PSVR a 1920x1080/60 Hz
  con contenuto SteamVR renderizzato. **30/30 CTest PASS**.
- Serve conferma visiva nelle lenti. Distorsione identity diagnostica; L/R,
  scala e ottica non accettate.
- [Verifiche runtime](docs/RUNTIME_CHECKS_20261004.md).

## Avvio verifiche reali — 2026-10-04 — preparazione

Primo avvio driver attivo: **DLL/provider/HMD/Activate/calibrazione riusciti**,
tracking nel log e **10/10 pose OpenVR valide** con client Utility reale,
Running_OK/connected, exit 0. Backup settings conservato; override driver
enable=true, displayEnabled=false. Compositor DisplayFrequencyFailure nella
prova tracking-only; output nel pannello ancora da verificare. M4/M5 restano
IN_PROGRESS. Probe runtime aggiunto per misure/IPC debug del solo nostro HMD.

Aggiornamento: SteamVR installato, USB nuovamente presente, PSVR rilevato come
secondo display tramite adattatore DP→HDMI a 1920x1080/60 Hz. VR mode confermata
con A0/F0; registrazione/rimozione/reinstallazione reali PASS. Primo avvio
SteamVR vede package ma driver resta disabilitato; HMD/posa non ancora provati.
Config root tornata full; yaw fisico resta pendente.
[Verifiche runtime](docs/RUNTIME_CHECKS_20261004.md).

- Steam presente, SteamVR non trovato nell'unica libreria configurata;
  aperta richiesta installazione SteamVR. GPU RTX 5070 Ti, driver 32.0.16.1714.
- Inventario display reale: solo LG TV, 3840x2160, refresh path/signal
  60000/1001 Hz, progressive/rotation identity/scaling identity; nessun PSVR
  attivo rilevato. Nessuna impostazione video modificata.
- Monitor fermo chiuso normalmente, installati i nuovi eseguibili in
  `out/diag` (inclusi display/optics). Config root temporaneamente `yaw`
  per il test M3 residuo; **ripristinare full dopo la prova**.
- Monitor riaperto; nuova enumerazione exit 2, PSVR USB non rilevato.
  Occorre ricollegamento prima della calibrazione. Nessuna prova yaw o
  registrazione/attivazione SteamVR ancora eseguita.

## Sessione autonoma M6 integrazione — 2026-10-04 — IN_PROGRESS

- Loader JSON ottico rigoroso, snapshot immutable e adapter forward/inverse
  RGB OpenVR, opt-in `distortionMode=profile` e path assoluta. Default identity
  diagnostica; driver/display disabilitati.
- Policy SDK float distinta da math; fix bordi/angoli e rifiuto profili
  numericamente collassanti. Review parallela finale senza ulteriori must-fix.
- Package `out/optics/` (validatore offline + esempio), `out/patterns/`
  (griglia/RGB/geometria stereo) e risorse driver aggiornati.
- **Build MinGW x64 OK, 28/28 CTest PASS**: 61.159 check math strict,
  1.574 loader, 36.834 adapter SDK, 28 test pattern Python e regressioni.
- **M3–M6 non complete**: controlli hardware/runtime/calibrazione ottica
  rinviati. Nessuna acquisizione USB, query display live o attivazione SteamVR.
- [Report M6](docs/MILESTONE_6_SOFTWARE.md),
  [schema profilo](docs/OPTICAL_PROFILE_SCHEMA.md),
  [coda manuale](docs/MANUAL_VALIDATION_QUEUE.md).

## Sessione autonoma M5 / fondazione M6 — 2026-10-04 — IN_PROGRESS

- Su richiesta dell'utente: implementazione e review parallelizzate con
  subagenti; tutti i controlli manuali rinviati. **M3–M6 non dichiarate complete.**
- M5 software: inventario read-only `out/display/psvr_display.exe`, JSON
  percorsi monitor/adapter/geometria/refresh/modi; nessuna selezione automatica.
- Driver: componente `IVRDisplayComponent_003` per desktop esteso opt-in,
  bounds/viewports/projection espliciti, identity distortion diagnostica.
  Verifica monitor esatto/source non clonata, scaling identity e refresh
  razionale; recheck Activate e thread topologia con invalidazione latched e
  richiesta SDK di riavvio. Driver/display restano disabilitati per default.
- M6 fondazione matematica: polinomio radiale/RGB e inversa con monotonicità
  analitica, **61.159 check sintetici PASS**; non ancora applicata al display.
- Suite build MinGW x64 **24/24 CTest PASS**, inclusi test mock di Init/
  registrazione HMD senza Activate/hardware; display/ottica reali non verificati.
- Report: [M5](docs/MILESTONE_5_REPORT.md),
  [M6 software](docs/MILESTONE_6_SOFTWARE.md),
  [controlli manuali rinviati](docs/MANUAL_VALIDATION_QUEUE.md).

## Sessione M4 software — 2026-10-04 — IN_PROGRESS

- Test fisici rinviati dall'utente. **M3 e M4 IN_PROGRESS**; M5 non iniziata.
- Driver OpenVR HMD-only C++17, SDK 2.15.6 fissato, thread HID/fusion M3,
  snapshot/pose con freschezza e invalidazione, factory/provider correnti,
  cleanup e ownership condivisa con nuova build diagnostico.
- DLL x64 e package `out/driver_psvrpc/psvrpc` generati, driver disabilitato
  per default; script Inspect/Install/Remove e filtro log disponibili.
- **Build MinGW OK, 18/18 CTest PASS**, inclusi 71 test Python monitor e
  20 casi script PowerShell. Revisione e test script parallelizzati con subagenti.
- Nessuna registrazione/attivazione SteamVR eseguita; riconoscimento HMD/pose
  runtime e display restano da verificare. Nessun output compositor dichiarato.
- Dettagli: [report M4](docs/MILESTONE_4_REPORT.md). Nuovo diagnostico compilato
  in `build/mingw`; aggiornare `out/diag` con build/install dopo chiusura monitor.

## Sessione M3 — 2026-10-03 — IN_PROGRESS (non completa)

- **M3 IN_PROGRESS, non COMPLETE; M4 non iniziata.** Fusion propria C++17
  (`orientation_math.hpp` + `imu_fusion.hpp` + `imu_capture.*`): quaternione
  complementare gravità, cal 8 s tavolo hands-off dopo 200 ms warm-up,
  scale gyro 0.00105 / accel 9.81/16384, gate STD 0.75 dps + meanShift 0.05 dps,
  bias congelato, gravityGain 1, smoothing 0, recenter full/yaw. Full corretto
  two-sided; regressione tilt-30° passa.
- GUI `tools/psvr_monitor/monitor.py` (Py 3.11 stdlib Tk 8.6) via
  `scripts/run_monitor.ps1` / root `run_monitor.ps1`; API readonly loopback
  effimera riscoperta via `logs/monitor_api.json`. Dettagli operativi in
  `docs/IMU_TESTING.md`, `docs/MONITOR_APP.md`.
- Build MinGW x64 OK **15/15 CTest**, inclusi **71 test Python** del monitor;
  statico 60 s originale PASS
  (drift -0.16°/min, pre-nuovi gate); due capture asse precoci fallite
  conservate; drift-reclamo spiegato da cal in movimento (+0.648 dps Y).
  Statico post-fix nella GUI **PASS**: 42000 report, 33.799532 s di tracking,
  yaw +0.236718° = **+0.420216°/min**, pitch/roll in escursioni ~0.060°/~0.034°,
  2 rollover, nessuna perdita/discontinuità, exit 0.
- Verificata anche l'integrazione reale del backend GUI con **Stop immediato
  all'avvio**, recenter, ricalibrazione e Stop/EOF: nessuna posa obsoleta valida.
  Rafforzati watchdog di startup/progressione, rifiuto di duplicati,
  invalidazione JSON sulle eccezioni e disponibilità IPC durante l'avvio.
- Il 2026-10-04 le tre prove fisiche guidate post-fix sono **PASS**:
  yaw-right −50,65°, pitch-up +38,15°, roll-right −45,19°, assi incrociati
  entro i limiti. Evidenze in [verifiche fisiche](docs/M3_PHYSICAL_CHECKS_20261004.md).
  Completato anche il confronto guidato **indossato/non indossato**:
  gyro e orientamento rispondono in entrambi i casi, con flag distinti;
  nessuna soppressione totale osservata da non indossato. Confronto qualitativo,
  non certificazione di uguale sensibilità. Restano gli ulteriori gate hardware
  Scollegamento USB reale durante tracking verificato: errore HID/exit 3,
  GUI/API rispondente, posa invalidata e rimossa, worker concluso.
  Ripartenza manuale dopo ricollegamento verificata con nuovo worker,
  nuova calibrazione accettata e campioni validi/freschi in progressione.
  Restano gli ulteriori gate hardware documentati (recenter yaw,
  ripetizioni termiche);
  M3 resta IN_PROGRESS.
- Report: [docs/MILESTONE_3_REPORT.md](docs/MILESTONE_3_REPORT.md).
  Non dichiarate stereo/display/SteamVR; Win11 reale, VS 2022/Win10 non verificati.

## Sessione M2 — 2026-10-03

- **M2 completata, verificata sul PSVR reale.** Comandi espliciti `--power-on`,
  `--power-off`, `--vr-mode`, `--cinema-mode`; pacchetti documentati di 8 byte,
  verifica con risposta A0 accettata e nuovo F0.
- Autorizzazione dell'utente e installazione manuale Zadig: **solo MI_05** da
  HidUsb/input.inf a **WinUSB/oem88.inf**. MI_04 resta HidUsb/input.inf;
  composite e audio invariati. Inventario originale in `logs/`, ripristino
  documentato in `docs/WINUSB_CONTROL.md`.
- Endpoint rilevati: **IN 0x84 / OUT 0x04**, interrupt, max packet 64.
  Backend WinUSB nativo, nessuna dipendenza libusb.
- Power-off: mask **0x64**; power-on **0x15**;
  VR **0x51, vr_active=1**; cinema **0x55, vr_active=0**; exit 0 per ogni cambio.
- Stato finale: **visore acceso in cinema mode**.
- Regressione passiva M1 col binding misto: **2002 report validi in 2 s**, 0
  discontinuità/rifiuti, exit 0. Build x64 MinGW riuscita, **9/9 test passati**.
- **M3–M10 non iniziate.** Prossimo passo: IMU/fusion 3DoF e recenter.

Report: [docs/MILESTONE_2_REPORT.md](docs/MILESTONE_2_REPORT.md).
`--status` osserva passivamente eventi, non fa polling: senza nuovo F0
termina **6 / stato sconosciuto**. Anche un comando già soddisfatto può non
produrre F0: ACK da solo non diventa successo di verifica.
Ripristino del binding HID, hot-unplug durante I/O e Ctrl+C non provati
fisicamente nella sessione M2. VS 2022 e Windows 10 ancora non verificati.
Verifica display/SteamVR resta alle sue milestone. **Sessione conclusa a M2.**

## Sessione M0/M1 — 2026-10-03 (storico)

- **M0: analisi completata.** Revisioni, sorgenti, compatibilità e rischi in
  `ARCHITECTURE.md`. Ambiente iniziale: repository vuoto, Windows 11 Pro x64
  10.0.26200, Git/Python disponibili; VS 2022 non rilevato nei percorsi standard.
- **M1: completata e verificata sul PSVR reale.** `out/diag/psvr_diag.exe`
  compilato Windows x64, 5/5 test CTest passati. Rilevato `054C:09AF`,
  USB release `0107`, HID sensori 4 e controllo 5 aperte.
  Dopo accensione fisica confermata dall'utente: **5002 report sensore da 64 byte
  in 5 s**, 0 rifiutati, 0 discontinuità, ~1000.20 report/s e ~2000.41 campioni/s.
  Exit **0**. USB scollegato fisicamente: exit **2**, nessun crash.
  Ricollegato: **2001 report in 2 s**, exit **0**, nessuna discontinuità.
- Al termine della prima sessione **M2–M10 non iniziate**.

## Limiti verificati e gate successivo

- In standby iniziale le HID si aprivano ma non arrivavano report: exit 4,
  nessun comando software aggiunto. L'accensione fisica ha sbloccato il flusso.
- Sensori: input HID Windows 65 byte (prefisso zero rimosso da HIDAPI).
- Controllo: input **0**, output **0**, feature **5** byte dal descriptor Windows.
  Non leggibile come flusso input: la lettura viene saltata esplicitamente.
  Apertura riuscita NON verifica invio comandi power/VR mode. Prima di M2
  verificare il trasporto per questa interfaccia senza indovinare feature report.
- MI_00 3D Audio, MI_06 H.264 e MI_07 BulkIn: problem code 28, driver assente;
  non hanno impedito la lettura sensori e non sono stati modificati.
- Build verificata con toolchain portabile MinGW; percorso VS 2022 documentato,
  non eseguito perché VS non disponibile. Windows 10 non testato in questa sessione.
- Scollegamento **durante** una capture e Ctrl+C restano prove manuali aggiuntive.
- Nessuna verifica di orientamento/drift, display, distortion o SteamVR in M1.

Report con comandi/output e log: [docs/MILESTONE_1_REPORT.md](docs/MILESTONE_1_REPORT.md).
Commit M0: `80debcb`. M1 ha un commit dedicato nella cronologia Git.
La prima sessione si è conclusa a M1; la successiva istruzione "procedi" ha
avviato M2, riportata sopra. Il commit M2 dedicato è nella cronologia Git.
