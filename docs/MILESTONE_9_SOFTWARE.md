# M9 — posizione esterna opt-in e integrazione IMU/OpenVR (software)

2026-10-05. **M9 IN_PROGRESS: software implementato, acquisizione PSEye reale
verificata; tracking metrico del visore e runtime 6DoF non validati.** Il lavoro driver non
modifica SteamVR, non registra il driver, non installa pacchetti e non esegue
`Activate` reale. I gate M3–M6 restano aperti.

Obiettivo: la posizione 6DoF da un tracker esterno (webcam) consegnata in
loopback, fusa con l'orientamento IMU 3DoF già esistente in un'unica posa
OpenVR. La posizione resta **disabilitata per default**: senza opt-in esplicito il driver
si comporta esattamente come prima (posizione zero, nessuna predizione).

## 1. Protocollo condiviso, non indovinato

`ARCHITECTURE.md` §6 documenta che `aitrack_udp.cpp` legge memoria nativa come
`double`, prende solo XYZ e applica guadagni empirici (X/90, Y/140+1.3, Z/140):
**non sono unità SI certificate**. Indovinare unità o layout di OpenTrack
produrrebbe posizioni plausibili ma non verificate.

M9 usa un protocollo **esplicito, versionato e metrico**, di cui
`tools/psvr_camera/protocol.json` e `tools/psvr_camera/PROTOCOL.md` sono il
contratto e `tools/psvr_camera/protocol.py` è la fonte eseguibile. Il lato
driver **rispecchia** `validate_packet` per evitare derive tra i due lati.

Endpoint default **127.0.0.1:4271** (evita AITrack 4242 e l'API del monitor),
trasporto UDP, un oggetto JSON compatto ordinato per datagramma, UTF-8, con
newline finale.

```json
{"ambiguity_ratio":null,"frame":"psvr_world","host_time_ns":123,"inliers":null,
 "monotonic_s":0.0,"note":null,"orientation_wxyz":null,"position_m":[0.1,0.2,-0.5],
 "protocol":"psvr.position","reprojection_px":null,"seq":0,"source":"aruco",
 "state":"tracking","units":"m","valid":true,"version":1}
```

Frame `psvr_world`: destrorso, **+X destra, +Y alto, −Z avanti**, unità metri.
`orientation_wxyz` è `world_from_body`, WXYZ, opzionale: **il driver M9 possiede
già l'orientamento dall'IMU**, quindi la webcam contribuisce solo la posizione.
I campi `host_time_ns` e `monotonic_s` sono validati ma **non usati** per il
tempo: la freschezza dipende solo dal tempo monotono locale di ricezione.

### Validazione stretta (`driver/position_protocol.hpp`)

Un datagramma è accettato solo se, rispecchiando il mittente:

- oggetto JSON; `protocol == "psvr.position"`, `version == 1`,
  `frame == "psvr_world"`, `units == "m"`;
- `seq` intero ≥ 0; `monotonic_s` numero finito ≥ 0; `host_time_ns` intero ≥ 0;
- `valid` booleano; `state` in {tracking, lost, calibrating, ambiguous, error};
  `source` in {aruco, blob_led, none};
- **coerenza `valid`/`state` (regola aggiuntiva del driver)**: `valid=true` è
  accettato solo con `state="tracking"`. Un frame `valid=true` con
  `lost`/`error`/`ambiguous`/`calibrating` è contraddittorio e viene rifiutato:
  non deve mai diventare una posizione. Il validatore Python di riferimento non
  impone questa regola, quindi il driver è **di proposito più severo**; il
  `PositionBridge` la riapplica anche ai campioni costruiti direttamente;
- se `valid`: `position_m` è `[x,y,z]` finito; se `!valid`: `position_m` è null
  oppure `[x,y,z]` finito;
- `orientation_wxyz`, se presente, è quattro numeri finiti con
  `|norma − 1| ≤ 1e-3`;
- `reprojection_px`/`ambiguity_ratio` null o finiti ≥ 0; `inliers` null o
  intero ≥ 0; `note` null o stringa.

In più il driver impone un **limite metrico** `|componente| ≤ positionMaxAbsM`
(più severo del mittente), rifiuta chiavi duplicate e annidamento oltre 8
livelli, NUL grezzo e payload oltre 1024 byte. Le chiavi **sconosciute sono
tollerate** per compatibilità in avanti (come il validatore di riferimento).

### Limiti di interoperabilità (dichiarati)

- **OpenTrack/AITrack non sono interpretati direttamente**: i loro formati
  binari e le unità tipicamente in centimetri richiedono un adattatore che
  riscriva i pacchetti in `psvr.position` v1 e in metri. La porta 4242 di
  AITrack non viene ascoltata.
- Una versione diversa da 1 viene **rifiutata**, non interpretata per analogia.
- Il frame deve essere `psvr_world`; un mittente con assi diversi deve
  convertirli prima dell'invio.
- La posizione della webcam è una sorgente metrica **separata** dall'IMU e non
  è assunta calibrata: recenter e verifica fisica restano gate aperti.
- Il protocollo è JSON testuale: adatto a loopback e debug, non a payload ad
  alta frequenza su rete reale.

## 2. Ricevitore loopback (`driver/position_receiver.hpp`)

`PositionReceiver` è opt-in e vincolato a `127.0.0.1`:

- RAII Winsock (`WinsockSession`) e socket UDP chiuso nel distruttore;
- `bind` su `INADDR_LOOPBACK` con porta configurabile (porta effimera `0`
  consentita al costruttore puro, utile ai test);
- attesa limitata con `select` (50 ms per iterazione), così stop e recenter
  vengono serviti puntualmente;
- datagrammi troncati/eccessivi (`WSAEMSGSIZE` o > 1024 byte) contati e
  scartati; errori transitori (`WSAECONNRESET` da ICMP, `WSAEINTR`,
  `WSAETIMEDOUT`) non uccidono il ricevitore; errori fatali restituiscono `-1`
  e il chiamante marca la posizione come persa;
- contatori atomici (datagrammi/parsati/malformi/eccessivi/errori/fatali) per
  diagnostica e test.

## 3. Ponte e coerenza del recenter (`PositionBridge`)

`PositionBridge` è thread-safe e non inventa mai una posizione:

- **Sequenza**: duplicati e riordini entro la finestra di timeout sono
  rifiutati; un salto in avanti oltre `max_sequence_gap` è contato come gap ma
  accettato (nessuna interpolazione). Una sequenza più vecchia che arriva dopo
  che il flusso è rimasto silente oltre il timeout è accettata come nuovo
  baseline (recupero dopo riavvio del mittente).
- **`valid=false`**: avanza la sequenza e rende la posa inutilizzabile, senza
  però cancellare l'ultima posizione buona (che non viene mai pubblicata).
  `state="calibrating"` mappa su `waiting`, gli altri su `lost`.
- **Freschezza**: solo tempo monotono locale; `view(now, imu_epoch)` è valida
  se l'ultimo pacchetto è valido, entro `positionTimeoutMs`, e l'epoca di
  recenter è coerente.
- **Recenter condiviso**: `DebugRequest("recenter")` incrementa una richiesta
  atomica. Il thread IMU applica il recenter e produce una **epoca di mondo**
  monotona (`ImuFusion::world_epoch()`) insieme alla `world_rotation`
  (`reference_.conjugate()`); entrambe vengono lette dallo **stesso** stato di
  fusione e scritte nello snapshot (`recenter_epoch`, `world_rotation`), non più
  in un atomico separato pubblicato prima dell'orientamento. Il thread posizione
  legge l'epoca **stampata nello snapshot** e applica esattamente quella, così
  orientamento e origine non possono mai venire da epoche diverse. Se l'ultimo
  campione è invalido o stantio, il recenter resta pendente e si applica al primo
  campione valido e fresco. L'epoca avanza anche al recenter automatico di fine
  calibrazione e a `recalibrate`, quindi un cambio del frame di mondo forza
  sempre una nuova origine seduta.
- **Frame seduto**: il primo campione valido (e ogni recenter completato) fissa
  l'**origine** dalla posizione camera: prima di allora nessuna posizione è
  pubblicata, quindi le coordinate camera grezze (la cui origine è la camera,
  non la testa) non diventano mai una posa. Il vettore posizione viene poi
  espresso nel frame IMU recenterato:
  `position = Ry(cameraYawOffset) · world_rotation · (raw − origin)`.
  `world_rotation` è il riferimento di yaw di mondo applicato dall'IMU al
  recenter: **senza** applicarlo, un recenter di yaw lascerebbe la traslazione
  nel frame precedente e posizione e orientamento sarebbero ruotati l'uno
  rispetto all'altro della quantità di yaw rimossa.

## 4. Integrazione driver (`driver/driver_psvrpc.cpp`)

Configurazione `driver_psvrpc` (default disabilitato, valori esistenti
invariati):

| Chiave | Default | Vincolo |
|---|---|---|
| `positionEnabled` | `false` | assente ⇒ disabilitato |
| `positionPort` | `4271` | 1..65535 |
| `positionTimeoutMs` | `200` | 20..5000 |
| `positionMaxAbsM` | `10.0` | 0.05..1000, finito |
| `positionCameraYawOffsetDeg` | `0.0` | opzionale, −180..180, finito |

`positionCameraYawOffsetDeg` è l'allineamento **esplicito** tra il frame camera
(`psvr_world`) e il frame IMU iniziale, attorno a +Y (entrambi i frame sono
gravità-allineati, quindi solo lo yaw è fisicamente significativo). Il default
`0` è l'assunzione neutra documentata, **non una misura**: il montaggio
camera/IMU è un gate di calibrazione fisica aperto. La chiave è opzionale, così
le configurazioni esistenti non si rompono.

Le chiavi di posizione sono lette **solo in `Activate`** (quando la funzione è
effettivamente attiva), non in `Init`: in questo modo il contratto 3DoF di
default e i test mock di `Init` restano invariati. Con `positionEnabled=true`
ogni chiave è obbligatoria e validata; porta occupata o configurazione invalida
fanno fallire l'attivazione, senza degradare silenziosamente a 3DoF.

- Thread `read_position` dedicato: riceve, inoltra a `PositionBridge`, applica
  il recenter condiviso; fermato e unito in `Deactivate` prima del rilascio di
  socket/bridge (RAII).
- `GetPose` fonde snapshot IMU e vista posizione e produce la posa OpenVR:
  - `positionEnabled=false`: contratto 3DoF **identico**, `vecPosition=0`;
  - `positionEnabled=true`: servono orientamento valido **e** posizione fresca,
    valida, coerente (stessa `recenter_epoch` dello snapshot) e finita; altrimenti
    `TrackingResult_Running_OutOfRange` con posizione a zero (perdita esplicita,
    nessuna posizione inventata o vecchia);
  - `GetPose` copia snapshot e vista posizione sotto lo stesso lock, con
    `recenter_epoch`/`world_rotation` presi dallo snapshot: il puntatore al
    bridge non può essere azzerato sotto una `GetPose` concorrente a
    `Deactivate`;
  - `poseTimeOffset` usa il campione più vecchio tra orientamento e posizione;
  - `vecVelocity`/`vecAngularVelocity` restano zero: nessuna predizione.

## 5. Test (`tests/position_tests.cpp`)

Due metà, senza hardware né runtime OpenVR:

**Pura** — accettazione dell'esatto pacchetto compatto prodotto dal mittente
Python; rifiuto di protocollo/versione/frame/unità errati, campi mancanti, tipi
errati, `seq`/`monotonic_s`/`host_time_ns` invalidi, `valid` non booleano,
`state`/`source` sconosciuti, **coerenza `valid=true`/`state` non-tracking**
(`lost`/`error`/`ambiguous`/`calibrating` rifiutati, `tracking`+`valid=false`
tollerato), posizione nulla con `valid=true`, array corti, fuori bound,
quaternione non unitario, `reprojection_px`/`inliers` negativi, `note` non
stringa, chiavi duplicate, annidamento eccessivo, NUL, oversize, vuoto;
tolleranza chiave sconosciuta e pacchetto `valid=false` con posizione nulla o
stantia. Poi **allineamento frame** (il primo campione valido azzera la posizione
seduta; `world_rotation`/`cameraYawOffsetDeg` ruotano la traslazione, `Ry(90°)`
mappa +X su −Z; epoca incoerente invalida anche con rotazione), sequenza
(duplicati, riordino, gap, resync su stantio), `valid=false`/`calibrating`,
freschezza e recenter (azzeramento, epoca incoerente, pending) e `driver_pose`
(3DoF invariato, 6DoF valido, perdita e incoerenza ⇒ OutOfRange, NaN ⇒
OutOfRange).

**Live loopback** — bind su porta effimera, thread ricevitore reale, invio da
socket UDP: il primo frame valido fissa l'origine seduta (posizione zero), un
frame spostato viene inquadrato rispetto a quell'origine; duplicato osservato;
pacchetto `valid=false` che rende la posizione inutilizzabile; malformato,
oversize e fuori bound rifiutati; ripresa con un pacchetto valido nuovo, sempre
relativa all'origine seduta; stop entro un tempo limitato; riavvio del ricevitore
che prova il rilascio RAII del socket.

## 6. Build integrata

Il `CMakeLists.txt` di root integra i seguenti target e link:

1. `driver_psvrpc` deve linkare Winsock:

   ```cmake
   target_link_libraries(driver_psvrpc PRIVATE psvr_diag_core psvr_openvr hidapi::winapi psvr_display_inventory ws2_32)
   ```

2. Nuovo test sotto `if(BUILD_TESTING)` dentro il blocco `PSVR_BUILD_DRIVER`:

   ```cmake
   add_executable(psvr_position_tests tests/position_tests.cpp)
   target_include_directories(psvr_position_tests PRIVATE driver)
   target_link_libraries(psvr_position_tests PRIVATE psvr_diag_core psvr_openvr nlohmann_json::nlohmann_json ws2_32)
   if(MINGW)
       target_link_options(psvr_position_tests PRIVATE -static -static-libgcc -static-libstdc++)
   endif()
   add_test(NAME position_udp_contract COMMAND psvr_position_tests)
   ```

Nessun'altra modifica CMake è necessaria: `position_protocol.hpp` è header-only,
`orientation_math.hpp` arriva da `psvr_diag_core`, gli header OpenVR da
`psvr_openvr`, JSON da `nlohmann_json`.

## 7. Verifica eseguita e gate

Verifica manuale della sessione (toolchain w64devkit GCC 16.2.0, C++17,
`-Wall -Wextra -Wpedantic`):

- `driver/driver_psvrpc.cpp` compila senza warning con i flag del target;
- `tests/position_tests.cpp` compila senza warning ed esegue **PASS** su
  protocollo, frame, sequenza/freschezza, coerenza recenter, contratto di posa
  e ciclo live loopback;
- il contratto di posa esistente passa nella suite completa: **35/35 CTest**,
  incluso il nuovo test interoperabilità processo Python → C++.

**Gate M9 aperti**: acquisizione PSEye reale verificata, ma intrinseci/bersaglio
fisici non calibrati; nessuna cattura AITrack/
OpenTrack adattata, nessuna attivazione SteamVR, nessuna verifica di latenza,
scala, jitter o deriva posizione sul PSVR. Il default 3DoF non è modificato e
nessuna posizione viene inventata quando il flusso manca.

**Calibrazione fisica non risolta** (nessun valore inventato):

1. **Yaw camera↔IMU** (`positionCameraYawOffsetDeg`): il default `0` assume i
   due frame gravità-allineati con lo stesso yaw, ma il montaggio reale
   camera/IMU non è misurato. Va determinato con una prova fisica (spostamento
   noto lungo un asse e confronto con l'orientamento) prima di considerare
   allineate posizione e orientamento.
2. **Latenza e tempo del recenter**: origine camera e orientamento IMU sono
   campionati da thread diversi; la piccola differenza temporale al recenter
   (e la latenza del flusso posizione) non è misurata.
3. **Segni/assi del montaggio PSVR**: M3 resta aperto; l'allineamento
   posizione-orientamento non certifica i segni fisici di yaw/pitch/roll.

Applicare `world_rotation` alla traslazione rende il recenter **coerente** con
l'orientamento, ma non risolve lo yaw costante di montaggio camera/IMU: quello
resta una misura fisica da fare, non un valore da indovinare.
