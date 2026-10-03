# Configurazione

`config.json` schema 1 contiene solo i parametri già funzionanti M1. Il tool
lo legge esclusivamente con `--config`; i flag CLI hanno precedenza, i nomi
sconosciuti/range errati vengono rifiutati prima dell'accesso hardware.

| Campo diagnostic | Range / significato |
|---|---|
| duration_ms | 100–60000, durata capture monotonic |
| read_timeout_ms | 1–1000, timeout singola lettura, limitato dalla durata restante |
| raw_packets | 0–100, limite report raw per interfaccia |
| log_dir | directory scrivibile, relativo alla directory di esecuzione |
| logging_verbosity | info/debug; debug aggiunge delta timestamp intra-pacchetto (24 bit) |

In M3–M6 aggiungere schema versione successiva con sezioni realmente consumate:
`tracking` (smoothing, sensitivity, recenter quaternion/offset, orientation gain),
`display` (IPD metri, tangenti FOV per occhio, render resolution per occhio,
refresh rilevato/override esplicito), `optics` (centri, coefficienti, RGB).
In M9 `position` (AITrack enable, UDP loopback/port, unit conversion,
positional gain, smoothing, deadzone, stale timeout).

Non inserire valori ottici arbitrari in un file che il driver ancora non usa.
Non confondere impostazioni configurate con refresh/coordinate misurati.
