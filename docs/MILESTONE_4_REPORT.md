# M4 — driver OpenVR HMD-only — software implementato, runtime pendente

Aggiornamento autonomo successivo: componente display opt-in M5 implementata,
mock Init/registrazione ampliati e suite corrente 24/24 CTest. Il resto del
report conserva la prima sessione M4 tracking-only. Vedere
[M5](MILESTONE_5_REPORT.md) e [coda manuale](MANUAL_VALIDATION_QUEUE.md).

2026-10-04. **M4 IN_PROGRESS**, non COMPLETE. L'utente ha rinviato le
prove fisiche e autorizzato l'implementazione indipendente; M3 mantiene i gate
residui recenter yaw/ripetizioni termiche. M5 non iniziata.

## Implementazione

- `driver/driver_psvrpc.cpp`: factory C esportata `HmdDriverFactory`, provider
  OpenVR `_004`, HMD `_005`, seriale stabile `PSVRPC-HMD-0`, un solo HMD.
- SDK Valve **2.15.6**, revisione `0924064316de3effbcd1acf1e309182a2deb1c05`.
  Solo header driver; nessuna dipendenza runtime `openvr_api.dll`.
- Thread dedicato apre/legge/chiude HID VID 054C, PID 09AF, interfaccia 4;
  rifiuta più sensori. Letture con timeout 10 ms, cleanup con stop/join.
  Nessun accesso a MI_05, comando power/mode o cambio binding.
- `DriverTracker` in `driver/pose_bridge.hpp` riusa decoder/fusion originali
  M3 e gate di calibrazione predefiniti: warm-up 200 ms, 8 s tavolo immobile,
  STD 0,75 dps, meanShift 0,05 dps. Il driver usa questi default C++,
  **non legge il `config.json` del diagnostico**.
- Pubblicazione in `RunFrame`: snapshot protetto da mutex breve, pose WXYZ,
  world/head identity, origine fissa zero (3DoF), drift yaw dichiarato,
  `poseTimeOffset` età host negativa in secondi. Nessuna prediction:
  velocità angolare/lineare e accelerazione pubblicate zero finché basis e
  latenza non sono verificate. Timestamp host dopo lettura: non misura
  la latenza sensore/USB.
- Invalidazione assenza, calibrazione, errori, NaN/non-unit, timestamp futuro,
  posa stale >100 ms; watchdog startup 3 s / pretracking 1 s. Duplicati e
  malformati non aggiornano la freschezza. Una lettura con tick continui dopo
  un ritardo host oltre soglia non può resuscitare una posa scaduta.
- DebugRequest `recenter` / `recalibrate` accodano richieste al reader;
  `driver_psvrpc.recenterMode` può essere `full` o `yaw`. Risposta troncata e
  terminata NUL. Richieste accodate non attestano l'applicazione: consultare log.
- Mutex Windows `Local\PSVR-PC-DeviceSession` condiviso con la nuova build
  del diagnostico: un solo proprietario cooperante per sessione Windows.
  Enumerazione resta possibile; acquisizioni e azioni controllo del diagnostico
  richiedono ownership. Versioni precedenti e software di terzi non usano il
  mutex: chiudere il monitor prima delle future prove driver.
- `GetComponent` non offre display/direct-mode/virtual-display. Proprietà
  corrispondenti false: output nel pannello non implementato in M4.
  Nessun controller, tracking posizionale, audio o driver watchdog.
- Perdita terminale richiede deactivation/reactivation (normalmente riavvio
  SteamVR); auto-reconnect non implementato. Standby passivo.

## Build e pacchetto

Default CMake `PSVR_BUILD_DRIVER=ON`, disattivabile con `OFF` per diagnostico
soltanto. Dipendenza SDK FetchContent fissata, senza compilare esempi upstream.
Override offline `FETCHCONTENT_SOURCE_DIR_OPENVR_SDK` deve indicare quel commit.
Toolchain verificata: MinGW x64 w64devkit; VS 2022/Windows 10 non verificati.

```powershell
 .\build.ps1 -Toolchain MinGW -ToolchainRoot "C:\Users\rickk\AppData\Local\Temp\opencode\w64devkit"
```

Il target `psvr_driver_package` esegue staging a ogni build, anche senza
relink: DLL, manifest, settings e licenze in `out/driver_psvrpc/psvrpc`.
La build non registra il driver. Settings default `enable=false`;
manifest `alwaysActivate=false`, USB presence `054C.09AF` nel formato SDK.
Non modificare gli settings default nello staging: vengono ricopiati alla build.

Nota B14-09 — flussi package: `driver_registration.ps1` registra di default `out/driver_psvrpc/psvrpc` (flusso SteamVR), `tools/psvr_suite/deploy.py` archivia di default `out/driver_psvrpc_6dof/psvrpc` (flusso backup/integrazione suite); default e path invariati.

Nella sessione configure/build/CTest eseguiti direttamente su `build/mingw`.
Il nuovo diagnostico compilato è `build/mingw/psvr_diag.exe`; la copia operativa
`out/diag/psvr_diag.exe` non è stata aggiornata mentre il monitor precedente
era in uso. Alla successiva chiusura del monitor eseguire la build completa
con installazione sopra per utilizzare anche il nuovo guard di ownership.

## Script per le future prove runtime

Percorsi espliciti per gestire installazioni Steam su dischi diversi:

```powershell
.\scripts\driver_registration.ps1 -Action Inspect -SteamVRPath "D:\SteamLibrary\steamapps\common\SteamVR"
.\scripts\driver_registration.ps1 -Action Install -SteamVRPath "D:\SteamLibrary\steamapps\common\SteamVR" -DryRun
.\scripts\driver_registration.ps1 -Action Install -SteamVRPath "D:\SteamLibrary\steamapps\common\SteamVR"
.\scripts\driver_registration.ps1 -Action Remove -SteamVRPath "D:\SteamLibrary\steamapps\common\SteamVR"
.\scripts\inspect_steamvr_logs.ps1 -SteamPath "C:\Program Files (x86)\Steam" -Tail 1000
```

Install/Remove: SteamVR chiuso, `vrpathreg.exe` ufficiale, path file OpenVR
esistente, controllo idempotente di `external_drivers`, exit code e rilettura
di verifica. Script non sovrascrive settings SteamVR né disabilita altri driver.
Inspect/DryRun non cambiano registrazione. Log inspector filtra `psvrpc`,
errori/fallimenti in `vrserver.txt` e `vrcompositor.txt`, segnalando file assenti.

Per attivazione futura aggiungere manualmente la sola sezione
`"driver_psvrpc": {"enable": true, "recenterMode": "full"}` agli override
utente SteamVR, preservando il resto e facendo prima backup. Riavviare il runtime
e calibrare su tavolo. Questa configurazione/registrazione **non è stata eseguita**.

## Verifiche software eseguite

**Build OK, 18/18 CTest PASS**, inclusi i 71 test Python monitor e 20 casi
PowerShell su fixture isolate. Nessun test richiede USB o avvia SteamVR.

- Contratto pose: assenza/calibrazione/lost/stale, future time, quaternion
  non finiti/non-unit, WXYZ, tempo negativo, origine fissa e flag 3DoF.
- Pacchetti sintetici → calibrazione → posa; duplicati/malformati non
  aggiornano età; timeout startup/pretracking; ricalibrazione cancella vecchia
  posa; report buffered dopo scadenza mantiene invalidazione.
- Ownership mutex: secondo thread rifiutato, nuovo owner ammesso dopo release.
- DLL caricata Windows x64; factory valida/null/sconosciuta, interfacce,
  Init senza contesto/interfacce richieste e cleanup ripetuto senza hardware.
- JSON manifest/default, licenze presenti, factory caricabile nello staging.
- Script: parsing PowerShell 5.1, log filter/tail/missing, argomenti, path,
  idempotenza e DryRun con `LOCALAPPDATA` fixture, senza eseguire vrpathreg reale.
- Revisione indipendente con subagente: corretti USB-presence format,
  staging incrementale e watchdog prima di accettazione pacchetti buffered.

## Gate rinviati

La compilazione e i mock non provano successo di Init/Activate con il runtime
reale. Restano riconoscimento HMD/pose in SteamVR, lifecycle successful
Activate/Deactivate, DebugRequest, stop/perdita USB da driver, ownership tra
processi nuovi, ABI effettiva nel runtime, registrazione/rimozione reali.
Registrare versione SteamVR/GPU e log. M4 diventa completa solo dopo quei gate.
Display, stereo, refresh e ottica richiedono M5/M6 e prove fisiche separate.
