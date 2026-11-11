# PSVR-PC — architettura e analisi (milestone 0)

Analisi del codice effettuata il 2026-10-03. Target: PSVR1 per PS4, Windows
10/11 x64, uso seduto. Questo documento distingue evidenza dal sorgente,
decisioni progettuali e compatibilità ancora da verificare sul runtime reale.

## 1. Sorgenti e revisioni esaminate

| Progetto | Revisione analizzata | Ruolo |
|---|---|---|
| [mSparks43/PSVR-SteamVR-openHMD](https://github.com/mSparks43/PSVR-SteamVR-openHMD) | `3eb8a4d4a725698843976448e1966ed99fbf900a` | bridge OpenHMD/OpenVR, distortion, esperimenti 6DoF |
| [alatnet/OpenPSVR](https://github.com/alatnet/OpenPSVR) | `cb86fd7a2fc7cb47733074bb49856b3af3095357` | lifecycle Windows, display, controllo |
| [OpenHMD/OpenHMD](https://github.com/OpenHMD/OpenHMD) | `85075b0c7e3c723ded2577edb79d00ee11aac339` | driver PSVR e fusion 3DoF di riferimento |
| [ValveSoftware/openvr](https://github.com/ValveSoftware/openvr) | `0924064316de3effbcd1acf1e309182a2deb1c05`, SDK 2.15.6 | API driver attuale e documentazione ufficiale |
| [alatnet/libpsvr](https://github.com/alatnet/libpsvr) | `7858e9cffcd17da5c9e26b301d9d4442a1e6de93` | dipendenza effettiva di OpenPSVR; protocollo/libusb |
| [libusb/hidapi](https://github.com/libusb/hidapi) | `d6b2a974608dec3b76fb1e36c189f22b9cf3650c`, 0.15.0 | backend HID Windows del diagnostico |

Le copie di studio sono esterne alla repo, nella cartella temporanea OpenCode;
i link permanenti e le revisioni rendono ripetibile la consultazione.
Non sono stati eseguiti i binari pubblicati nelle repo.

## 2. Architettura scelta

```text
PSVR1 / processor unit
  USB ── Windows HID ── psvr transport ── packet decoder ── orientation fusion
                                                     └── psvr_diag (indipendente)
  HDMI ── GPU ── display discovery/EDID
                         │
                driver_psvrpc.dll (futuro)
                         │ OpenVR driver API
                       SteamVR
                  ┌──────┴─────────┐
              Assetto Corsa     player VR
                         │ SteamVR OpenXR runtime (futuro)
                        MSFS
```

Nuovo progetto, C++17, CMake, dipendenze fissate. Trasporto e decodifica sono
condivisibili tra diagnostico e futuro driver; nessuna dipendenza SteamVR per M1–M3.
Il backend iniziale usa HIDAPI/WinAPI, senza libusb e senza sostituire i driver HID.
OpenHMD è riferimento e possibile backend di orientamento: non viene usato
direttamente in M1 perché `ohmd_list_open_device` accende il display e attiva VR mode.
In M3 scegliere un sottoinsieme PSVR-only upstream con lifecycle separato, oppure
estrarre la fusion Boost in un modulo piccolo. Nessuna dipendenza da un ramo mobile.

Componenti futuri: `transport`, `protocol`, `orientation`, `display_windows`,
`steamvr_hmd`, `position_udp`. Un solo proprietario delle interfacce alla volta:
non eseguire diagnostico e driver insieme durante prove successive.

## 3. Confronto del codice

### mSparks43

Struttura: `driver_openhmd.cpp` (1119 righe), `driverlog.*`, `ohmd_config.h`,
`aitrack_udp.*`, `joystick.*`, `subprojects/openhmd`, header OpenVR incorporato,
manifest, risorse, CMake/Meson. Molti cache CMake, oggetti e binari Linux sono
versionati. CMake prevede `bin/win64`; a questa revisione `git ls-files bin/*`
contiene solo il `.so` Linux: la struttura Windows non prova una build Windows attuale.

Riutilizzabile come disegno:
- factory/provider/HMD, `TrackedDeviceAdded`, property container, `GetComponent`;
- pubblicazione quaternion OpenHMD XYZW come quaternion OpenVR WXYZ;
- viewport sinistro/destra metà pannello;
- trasformazione UV radial-polynomial/RGB di `ComputeDistortion` (righe 764–859).

Da sostituire/correggere:
- `m_nWindowX=1920`, refresh fissato a 60, FOV e altezza arbitrari;
- `Prop_HasDriverDirectModeComponent_Bool=true` senza implementare/esporre
  `IVRDriverDirectModeComponent`: non abilita magicamente Direct Mode;
- pose sempre valide/connesse anche senza un campione fresco; nessuna validazione
  degli errori `ohmd_device_getf`; dereferenziazioni dopo open fallito;
- update IMU dentro `RunFrame` invece di un thread dedicato; cleanup incompleto;
- watchdog Linux senza pausa attiva un loop stretto; sample Windows sulla lettera Y;
- CMake aggiunge socket POSIX e joystick Linux incondizionatamente; non è Windows-ready;
- Meson non include `aitrack_udp.cpp`, manca inoltre l'include OpenVR esplicito;
- output DLL e copia dipendenze Windows da ricostruire, non importare cache build;
- file `steamvr.vrsettings` disabilita driver altrui: non usarlo.

Commit studiati: `ec963d2` (distortion/Monado, 2021), `9eb2a53` (AITrack),
`fd28955` (6DoF initial), `025c4f1`, `d33ed19`, `3eb8a4d` (tuning, 2024).
Il codice recente rimane sperimentale, non una validazione hardware riproducibile.

### OpenPSVR e libpsvr

`ServerDriver_OpenPSVR.cpp` apre libpsvr e registra un solo HMD;
`OpenPSVRDeviceDriver.cpp` richiede device-info nel costruttore, accende il visore
e invia mode in `Activate`, legge sensori su thread, spegne in `Deactivate`.
`FindHMDMonitor` cerca `SNYB403` con `EnumDisplayDevices` e trova il rettangolo
tramite `EnumDisplayMonitors`. È un riferimento utile, da adattare a Unicode,
QueryDisplayConfig, hotplug e validazione EDID senza presumere il layout del desktop.

Limiti concreti:
- header OpenVR 1.0.13 scaricato; libusb 1.0.22, GLM 0.9.8.5 e vecchi URL AppVeyor/S3;
- `set(CMAKE_CXX_STANDARD_17)` e `set(CMAKE_CXX_STANDARD_REQUIRED_ON)` non impostano
  correttamente le variabili CMake; flag globali C++11 contraddicono l'intento;
- `ComputeDistortion` restituisce UV identiche, `GetProjectionRaw` usa ±1:
  non è una calibrazione ottica PSVR;
- `poseTimeOffset` viene impostato in millisecondi e non come età negativa del
  campione in secondi: incompatibile con il contratto OpenVR;
- mutex attorno a lettura bloccante; stop flag non atomico; `join` non protetto;
  nessuna gestione affidabile disconnessione/letture corte;
- seriale casuale dopo timeout device-info; stato/calibrazione non robusti;
- `BMI055Integrator` calibra 2000 campioni, stabilizza la fusion, supporta recenter;
  rollover timestamp usa `0xFFFFFF` anziché modulo `0x1000000` e conversione
  quaternion→Euler→quaternion è superflua.

La reale libreria `alatnet/libpsvr/src/psvr.c` apre libusb VID/PID, claims 4/5,
usa `libusb_bulk_transfer` per invio/lettura e timeout 0 per lettura sensori.
Questo backend richiede driver Windows compatibile libusb (WinUSB/libusbK)
per le interfacce interessate; non è lo stesso percorso del backend HIDAPI.
Non importiamo questa dipendenza in M1. `ReportID.h` documenta 0x17 power,
0x23 mode; diversi altri comandi sono esplicitamente "unknown" e restano esclusi.

### OpenHMD upstream PSVR

File letti: `src/drv_psvr/psvr.c`, `packet.c`, `psvr.h`, build CMake e API.
- VID `054C`, PID `09AF`; sensori interfaccia **4**, controllo **5**.
- enumerazione HID filtrata per interface_number, apertura per path, non per primo VID/PID;
- report sensore **64 byte**, due campioni: timestamp LE32 a 16/32,
  gyro int16 a 20/36, accelerometro int16 a 26/42, sequenza byte 63;
- timestamp effettivo trattato a 24 bit, tick 1 µs, periodo nominale campione
  500 µs, quindi circa 2000 campioni/s e 1000 report/s. È un'ipotesi dal codice
  da misurare, non refresh video;
- mapping verso X/Y/Z: gyro `(raw[1], raw[0], -raw[2])*0.00105 rad/s`;
  accel mapping equivalente, fattore `9.81/16384 m/s²`;
- quaternion normalizzato, gravity correction per tilt; magnetometro assente:
  yaw assoluto non osservabile, drift da calibrare/recenter;
- power-on `{17,76,aa,04,01,00,00,00}` e VR `{23,00,aa,04,01,00,00,00}`
  su open; cinema su close. Scritture ammesse solo in M2 esplicita;
- decoder controlla lunghezza, ma il chiamante upstream continua dopo decode
  fallito: la nostra decodifica rifiuta il pacchetto e non aggiorna la pose.

Upstream oggi si dichiara **UNMAINTAINED**. CMake legacy 3.1/FindHIDAPI va adattato
ai target importati moderni e a CMake 4 se verrà integrato. Abilitare solo PSVR,
disabilitare altri driver/esempi; fissare revisione e applicare patch piccole.

## 4. SteamVR/OpenVR attuale

Letti `headers/openvr_driver.h`, `docs/Driver_API_Documentation.md`, confrontato
l'header incorporato mSparks. Provider `_004`, device `_005` e server host `_006`
restano presenti; `VR_INIT_SERVER_DRIVER_CONTEXT`, factory, property helpers,
`TrackedDeviceAdded`, `TrackedDevicePoseUpdated` e `IVRDriverLog` sono utilizzabili.
La documentazione Valve promette retrocompatibilità: un driver vecchio non è
automaticamente inutilizzabile, ma ciò non prova che il display PSVR funzioni.

Differenze/deprecazioni concrete:
- display mSparks `_002` vs attuale **`IVRDisplayComponent_003`**: aggiunto
  pure virtual `ComputeInverseDistortion`; non basta sostituire l'header;
- `ITrackedDeviceServerDriver::GetPose` va implementato, ma è deprecato/unused
  dal runtime: la pose va inviata esplicitamente con `TrackedDevicePoseUpdated`;
- watchdog deprecato nella documentazione ufficiale: nessun nuovo watchdog;
- vecchi metodi controller/sample non necessari; nessun controller in questo progetto;
- `poseTimeOffset` in secondi relativo al momento di invio, tipicamente negativo
  per un campione passato; coordinate metri/radianti, +X destra +Y alto −Z avanti;
- i flag Direct Mode e debug non costituiscono un'implementazione display.

Driver futuro: nome coerente `psvrpc`, package `out/driver_psvrpc/psvrpc/`,
`driver.vrdrivermanifest`, `bin/win64/driver_psvrpc.dll`, `resources/settings`.
Registrazione `vrpathreg adddriver/removedriver`,
script idempotenti, verifica exit code e backup prima di eventuali settings.
Non sovrascrivere `steamvr.vrsettings` e non disabilitare driver globalmente.
M4 registra HMD/pose; M5 dimostra che il compositor presenta sul display giusto.

## 5. Display, refresh e ottica

1920×1080 pannello totale, 960×1080 ciascun viewport; render target per occhio
è separato e può essere più grande per oversampling/distortion. EDID legacy
`SNYB403` (`0xD94D`, `0xB403`) è indizio, non selezione automatica universale.
Enumerare EDID, GPU adapter, origine desktop, orientamento e modi disponibili.
Prima prova extended desktop; Valve raccomanda Direct Mode ma il suo supporto
effettivo su Windows/GPU/SteamVR moderno è un rischio da risolvere in M5.
Nessun EDID override/custom refresh nella prima sessione.

Il pannello PSVR è noto per 90/120 Hz; il fork impone 60, OpenPSVR configura 120.
Nessuno dei valori dimostra il modo HDMI effettivamente negoziato sul PC.
Rilevare il modo attivo e la lista supportata, poi misurare frametime/vsync.
Separare frequenze report USB, campioni IMU, publishing pose e scanout.

Upstream: hsize 0.126 m, vsize 0.071 m, lens_sep 0.0630999878 m,
lens_vpos 0.0394899882 m, FOV 103.57° con TODO di conferma;
`OHMD_DISTORTION_K` restituisce zero, nessuna calibrazione PSVR universale
dedicata nel suo open. mSparks imposta `[0.20,0.09,-0.06,0.77]`,
hsize/vsize 0.13/0.07, lens_sep 0.065, lens_vpos 0.035, FOV 92°.
Sono **candidati non equivalenti né validati**, non default da dichiarare corretti.

M6: modulo distortion puro, polinomio radiale `d+c*r+b*r²+a*r³`, centri per
occhio e fattori RGB, verso UV verificato con API SteamVR; test centro/bordi,
monotonicità/inversa e pattern griglia/cerchi/etichette L/R. IPD utente non è
la distanza fissa tra centri delle lenti. Non avanzare con scala/geometria errata.

## 6. AITrack e 6DoF: analisi e sostituzione

`aitrack_udp.cpp` ascolta loopback UDP **4242**, legge memoria come double nativi,
prende solo XYZ, azzera sul primo pacchetto. `getPose` converte con guadagni
empirici: X negativo /90, Y /140 +1.3, Z negativo /140. Non sono unità SI certificate.
Difetti: header POSIX su Windows, `socklen_t len` non inizializzato, nessun
controllo lunghezza/recv error/NaN, `hadData` e `run` in data race, nessun timeout
o timestamp, thread indicizzato `[0]` anche se non creato, shutdown bloccabile,
bind fallito perde socket, `finish` vuoto, niente recenter successivo/perdita tracking.
Il fork attiva AITrack incondizionatamente nel costruttore HMD.

Gli altri commit 6DoF modificano `fusion.c` con doppia integrazione accelerazione,
smorzamento/clamp e `Simulation.cpp` Kalman derivato da conversioni geografiche.
`world_pos.x` viene incrementato sia con velocità X sia Y, manca il fattore dt
nell'integrazione posizione; `psvr.c` salva il tick precedente DOPO il decode,
rendendo il primo delta zero; correzione yaw proporzionale al tick non calibrata.
Non usare questo ramo come tracking posizionale affidabile.

M9: nuovo receiver Winsock, formato OpenTrack/AITrack **verificato con capture**
(tipicamente sei double 48 byte, posizioni spesso cm: confermare il sender),
loopback e porta configurabili, lunghezza/finitezza/endian/unità controllate,
tempo monotonic locale, timeout e recovery, RAII/atomiche/mutex brevi.
Rotazione da IMU, posizione da webcam convertita in metri nello stesso frame;
recenter comune, gain/smoothing time-based/deadzone minima, freeze/fade
controllato su perdita e stato esplicito. Misurare lag prima di prediction.

## 7. Dipendenze, licenze e toolchain

M1: Windows HID nativo tramite HIDAPI **0.15.0**, JSON nlohmann **3.12.0**,
SetupAPI/CfgMgr32 Windows, C++17. Librerie statiche, eseguibile standalone;
FetchContent con commit esatti; override locale per build offline.
Toolchain preferita: VS 2022 Desktop development with C++, MSVC v143,
SDK Windows 10/11, CMake ≥3.24, Git. Ninja facoltativo.
Per questa macchina priva di VS rilevato: w64devkit x64 2.10.0 portabile
(GCC 16.2.0, CMake 4.4.3, Ninja), archivio SHA-256 verificato, cartella temporanea.
Le due toolchain usano build directory distinte; non mescolare ABI di DLL C++.

OpenHMD BSL-1.0, OpenPSVR AGPL-3.0, libpsvr LGPL-3.0-or-later;
mSparks non ha LICENSE globale, `Simulation.cpp` dichiara LGPLv3 e altri file
hanno intestazioni Valve/Boost. Non attribuire una sola licenza all'intero fork.
M1 usa protocollo/decoder adattato da OpenHMD con avvisi Boost preservati,
HIDAPI sotto BSD-3-Clause (una delle licenze offerte), JSON MIT. Non copia codice
OpenPSVR/mSparks; per eventuale importazione futura valutare file per file.
I sorgenti originali nuovi sono MIT; vedere `third_party/README.md`.

## 8. Debug e criteri di avanzamento

Ogni esecuzione scrive log timestampato in `logs/`, stesso contenuto in console.
Enumerazione PnP USB completa, service/problem code delle interfacce PSVR,
path HID/VID/PID/release/product/serial/usage, risultati open separati e letture
con timeout. Solo campioni raw limitati: niente megabyte di log a ogni frame.
Errore reale prima di patch; massimo tre tentativi della stessa strategia.

PnP presente ≠ HID accessibile ≠ report valido ≠ tracking corretto ≠ display valido.
Nel PC iniziale sono presenti MI_04/MI_05 HID; audio 3D/H.264/BulkIn hanno
stato errore. Non sono prerequisiti IMU: riportarli senza installare driver casuali.
M1 richiede almeno un report sensore 64 byte e apertura controllo; una lettura
controllo in timeout senza comando è lecita solo se esistono input report.
La prova reale ha mostrato controllo input=0/output=0/feature=5: leggere le
capacità Windows e saltare l'input, senza confonderlo con un errore USB.
Prima di M2 verificare se il backend HID può inviare il comando documentato o
se occorre backend libusb/WinUSB selettivo. L'apertura non prova il protocollo power.
Assenza device deve terminare ordinatamente; scollegamento va provato fisicamente.

Futuro logging: transport error, packet invalid, sequence gap, timestamp rollover,
Hz/jitter, calibrazione/drift; SteamVR init/factory/registration/pose/display,
invalidazione timeout e shutdown. M4 parser PowerShell di `vrserver.txt`,
`vrcompositor.txt` con percorso Steam reale; conservare versione SteamVR/GPU.
Log e report di ogni milestone, test ripetibili e commit dedicato; nessun successo
hardware dedotto solo da build/fixture software.

## 9. Milestone e gate

| M | Risultato richiesto prima di avanzare |
|---|---|
| 0 | Analisi sorgenti, architettura/licenze/toolchain documentate e commit |
| 1 | psvr_diag x64, USB/HID 4/5, report 64 byte, errori/assenza gestiti, log reale |
| 2 | Comandi espliciti power on/off e VR/cinema già documentati, verifica stato e errori |
| 3 | IMU/fusion 3DoF, assi, quaternion, recenter, drift/rumore/Hz misurati |
| 4 | Driver HMD-only con API corrente, install/remove script, HMD e pose riconosciuti |
| 5 | Output compositor nel PSVR, L/R, modo/refresh/risoluzione reali |
| 6 | Ottica accettata tramite pattern e test matematici |
| 7 | Player esistente: 180/360 mono/stereo SBS/TB e flusso YouTube verificato |
| 8 | Assetto Corsa modalità OpenVR, scala/recenter/latency/frametime documentati |
| 9 | Posizione webcam + IMU, uso seduto, perdita tracking e lag verificati |
| 10 | MSFS con SteamVR OpenXR runtime attivo, prestazioni e recenter verificati |

Whirligig/GizmoVR sono candidati da verificare in M7, non dichiarati già
compatibili oggi. Per YouTube valutare yt-dlp→URL/file→player tenendo conto di
scadenza URL, stream separati audio/video e proiezione (equirettangolare vs EAC).
Per MSFS documentare versione 2020/2024 e selezionare SteamVR come runtime OpenXR.

Monado resta possibile evoluzione OpenXR e riferimento matematico: l'accesso
web al sorgente upstream in questa sessione è bloccato da challenge Anubis;
non è stata verificata né promessa la compatibilità Monado Windows/MSFS.
Nessun runtime Monado introdotto in M1. Nessun PS Move/Camera/room-scale.

**Fine sessione: fermarsi dopo M1 anche se il dispositivo è già presente.**

## 10. Evoluzione verificata in M2 (sessione successiva autorizzata)

Controllo Windows risolto con binding misto: **MI_04 HID/HIDAPI, MI_05
WinUSB/API nativa**. Cambio manuale autorizzato via Zadig; inventario/ripristino
in `docs/WINUSB_CONTROL.md`. Default diagnostico passivo; scritture esplicite
solo power 0x17 e mode 0x23, header gp_id=0 come libpsvr.

Descriptor reali: controllo class HID 3, alternate 0, interrupt IN 0x84 e
OUT 0x04, max packet 64. WinUSB accede ai pipe USB anche se il descriptor
report HID Windows non dichiara output standard. GUID/path scoperti dalla
registrazione dell'esatta MI_05, non hardcoded.

F0 reale: **20 byte/payload16**; `libpsvr/Device_Status.h` descrive solo i primi
campi, non il frame completo. Conferma: PSVRFramework
`ccda7ca840f8f820197dd8571bbc869a02563ea4`, `PSVR.cs` e
[wiki F0](https://github.com/gusmanb/PSVRFramework/wiki/Report-0xF0---PSVR-Status).
Mask byte4: power bit0, worn bit1, cinema bit2; VR active byte11.
A0 reale: 64 byte/payload60, command ID byte4, result byte5 (0=accettato).
F0 su cambi/eventi, non polling continuo.

M2 richiede write completo + A0 corrispondente accettato + F0 successivo
coerente. Prima del comando drenaggio passivo limitato; nessun vendor query,
reset o retry automatico. VR/cinema devono concordare entrambi i flag:
aggiornamento in due fasi a ~1 s di distanza; power-on ~2 s. Timeout/stato
sconosciuto distinti da I/O fallito; nessun successo dedotto dal solo ACK.
Dettagli nel [report M2](docs/MILESTONE_2_REPORT.md). M3 non iniziata.

## 11. Evoluzione M3 IN_PROGRESS (2026-10-03, non completa)

Fusion originale C++17 `orientation_math.hpp`/`imu_fusion.hpp`/`imu_capture.*`:
quaternione complementare gravità, MIT indipendente; solo fatti asse/unità
OpenHMD riusati + decoder BSL; no OpenHMD-lib/Madgwick. Scale gyro 0.00105,
accel 9.81/16384; cal 8 s + 200 ms warm, STD 0.75 dps / meanShift 0.05 dps,
bias frozen, gravityGain 1, smoothing 0, recenter full/yaw.
Full two-sided `worldYawReference.inverse*absolute*neutralHeadOffset`
(offset destro, head-neutral identità senza tilt world-up); regressione
tilt-30° + world-yaw puro senza coupling. `--json-stream` DATA (gyro/accel,
tick24 1 µs rollover 16.777 s, WXYZ, YXZ, validity, wear bit0/IR LE16),
F0 da IN WinUSB, CSV omonimo, gap-max 5 ms / stale 100 ms.
Monitor `tools/psvr_monitor/monitor.py` Py3.11 Tk8.6, un NativeWorker, API
readonly loopback effimera via `logs/monitor_api.json`. Assi RHand +X/+Y/−Z,
segni yaw−/pitch+/roll−; grafici Y rosso, X verde, Z blu.
M4 non iniziata; stereo/display/SteamVR non dichiarati.

## 12. Evoluzione M4 software (2026-10-04)

Su istruzione dell'utente, prove fisiche residue rinviate e implementazione
M4 eseguita senza accesso hardware/runtime. Driver OpenVR HMD-only originale,
SDK 2.15.6 fissato, thread HID MI_04/fusion M3, snapshot e adapter pose,
factory/provider, package e script di registrazione/log. Build x64 e 18/18
CTest PASS; driver disabilitato per default, non registrato. Display component
non implementata: M4 resta IN_PROGRESS fino al riconoscimento runtime HMD/pose;
M5 non iniziata. [Report M4](docs/MILESTONE_4_REPORT.md).

## 13. Evoluzione autonoma M5 / fondazione M6 (2026-10-04)

Inventario display read-only Windows (QueryDisplayConfig/device-info/modi GDI),
componente OpenVR `_003` extended-desktop opt-in e selezione percorso monitor
esatto/geometria/refresh verificati contro snapshot. Controllo topologia su
thread separato, invalidazione latched e RequestRestart SDK; mai fallback
automatico primary/EDID. Modulo distortion pure math forward/inverse e
monotonicità analitica, non ancora applicato a rendering ottico.
Build x64 24/24 CTest PASS; nessuna enumerazione live, registrazione SteamVR,
Activate USB, presentazione sul pannello o accettazione ottica effettuata
dalla sessione autonoma. Gate fisici/runtime rinviati su richiesta utente:
[M5](docs/MILESTONE_5_REPORT.md), [M6](docs/MILESTONE_6_SOFTWARE.md),
[coda manuale](docs/MANUAL_VALIDATION_QUEUE.md).

## 14. Integrazione autonoma M6 (2026-10-04)

Loader profili JSON schema v1 display-to-render, bounded/strict e snapshot
immutable; adapter RGB forward/inverse nel componente OpenVR con opt-in
esplicito, nessun fallback identity su errore profilo. Policy SDK float
separata da math e inversa uncertainty-aware soltanto al boundary SDK.
Validatore offline Windows Unicode `out/optics/psvr_optics.exe`, pattern SVG
stereo `out/patterns/`, risorse driver aggiornate. Build x64 28/28 CTest PASS.
Profili calibrati/ottica/runtime ancora non accettati, test manuali rinviati.
[Report M6](docs/MILESTONE_6_SOFTWARE.md).

## 15. Evoluzione M9 / PSEye (2026-10-05)

La sessione autonoma aggiunge la PS3 Eye come sorgente video USB dedicata,
con binding libusb-win32 già esistente distinto dalle interfacce PSVR.
Acquisizione reale verificata a 640×480/60–75 e 320×240/187; sorgenti fissati,
patch bounded-grab e licenze in package camera separato.

Il piano OpenTrack/AITrack del §6 è sostituito inizialmente da un protocollo
JSON metrico esplicito `psvr.position` v1 su loopback 4271. Posizione opt-in,
orientamento IMU, origine seduta/epoca e rotazione recenter condivise; tracking
perso rende la posa invalida. Camera: intrinseci misurati e bersaglio rigido
ArUco, con rifiuto ambiguità/reproiezione/range/discrepanze di risoluzione.
Geometria LED PSVR, extrinseci, lever arm e latenza restano da misurare.
**35/35 CTest PASS**, M9 IN_PROGRESS. Nessuna certificazione room-scale.
[Report sessione](docs/AUTONOMOUS_20261005.md),
[protocollo e driver](docs/MILESTONE_9_SOFTWARE.md).

## 16. Estimatore di posizione selezionabile (2026-10-13)

Il tracking LED trasforma il punto HEAD nel frame IMU non-recentered prima della
selezione esclusiva One Euro/Kalman e dell'invio v2. Il Kalman esistente mantiene
stato CV 6D, Q da dt reale, R PnP e gating; le ottimizzazioni opt-in aggiungono
Q quasi-statica/onset, inflazione PSD lungo `A.T@[0,0,1]` e recovery GLS correlato.
Orientamento IMU e pipeline ottica restano indipendenti e invariati.

`prediction_output=driver_hold` separa validità interna e wire: predizioni interne
non rinnovano il last-good driver, evitando di sommare il loro budget al grace.
Protocollo/binding/timestamp restano quelli esistenti. Baseline originale numerica
conservata; logging/replay distinguono misura, predizione e discontinuità invalida.
[Report quantitativo e compromessi](docs/KALMAN_OPTIMIZATION_20261013.md).
