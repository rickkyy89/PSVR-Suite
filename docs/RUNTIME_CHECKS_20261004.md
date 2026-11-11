# Verifiche runtime iniziate — 2026-10-04

Aggiornamento finale del checkpoint: salvati candidata e package in
`configurations/candidates/hdmi_native_120hz.json` e
`configurations/candidates/hdmi_native_120hz_driver_package.zip`.
HDMI120 e pose validi, ma −202 ricorre: nessuna stabilità certificata.
Prova focus Win32 unica: foreground realmente acquisito (BOOLtrue e verifica
immediata/100ms), fullscreen ancorafalse al probe successivo. Nessun'altra
modifica power, input simulato o workaround topmost/AttachThreadInput.
Test DP sospeso; [candidata/restore](WORKING_CANDIDATE_HDMI_120.md).

**Stato corrente (23:49:31): M3–M6 IN_PROGRESS; candidato HDMI nativo 120 Hz
con ricorrenza nota di −202, non stabile certificato. Prova DP PAUSED su
richiesta dell'utente; nessun cambio cavi durante la ricerca.** Le sezioni
seguenti conservano la cronologia, inclusi successi parziali e fallimenti;
il checkpoint finale aggiorna le conclusioni senza cancellare quelle evidenze.

M4/M5 restano IN_PROGRESS. Collegamento confermato dall'utente:
GPU DisplayPort → adattatore HDMI → processor unit PSVR; HDMI GPU diretto
riservato al monitor. Marca/modello e capacità dell'adattatore non ancora noti.

## Ambiente e inventario reale

- SteamVR installato in `C:\Program Files (x86)\Steam\steamapps\common\SteamVR`,
  app 250820 buildid 25330290; primo avvio settings indica 2.17.10.
- GPU RTX 5070 Ti, driver 32.0.16.1714.
- USB presente: MI_04 HidUsb, MI_05 WinUSB, entrambe problem code 0.
  Nuova enumerazione exit 0:
  `logs/psvr_diag_20261004T093529.873Z_24108.log`.
- LG TV DISPLAY1, 3840x2160, origine (0,0), refresh 60000/1001 Hz.
- PSVR `SIE  HMD *08`, DISPLAY2, percorso
  `\\?\DISPLAY#SNY6A04#5&625d12c&1&UID4354#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}`,
  1920x1080, origine (3840,492), path/signal 60/1 Hz,
  progressive, rotation/scaling identity.
- Modi GDI non interlacciati 1920x1080 esposti: 60,59,50,24,23 Hz;
  nessun 90/120 Hz osservato. Non è una prova delle capacità assolute
  dell'adattatore, né misura vsync/frametime.

## Controllo modalità e registrazione

- `--vr-mode`: write 8 byte, A0 accepted, F0 fresco finale mask 0x51 /
  vr_active=1, exit 0. Log `psvr_diag_20261004T093756.810Z_11192.log`.
  Inventario successivo conserva gli stessi modi e 60 Hz attivi.
- Monitor diagnostico fermo chiuso normalmente. Test recenter yaw non
  eseguito; `config.json` root ripristinato a full.
- Script registrazione corretto per PowerShell 5.1 `-File`: default DriverPath
  calcolata nel corpo anziché nel param block dove PSScriptRoot era vuoto.
  Aggiunto caso di regressione: 21 casi script PASS.
- Inspect reale: inizialmente registered=false. Ciclo reale Install → Remove
  → Install PASS, rilettura `openvrpaths.vrpath` conferma il solo package
  `C:\Users\rickk\Desktop\PSVR\out\driver_psvrpc\psvrpc`.
- Primo avvio SteamVR con driver ancora disabilitato: `vrserver.txt`
  registra "Not loading driver psvrpc because it is disabled in settings",
  poi HmdNotFound. Questo verifica registrazione visibile al runtime, non
  caricamento DLL, Init/Activate o riconoscimento HMD/posa del nostro driver.

## Prossimo passo

Chiudere SteamVR dal menu Esci prima di modificare gli override; salvare
backup settings e abilitare solo driver_psvrpc per test tracking-only.
Avviare acquisizione reale solo quando utente conferma visore su tavolo
immobile per calibrazione 8 s. Nessuna accettazione runtime/ottica dichiarata.

## Primo caricamento reale e pose — PASS parziale M4

- SteamVR chiuso dall'utente, nessun vrserver/vrcompositor/diagnostico attivo.
  Backup settings `logs/steamvr_settings_before_M4_20261004.json`.
- Override aggiunto solo `driver_psvrpc`: enable=true, displayEnabled=false,
  recenterMode=full; resto dei settings conservato. Package defaults invariati.
- Nuovo avvio SteamVR 2.17.10: log 11:42:52.143 locale mostra DLL caricata,
  provider `_004`, HMD `PSVRPC-HMD-0` registrato e Activate riuscita.
- Sensor opened 11:42:52.144; IMU calibrating 11:42:52.345;
  IMU tracking 11:43:00.344 dopo 8 s. Nessun comando power/mode dal driver.
- Nuovo probe client Utility reale con DLL runtime win64: 10/10 pose accettate
  in 1013 ms, seriale verificato index 0, connected=true, valid=true,
  tracking_result=200 (Running_OK), exit 0. Quaternion normali, posizione
  (0,0,0) come previsto per 3DoF. Questi 10 snapshot non misurano drift/latency.
- Compositor tenta l'avvio ma fallisce con DisplayFrequencyFailure:
  la configurazione tracking-only non espone frequency/display. Non è
  prova di guasto del trasporto DP-HDMI; immagine nel PSVR non ancora verificata.
- M4 resta IN_PROGRESS: movimento/recenter/recalibrazione/perdita dal driver
  reale e lifecycle finale ancora da provare; M5 richiede display opt-in.

Probe persistente `out/runtime_probe/psvr_runtime_probe.exe`; CLI offline
testata, default read-only, richieste opzionali recenter/recalibrate tramite
IVRDebug al solo seriale verificato. Il probe non apre USB o modifica settings.
Prossimo gate: chiudere SteamVR normalmente, preparare bounds/refresh/projection
diagnostica espliciti e riavviare con displayEnabled=true per la prova M5.

## Recenter remoto nel driver reale — PASS

- Probe `--request recenter`: risposta `recenter queued`, target index 0 e
  seriale verificato. Il messaggio queued da solo non è prova di applicazione.
- Log `vrserver.txt` 11:55:52.916 locale: `psvrpc: recenter applied`.
- Snapshot client prima applicazione yaw −1,605712°, pitch −0,072647°,
  roll +0,050355°; a 113 ms yaw −0,000749°, pitch +0,002707°,
  roll −0,000120°. Tutti i 10 snapshot validi/connected Running_OK, exit 0.
- Verificato recenter full tramite IVRDebug e pubblicazione posa modificata.
  Non è prova fisica yaw-only, né misura drift/accuratezza ottica.

## Prova M5 display — creazione error ABI, poi PASS compositor

Primo tentativo con displayEnabled=true (12:24-12:25):
- Il compositor supera frequenza e device grafico, ma fallisce in
  `CreateDistortionSurfaces` con "failed to compute inverse distortion
  coordinates (green)" e, in una sessione, crash `vrserver.exe` c0000005.
- Diagnostica strumentata: `GetComponent(IVRDisplayComponent_003)` e
  `GetProjectionRaw` corretti, ma **zero chiamate** a ComputeDistortion/Inverse
  registrate. Il crash avveniva sulla prima call, prima del corpo del metodo.

Causa individuata: **ABI di ritorno aggregato su Windows x64**. SteamVR (MSVC)
chiama `ComputeDistortion` con RCX=this, RDX=buffer 24 byte e legge il risultato
in RAX. GCC/MinGW usa invece RCX=sret (hidden return), RDX=this per i membri che
restituiscono aggregati. Il codice generato interpretava il buffer del runtime
come `this`, corrompeva lo stato e andava in access violation.

Correzione: `driver/display_windows_abi.hpp` espone una vtable in stile C a 8
slot (`IVRDisplayComponent_003`), con `this` esplicito e ritorno aggregato
esplicito. Applicata solo a MinGW; MSVC usa la vtable nativa. Test dedicato
`display_windows_abi_contract` (1000 chiamate, guardie su vtable e dimensione
aggregato) **PASS**; suite **30/30 PASS**.

Secondo tentativo (15:25:14) con la correzione:
- `vrserver.txt`: `Enabling direct mode for NVIDIA [Vid=0xD94D Pid=0xC207]`,
  `Successfully set display visibility`, HMD activated.
- `ComputeDistortion` ora tracciato con eye/u/v reali e risultati identity.
- `vrcompositor.txt`: `Headset display is on desktop`, device e swap chain
  creati, `Updated HMD Prop_DisplayFrequency_Float to 59.999996`,
  `Creating/Updating distortion surfaces (mesh resolution 49)` **senza errori**.
- Compositor **vivo**, statistiche `Total 331 presents, 0 dropped, 0 reprojected`.
  Nessun evento di crash applicativo nella finestra di prova.
- Cattura del display PSVR (`logs/psvr_display_capture.png`, 1920x1080):
  contenuto SteamVR (Room Setup) renderizzato sul display.

Esito: avvio e presentazione iniziali del compositor riusciti via desktop
esteso con la correzione ABI; stabilità e immagine normale non ancora accettate.
La riga generica "Enabling direct mode" con PID C207 non prova Direct Mode
per il nostro monitor PID 6A04: il driver espone solo desktop esteso.
**Resta da confermare visivamente cosa appare nelle lenti**:
la cattura mostra la superficie del display, non l'immagine attraverso
l'ottica. Distorsione ancora identity diagnostica; separazione L/R e scala
ottica non verificate. Nessuna accettazione ottica dichiarata.

## Riscontro utente: schermata rossa con «riavvia»

- Utente vede nelle lenti una schermata rossa che chiede riavvio e non dispone
  di puntatore/controller per cliccare. Questo non costituisce accettazione M5.
- Log driver: tracking-lost alle 15:53:40.227, capture invalid; successiva
  invalidazione latched per topologia/timing alle 16:26:14.811.
- Probe reale successivo: 0/10 pose valide, connected=false, tracking_result
  201 (Running_OutOfRange), exit 4. Nessuna posa valida inventata.
- Inventario attuale coincide con config Sony: origine (3840,492), 1920x1080,
  60/1 Hz, progressive/rotation/scaling identity. Il latch richiede riavvio,
  non si azzera al solo ritorno della topologia.
- Presenti anche errori GPU fence dopo l'avvio iniziale: zero dropped del
  breve campione precedente non garantisce stabilità della sessione lunga.
  Causa di gap/topologia/GPU fence non isolata.
- Prossimo passo: utente appoggia visore, riavvio dal PC e nuova calibrazione.
  Nessun clic VR richiesto; driver HMD-only non fornisce controller/puntatore.

## Riavvio con visore sul tavolo — tracking ripristinato

- Utente conferma visore appoggiato e autorizza riavvio. Config/inventario
  Sony ancora corrispondenti (1920x1080, origine 3840/492, 60/1 Hz).
- Tentativo `vrmonitor -shutdown` non ha concluso la sessione. Arrestati i
  soli processi della sessione SteamVR di prova e rilanciato vrstartup;
  questo arresto non certifica Cleanup ordinata del driver.
- Nuova sessione 18:33:07: HMD/display attivati, calibrating 18:33:07.446,
  tracking 18:33:15.446. Compositor PID 5192 attivo, device/render 60 Hz,
  superfici distorsione create; nessun errore nel breve intervallo esaminato.
- Probe Utility reale: 10/10 validi e connected, result 200, exit 0,
  durata 1003 ms. Nuova conferma dell'immagine normale nelle lenti pendente;
  questa breve prova non stabilisce la causa né la risoluzione permanente
  dei precedenti errori di lunga sessione.

## Configurazione stanza completata — primo riscontro visivo positivo

- Utente: «calibrazione di steamvr stanza eseguita, sembra funzionare».
  Primo riscontro positivo dopo il riavvio; non equivale ancora a verifica
  separazione L/R, distorsione/scala ottica o stabilità a lungo termine.
- Verifica immediata: stessa sessione vrserver 21428 / compositor 5192,
  probe 10/10 pose valide/connected, exit 0, durata 1005 ms.
- Nel log corrente non risultano nuove transizioni tracking-lost/topologia
  nella sessione iniziata alle 18:33 (le due rilevate sono della sessione precedente).
- Prossimi test: movimento indossato, ricentraggio nella posa neutra,
  verifica separata dei due occhi e successiva stabilità. Tracking resta 3DoF.

## Errore visore −202: fullscreen non attivo, tracking valido

- Utente invia screenshot SteamVR: «Errore del visore ... modalità a schermo
  intero (-202)», oltre al distinto avviso OpenXR non predefinito.
- In questa sessione probe reale conferma ancora 10/10 pose valide e
  connected; nessuna nuova transizione tracking-lost/topologia nel controllo.
- Avviso OpenXR distinto dal percorso OpenVR provato; nessuna modifica
  ActiveRuntime effettuata. Utente ha ignorato l'avviso nelle UI settings.
- Probe esteso con `--compositor-status` read-only e `--bring-to-front`
  esplicito, mai automatico. Utility non può accedere IVRCompositor (errore
  123): solo quei flag selezionano client Background, mai Scene/fallback.
  Il percorso pose/request normale continua a usare Utility.
- Status reale: IsFullscreen=false, CanRenderScene=true, scene focus PID 0.
- Una sola CompositorBringToFront sul nostro HMD verificato: richiesta
  inviata, ma IsFullscreen resta false dopo 100 ms e a fine 1010 ms;
  10/10 pose valide. BringToFront è un'azione di z-order/focus, non garanzia
  della transizione DXGI fullscreen. Nessun successo fullscreen dichiarato.
- Test CLI offline del probe aggiornato PASS. Prossimo intervento minimo:
  link «Attiva modalità a schermo intero» dalla finestra SteamVR sul PC.
  Confermare UI/immagine e ricontrollare API prima di accettare M5.

## Riavvio richiesto dall'utente dopo errore −202

- Arrestata la sola sessione SteamVR di prova e rilanciato vrstartup,
  senza modificare settings driver, display o OpenXR.
- Nuova sessione 19:04:31: vrserver 17940, compositor 13288, vrmonitor 6192.
- Dopo attesa calibrazione, probe Background status read-only: **IsFullscreen
  true** prima, dopo 100 ms e al termine di 1007 ms; CanRenderScene=true.
  Nessuna richiesta BringToFront in questa misura; scene focus PID 0.
- 10/10 pose valide/connected, Running_OK, exit 0. Stato fullscreen
  ripristinato rispetto alla sessione precedente; conferma nuova immagine
  nelle lenti e stabilità prolungata ancora richieste.

## Avviso «visore come monitor» — desktop esteso previsto

- Screenshot successivo dell'utente: SteamVR consiglia modalità di
  visualizzazione diretta perché il visore è usato come monitor.
- Avviso coerente con IsDisplayOnDesktop=true e driver extended-desktop;
  Direct Mode non implementato né dichiarato dal nostro componente.
- Nuovo controllo read-only: IsFullscreen=true nelle tre osservazioni,
  10/10 pose valide/connected, exit 0, 1016 ms. L'avviso da solo non implica
  il precedente errore −202. Suggerito Ignora per continuare la prova M5,
   senza modificare visibilità del display o abilitare componenti inesistenti.

## Fullscreen perso nuovamente — prova mirata standby

- Dopo Ignora utente vede ancora rosso e link fullscreen non cliccabile.
  Status reale: fullscreen=false, 10/10 pose valide, scene focus PID 0.
- Il log dimostra che i click URL `vrmonitor://makecompositorfullscreen`
  raggiungono `CVRCompositor::OnMsg_BringToFront`; il link non è una
  garanzia di transizione fullscreen (non ripetuti altri BringToFront).
- Inspector read-only Windows con thread PerMonitorV2: Headset Window
  fisica 3840/492, 1920x1080, DPI 96/PerMonitorAware, visibile non minimizzata;
  coincide con source Sony. Nessuna correzione DPI/coordinate applicata.
- Windows AC monitor-idle e suspend entrambi 0: non alterati. I valori DC
  180/600 s non provano la causa su questo desktop alimentato da rete.
- Review indipendente: SteamVR entra realmente in standby (19:04:55),
  esce 19:07:05 e rientra 19:07:27; default runtime power.pauseCompositorOnStandby
  true. Pose valide non escludono standby. Causalità con −202 non dimostrata.
- Prova a una sola variabile, via IVRSettings e HMD verificato: impostato
  power.pauseCompositorOnStandby=false; readback prima=true/dopo=false e
  persistenza nel file confermate. Restore metadata in
  `logs/steamvr_standby_test_restore_20261004.json`; ripristinare solo la chiave,
  non tutto il file sopra successivi cambi utente.
- Nessun falso segnale di presenza, cambio OpenXR, driver Direct Mode o
  grafica/power Windows. CLI offline del nuovo flag esplicito PASS.
- Stato immediato dopo modifica: 10/10 pose valide, fullscreen ancora false.
  La modifica non è ancora una soluzione verificata: occorre riavvio con
  visore su tavolo, poi osservazione oltre il precedente intervallo di perdita.

## Prova temporizzata con pausa in standby disattivata — esito positivo

- Utente conferma «pronto», visore sul tavolo. Nuovo avvio SteamVR alle
  22:07:12, stessi bounds/projection/refresh e unico override di prova
  power.pauseCompositorOnStandby=false (read/persistenza verificati).
- PID stabili: vrserver 11548, compositor 26056, vrmonitor 6388. Calibrating
  22:07:13.315 → tracking 22:07:21.313; probe iniziale 10/10 validi,
  fullscreen true e exit 0.
- Quattro checkpoint read-only senza BringToFront/Scene focus:

| Tempo dal ciclo di osservazione | Ora locale | Fullscreen | Pose valide | Exit |
|---|---|---|---|---|
| 61,32 s | 22:09:43 | true | 10/10 | 0 |
| 122,62 s | 22:10:45 | true | 10/10 | 0 |
| 183,90 s | 22:11:46 | true | 10/10 | 0 |
| 225,19 s | 22:12:27 | true | 10/10 | 0 |

- Runtime entra realmente in standby a 22:07:36.404 (monitor conferma
  22:07:36.572). Le osservazioni successive restano fullscreen/pose valide;
  non è stato forzato alcun segnale fittizio di presenza.
- Nessun nuovo −202, GPU-fence error, tracking-lost o invalidazione topologia
  nel periodo/log esaminato. 50/50 snapshot complessivi accettati nelle cinque
  finestre da circa 1 s; non è telemetria continua né garanzia per sessioni lunghe.
- Test a una variabile compatibile con una mitigazione della pausa in standby,
  non prova definitiva dell'intera catena causale. M5 resta IN_PROGRESS fino
  a nuova conferma immagine normale/L-R e stabilità indossata.

## Occhi e ricentraggio: riscontro positivo; refresh effettivo 60 Hz

- Dopo recenter neutro e controllo un occhio alla volta, utente: «tutto ok,
  unica cosa, a sensazione non sembra 120hz». Riscontro qualitativo positivo
  su allineamento/immagine; non è calibrazione ottica quantitativa.
- Inventario nuovamente verificato sul percorso Sony selezionato: refresh
  path 60/1 e video signal 60/1 Hz. Modi GDI full-HD progressivi validi:
  60,59,50,24,23 Hz. 90/120 Hz non esposti nel collegamento corrente.
- La prova è realmente a 60 Hz: frequenza USB/IMU non è scanout video.
  Non impostare solo Prop_DisplayFrequency a 120 senza modo video reale.
- Collegamento corrente GPU DP→adattatore HDMI→PSVR. Non isolato se
  l'assenza dei modi dipenda da adattatore, EDID o altre parti della catena.
  Prossima prova proposta: PSVR su HDMI GPU nativo, monitor sull'adattatore
  se compatibile, con SteamVR chiuso e successivo nuovo inventario/setting.

## Scambio porte effettuato — limiti distinti monitor/adattatore ed EDID PSVR

- Utente sposta monitor 4K60 sull'adattatore DP-HDMI: riferisce solo Full HD
  e immagine molto sgranata se forza 4K dal pannello NVIDIA.
- Inventario reale: LG UID4354, source/scanout 1920x1080 60/1 Hz, main (0,0).
  Il driver elenca anche modi 4K60 (potenzialmente personalizzati): non sono
  prova di segnale attivo 4K. Causa della qualità forzata non isolata.
- Sony ora sul GPU HDMI nativo UID4352, source/scanout 1920x1080 60/1 Hz,
  posizione (1920,492); full-HD progressivi ancora 60/59/50/24/23, nessun
  90/120. Adattatore non spiega da solo questo limite del visore.
- SteamVR chiuso. Status USB passivo fresco: powered on, VR mode, worn true,
  mask0x53, vr_active1, exit0, log psvr_diag_20261004T210933.740Z_19620.log.
  Non è semplicemente il visore rimasto in cinema mode.
- EDID registry cached Sony nativo: 256 byte, checksum dei due blocchi 0/0,
  range verticale 23..61 Hz, max pixel clock150MHz, DTD preferred1080p60.
  Nessuna chiave EDID_OVERRIDE presente su questa istanza.
- Ricerca indipendente: PSVRFramework "Video routing and EDIDs" documenta
  proprio EDID PSVR di classe60Hz e metodo storico override per90/120;
  esempio SNYB403 diverso ID ma range/DTD/CTA corrispondenti al nostro SNY6A04.
  https://github.com/gusmanb/PSVRFramework/wiki/Video-routing-and-EDIDs
- Nessun comando USB aggiuntivo "sblocca120" verificato nelle fonti pin;
  VR mode non certifica refresh HDMI. Nessun override/custom timing applicato.
  Cache/variante firmware e capacità fisiche restano distinte dai modi dichiarati.
- Aggiornati solo path Sony e X negli override SteamVR per la nuova porta;
  backup selezione precedente in logs/steamvr_display_before_native_hdmi_20261004.json.
  Refresh resta60 reale; nessun riavvio/calibrazione effettuato durante questa prova.

## Conferma visiva nelle lenti — mondo e dashboard presenti

- Utente dopo la prova temporizzata: «funziona, compare il mondo e la dashboard».
- Confermata immagine reale visibile nelle lenti dopo riavvio con
  power.pauseCompositorOnStandby=false; supera il precedente riscontro
  limitato a screenshot desktop e schermata rossa.
- Gate output visivo di base positivo. Restano movimento/ricentraggio
  indossato, controllo L/R separato, geometria/ottica e stabilità prolungata.
  Non certifica Direct Mode, refresh 90/120 Hz o tracking posizionale.

## Movimento indossato — riscontro qualitativo positivo

- Dopo richiesta di ruotare lentamente destra/sinistra e alto/basso,
  utente risponde «sembra tutto ok».
- Registrato riscontro qualitativo positivo sul comportamento rotazionale
  nella scena reale. Non è una misura di latenza, cross-axis o drift.
- Prossimo passo: recenter full nella posa neutra scelta dall'utente,
  poi controllo dei due occhi separatamente. Ottica non ancora accettata.

## Recenter full indossato nella posa neutra richiesta

- Utente in posa neutra richiede «ricentra». Comando IVRDebug inviato al
  seriale verificato PSVRPC-HMD-0, risposta recenter queued.
- Log reale 22:48:53.062: recenter applied. Probe successivo nella stessa
  chiamata: 10/10 pose valide/connected, exit 0, 1007 ms.
- Applicazione del comando verificata nel driver, nessuna ricalibrazione
  bias richiesta. Conferma visiva dell'allineamento e test occhi separati pendenti.

## HDMI nativo 120 Hz — avvio positivo, poi nuova schermata rossa/−202

- Conservati i riscontri qualitativi positivi a 60 Hz: mondo/dashboard nelle
  lenti, movimento rotazionale, recenter e controllo un occhio alla volta.
  Anche il candidato 120 Hz ha avuto avvio e riscontro utente positivi;
  i dettagli di timing/scanout e avvio sono in
  [PSVR_HIGH_REFRESH_TEST.md](PSVR_HIGH_REFRESH_TEST.md). Non sono misure
  quantitative di ottica, cadence, frame skipping o stabilità prolungata.
- Dopo l'avvio delle 23:40:19, l'utente riferisce nuovamente rosso/−202.
  Verifica fresca delle **23:49:31** nella stessa sessione: vrserver **23376**,
  compositor **23424**, **10/10 pose valide e connected**, **IsFullscreen=false**.
  Il tracking valido non certifica fullscreen o immagine normale nelle lenti.
- `power.pauseCompositorOnStandby` è ancora **false**. Config refresh **120 Hz**,
  percorso HDMI Sony **SNY6A04 UID4352**, origine **(1920,492)**.
- WindowInspector: Headset Window reale **(1920,492,1920,1080)** (x/y/w/h),
  **DPI 96 / PerMonitorAware**, visibile, non minimizzata, non foreground.
  Bounds corrispondenti non provano una transizione DXGI fullscreen;
  non foreground da solo non dimostra la causa.
- Log della stessa sessione: standby **enter 23:40:43**, **leave 23:42:11**,
  **enter 23:43:12**, **leave 23:45:22**, **enter 23:45:44**.
  Sono transizioni osservate, non prova che standby causi −202.
- La ricorrenza con pause=false **confuta la sufficienza di questa sola chiave
  come soluzione permanente**. La prova positiva delle 22:07 resta valida
  per il suo intervallo, non come certificazione della sessione successiva.
- Driver **solo desktop esteso**: la precedente interpretazione Direct Mode
  era errata; la riga generica NVIDIA non cambia il contratto del componente.
- **M3–M6 IN_PROGRESS; DP PAUSED.** Attendere nuova autorizzazione prima di
  scambiare il collegamento. Nessun cambio cavi durante la ricerca né modifica
  settings/processi/hardware eseguita per questo aggiornamento documentale.
- Il parent sta salvando `configs/candidates/hdmi_native_120hz.json` e
  `driver-package.zip`: candidato startup/user-positive con ricorrenza nota,
  non «best config» stabile certificata. Contenuto e ripristino selettivo in
  [WORKING_CANDIDATE_HDMI_120.md](WORKING_CANDIDATE_HDMI_120.md).
