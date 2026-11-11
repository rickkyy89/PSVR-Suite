# Build MSVC e prova runtime — 2026-10-06

La DLL descritta in questa prova lunga è stata conservata come baseline.
Una build successiva aggiunge [diagnostica alla perdita IMU](IMU_DRIVER_DIAGNOSTICS_20261006.md),
con hash distinto; il package MSVC registrato contiene ora quella nuova build.

## Esito

**30 minuti non presidiati con presenza sintetica: nessun −202 osservato.**
Non è ancora l'accettazione fisica di 30 minuti nelle lenti. L'utente era fuori
casa; dashboard, movimenti, qualità ottica e accensione del pannello non sono
stati verificati direttamente. L'ABI MinGW non è dimostrata come causa del −202.

## Build e artefatti

Installati con autorizzazione Build Tools 2022 17.14.41, MSVC v143
19.44.35229 e SDK Windows 10.0.26100.0, da installer Microsoft firmato valido.
Configurazione x64 `RelWithDebInfo`, runtime CRT statico.

- Driver: `out/driver_psvrpc_msvc/psvrpc/bin/win64/driver_psvrpc.dll`.
- SHA256 della DLL finale:
  `12A7724C9A6DF72DB9B9ABFA277BED8DAAECF4B25F565FA6AA5B8025FEE2E8E7`.
- Export verificato `HmdDriverFactory`, PE x64; dipendenze importate solo
  `WS2_32.dll`, `USER32.dll`, `KERNEL32.dll`.
- Log runtime: `build compiler=MSVC version=1944 display_abi=native`.
- Il ponte `WindowsDisplayAbi` è escluso dal driver MSVC tramite preprocessore;
  MinGW continua a usarlo.
- Suite iniziale 47/48: il test della matrice EDID assumeva `applied=true`
  nel log reale. Corretto il test per rispettare il valore dell'evidenza;
  il test fallito rieseguito è passato. Non modificati generatori né EDID.
- Dopo aggiunta delle richieste diagnostiche, i quattro test mirati di DLL,
  package, mock runtime e CLI probe sono passati.
- Verifica finale: tutti i **48 CTest** passati. `driver_pose_contract` richiede
  il mutex di ownership libero: rieseguito con esito positivo dopo chiusura
  normale di SteamVR, che nella prima esecuzione della suite lo occupava.

SteamVR era chiuso al cambio registrazione. Rimossa la registrazione del
pacchetto MinGW e registrato quello MSVC; il vecchio pacchetto rimane disponibile.
Il log conferma il caricamento della DLL dal percorso MSVC.

## Baseline hardware/runtime

Utente: collegamento HDMI nativo ritenuto attivo, senza riscontro fisico in loco.
Windows: Sony SNY6A04, UID4352, GPU NVIDIA RTX 5070 Ti, sorgente e scanout
1920×1080, origine (1920,492), path e signal **90/1 Hz**.
Settings del driver coerenti, `nativeDirectMode` non abilitato, distorsione
identity. Sul target interrogato nessun valore EDID_OVERRIDE restituito.
Mantenuto `power.pauseCompositorOnStandby=false`, già presente.
Nessun cambio di timing o EDID nella prova.

## Presenza sintetica

Richiesta dell'utente: simulare la presenza nel visore durante l'assenza.
Implementato `--request simulate-worn` nel probe: il driver applica in RunFrame
il booleano riservato OpenVR `/proximity=true` e abilita temporaneamente
`Prop_ContainsProximitySensor_Bool`. Lease **180 secondi**, rinnovabile;
`--request simulate-unworn` lo termina prima. Alla scadenza/deactivation
pubblica false e ripristina il valore precedente della proprietà.
Non modifica pose IMU o settings persistenti.

API documentata negli header/docs dell'SDK fissato: Driver API,
“Reserved input sources”, `/proximity`. L'applicazione è attestata nei log del
driver, non dalla sola risposta “queued”. Il primo test ne verifica anche la
scadenza alle 09:50:07 e lo standby alle 09:50:13 (orari locali).
Questo input rappresenta la presenza **per SteamVR**, non un comando al sensore
fisico Sony né una prova che il pannello sia illuminato.

## Raccolte e risultati

| Raccolta in `logs/` | Esito |
|---|---|
| `presentation_20261006_094103` | Prima DLL MSVC: 29 campioni, 10/10 pose valide ciascuno, fullscreen/render true. |
| `presentation_20261006_094708` | Presenza sintetica 180s + scadenza: 30 campioni, 10/10 pose valide ciascuno, fullscreen/render true. |
| `presentation_20261006_095121` | Tentativo lungo fermato dopo 488s al rinnovo del probe con pose invalide: 76 campioni, fullscreen true in tutti; 59 campioni 10/10 validi, uno 1/10, 16 zero validi. |
| `presentation_20261006_100025` | Conferma del tracking perso, senza fingere pose valide. |
| `presentation_20261006_100149` | Dopo chiusura normale e riavvio SteamVR: cattura IMU nuovamente valida. |
| `presentation_20261006_100222` | Prova completa 1804s: 282 campioni, **2820/2820 pose valide**, fullscreen/render true in tutti. |

Nel tentativo interrotto, vrserver riporta alle **09:57:40.620**
`IMU state=tracking-lost` e `capture invalid; deactivate/reactivate to restart`.
La causa fisica/USB non è dimostrata. Non è stato osservato un cambio della
topologia o un −202 concomitante. Il collector ora conserva anche le pose
invalide senza interrompere la raccolta quando la presenza è comunque accodata.

Prova finale: 10:02:22–10:32:26 locali, con 15 rinnovi della presenza a circa
120s. Tutti i campioni riportano Sony con gli stessi bounds e refresh path/signal
90/1. Nessun −202, `DXGI_ERROR`, `SetFullscreen` o `[Error]` trovato nel log
compositor raccolto. Alle **10:32:24.472** il driver conferma il rilascio della
presenza e ripristino della proprietà.

Sono snapshot a circa 6.3s (probe 1s più pausa 5s), non osservazione continua
dei frame o intercettazione di ogni cambio focus. I log completi prima/dopo
sono conservati; HRESULT DXGI disponibili solo se registrati da SteamVR.
L'inspector delle finestre contiene un vecchio campo di riferimento (3840,492):
il confronto corretto è con l'inventario **live**, qui (1920,492), non quel campo.

## Ripetizione

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -Toolchain VS2022
# SteamVR già avviato, Sony e settings coerenti:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\capture_presentation_trial.ps1 -DurationSeconds 1800 -IntervalSeconds 5 -SimulateWorn
```

Senza `-SimulateWorn`, il collector è read-only. Con lo switch invia una richiesta
temporanea ogni 120s e la rilascia in `finally`. Anche se il collector termina
inaspettatamente, il lease del driver scade. La DLL registrata resta MSVC;
nessun rinnovo della presenza continua dopo il termine della raccolta.
SteamVR è stato infine chiuso normalmente; la registrazione MSVC è conservata
per la successiva prova fisica.

## Prossimo gate

Al rientro: almeno 30 minuti nelle lenti, dashboard, movimenti, standby e ritorno,
seguiti da più riavvii SteamVR. Verificare anche l'episodio IMU perso se ricorre.
Se torna −202 con MSVC, usare questa raccolta per correlare fullscreen, finestra,
foreground e topologia; poi passare al presenter D3D11 `IVRVirtualDisplay`.
Il risultato corrente non giustifica ancora il cambio di architettura.
