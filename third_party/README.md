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
