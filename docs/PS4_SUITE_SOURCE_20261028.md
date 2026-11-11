# PS4 Camera nella Suite — integrazione sorgente

## Contratto e uso

La PS4 Camera si aggiunge al registro descritto in
`AUDIO_TRACKING_CAMERA_20261015.md`, §4: stessa tendina Camera, stesso
`TrackingWorker`, stesso bridge documentato in `DRIVER_CAMERA_CONTRACT.md`.
Non richiede un'altra GUI, un runtime separato o una nuova configurazione SteamVR.

L'integrazione usa l'identificatore `ps4-stereo`, con backend dedicato dello
stesso nome e indice 0. Il backend usa internamente DirectShow; non coincide
con la sorgente generica `opencv-dshow`.

La PS3 Eye resta il default. Il profilo PS4 è separato dalle preferenze PS3:
640×400 per occhio, 60 FPS, soglia V160/S90 e area minima 20. Il profilo LED
misurato usa exposure −7, gain 8, brightness 1 in unità native OV580/DirectShow.
I valori PS3 di esposizione e guadagno non sono controlli PS4.
La calibrazione a scacchiera può richiedere luminosità temporaneamente maggiore,
da ripristinare al termine; non è il profilo di acquisizione LED. **Brightness
non è esposizione**: imporre brightness 0 non riduce il tempo di integrazione.

**Rilievo live 2026-11-01:** la luminosità alta della scacchiera era rimasta sul
device (readback 4.0 contro 0 richiesto) perché il path di acquisizione non
applicava i controlli: con luce diurna ha prodotto 10–42 candidati/occhio e zero
pose valide. La sola correzione brightness 0 lasciava `exposure: null`, quindi
non correggeva l'esposizione della scacchiera. **Una stanza buia non è un
prerequisito** e non sostituisce la verifica dei controlli e dei cinque LED.

Il path Suite (`_SuiteControls`, senza ripristino) ora usa i controlli standard
DirectShow della sola PS4 selezionata: legge range/step/capabilities, scrive
Manual=2 e verifica valore **e flag**. Il setter numerico exposure di OpenCV
5.0.0 passa invece Flags=0. Il profilo ora dichiara esplicitamente i controlli
validati **−7/8/1**: esposizione breve più segnale sufficiente per i LED deboli,
non brightness 0 a qualunque costo. Per un profilo legacy con exposure null
resta la policy `led_frame` (a 60 FPS, −6 nominale); gain null non viene toccato.
Nessun valore PS3 viene riutilizzato. Il valore shutter è nominale API, non un
timestamp o una misura del tempo d'integrazione hardware.

Su questo OV580 il readback immediato può essere vecchio: le verifiche dopo
4/16/60 frame sono limitate, sullo stesso owner e senza riaprire lo stream.
Un problema resta un warning, non una conferma inventata. La prova Suite a
−6 ha confermato exposure −6/Manual e brightness 0/Manual dopo quattro frame,
ma **1.829 frame, IMU fresca e UDP attivo hanno ancora prodotto zero pose
valide**: mancavano cinque corrispondenze stereo non ambigue. Questo esito è
**precedente alla correzione geometrica e al profilo finale**: il controllo
raw-count 5/5 scartava anche cinque LED correttamente abbinati in presenza di
riflessi non abbinati. Ora si richiede una sola costellazione front-five tra
match stereo certificati; ambiguità, soglie e gate di posa restano invariati.
La prima prova completa ha ottenuto **731 pose valide su 1.037 frame**, IMU
fresca, UDP e probe 6DoF valido, `valid_published=1620`, con perdite ancora
presenti. Dettagli, PNG sincroni, matrici, regressioni registrate e cleanup:
[PS4_FRONT5_DIAGNOSTICS_20261101.md](PS4_FRONT5_DIAGNOSTICS_20261101.md).
Storia dei primi controlli:
[PS4_EXPOSURE_20261101.md](PS4_EXPOSURE_20261101.md).

Dopo il riavvio della Suite, scegliere **PS4 Camera stereo** nella tendina
Camera e applicare la sorgente. I campi PS3 restano salvati per tornare alla
PS3 Eye; non rappresentano i controlli effettivi della PS4. Il profilo effettivo
è `configurations/ps4_suite_source.json`, registrato con hash e ordine occhi
nel log all'apertura e alla riapertura della camera.

## Geometria e limiti

Il rig frontale raffinato è
`configurations/psvr_front_leds_stereo_refined_20261028.json`. La sua evidenza
di validazione appartiene alla calibrazione risolta separatamente
`logs/ps4_camera/calibration/ps4_stereo_20261023T231947385332Z.swapped.json`.
Il profilo Suite deve usare una copia esatta di questo artefatto e dichiarare
esplicitamente l'ordine dei crop grezzi `right,left`; non deve invertire o
sostituire la calibrazione canonica
`configurations/ps4_stereo_640x400_calibrated.json`.

La prova di occlusione identifica i sensori fisici, non modifica da sola il
riferimento geometrico di una calibrazione. Nessun tentativo automatico con
occhi scambiati è ammesso per ottenere una posa valida.

Il modello contiene cinque LED frontali: non dimostra tracking laterale o
posteriore, né tracking parziale a due LED. Scala e origine anatomica restano
sperimentali. Posa ambigua, LED insufficienti o IMU driver non fresca devono
produrre perdita esplicita, mai una posizione inventata.

## Sicurezza e validazione

Il driver conserva la proprietà esclusiva dell'HID. La Suite usa il ricevitore
IMU esistente e il protocollo posizione v2 con identità di sessione/epoca;
filtri e sender non cambiano. Selezionare la sorgente non deve scrivere
`camera_source` in `steamvr.vrsettings`.

Lo smoke di anteprima può usare `tracking_mode="3dof"`, senza pubblicazione
posizione e senza avviare SteamVR. Un test ottico standalone o un replay non
equivalgono a una validazione 6DoF completa della Suite con il driver.

L'incertezza usa un'approssimazione dall'occhio di riferimento e dai cinque
punti frontali, propagata nel frame IMU e al punto testa dal bridge esistente.
Non è una covarianza stereo validata e non comprende gli errori sistematici
di rig, scala o calibrazione.

Stato aggiornato 2026-11-01: una prova hardware reale della Suite ha osservato
pose 6DoF valide con il driver. Non è una validazione metrologica, di scala,
movimento completo o assenza di dropout; i cinque LED frontali e l'IMU fresca
restano necessari. Runtime avviato solo per prove esplicitamente autorizzate.

Verifica finale offline: **1126 passed, 7 skipped, 1023 subtests passed**
(`tests/test_camera*.py`, `tests/test_suite*.py`, `tests/test_imu_clock.py`).
Coperti anche rotazione IMU non identità, offset testa/scala, conservazione
dell'allineamento al recenter, conflitto ottico/IMU e rifiuto della pubblicazione
dopo scadenza o cambio sessione/epoca durante solve, covarianza o filtro.
Hash invariati per preferenze utente, intrinseci PS3, rig PS3 a nove punti
e calibrazione PS4 canonica; la copia Suite coincide con l'artefatto swapped.
