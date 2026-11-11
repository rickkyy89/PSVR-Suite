# Driver OpenVR — M4/M5 software

Implementazione C++17 originale HMD-only, SDK OpenVR 2.15.6 fissato:
provider `_004`, device `_005`, thread HID sensori MI_04, fusion M3,
pubblicazione `TrackedDevicePoseUpdated` da `RunFrame`.

Package generato dalla build:
`out/driver_psvrpc/psvrpc/bin/win64/driver_psvrpc.dll`, manifest,
`resources/settings/default.vrsettings` e licenze. Driver disabilitato
per default. Non registrato automaticamente dalla build.

**M4/M5 IN_PROGRESS:** build e test software verificati; riconoscimento
HMD/pose e output nel runtime SteamVR non ancora provati. Componente
`IVRDisplayComponent_003` desktop-esteso disponibile opt-in, con selezione
monitor esatto e settings projection espliciti. Default `displayEnabled=false`.
Non dichiara Direct Mode. Nessun comando USB power/mode.

Controllo topologia separato: mismatch/perdita invalida la posa e richiede
riavvio del runtime; invalidazione conservata anche tra Deactivate/Activate.
La richiesta SDK non certifica l'arresto immediato del compositor.
Distorsione display identity diagnostica per default. M6 offre loader profilo
JSON rigoroso e adapter forward/inverse opt-in (`distortionMode=profile`, path
assoluta), senza profili fisicamente accettati. Validatore `out/optics/`,
pattern `out/patterns/`. Suite corrente 28/28 CTest PASS (hardware-free).

Procedura operativa e gate: [docs/MILESTONE_4_REPORT.md](../docs/MILESTONE_4_REPORT.md).
Display: [docs/MILESTONE_5_REPORT.md](../docs/MILESTONE_5_REPORT.md).
Ottica: [docs/MILESTONE_6_SOFTWARE.md](../docs/MILESTONE_6_SOFTWARE.md).

M9 posizione esterna opt-in, default **disabilitato**: protocollo condiviso con
il mittente webcam `psvr.position` v1 su loopback UDP (127.0.0.1:4271, `seq` +
`position_m` in metri nel frame `psvr_world`), ricevitore Winsock RAII con
timeout limitato e freschezza monotona locale, recenter condiviso
IMU+posizione, perdita esplicita `Running_OutOfRange` senza posizione inventata
o predizione. L'orientamento della webcam è validato ma ignorato: l'IMU possiede
l'orientamento. OpenTrack/AITrack non sono interpretati direttamente: serve un
adattatore che riscriva i pacchetti in questo schema e in metri. Dettagli,
configurazione e limiti:
[docs/MILESTONE_9_SOFTWARE.md](../docs/MILESTONE_9_SOFTWARE.md).
