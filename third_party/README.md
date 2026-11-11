# Dipendenze e provenienza

La licenza MIT di progetto (`../LICENSE`, Copyright (c) 2026 rickkyy)
copre solo il codice originale. Non rilicenzia terze parti né studi:
vedi `../THIRD_PARTY_NOTICES.md`. I testi originali completi sono in
`licenses/` — file elencati in `licenses/manifest.json`, in 3 categorie
(`included`, `reference`, `runtime`; manifest con categoria, url,
revisione, versione, sha256, byte, retrieval e verification).
Verifica self-contained: `python tools/collect_licenses.py check`
(solo manifest + file DEST); rigenerazione:
`python tools/collect_licenses.py collect`. Test:
`tests/test_license_collection.py`.

I progetti di studio non sono vendorizzati. Revisioni e file esaminati:
`../ARCHITECTURE.md`. Le dipendenze di build sono scaricate da CMake con
revisioni immutabili e conservate in `build/<toolchain>/_deps/`.

- HIDAPI 0.15.0: `d6b2a974608dec3b76fb1e36c189f22b9cf3650c`,
  https://github.com/libusb/hidapi — scelta BSD-3-Clause della tripla licenza.
  Testi: `licenses/hidapi-LICENSE.txt` (scelta), `hidapi-LICENSE-bsd.txt`,
  `hidapi-LICENSE-gpl3.txt`, `hidapi-LICENSE-orig.txt`.
- nlohmann/json 3.12.0: `55f93686c01528224f448c19128836e7df245f72`,
  https://github.com/nlohmann/json — MIT. Testo: `licenses/json-MIT.txt`.
- Decoder sensore adattato da OpenHMD `src/drv_psvr/packet.c` e `psvr.h`,
  revisione `85075b0c7e3c723ded2577edb79d00ee11aac339`, BSL-1.0.
  Preservare copyright Joey Ferwerda e licenza nei sorgenti e nel pacchetto.
  Testo: `licenses/BSL-1.0.txt` (copia di `BSL-1.0.txt`, qui conservato per
  la build `install()` esistente).

Non copiato codice da OpenPSVR (AGPL-3.0) né dagli esperimenti mSparks
(nessun LICENSE globale al pin — nota onesta, nessun testo fabbricato;
licenze per file). Nessun binario upstream
usato come driver. libpsvr (LGPL-3.0) e PSVRFramework (AGPL-3.0) solo
consultati; testi originali al pin conservati come
`licenses/references/` (categoria `reference`, NESSUN codice
incluso); la MIT di progetto non li rilicenzia.

M2 aggiunge soltanto **WinUSB nativo Windows**, nessuna dipendenza di build.
`control_protocol.hpp` implementa indipendentemente valori power/mode da
OpenHMD/libpsvr e F0/A0 da libpsvr/PSVRFramework. Riferimento supplementare:
`gusmanb/PSVRFramework` revisione `ccda7ca840f8f820197dd8571bbc869a02563ea4`,
`PSVRFramework/PSVR.cs` e wiki F0/USB Command Response Format: consultati,
non copiati come sorgenti. Zadig 2.9 è un tool esterno GPLv3 usato dall'utente
per cambiare MI_05; non distribuito nel pacchetto né invocato dalla build.

M3: matematica/fusion nuove (`orientation_math.hpp`, `imu_fusion.hpp`) **MIT
di progetto**, indipendenti (no OpenHMD-lib/Madgwick); solo fatti asse/unità
OpenHMD riusati + decoder BSL preservato. Wear raw/IR: codice consultato da
PSVRFramework, non copiato. GUI `tools/psvr_monitor/monitor.py` solo
**stdlib Python 3.11 / Tk 8.6**. Il codice della GUI è MIT; CPython e Tcl/Tk
installati separatamente conservano le proprie licenze e non sono vendorizzati.

M4: header driver Valve OpenVR SDK **2.15.6**, commit
`0924064316de3effbcd1acf1e309182a2deb1c05`,
https://github.com/ValveSoftware/openvr — licenza BSD-3-Clause Valve, testo
in `licenses/openvr-LICENSE.txt` (inclusa nel package come
`licenses/openvr-LICENSE.txt`). Nessun sorgente dei driver
di riferimento copiato; adapter/provider originali MIT. Nessuna libreria
client OpenVR necessaria; HIDAPI resta statica. Il package conserva inoltre
licenze progetto, decoder BSL, HIDAPI BSD e JSON MIT.

M9 camera: shim e strumenti originali MIT; il backend PS3 Eye separato usa
`inspirit/PS3EYEDriver@fe03494e648d0bf2aaa77869e5b50965e2e23620`
(MIT per nuovo codice, tabelle OV534 derivate dal kernel GPLv2). La patch
locale è in `tools/psvr_pseye/patches/`, applicata a una copia di build.
Caricamento dinamico non elimina gli obblighi del backend GPL; package
`out/pseye` include licenze complete e `NOTICE.txt` con provenienza/sorgenti.
Testi centrali: `licenses/PS3EYEDriver-LICENSE.txt`, `licenses/GPL-2.0.txt`.
libusb header v1.0.27 (`d52e355daa09f17ce64819122cb067b8a2ee0d4b`) e runtime
locale 1.0.29, LGPL-2.1-or-later (`licenses/LGPL-2.1.txt`); non viene
redistribuito nel package del driver SteamVR. NumPy 2.4.6 / OpenCV
contrib headless 5.0.0.93: testi originali dal dist-info dei wheel in
`licenses/runtime/numpy/` e `licenses/runtime/opencv-contrib-python-headless/`,
inclusi i testi dei componenti NumPy e `LICENSE-3RD-PARTY.txt` di OpenCV
(183251 byte, mai binari/wheel). CPython, Tcl e Tk hanno testi separati
in `licenses/runtime/`; include anche la licenza MIT di pytest.
Dettagli e provenienza: `THIRD_PARTY_NOTICES.md`.
