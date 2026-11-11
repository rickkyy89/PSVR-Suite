# PS4 Camera — prova statica punti stereo (visore fermo, prova storica non tracking 2026-10-24)

Prova statica a visore fermo del **2026-10-24**: acquisizione stereo
`640x400` a `60 FPS` (backend `dshow`, indice `0`) per **~30 s**
(`duration_s` 30,296), senza indossare il visore e senza muoverlo.

## Risultato (sessione `ps4-stereo-points-20261023T234847436527Z`)

- Coppie analizzate: **1688** (`frames_published` 1688, `dropped` 0).
- FPS tra callback host: **60,08** (`stream_pair_fps` 60,0805; misura di
  intervalli di callback host, non timestamp di esposizione).
- Stati `points`: **1687** su 1688 (1 `partial`).
- Istogramma punti: **1662** coppie con 5 punti, 25 con 6, 1 con 4
  (8464 osservazioni di punto totali).
- RMS di riproiezione sui punti: **0,145 px** (0,14494).
- Profondità triangolata dei punti: **0,746–0,843 m**.
- Soglia rivelatore: `value_min` **160**; controllo luminosità richiesto
  **brightness 0** (readback non confermato: il driver ha riportato 4,0;
  avviso registrato nel JSON, nessun errore di cattura).
- Calibrazione usata:
  `configurations/ps4_stereo_640x400_calibrated.json`
  (SHA-256 `1990fae7…d8ee522`, registrato nel report).

## Limiti espliciti (non è tracking) — verifica di lettura storica (2026-10-24)

- **Nessuna posa testa stimata** (`head_pose_estimated: false`).
- **Nessun ID anatomico assegnato** (`identities_assigned: false`).
- **Nessuna posizione pubblicata / UDP / 6DoF**
  (`position_published: false`, `tracking_6dof: false`).
- **6DoF statico in FRONT5 e BOTH_CAMERAS** (non tracciamento in PS4_STEREO_POINTS.md).
- Coordinate dei punti **solo nel sistema left-camera dello stream**
  (`coordinate_frame: left_camera_stream_not_head_or_world`), non
  testa/mondo.
- Timestamp **host a fine lettura**, non esposizione hardware
  (`timing_source: host_read_completion_not_exposure`).

## File

- Report completo:
  `logs/ps4_camera/ps4-stereo-points-20261023T234847436527Z.json`
- Snapshot annotato L/R:
  `logs/ps4_camera/ps4-stereo-points-20261023T234847436527Z.jpg`

Contesto acquisizione: [PS4_CAMERA.md](PS4_CAMERA.md).
Calibrazione stereo di riferimento:
[PS4_GUIDED_CALIBRATION.md](PS4_GUIDED_CALIBRATION.md).
