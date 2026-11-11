# 6DoF frontale e raccordo al centro nelle grandi rotazioni

Aggiornamento dopo la prova hardware: stabilità rifiutata dall'utente;
corretta una regressione del raccordo nei dropout frontali. Dettagli,
nuova DLL e pulizia log in `ANGULAR_TRIAL_REGRESSION_AND_CLEANUP_20261015.md`.
I numeri di build e SHA sotto identificano la prima variante provata.

## Comportamento

Il normale 6DoF della suite richiede almeno tre LED associati. Le ipotesi
a due LED restano disponibili come esperimento esplicito, disattivato di default.
I seed a due punti possono ancora generare ipotesi del matcher: è la soluzione
finale pubblicabile che richiede almeno tre corrispondenze.

Con l'esperimento disattivato, il driver applica questa politica di rendering:

- fino a circa 90 gradi di yaw: posizione ottica e rotazione IMU;
- tra 90 e 120 gradi: posizione raccordata verso il centro seduto con una curva
  smoothstep; la velocità del raccordo è limitata a 0,25 m/s;
- da 120 gradi verso il retro: obiettivo di posizione centrata, rotazione
  sempre dall'IMU. La posizione raggiunge il centro rispettando il limite di
  velocità anche se la testa ruota rapidamente;
- tornando avanti, sotto 85 gradi e con una posizione ottica valida, rientro
  al 6DoF usando il raccordo di riaggancio già configurabile (150 ms e
  massimo 1,5 m/s per default, esteso se il salto richiede più tempo);
- se i LED mancano ancora al ritorno, resta il fallback centrato. Una nuova
  perdita breve durante il raccordo di rientro non interrompe la posa.

La politica è speculare a destra/sinistra. Lo yaw è quello dell'orientamento
3DoF nel riferimento del ricentraggio; non è un limite alla rotazione né una
misura calibrata dell'angolo rispetto alla camera. Una deriva del 3DoF può
spostare il punto fisico in cui interviene la politica: questa modifica non
implementa una nuova correzione della deriva dell'orientamento.

Il centro è l'origine seduta gestita dal bridge all'ultimo ricentraggio, più
`initial_offset_m`. Non è la posizione della telecamera, né una traslazione
ruotata con la testa. All'ingresso si memorizza l'ultima posizione pubblicata;
le misure ottiche incerte alle grandi rotazioni non trascinano quel riferimento.

## Validità e separazione dei canali

Il raccordo è nel controller del driver, non in un pacchetto ottico inventato.
La rotazione e la velocità angolare rimangono quelle IMU. Il driver può
autorizzare la posa `3dof_fallback` senza una posizione ottica valida; la
validità ottica originale rimane distinguibile nei dati diagnostici.

IMU stale/disconnessa/non valida, display non valido e contesti incoerenti
non vengono promossi a pose valide. Cambi di sessione, epoca di ricentraggio
e clock regressivo eliminano i riferimenti di raccordo precedenti. Solo la
pubblicazione RunFrame avanza il raccordo: GetPose e le query diagnostiche
non spostano la posizione.

Le perdite ottiche frontali, inclusi i problemi noti a 30–40 gradi, mantengono
la politica precedente di grace/perdita se non si era già entrati nel raccordo
angolare. Questa funzione non è presentata come soluzione di quei problemi.

## Opzione nella suite

Scheda **Tracking → Opzioni 6DoF → Sperimentale: posizione anche con 2 LED**.

- **Disattivata, default:** worker con almeno tre LED e
  `positionAngularCenterEnabled=true` in modalità 6DoF.
- **Attivata:** worker autorizzato a usare due LED con vincolo di orientamento
  IMU; raccordo angolare disattivato, quindi il tracking può proseguire anche
  posteriormente con due LED. Rimane sensibile a deriva, geometria e identità
  dei LED. È richiesto l'aggancio frontale iniziale per il riferimento camera/IMU.

In 3DoF puro non viene attivata la politica della posizione camera. Il fallback
generico già presente resta una scelta indipendente.

La preferenza persistente è `two_led_experimental`, booleana. I vecchi file
vengono caricati aggiungendo il nuovo default; lo schema UI passa da 28 a 29
chiavi. L'impostazione aggiorna entrambi i consumatori (worker e driver).
Il cambiamento richiede riavvio VR: **Avvia VR** dalla suite applica le chiavi
prima di partire; **Collega** a un runtime già avviato non applica il driver.

## Diagnostica e file

- `driver/position_continuity.hpp`: raccordo, centro e continuità di ritorno.
- `driver/driver_psvrpc.cpp`: autorizzazione nel contesto IMU corrente e status.
- `tools/psvr_camera/coherent_imu_led_pose.py`: minimo di LED configurabile.
- `tools/psvr_suite/{settings,tracking,app,i18n}.py`: preferenza, inoltro e UI IT/EN.

`tracking-status` espone `angular_centering`, `angular_center_weight` e la
configurazione `positionAngularCenterEnabled`. Il peso è quello dell'obiettivo
angolare, non la percentuale effettiva di movimento già compiuto quando è
attivo il limite di velocità. Il log del worker conserva
`optical_tracking.two_led_experimental`.

## Verifica software

- 295 test Python e 219 sottotest passati su suite, impostazioni, UI headless,
  camera, consumatori, IMU/LED e driver link.
- Due gruppi CTest nativi passati: `position_continuity_contract` e
  `driver_runtime_mock`.
- Il contratto di continuità include ingresso a 90 gradi, metà percorso a
  105 gradi, centro a 120/180 gradi, rotazione rapida a sinistra, query senza
  mutazioni, mancanza di LED al ritorno, riaggancio continuo, perdita breve
  durante il riaggancio, cambio epoca/sessione, IMU stale e display invalido.
- Sweep sintetico nativo di **40 minuti**, attraversando ripetutamente le
  soglie con misura ottica assente oltre 90 gradi: pose valide e velocità di
  centering entro il limite. È simulazione, non prova hardware prolungata.

Replay del solo worker, con almeno tre LED, sulla registrazione v2 di
135.114 frame: `logs/ac_evo_coherent_v2_20261015/minimum-three-led-replay.json`.
Frame invalidi nella finestra attiva 276 → 305; episodi 85 → 91;
salti pubblicati >15 mm 61 → 55; massimo salto pubblicato invariato 71,76 mm.
Questi numeri **non includono il nuovo controller angolare del driver** e
non dimostrano miglioramento dei grigi nel visore. Il costo del rifiuto delle
coppie è esplicitamente conservato, senza promuoverle a misure affidabili.

## Installazione e prova necessaria

DLL MSVC compilata e installata nel pacchetto già registrato:
`out/driver_psvrpc_6dof/psvrpc/bin/win64/driver_psvrpc.dll`.

SHA256 installato:
`4db41c22639117f1a97fd4d7fe6e110a1195d8f19c354653da46ca8a454b9f68`.

Backup precedente e manifest:
`logs/angular_center_install_20261015T213933Z/`.
Nessun avvio hardware effettuato dall'installazione.

Per la prova: riaprire la suite, lasciare l'esperimento spento e usare **Avvia
VR** dopo aver chiuso l'eventuale runtime precedente. Agganciare frontalmente;
verificare poi una rotazione oltre 90 gradi, permanenza verso il retro e
ritorno avanti, controllando il raccordo della posizione e la continuità
della rotazione. **Accettazione nel visore ancora da effettuare.**
