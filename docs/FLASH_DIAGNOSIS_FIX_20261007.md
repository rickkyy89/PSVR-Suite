# Bagliori: diagnosi correlata e correzione GetPose — 2026-10-07

## Conclusione

I bagliori riprodotti con telemetria sono il **fade protettivo del compositor
dopo una posa temporaneamente invalida generata da una race nel driver**.
Non risultano un comando screenshot o un problema esclusivo dei 120Hz:
sono stati riprodotti e correlati anche a 90Hz.

La race è corretta nella DLL MSVC registrata. Dopo il fix, **11 minuti** di
registrazioni controllate a 90/120Hz, con e senza dashboard, non mostrano
bagliori, pose invalide o transizioni `future_sample`.
È ancora necessaria la conferma fisica nelle lenti e la validazione prolungata.

## Diagnostica aggiunta

### Driver

`driver/pose_transition_trace.hpp` registra cambi di validità/connessione/
risultato/motivo. Buffer fisso di 128 eventi, producer con try-lock, nessuna
allocazione/serializzazione/log OpenVR dentro GetPose. RunFrame drena e scrive
le righe JSON `pose_validity_transition`; eventuali osservazioni perse sono
segnalate con `pose_trace_dropped`. Nessuna segnalazione di drop trovata nelle
raccolte di verifica.

Campi principali: `utc_unix_us`, `sequence`, `valid`, `reason`, `imu_state`,
`sample_age_ms`, `sample_age_after_copy_ms`, `query_to_copy_ms`, risultato
OpenVR e soglia attiva. I motivi distinguono staleness, future sample,
disconnessione, perdita IMU, calibrazione, quaternion, posizione e display.

### Collector

Nuovo eseguibile MSVC `psvr_runtime_telemetry`: **un solo client Background**
per tutta la prova, nessuna Scene init o auto-avvio runtime. Identità HMD
verificata con classe/seriale `PSVRPC-HMD-0`. Campionamento richiesto ogni 10ms,
con ritardi effettivi conservati, dati JSONL buffered su disco.

Raccoglie pose raw, GetLastPoses, colori/alpha dei fade, grid alpha, fullscreen,
dashboard, activity/focus, eventi della propria coda e history frame timing
con deduplicazione degli indici. `m_HmdPose` nei frame timing è la posa usata
dall'app per il rendering: non viene spacciata per stato corrente del driver.
I timestamp di ricezione dei frame storici non sono timestamp esatti del guasto.

`record_headset_view.ps1` usa questo collector di default: il medesimo client
rinnova/rilascia la presenza sintetica e segnala ready/stop. Non apre probe
periodici aggiuntivi durante il video. `-NoTelemetry` mantiene il vecchio modo
diagnostico. `correlate_flashes.py` allinea video, telemetria e transizioni del
driver; i tempi video sono approssimativi per la latenza di startup della cattura.

## Meccanismo della race

Prima:

```text
GetPose legge now
il reader riceve e pubblica una snapshot IMU più recente
GetPose acquisisce il mutex e copia quella snapshot
sampled_at > now -> posa invalida (Running_OutOfRange)
```

La snapshot è sana. È il riferimento temporale preso prima della copia a
essere vecchio di pochi microsecondi. Un singolo publish invalido dura circa
9–11ms prima della posa valida successiva, ma il fade visibile dura circa 0.8s.
Il reader resta in `tracking`, quindi non produce un `imu_capture_fault` terminale.

Esempi reali:

- 90Hz: età `-0.0058ms` al riferimento iniziale e `+0.0065ms` dopo la copia.
- 120Hz: età `-0.0060ms` prima e `+0.0058ms` dopo; ritorno valido circa 9ms dopo.

Durante i bagliori correlati: fade alpha fino a 1, grid alpha 1, invalidità
raw osservata, nessun frame timing perso/mispresentato nella finestra e GPU
compositor circa 1–2ms. I quattro eventi di rifiuto a 90Hz generano tre bagliori:
uno dei recuperi avviene in meno di 1ms e non si riflette in un bagliore rilevato.

## Correzione

In `Hmd::GetPose`, la lettura del clock è ora **dopo la copia di snapshot_ e
all'interno del medesimo mutex**. Il reader non può pubblicare nel mezzo.
Il calcolo della posa usa quindi un tempo non precedente alla snapshot copiata.
Non è stato introdotto un clamp o una tolleranza per accettare dati realmente
futuri. Restano i controlli di freschezza e validità; i test coprono anche il
rifiuto di un query time realmente precedente al campione.

## Prima/dopo: raccolte in logs/

| Raccolta (prefisso file) | Hz | UI | Video | Bagliori | Motivi invalidi osservati |
|---|---:|---|---:|---:|---|
| `flash_trace_90_shown_20261007` | 90 | dashboard | 180s | **3** | 4 `future_sample`; 5 campioni raw invalidi |
| `flash_trace_120_shown_before_20261007` | 120 | dashboard | 180s | **1** | 1 `future_sample`; 1 campione raw invalido |
| `flash_trace_120_shown_after_20261007` | 120 | dashboard | 300s | **0** | nessuno; 28.674 campioni raw validi |
| `flash_trace_120_hidden_after_20261007` | 120 | senza dashboard | 180s | **0** | nessuno; 17.202 campioni raw validi |
| `flash_trace_90_shown_after_20261007` | 90 | dashboard | 180s | **0** | nessuno; 17.192 campioni raw validi |

Dopo il fix: **63.068/63.068 campioni raw validi**, 74.092 frame timing raccolti
durante i video, zero campioni fullscreen false. Dashboard true/false verificata
continuamente, non dedotta dall'intenzione del comando. Refresh sorgente/scanout
e driver coerenti per ciascuna prova; geometry Sony invariata.

I tempi dei lampi prima del fix sono 0.2s, 1.3s e 123.433s nella prova a 90Hz;
142.633s nella prova a 120Hz. Il ritardo di startup WGC/encoder rende i tempi
basati sul lancio processo anticipati di circa 0.16–0.19s rispetto agli eventi
driver in queste raccolte. La correlazione usa una finestra ±2s e non dichiara
sincronizzazione precisa del singolo frame GPU.

Sono conservati MP4, JSONL, metadata, JSON di correlazione, stderr e log completi
prima/dopo per tutte le prove. Le registrazioni sono software a circa 30fps,
non una misura di frame skipping del pannello né accettazione ottica.

## Confronto senza dashboard

Un tentativo iniziale di pulse `/input/system/click` sull'HMD non chiudeva la
dashboard: fallito il readback, nessun video dichiarato riuscito. Quel codice
sperimentale è stato rimosso.
Per il confronto riuscito, a SteamVR chiuso è stato applicato temporaneamente
`dashboard.enableDashboard=false`, poi avviato il runtime. Collector e video
verificano l'assenza della dashboard. Al termine è stata ripristinata la sola
chiave dal backup immediatamente precedente al test (era assente), preservando
le altre impostazioni. Nessun pulsante fisico è stato rimappato.

## Verifica e stato finale

- Build MSVC x64 `RelWithDebInfo`, **52/52 CTest passati**.
- Nuovi test: transizioni, deduplicazione, buffer limitato/overflow/reset,
  riproduzione microtemporale del falso futuro e clock corretto dopo la copia;
  CLI collector offline con DLL inesistente, bounds e opzioni duplicate.
- DLL corrente: `out/driver_psvrpc_msvc/psvrpc/bin/win64/driver_psvrpc.dll`.
- SHA256: `7D587D7DC5DC2C025BAABEB7FE95609700B29266157CE70BDA0F1C20A98E0CF0`.
- DLL con trace **prima del fix**, riproducibile:
  `out/driver_psvrpc_msvc/driver_psvrpc_pose_trace_before_fix_20261007.dll`,
  SHA256 `63C778B718D585D004F71466284AF33500E6144DC094F6B632D1CC293A93E4FB`.
- Baseline precedente senza trace conservata in
  `driver_psvrpc_before_pose_trace_FD33D56F.dll`; precedente prova 30min in
  `driver_psvrpc_30min_12A7724C.dll`.
- SteamVR chiuso normalmente; collector/encoder terminati; presenza rilasciata.
- Windows modo attivo/salvato **90Hz**, driver 90/1, dashboard ripristinata al
  default precedente, mirror false, nessun EDID o custom timing creato.

Nel log storico compare anche una perdita terminale distinta alle 14:53:12:
`sensor_tick_gap`, delta 5503µs contro soglia 5000µs. Non coincide con i bagliori
correlati e il fix GetPose non modifica quel controllo. Le prove post-fix qui
riportate non mostrano perdite terminali, ma non certificano stabilità prolungata.

Prossimo gate: confermare nelle lenti l'assenza dei bagliori, anche a 120Hz,
poi completare uso prolungato, dashboard, standby/ritorno e ottica/proporzioni.
