# Prova PSVR HDMI nativo 90/120 Hz — candidato 120 Hz, −202 ricorrente

**Nuova prova90 predisposta su richiesta,2026-10-05:**
[procedura dedicata](PSVR_90HZ_TRIAL_20261005.md) e manifest
`configurations/candidates/hdmi_native_90hz_trial.json`. È preparazione
offline, non una prova90 già eseguita. Le evidenze120 qui sotto restano
storiche e non attestano90Hz.

**Stato corrente, 2026-10-04 23:49:31:** modo manuale 120 Hz applicato e
avvio positivo, seguito da nuova schermata rossa/−202. Candidato con riscontro
utente positivo, **non stabile certificato**; M3–M6 IN_PROGRESS.
**Prova DP PAUSED**: attendere come richiesto dall'utente prima dello scambio;
nessun cambio cavi durante la ricerca. Le sezioni preparatorie sotto sono
storiche, non indicano che il modo sia tuttora non applicato.

## Baseline e preparazione storiche

2026-10-04. Il visore SNY6A04/SIE HMD *08 è collegato alla GPU HDMI nativa.
SteamVR è chiuso; modalità VR confermata da F0 fresco (mask53, vr_active1).
Baseline reale: 1920x1080, path/signal60/1 Hz, Sony UID4352, posizione1920/492.
EDID cached originale256 byte valido: range23..61Hz, clockmax150MHz; nessun
override. Nessun 90/120 esposto dal normale inventario.

## Provenienza dei timing

Wiki di progetto, non certificazione Sony della nostra revisione:
https://github.com/gusmanb/PSVRFramework/wiki/Video-routing-and-EDIDs

ZIP storico https://github.com/gusmanb/PSVRFramework/files/605425/psvr_120.zip
SHA256 `54574aae8e507ad7f3c5fe05437d003ce4ee0b426a0fa15300c48209cb740a08`.
Analizzato soltanto in memoria, nessuna estrazione/esecuzione/installazione.
BIN256 byte con checksumvalidi; originale60Hz e modificato60/90/120Hz.

| Parametro | 90 Hz | 120 Hz |
|---|---:|---:|
| Pixel attivi H/V | 1920 / 1080 | 1920 / 1080 |
| Front porch H/V | 88 / 4 | 88 / 4 |
| Sync width H/V | 44 / 5 | 44 / 5 |
| Back porch H/V | 148 / 36 | 148 / 36 |
| Totali H/V | 2200 / 1125 | 2200 / 1125 |
| Polarità H/V | positiva / positiva | positiva / positiva |
| Scansione | progressiva | progressiva |
| Pixel clock | 222,750 MHz | 297,000 MHz |
| Frequenza orizzontale | 101,250 kHz | 135,000 kHz |

Refresh derivato = pixelclock / (2200*1125), esattamente90/120 nel campione.
Questo dimostra quali timing furono codificati, non che la catena attuale li
accetti. Il profilo storico è SNYB403, cambia seriale e rimuove range descriptor,
alza HDMI maxTMDS300MHz: non importarlo interamente sopra l'identità SNY6A04.

Analyzer originale stdlib `tools/psvr_display/analyze_reference_edid.py`,
rete solo con flag esplicito --reference-url, download/espansioneZIP≤2MiB,
nessun file del profilo scritto; 9 test sintetici offline PASS.

## Prima prova proposta: NVIDIA manuale, non override EDID completo

Documentazione NVIDIA:
https://www.nvidia.com/content/Control-Panel-Help/vLatest/en-us/mergedProjects/nvdsp/To_create_custom_timings_for_your_HDTV_display.htm

1. Con SteamVR chiuso e monitor di controllo disponibile, selezionare SOLO
   Sony/SIE HMD nel pannello NVIDIA → Modifica risoluzione.
2. Personalizza → Crea risoluzione personalizzata → timing Manuale.
3. Prima della prova verificare con screenshot i valori e il monitor scelto.
   Non cambiare colore/scaling/TV o usare Auto/CVT al posto dei timing indicati.
4. Dopo conferma della prova usare Test. Se non compare un'immagine stabile
   sul visore, NON confermare: lasciare il revert del dialogo, verificando
   le opzioni di recupero effettivamente offerte prima di avviare il test.
   Non presumere una durata specifica del conto alla rovescia.
5. Solo con immagine stabile salvare/selezionare il modo Sony. Prima di
   SteamVR verificare con QueryDisplayConfig source/scanout/progressive e
   refresh path/signal reali: UI120 o propertySteamVR120 non bastano.
6. Se fallisce, mantenere baseline60; eliminare solo il nuovo modo quando
   non attivo. Non ritentare varianti arbitrarie o cambiare driverbinding.

In questa sessione preparatoria non è stato creato né testato alcun modo
custom. Aperto solo il pannello NVIDIA per scegliere il target e controllare
il dialogo. Nessun overrideEDID, CRU, monitorINF o pixelclockpatch applicato.
L'eventuale opzione NVIDIA "resolutions not exposed" è una modifica del target,
non una lettura: considerarla esplicitamente nella prova manuale.

## Ripristino e fallback

Baseline60 e identità Sony conservate. Preferire questa prova limitata a
importazione completa del vecchio EDID. Solo se necessaria e autorizzata,
una modifica CRU deve preservare nativeidentity e60Hz; exportbaseline e
restore dello stesso Sony, nessun reset-all sugli altri monitor.
Riavviare il drivergrafico per CRU può interrompere tutti i display; non
è il semplice riavvio SteamVR. Non installare l'INF storico SNYB403 sul
nostro SNY6A04. Range/TMDS diversi richiedono verifica hardware, non assunzioni.

Dopo prova positiva aggiornare il driver con il refresh effettivamente letto,
riavviare/calibrare e verificare fullscreen, pose e immagine/stabilità.
Il scanout dichiarato dal driverGPU non certifica da solo cadence del pannello
o assenza di frame skipping. M5 resta IN_PROGRESS.

## Primo test manuale 120 Hz — riscontro positivo

- Utente inserisce i timing documentati nel pannello NVIDIA e riferisce
  «fatto, sembra funzionare». SteamVR resta chiuso.
- QueryDisplayConfig successiva sul SonyUID4352: source/target1920x1080,
  path120/1 Hz e videoSignal.vSyncFreq120/1 Hz, progressive/rotation/scaling
  identity, origine1920/492. Questo supera il precedente modo60 del driverGPU.
- Lista dei modi GDI ancora priva di120 nell'osservazione iniziale: potrebbe
  essere test temporizzato/non ancora salvato o differenza/cache enumeration.
  Non usata come prova di persistenza del nuovo modo.
- Override SteamVR preparato a120/1 solo dopo questa lettura; backup refresh
  precedente e restore60 in logs/steamvr_refresh_before_120hz_20261004.json.
- Conferma/salvataggio del modo e nuovo controllo prima dell'avvio sono
  pendenti. Nessun EDID override/INF/CRU o driverbinding applicato.

## Conferma salvataggio e avvio SteamVR 120 Hz — primo esito positivo

- Utente conferma «salvato e appoggiato». Prima dell'avvio SonyUID4352
  resta1920x1080 progressivo, path/signal120/1 Hz, origine1920/492.
- Nuova sessione23:40:19: vrserver23376, compositor23424, vrmonitor26012.
  Calibrating23:40:20.007 → tracking23:40:28.006.
- Log compositor: DisplayFrequency119.999992 float (nominal120),
  HMD recommended960x1080 perEye120.0Hz, distortion surfaces create,
  Startup Complete23:40:21.020; nessun errore nel breve intervallo esaminato.
- Probe reale Background status read-only: fullscreen=true prima/dopo/fine,
  10/10 pose valide/connected Running_OK, exit0, durata1013 ms.
- Inventario post-avvio conserva path/signal120/1. Modo confermato dall'utente
  e mantenuto dopo avvio SteamVR; persistenza dopo riavvio Windows non testata.
- Output driverGPU e configurazione compositor120 verificati; nuova conferma
  nelle lenti, frame skipping e stabilità prolungata ancora da verificare.

## Ricorrenza −202 dopo l'avvio positivo — checkpoint 23:49:31

- Conservati il riscontro positivo del candidato 120 Hz e i precedenti
  successi a 60 Hz (entrambi gli occhi, movimento e recenter). Nessuno di questi
  certifica stabilità prolungata o frame skipping assente.
- Utente riferisce nuovamente schermata rossa/−202. Stessi vrserver23376 e
  compositor23424 della sessione23:40:19: **10/10 pose valide/connected**, ma
  **IsFullscreen=false**. Config refresh120, HDMI SonySNY6A04 UID4352,
  origine1920/492; non confondere il refresh configurato con nuova misura
  continua della cadence del pannello.
- Headset Window reale x1920/y492/w1920/h1080, DPI96/PerMonitorAware,
  visibile/non minimizzata/non foreground. Bounds corretti non garantiscono
  fullscreen. Driver desktop-esteso soltanto, **non Direct Mode**.
- `power.pauseCompositorOnStandby=false` ancora verificato. Standby nel log:
  enter23:40:43, leave23:42:11, enter23:43:12, leave23:45:22, enter23:45:44.
  La ricorrenza refuta pause=false da solo come fix permanente; causalità
  standby→−202 **non dimostrata**. Preservate le precedenti prove positive
  e i fallimenti nel [report runtime](RUNTIME_CHECKS_20261004.md).
- Salvataggio del candidato e package in carico al parent, non certificazione:
  [contenuto e restore selettivo](WORKING_CANDIDATE_HDMI_120.md).
  Non ripristinare tutto SteamVR settings né presumere che il modo custom
  NVIDIA venga ricreato dal JSON. DP resta sospeso, senza modifiche ai cavi.
