# M5 — display Windows/OpenVR — software implementato, prove reali rinviate

Aggiornamento M6 successivo: profili JSON e adapter ottico ora disponibili
opt-in, validatore offline e pattern stereo generati. Default resta identity;
suite corrente 28/28 CTest. I dettagli M5 sotto conservano la sessione iniziale.
Vedere [report M6](MILESTONE_6_SOFTWARE.md).

2026-10-04. **M5 IN_PROGRESS**, non COMPLETE. L'utente ha autorizzato lavoro
autonomo e subagenti, rinviando i controlli manuali. M3/M4 mantengono i gate
fisici/runtime pendenti. Nessuna presentazione compositor sul PSVR dichiarata.

## 1. Inventario display read-only

`tools/psvr_display/` implementa un tool Windows x64 indipendente da USB e
SteamVR. Package pronto `out/display/psvr_display.exe`, README e licenze.
La build completa lo installa anche accanto al diagnostico in `out/diag`.

```powershell
.\out\display\psvr_display.exe --help
.\out\display\psvr_display.exe --json
```

`--help` non enumera hardware. `--json` e l'avvio senza opzioni interrogano
i display reali; **non sono stati eseguiti in questa sessione**, solo preparati.
Exit: 0 successo, 1 argomenti invalidi, 3 errore enumerazione/output.

- `GetDisplayConfigBufferSizes` / `QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS)`:
  buffer e tentativi limitati, retry per cambi di topologia.
- `DisplayConfigGetDeviceInfo`: percorso monitor esatto, nome GDI/friendly,
  path adapter; source/target ID e LUID, origine desktop, risoluzione source
  e scanout, refresh razionale path e signal, rotation/scanline/scaling raw.
- `EnumDisplaySettingsExW`: modi GDI disponibili; frequenze intere esplicite,
  senza inferire supporto per refresh frazionari. Nessuna chiamata setter.
- EDID manufacturer/product solo se Windows dichiara disponibili. Raw EDID
  bytes non letti; campi availability e warning rendono esplicito il limite.
- JSON schema 1, `read_only=true`, `automatic_selection=false`, scope active
  paths. Hint 1920x1080 puramente informativo, non identifica un PSVR.

## 2. Componente OpenVR per desktop esteso

`driver/display_component.hpp`: implementa **IVRDisplayComponent_003** dello
SDK fissato. Opt-in separato `driver_psvrpc.displayEnabled=false` per default,
oltre a `enable=false` del driver. Nessuna attivazione automatica dalla build.

Quando abilitato e verificato:
- bounds dell'esatto monitor configurato, coordinate signed anche negative;
- pannello 1920x1080 non ruotato, metà L/R 960x1080;
- render target per occhio configurabile, 1..8192 per dimensione;
- projection raw per occhio **esplicita**, nessun FOV ottico inventato;
- identity distortion diagnostica, RGB identici; inverse identity conforme
  alla firma `_003` e controlli finitezza/channel/eye;
- HasDisplayComponent vero solo con componente presente; direct/virtual false;
- DisplayFrequency dalla frazione configurata, verificata contro lo snapshot.

Non è Direct Mode e non implementa un renderer client. `ComputeDistortion`
identity non corregge le lenti: non usare il suo risultato come accettazione
ottica M6. L'eventuale output reale richiede SteamVR/GPU/HDMI compatibili.

## 3. Selezione e cambi di topologia

`driver/display_selection.hpp` integra configurazione e inventario:
- percorso monitor **esatto**; niente fallback al primary/ordinal/primo EDID;
- un solo target selezionato attivo/disponibile, source non condivisa con clone;
- dimensioni source e scanout identiche a 1920x1080;
- rotation identity, scaling identity, progressive path e video signal;
- origine x/y esatta e refresh path/signal/config razionalmente equivalenti.

La verifica è applicata prima di registrare HMD e ripetuta in Activate.
Thread display separato verifica ogni circa 1 s senza enumerare modi GDI;
non blocca `RunFrame` o il reader IMU con query display. Perdita, cambiamento
o eccezione latched: posa invalidata, stop reader, richiesta SDK
`RequestRestart` una sola volta dal thread host. Non si sposta automaticamente
l'output su un altro monitor e un target riapparso non resuscita il componente.
Il latch vive con l'HMD ed è conservato anche tra Deactivate/Activate;
solo una nuova istanza nella reinizializzazione del provider lo ricrea.

**Limite:** controllo periodico e snapshot non sono atomici con presentazione;
`RequestRestart` è una richiesta al runtime, non prova di arresto sincrono
del compositor. Hotplug/rearrangement e cessazione output vanno verificati
manualmente. Riavvio con config non corrispondente viene rifiutato. Le query
Windows possono essere lente: shutdown attende il ritorno della query read-only.

## 4. Settings richiesti per la futura prova

Sezione `driver_psvrpc` negli override utente SteamVR; non modificare i
default nello staging (ricopiati dalla build). Conservare tutti gli altri
settings e fare backup prima del futuro intervento manuale.

| Chiave | Valore da fornire |
|---|---|
| `enable`, `displayEnabled` | opt-in esplicito per prova runtime |
| `recenterMode` | `full` oppure `yaw` |
| `displayMonitorPath` | copia esatta dal JSON di inventario |
| `displayX`, `displayY` | origine source desktop dello stesso snapshot |
| `displayWidth`, `displayHeight` | 1920, 1080 |
| `displayOrientationDegrees` | 0 |
| `displayRefreshNumerator`, `displayRefreshDenominator` | frazione attiva, non Hz arrotondati |
| `renderWidth`, `renderHeight` | dimensioni per occhio scelte per la prova |
| `projectionLeftLeft/Right/Top/Bottom` | quattro tangenti esplicite occhio sinistro |
| `projectionRightLeft/Right/Top/Bottom` | quattro tangenti esplicite occhio destro |

SDK raw projection: Left e Top negativi, Right e Bottom positivi; il nome
Top rappresenta il bound −Y (bottom), Bottom il bound +Y (top) nel contratto
SDK. Non scambiare i segni per una convenzione grafica intuitiva. Tangenti
simmetriche ±1 possono servire solo come profilo diagnostico dichiarato,
non come calibrazione PSVR. Mancanza/NaN/sign errati rifiutano l'attivazione.
I default lasciano path/refresh/render senza valori utilizzabili e omettono
projection: abilitare il solo switch non espone un display arbitrario.

## 5. Test e verifica software

Build MinGW x64 OK; suite completa aggiornata **24/24 CTest PASS**.
- 76 check inventario sintetico: identità, clone/duplicate/missing,
  geometry, scale/rotation/interlace, refresh equivalente/mismatch e JSON.
- Component test: output `_003`, bounds/viewport/projection/inverse,
  103 configurazioni invalide e verifier assente/rifiutato/eccezioni.
- Integration selection test: fraction vs rounded Hz, origini, scanout,
  path/clone/unplug, invalid projection e topology loss latch.
- CLI test help e invalid args: nessun percorso live di enumerazione.
- M4 mock runtime ampliato: 19 casi e 192 sessioni lifecycle, registrazione
  HMD via host mock con display disabled, settings/reject/cleanup/debug buffer.
  **Activate non chiamata**: nessun HID, runtime SteamVR o query display.
- Regressioni M3/monitor, package factory e script precedenti tutte PASS.
- Moduli e review realizzati in parallelo con subagenti. Review: aggiunti
  rifiuto scaling nonidentity, equivalenza refresh path/signal, verifica in
  Activate e monitor topologia con richiesta restart.

## 6. Gate manuali ancora aperti

Inventario reale/identità PSVR, desktop esteso attivo, geometria/refresh HDMI,
registrazione/HMD/pose SteamVR, output sul pannello giusto, separazione L/R,
continuità/vsync/frametime e comportamento hotplug/restart. Nessun EDID override
o cambio modo implementato. Seguire [coda manuale](MANUAL_VALIDATION_QUEUE.md)
quando l'utente torna; M5 resta IN_PROGRESS.
