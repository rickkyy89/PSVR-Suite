# Tasti visore: decoder e forwarding

**Stato attuale (2026-10-18):** decoder dei byte tasti e forwarding dal driver
implementati; test offline PASS. Le prove fisiche live **B13-HW-01/02 restano
aperte**: pressione dei tasti, OSD/mute e ascolto jack non sono ancora verificati.

**Nota storica:** prima dell'implementazione né il driver né `tools/psvr_diag`
leggevano i byte tasti. Windows non li gestisce autonomamente perché manca una
collection consumer 0x0C (sono presenti solo vendor page fff0/ff01);
`docs/VR_VIEW_90_120_20261007.md:33` registrava quindi la funzione da implementare.

## Verità hardware (fonte: PSMoveService `MorpheusHMD.cpp`, già citato
come riferimento per il rig in questo repo)

Report sensore MI_04 HID, 64 byte, stessi dei dati IMU già letti:

| Byte | Contenuto |
|---|---|
| 0 | bitmask tasti: `VolumePlus = 2`, `VolumeMinus = 4`, `MicrophoneMute = 8` |
| 2 | livello volume |
| 8 | bitfield: bit3 `microphoneMuted`, bit4 `headphonesPresent` (+hmd on head, display on, HDMI) |

Il driver apre già MI_04 per l'IMU: i tasti arrivano nello stesso
report, ora decodificati e inoltrati dal driver; la risposta alle pressioni fisiche resta da verificare.

## Piano originale (storico, precedente all'implementazione)

1. **Prova di arrivo (al ritorno utente, visore indossato o no):**
   cattura raw MI_04 (`psvr_diag` capture) premendo Vol+/Vol-/Mute;
   verificare transizioni byte 0 (2/4/8), byte 2 e byte 8 bit3.
   Se i byte non cambiano, un'ipotesi da indagare è che la PU li consumi
   internamente; non è una prova. Occorre verificare il percorso prima di
   cambiare piano (mixer hardware USB-audio, non forwarding).
2. **Decoder:** estendere il parser del report sensore (byte 0/2/8),
   con test offline su frame sintetici (pattern B2/B4 esistente).
3. **Forwarding (driver `psvrpc`, edge-trigger sui fronti dei bit):**
   Vol+/Vol- → `IAudioEndpointVolume::VolumeStepUp/Down` sull'endpoint
   di rendering (`{cdf7602...}`, già noto al driver via override);
   Mute → `SetMute` toggle sull'endpoint di cattura (ID da scoprire
   dinamicamente, NON hardcodare `{C3A38B99...}` visto in PnP).
   Mai inviare payload USB inventati (regola M2: solo comandi
   documentati).
4. **Validazione:** OSD volume / stato mute Windows + jack; ripetere
   in cinema e VR (il routing PU cambia fra i modi, vedi
   `docs/AUDIO_TRACKING_CAMERA_20261015.md`).

## Stato implementazione (2026-10-18, verificato nel working tree)

- `tools/psvr_diag/sensor_packet.hpp`: `decode_sensor` esteso con
  `buttons_raw` (byte0), `volume_raw` (byte2), `headset_flags_raw`
  (byte8) + helper `volume_up/down/mic_mute_pressed/mic_muted/
  headphones_present`; framing IMU invariato byte-identico.
  Logging esteso in `imu_capture.cpp` (CSV+JSON) e `main.cpp`.
- `driver/headset_buttons.hpp` (nuovo): edge-trigger + debounce
  150 ms per tasto, funzione pura testabile.
- `driver/headset_audio.hpp` (nuovo): `IHeadsetAudioControl` +
  backend Windows Core Audio (`VolumeStepUp/Down` su render noto,
  toggle `SetMute` su capture con discovery dinamica MI_01, mai
  ID hardcodati, fail-closed).
- `driver/driver_psvrpc.cpp`: wiring nel thread reader MI_04.
- Test: `diag_core` (~37 asserzioni tasti) + nuovo
  `headset_buttons_offline` (12 asserzioni) — entrambi PASS in
  build temp; `driver_psvrpc` compila.
- `docs/MANUAL_VALIDATION_QUEUE.md` §13: checklist live;
  `TESTING.md`: comandi di regressione.

## Cosa resta solo live (invariato)

- Pressione fisica reale Vol+/Vol-/Mute + cattura raw MI_04
  (`psvr_diag` capture): verificare transizioni byte 0 (2/4/8), byte 2 e
  byte 8 bit3. **Senza visore in mano non eseguibile.**
- Ascolto jack cuffie visore in cinema vs VR (matrice A/B SIE-HMD vs
  PS VR Audio, cfr. `docs/AUDIO_TRACKING_CAMERA_20261015.md` §1) e
  OSD volume Windows / stato mute mic: distinguono un forwarding reale
  da un no-op. Tutto rinviato al ritorno utente.

## Dipendenza

Il test d'ascolto richiede il path audio VR funzionante (matrice A/B
SIE-HMD vs PS VR Audio, ancora da eseguire al ritorno utente): a
tasti funzionanti ma audio muto non si distingue l'effetto.
