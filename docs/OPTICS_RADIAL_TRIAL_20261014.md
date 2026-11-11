# PSVR — primo test radiale nel visore, 2026-10-14

**Profilo diagnostico moderato attivo, non calibrato né accettato otticamente.**
Richiesta dell'utente: compensare la netta deformazione attribuita alle lenti
prima del test di gioco. Tracking mantenuto su One Euro 2,5 Hz/β8.

## Baseline e convenzioni

Baseline live: `distortionMode=identity`, path vuota, proiezioni ±1 per occhio,
viewport 960×1080, display 1920×1080 a 90 Hz. Nessuna compensazione radiale.
L'implementazione esistente di ComputeDistortion riceve UV display per occhio
con origine in alto a sinistra e restituisce UV render sampling RGB.
Il profilo usa **forward display-to-render**, non inverse né flip Y.

I riferimenti pubblici OpenHMD/PSVRFramework non forniscono una calibrazione Sony:
la pagina Optical-characteristics avverte che K1/K2 0,22/0,24 provengono da DK1,
con misure approssimate. Non sono stati copiati come coefficienti PSVR validati.

- https://github.com/OpenHMD/OpenHMD/blob/master/src/drv_psvr/psvr.c
- https://github.com/gusmanb/PSVRFramework/wiki/Optical-characteristics

## Famiglia diagnostica e primo profilo

Centro iniziale invariato `(0,5;0,5)`, separato per occhio. Correzione radiale
isotropa in metrica del pannello: `axis_scale=(960/1080,1)`, non UV square pura.
Radius normalizzato `r_n=2*hypot(aspect*(u-cx),v-cy)`;
gain `1+k1*r_n²`, equivalente nello schema v1 a `b=4*k1, a=c=0, d=1`.

| Profilo | k1 diagnostico | b schema v1 |
|---|---:|---:|
| mild | 0,05 | 0,2 |
| **medium, attivo** | **0,10** | **0,4** |
| stronger | 0,15 | 0,6 |

È uno sweep prudente della direzione/intensità, non fitting delle lenti. Il
modello corrente non rappresenta direttamente il termine classico `k2*r_n⁴`:
non fingere equivalenza con DK1. RGB tutti 1 per isolare la geometria; correzione
cromatica da affrontare dopo. Scala centrale d=1, FOV/proiezioni invariate.

Mapping outward delle coordinate di sampling produce la pre-deformazione barrel
dell'immagine. UV possono uscire dal texture bounds vicino ai bordi/corner:
nessun clamp artificiale nel driver; eventuali bordi neri o campo ridotto vanno
annotati, non considerati calibrazione riuscita. Curvatura opposta o peggioramento
richiedono rollback/rivalutazione della direzione, centro e normalizzazione.

## Verifiche effettuate

- Validator nativo: tutti e tre i profili PASS schema/math e policy SDK;
  `optical_acceptance_verified=false`.
- Python: **3/3 test PASS** (modifica selettiva/reversibile, aspect/scala centrale,
  validatore nativo). CTest ottici/display: **6/6 PASS**.
- Nessuna modifica al codice/math del driver, alla fusione IMU, tracking,
  calibrazione PS Eye, rig, preset 60/6/13/0, refresh, proiezione, IPD o dashboard.
- Cambiate **solo** `distortionMode` e `distortionProfilePath`, con SteamVR chiuso,
  backup byte-esatto e sostituzione atomica; preservati tutti gli altri settings.
- Runtime reale: `optical profile loaded ... medium k1=0.10`, Activate `identity=0`,
  refresh 90/1 e proiezioni ±1 confermate. Callback esempio display `(0,0)` →
  sampling `(-0,08950617,-0,08950617)` RGB, prova della mappa non identity attiva.
- IMU `tracking` dopo nuova calibrazione; griglia attivata **solo dopo** pose
  PSVR valide e compositor fullscreen. Log: `visible=true`, piano piatto
  HMD-relative, distanza 1,5 m, larghezza 3,4 m, durata 120 s. Nessuna presenza
  sintetica o posa inventata. Il piano non è dashboard curva né superficie sferica.

## Valutazione richiesta

Chiudere alternativamente un occhio. Guardare linee verticali/orizzontali al
centro e ai lati: più dritte, invariato o curvatura opposta? Annotare differenze
sinistro/destro, bordi neri/tagliati e dimensioni apparenti. Poi valutare frange
RGB e nitidezza separatamente: vera aberrazione sferica/defocus non è corretta
necessariamente dalla sola mappa radiale. Posizione degli occhi/eye relief resta
una variabile fisica; non inferire un profilo universale da una sola osservazione.

**Feedback visivo ancora in attesa.** Nessun risultato ottico accettato dichiarato.

## Artefatti e rollback

- `configurations/optics_radial_{mild,medium,stronger}_20261014.json`
- `logs/optics_medium_20261014_0110/settings_before.json`, backup esatto
- `logs/optics_medium_20261014_0110/journal.json`, due chiavi before/after
- `logs/optics_camera_20261014_0116.jsonl`, One Euro
- `logs/optics_grid_medium_20261014_0118.jsonl`, stato/probe/griglia
- `tools/psvr_optics/trial.py`, applicazione/rollback selettivo
- `tools/psvr_optics/visual_trial.py`, attesa bounded di tracking reale prima della griglia

Con SteamVR chiuso (ripristina solo le due chiavi ottiche, non vecchi settings
di display/tracking o di altre app):

```powershell
.\.venv-camera\Scripts\python.exe -m tools.psvr_optics.trial `
  --restore logs\optics_medium_20261014_0110\journal.json `
  --backup-dir logs\NEW_optics_restore
```

Per riaprire la griglia con runtime già attivo e visore in tracking:

```powershell
.\.venv-camera\Scripts\python.exe -m tools.psvr_optics.visual_trial `
  --out logs\NEW_grid.jsonl --wait-seconds 180 --duration-ms 120000
```

Nuova directory/file richiesti, no overwrite. Il rollback rifiuta journal stale
se nel frattempo le chiavi ottiche sono state cambiate, preservando il lavoro
successivo. Al riavvio lasciare il visore fermo su appoggio per gli 8 s IMU.
