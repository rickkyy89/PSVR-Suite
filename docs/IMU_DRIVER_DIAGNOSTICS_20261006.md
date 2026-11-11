# Diagnostica alla perdita IMU nel driver

Implementata il 2026-10-06. Il driver scrive in `vrserver.txt` una riga JSON
con `event="imu_capture_fault"`, `schema_version=1`, prima di terminare la
cattura per tracking perso, calibrazione fallita o errore HID.
È preceduta dal normale prefisso log `psvrpc:`. Non registra i singoli pacchetti.

## Motivi

| `reason` | Significato |
|---|---|
| `watchdog_no_reports` | Nessun report durante la finestra iniziale di 3000ms. |
| `watchdog_no_advancing_samples` | Mancato avanzamento dei campioni: 100ms in tracking, 1000ms prima del tracking. |
| `sensor_tick_gap` | Delta tra tick consecutivi del sensore oltre `max_sample_gap_us` (default 5000µs). |
| `hid_read_error` | `hid_read_timeout` ha restituito un valore negativo. |
| `calibration_failed` | Fallimento dei controlli di calibrazione; dettaglio in `calibration_error`. |

Il primo motivo rilevato dal tracker è conservato. Un watchdog successivo non
sovrascrive un gap tick già osservato. I valori dei tick sono quelli del
campione che ha causato il gap, anche se il report contiene un secondo campione.
Il ramo errore HID è esplicitamente distinto e non identifica la causa fisica
dell'errore. Le eccezioni prima dell'apertura sensore mantengono il log esistente.

## Campi principali

- `previous_state`, `state`: stato prima del guasto e stato della snapshot.
  Nel ramo HID è lo stato del tracker prima dell'invalidazione finale.
- `failure_elapsed_ms`, `failure_progress_age_ms`: tempo dalla partenza/reset
  e distanza dall'ultimo avanzamento al **primo** guasto del tracker.
  Sono null se il guasto non proviene dal tracker, ad esempio un errore HID.
- `progress_age_ms`, `last_sample_age_ms`: età osservate al momento del log.
  La seconda è null quando non esiste una snapshot con campione; non sostituisce
  l'età congelata al primo guasto.
- `failure_timeout_ms`, `stale_timeout_ms`, `max_sample_gap_us`: soglie attive.
  `failure_timeout_ms=0` nei guasti non temporizzati dal watchdog.
- `hid_last_read_ms`, `hid_max_read_ms`, `reader_last_loop_interval_ms`:
  durata dell'ultima lettura, massimo dall'ultimo reset e distanza tra due
  completamenti del ciclo reader. Usano il clock monotono host.
- `hid_reads`, `hid_empty_reads`, `hid_last_bytes`, `hid_timeout_ms`:
  letture effettuate, ritorni zero, ultimo risultato (anche -1), timeout 10ms.
- `reports_received`, `reports_decoded`, `reports_malformed`,
  `reports_duplicate`, `reports_blocked_by_watchdog`: contatori dei report.
  Un report bloccato dal watchdog prima dell'accettazione non viene decodificato;
  `last_report_sequence`/`last_report_tick` restano quelli dell'ultimo decodificato.
- `previous_sample_tick`, `sample_tick`, `sample_tick_delta_us`: tick/delta
  modulo 24 bit; null quando non esiste una coppia di tick da confrontare.
- `samples_seen`, `samples_advanced`, `samples_rejected`, `sample_duplicates`,
  `sample_gaps`: contatori del percorso fusion. “Advanced” segue il risultato
  dell'adapter fusion, includendo avanzamento warmup/calibrazione, non solo pose
  di tracking valide. Duplicati di report e duplicati di campioni sono distinti.

I contatori si azzerano alla ricalibrazione. Il logging avviene sul thread
reader solo al guasto terminale; un errore del logging non maschera il guasto.
Soglie, ordine dei controlli, invalidazione delle pose e richiesta di restart
mantengono il comportamento precedente.

## Verifica e artefatti

- Build MSVC x64 `RelWithDebInfo`: **49/49 CTest passati**.
- Test sintetici: watchdog tracking/startup e confine 100ms, pacchetto buffered
  dopo scadenza, gap tick con conservazione del primo motivo, rollover 24 bit,
  report/campioni duplicati, report malformati, reset contatori, calibrazione
  fallita, campi null e serializzazione del ramo errore HID.
- Nuova DLL:
  `out/driver_psvrpc_msvc/psvrpc/bin/win64/driver_psvrpc.dll`.
- SHA256:
  `FD33D56F6B0CDB3E1939F5630E2B8889CF995B7E1BBB139C79A6B9759CE0C728`.
- Baseline dei 30 minuti conservata con simboli in
  `out/driver_psvrpc_msvc/driver_psvrpc_30min_12A7724C.dll` e `.pdb`.
- Breve prova runtime della nuova DLL:
  `logs/presentation_20261006_113419`, tre campioni, **30/30 pose valide**,
  fullscreen/render true, Sony 1920×1080 a 90Hz. Presenza simulata rilasciata e
  SteamVR chiuso normalmente. Non si è verificato un guasto IMU reale durante
  questa prova; il ramo fault è verificato dai test sintetici, non da un
  guasto deliberatamente indotto sull'hardware.

La precedente prova lunga è riferita alla DLL baseline, non a questo nuovo
hash. La prossima perdita reale potrà essere correlata con i campi qui descritti.
