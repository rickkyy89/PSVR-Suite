# Configurazione

`config.json` contiene i parametri già consumati dal diagnostico M1/M2/M3. Il tool
lo legge esclusivamente con `--config`; i flag CLI hanno precedenza, i nomi
sconosciuti/range errati vengono rifiutati prima dell'accesso hardware.

Schema **2** attuale, retrocompatibile con 1: sezione `diagnostic` invariata +
sezione `tracking` opzionale consumata da M3. Nomi/lettura codice quando serve:
`tools/psvr_diag/imu_capture.cpp` + `main.cpp` (default se chiave assente).

| Campo diagnostic | Range / significato |
|---|---|
| duration_ms | 100–60000, durata capture monotonic |
| read_timeout_ms | 1–1000, timeout singola lettura, limitato dalla durata restante |
| control_timeout_ms | 100–15000, attesa ACK/stato M2; default 3000; `--control-timeout-ms` ha precedenza |
| raw_packets | 0–100, limite report raw per interfaccia |
| log_dir | directory scrivibile, relativo alla directory di esecuzione |
| logging_verbosity | info/debug; debug aggiunge delta timestamp intra-pacchetto (24 bit) |

In M3 `tracking` (tutti opzionali, vedi `config.json` corrente per default):
`capture_ms`, `calibration_ms` 8000, `startup_discard_ms` 200,
`output_interval_ms`, `stale_timeout_ms` 100, `max_sample_gap_us` 5000,
`gyro_scale` 0.00105, `accel_scale` 9.81/16384, `gravity_gain` 1.0,
`accel_tolerance`, `calibration_gyro_std_dps` 0.75,
`calibration_mean_shift_dps` 0.05, `calibration_gyro_mean_dps`,
`calibration_accel_std`, `smoothing_ms` 0, `max_drift_dpm`,
`recenter_mode` full/yaw.
In M4–M6 aggiungere solo sezioni realmente consumate:
`display` (IPD metri, tangenti FOV per occhio, render resolution per occhio,
refresh rilevato/override esplicito), `optics` (centri, coefficienti, RGB).
In M9 `position` (AITrack enable, UDP loopback/port, unit conversion,
positional gain, smoothing, deadzone, stale timeout).

Non inserire valori ottici arbitrari in un file che il driver ancora non usa.
Non confondere impostazioni configurate con refresh/coordinate misurati.
