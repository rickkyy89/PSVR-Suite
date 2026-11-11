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

## Preferenze della suite e consumatori

Le preferenze della suite sono uno schema distinto dal JSON diagnostico.
Al caricamento iniziale la precedenza è `--preferences` > file utente >
impostazioni driver > default suite. Un file utente malformato produce una
nota e il tentativo di lettura del driver; se anche questo è indisponibile
si usano i default. La lettura non riscrive i file.

- **FPS camera:** intero positivo per salvataggio, UI e worker. `75.0` viene
  rappresentato esattamente come `75`; `74.5` viene rifiutato prima del
  salvataggio, senza arrotondamenti.
- **`tracking_config_path`:** file passato come `--config` a `psvr_diag`
  per i comandi di avvio VR (`--vr-mode`, `--leds-on`) e ritorno cinema.
  I percorsi relativi sono risolti rispetto alla root della suite, gli assoluti
  restano assoluti, anche con spazi. Il default è `config.json`. Un percorso
  esplicito non valido non viene sostituito con la baseline. La UI acquisisce
  il percorso al click dell'azione. Il campo non configura il worker camera
  né il driver OpenVR: quest'ultimo legge le chiavi `imu*` da
  `steamvr.vrsettings`, applicate separatamente dalla suite.
- **Gap IMU per destinazione:** suite e driver ammettono `1000..50000 us`;
  `psvr_diag` ammette `1000..20000 us`. `derive_imu_config` rifiuta valori
  oltre 20000 prima di creare il derivato, con errore esplicito e senza clamp.
  I tre override di timing del derivato sono `calibration_ms` (500..10000),
  `stale_timeout_ms` (20..1000) e `max_sample_gap_us` (1000..20000).
  La baseline globale resta di sola lettura e un derivato esistente non viene
  sovrascritto. Cambiare le preferenze non riscrive automaticamente un derivato.
- **Importazione driver:** `positionEnabled` assente o `false` significa
  `3dof`; soltanto `true` significa `6dof`, come nel driver nativo.
  Un valore presente di tipo diverso da bool viene rifiutato. I default di
  una nuova configurazione suite mantengono invece `tracking_mode=6dof`:
  creazione di preferenze e ricostruzione del driver sono operazioni distinte.

Regressioni offline: `tests/test_suite_settings_consumers.py` e callback UI
in `tests/test_suite_app_model.py`. Il test CTest `suite_config_native_interop`
verifica export Python → parser C++ reale ai limiti del contratto, senza USB:

```powershell
cmake --preset vs2022
cmake --build build/vs2022 --config Release --target psvr_suite_config_fixture
ctest --test-dir build/vs2022 -C Release -R suite_config_native_interop --output-on-failure
```

In alternativa, dopo aver compilato la fixture, eseguire
`python tests/check_suite_config.py <percorso-psvr_suite_config_fixture.exe>`.
