# Dipendenze e provenienza

I progetti di studio non sono vendorizzati. Revisioni e file esaminati:
`../ARCHITECTURE.md`. Le dipendenze di build saranno scaricate da CMake con
revisioni immutabili e conservate in `build/<toolchain>/_deps/`.

- HIDAPI 0.15.0: `d6b2a974608dec3b76fb1e36c189f22b9cf3650c`,
  https://github.com/libusb/hidapi — scelta BSD-3-Clause della tripla licenza.
- nlohmann/json 3.12.0: `55f93686c01528224f448c19128836e7df245f72`,
  https://github.com/nlohmann/json — MIT.
- Decoder sensore adattato da OpenHMD `src/drv_psvr/packet.c` e `psvr.h`,
  revisione `85075b0c7e3c723ded2577edb79d00ee11aac339`, BSL-1.0.
  Preservare copyright Joey Ferwerda e licenza nei sorgenti e nel pacchetto.

Non copiato codice da OpenPSVR (AGPL) né dagli esperimenti mSparks (licenze
per file e assenza LICENSE globale). Nessun binario upstream usato come driver.

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
