# PS4: esposizione manuale e verifica Suite, 2026-11-01

## Problema e correzione

Il profilo Suite iniziale era brightness 0, exposure null, gain null.
Brightness è un controllo diverso dal tempo di esposizione. Applicare solo
brightness non annullava i controlli più chiari usati per la scacchiera; non
è corretto richiedere una stanza buia per compensare questa omissione.

`ps4_dshow_controls.Ps4DshowControl` usa solo COM/DirectShow standard, senza
dipendenze aggiuntive, payload USB vendor o modifiche firmware/binding. Prima
di collegare il filtro verifica che il **medesimo indice esplicito** abbia un
moniker runtime PS4 `VID_05A9&PID_058A/B`; non seleziona la prima camera utile.
Legge `GetRange` e `Get`, valida range/step e supporto Manual prima di `Set`.
Il setter numerico exposure di OpenCV 5.0.0 passa Flags=0; qui si scrive
esplicitamente Manual=2. Sono verificati sia valore sia flag del readback.

Il path Suite persistente risolve un exposure legacy null con la policy esplicita
`led_frame`: a 60 FPS richiede −6 log2 secondi (15,625 ms nominali), supportato
dal range reale e non maggiore dell'intervallo nominale. Non è una misura
hardware del tempo d'integrazione. Il tentativo iniziale −7 (7,8125 ms) lasciava
solo tre candidati per occhio con i controlli di allora. Il profilo finale,
dopo diagnosi dei blob e prova live, è **exposure −7, gain 8, brightness 1**:
il quinto LED deve avere segnale sufficiente, non essere escluso per imporre
brightness 0. I controlli brightness/gain richiesti usano anch'essi
IAMVideoProcAmp Manual=2; un gain legacy null resta non modificato. La calibrazione
temporanea conserva il precedente contratto di rollback, separato dalla Suite.

Il controllo è sul bounded owner della camera, applicato una volta, senza
riaprire lo stream e senza ripristinare i valori scacchiera alla chiusura.
Un readback immediato vecchio genera warning; al massimo tre controlli di
lettura, dopo 4/16/60 frame consegnati, possono confermare il valore ritardato.
Errore o mancata conferma non vengono trasformati in successo; l'acquisizione
continua, mentre i gate ottici restano invariati.

## Misure hardware

- Inizialmente PnP riportava **USB Boot 05A9:0580, Status OK**, non un device
  "wedged". Il parent ha poi eseguito il loader esterno già documentato, con
  firmware temporaneo upstream pinned; questa implementazione non esegue il
  loader. Dopo il caricamento: `USB Camera-OV580`, runtime **058B, Status OK**.
  Nessuna installazione o modifica dei binding in questa verifica.
- Esposizione: min **−13**, max **−1**, step **1**, default **−3**,
  capabilities **3** (Auto/Manual); stato iniziale letto **−3, flags 2**.
- Brightness e gain: min **0**, max **8**, step **1**, default **4**,
  capabilities **2** (Manual). Le unità gain/brightness non sono esposizione
  e non vanno equiparate ai controlli PS3.
- Set exposure −7: prima osservazione ancora −3/Manual; lettura successiva
  indipendente −7/Manual. Nella sorgente Suite il readback ritardato si è
  confermato dopo quattro frame. Lo stesso comportamento è stato misurato
  per exposure **−6** e brightness **0**, entrambi Manual=2.
- Sweep finiti nella **luce corrente**, senza chiedere buio o replug, hanno
  provato esposizioni −9…−3 e diversi valori supportati di gain/brightness.
  Tutte le acquisizioni completate hanno rilasciato la camera senza timeout.
  Più segnale non ha garantito cinque LED: comparivano ulteriori candidati
  ambientali, oppure mancava il LED inferiore debole nell'altro occhio.
- Esempio nativo exposure −5, gain 2, brightness 0: **91 coppie**, riferimento
  **5 candidati** e altro occhio **4** in tutte le coppie. Nel PNG originale
  l'area del componente del LED inferiore sinistro, con la maschera HSV/white
  cores della Suite, è **22 pixel** nel riferimento e **2 pixel** nell'altro
  occhio, sotto l'area minima **20**. L'enumerazione dei componenti piccoli è
  stata solo diagnostica: soglie e gate di tracking non sono stati modificati.
  Non si attribuisce una causa fisica certa a questo segnale debole.

## Prime prove 6DoF: non superate, poi superate dalla diagnosi successiva

Prova reale tramite `RuntimeController` e `TrackingWorker`, sorgente
`ps4-stereo`, modalità `6dof`, power-on esplicito prima di `start_vr`:

| Prova | Frame | Ottici validi | IMU fresca | Driver |
| --- | ---: | ---: | ---: | --- |
| Exposure −7, prima integrazione | 1826 | 0 | 1826 | orientation valida, position/pose false |
| Exposure −6, brightness 0 nativi | 1829 | 0 | 1829 | orientation valida, position/pose false |
| Ripetizione −6 con conteggi stereo separati | 1052 | 0 | 1052 | orientation valida, position/pose false |

Nella seconda prova il riferimento ha restituito 3/4/5/6 candidati
(8/255/988/578 frame); tutte le pose sono state rifiutate con
`ps4_stereo_five_unambiguous_matches_required`. IMU fresca e UDP attivo non
sostituiscono la corrispondenza stereo. Il log driver ha registrato **42
finestre di pubblicazione e valid_published=0**. Non si rivendica 6DoF valido.
I diagnostici del tracker ora riportano i conteggi separati dei due occhi,
oltre a stato e numero di match, per distinguere questa causa dalla sola
luminosità del riferimento.

La ripetizione finale ha avuto **97 frame con 5 candidati per ciascun occhio**,
ma soltanto 1–4 corrispondenze accettate (70 frame con quattro match): avere
cinque blob non prova che siano gli stessi cinque LED. Nessun frame ha avuto
cinque match validi. Stati di triangolazione: 885 `points`, 167 `partial`;
`points` da solo non significa posa front-five. In questa prova **30 finestre
driver, valid_published=0**. Non si deduce da questi dati una calibrazione
errata né si allargano le tolleranze per forzare le corrispondenze.

Le prove hanno chiuso il worker, fermato SteamVR con il percorso graceful,
confermato cinema e power-off tramite nuovo stato F0. Nessun processo runtime
rimasto. Non sono stati cambiati preferenze, calibrazioni, rig o soglie.

Evidenze bounded in `C:\Users\rickk\AppData\Local\Temp\opencode`:

- `ps4-suite-live-20261101T132304-summary.json` e relativo JSONL;
- `ps4-suite-live-20261101T132523-summary.json` e relativo JSONL, con conteggi
  per occhio e match;
- `ps4-exposure-*-sweep-summary.json`, report per impostazione e PNG/JPEG;
- `ps4-suite-live-check-final.log`, con cleanup e power-off verificato;
- `ps4-exposure-all-tests.log`: **175 passed, 141 subtests passed**, tutti i
  `test_camera_ps4*.py` e `test_suite_ps4_panel.py`, senza aprire hardware.

Riferimenti API: [CameraControlFlags](https://learn.microsoft.com/en-us/windows/win32/api/strmif/ne-strmif-cameracontrolflags),
[CameraControlProperty](https://learn.microsoft.com/en-us/windows/win32/api/strmif/ne-strmif-cameracontrolproperty),
[OpenCV DirectShow](https://github.com/opencv/opencv/blob/5.x/modules/videoio/src/cap_dshow.cpp).

All'epoca mancava una nuova prova Suite con **cinque LED sufficientemente visibili in
entrambi gli occhi, cinque match geometricamente validi**, pose valide con
IMU fresca e UDP, e conferma driver `6dof`, `pose_valid=true`,
`position_valid=true`, `valid_published>0`. La correzione dei controlli è
misurata; da sola non risolveva il segnale del quinto LED. La successiva
diagnosi ha corretto il rifiuto di clutter non abbinato, mantenuto la
selezione geometrica non ambigua e validato il profilo −7/8/1 con **731 pose
valide su 1.037 frame** e `valid_published=1620`. Non equivale a tracking senza
perdite: dettagli in [PS4_FRONT5_DIAGNOSTICS_20261101.md](PS4_FRONT5_DIAGNOSTICS_20261101.md).
