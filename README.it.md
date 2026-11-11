# PSVR Suite (PSVR-PC) — specchio italiano

Copyright (c) 2026 rickkyy. Licenza MIT — vedi [LICENSE](LICENSE).

**Versione:** `0.3.0-alpha` (pre-release, **non stabile**).

> Il testo canonico è [README.md](README.md) in inglese; in caso di
> divergenza vince quello. I report storici restano in lingua originale
> e sono linkati dal README inglese.

## In breve

- **Lingua interfaccia:** selettore **Lingua / Language → Italiano / English**
  in alto nella suite; cambio immediato e persistente, senza riavviare camera
  o SteamVR e senza perdere i valori non ancora salvati.
- Suite open-source per **PSVR1 su Windows 10/11 x64**, con percorso
  6DoF opzionale via camera **PS3 Eye**.
- **PS4 Camera:** richiede adattatore AUX→USB 3 e preparazione firmware UVC
  esterna (non installata dalla Suite). Il pannello separato nella scheda Camera
  serve per acquisizione stereo e calibrazione; il tracking 6DoF usa invece la
  sorgente **PS4 Camera stereo** e il relativo profilo nella Suite. Il 6DoF
  statico è stato verificato sperimentalmente il 2026-11-01 (629 pose valide su
  954 frame), con dropout presenti; movimento e robustezza non sono certificati.
  Vedi [prerequisiti e guida PS4](docs/PS4_CAMERA.md).
- Diagnostica USB/HID verificata su hardware reale; tracking IMU 3DoF +
  GUI, driver OpenVR HMD-only (disabilitato di default), componente
  display opt-in, validatore ottico, bridge di posizione UDP/IMU/OpenVR.
- **Gioco reale:** MSFS 2024 con 2 sessioni VR vere il 2026-10-14
  (prima degradata con fallback pesante, seconda molto migliore con
  fallback disattivato e distorsione identity); feedback Assetto Corsa
  (oggetti stretti-alti) alla base del trial di proiezione. Nessuna
  garanzia di 90 fps nativi (riproiezione ~31% in entrambe le sessioni).
- **Scelte accettate (non ripristinare in automatico):**
  distorsione `identity` (peripheral/stronger rifiutati visivamente),
  filtro One Euro `2,5 Hz / beta 8` di default (Kalman non accettato in
  movimento), fallback 3DoF opzionale con default `false`, FOV
  diagnostico personale `H 78,267 / V 84,939` (non calibrazione
  universale), `-202` da resize desktop riproducibile con recupero
  solo-compositor non definitivo.
- **Requisiti:** Windows x64, PSVR1, PS3 Eye (solo 6DoF), WinUSB su MI_05
  come passo manuale separato con Zadig (non incluso). Build VS2022
  preferita, avvio da Start con "PSVR Suite" oppure `run_suite.ps1`
  diretto. Dettagli: [docs/QUICKSTART.md](docs/QUICKSTART.md).
- **Selezione lingua web e PSVR Suite**
  - Il selettore **Lingua / Language** cambia tra **Italiano** e **English**
    immediatamente; la scelta persiste senza riavviare la telecamera o SteamVR.
    L'italiano è la lingua iniziale. Le preferenze del driver e l'interfaccia
    utente sono memorizzate separatamente e non fanno parte delle rilasci
    di origine.
  - **PSVR Suite launcher** (`tools/psvr_suite/install_psvr_suite_shortcut.ps1`)
    installa il collegamento PSVR Suite dalla root del repo; eseguire da la
    root con `powershell -NoProfile -ExecutionPolicy Bypass -File .\run_suite.ps1`
    per avviare direttamente la suite. I flag `-StartVR` / `-Attach` / `-Check`
    sono documentati in `run_suite.ps1`.
  - **PS4 camera stereo acquisition** è disponibile dalla scheda Camera
    (configurazione UVC esterna richiesta; firmware UVC non installato dalla Suite).
    Il pannello separato nella scheda Camera serve per l'acquisizione stereo e
    la calibrazione; il tracking 6DoF usa invece la sorgente **PS4 Camera stereo**
    e il profilo corrispondente nella Suite. Il 6DoF statico è stato verificato
    sperimentalmente il 2026-11-01 (629 pose valide su 954 frame), con dropout
    presenti; il movimento e la robustezza non sono certificati.
    Vedi [docs/PS4_CAMERA.md](docs/PS4_CAMERA.md).
  - **Licenza/donazioni:** MIT, funzioni gratis per tutti; donazioni
    volontarie via PayPal ([paypal.me/rickkyy89](https://paypal.me/rickkyy89), vedi
    [docs/DONATIONS.md](docs/DONATIONS.md)).
  - **Verifica release:** build e 63/63 CTest superati anche dall'archivio
    estratto in una cartella pulita; verifiche e pacchettizzazione in
    [docs/RELEASE_VERIFICATION.md](docs/RELEASE_VERIFICATION.md).
  - **Release:** primo artefatto solo sorgenti
    (`dist/psvr-suite-0.3.0-alpha-source.zip`); niente ridistribuzione di
    binari runtime finché gli obblighi GPL/LGPL del backend
    non sono risolti. Vedi [docs/RELEASE.md](docs/RELEASE.md),
    [CHANGELOG.md](CHANGELOG.md).

## Aggiungere la propria webcam via agente

La suite permette di aggiungere una webcam personale registrando un nuovo
backend e profilo tramite l'agente. L'agente aggiunge il backend della
telecamera in `tools/psvr_camera/camera_backend.py` e
`tools/psvr_camera/camera_source.py`, registra il profilo sotto
`configurations/` ed espone i controlli in `tools/psvr_suite/cameras.py`
e la directory `camera_settings/`.

Esempio di prompt per l'agente (vincoli solo su questi file, niente
credenziali, nessun push a GitHub remoto):

    Aggiungi un nuovo profilo webcam per una telecamera USB/UVC locale.
    Registra il backend in `tools/psvr_camera/camera_backend.py` e
    `tools/psvr_camera/camera_source.py`, crea un profilo JSON sotto
    `configurations/` ed espone i controlli di cattura/calibrazione in
    `tools/psvr_suite/cameras.py` e `camera_settings/`. Esegui il
    controllo della suite con `pytest tests/test_camera_capture.py
    tests/test_suite_camera_source.py` e verifica la preview della tab
    della telecamera e la routine di calibrazione da `cli.py`.

Passaggi di verifica: esegui il flag `-Check` della suite, visualizza la
preview della tab della telecamera e esegui la calibrazione da
`cli.py`. Il profilo è locale; `psvr_suite_user.json` non viene
committato su GitHub.
