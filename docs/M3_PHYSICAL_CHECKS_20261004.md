# Verifiche fisiche M3 — 2026-10-04

M3 resta IN_PROGRESS. Prova guidata yaw-right confermata dall'utente con
«fatto»; dati acquisiti dal monitor e dal CSV nativo.

## Yaw verso destra — PASS

- CSV: `logs/psvr_diag_20261004T021328.427Z_8272.csv`.
- Recenter indossato a device elapsed 27,538532 s: count 2, posa identità.
- Finestra del movimento e ritorno: circa 33,3–39,2 s, count 2 costante,
  campioni validi, `sensor_worn=1`, IR raw 1023.
- Yaw minimo osservato: −50,645258° a 35,979543 s.
- Pitch durante la finestra: circa −0,20°…+3,60°;
  roll: circa −1,15°…+3,45°. Escursioni incrociate inferiori al limite
  `max(10°, 60% primaria)` e primaria superiore a 10°; segno corretto.
- Ritorno vicino al centro: a 39,159548 s yaw −0,020802°,
  pitch +0,017023°, roll +0,775132°.
- Calibrazione riportata dall'API: 16000 campioni, 8000,008 ms;
  gyro STD (0,545488; 0,581185; 0,613950) dps e half-mean shift
  (0,004142; −0,000533; 0,003226) dps, entro i gate.

Risultato relativo a questa prova guidata; non è una misura ottica
dell'accuratezza angolare. Pitch-up, roll-right e confronto controllato
indossato/non indossato restano pendenti. La presenza di movimento raw
durante la manipolazione non indossata nel CSV non sostituisce il confronto
controllato.

## Pitch verso l'alto — PASS

- Stesso CSV della prova yaw; movimento guidato confermato dall'utente.
- Recenter indossato a 149,409705 s: count 3, posa identità.
- Finestra analizzata: 149,409705–159,013720 s, count 3 costante,
  campioni validi, `sensor_worn=1`, IR raw 1023.
- Pitch massimo +38,150425° a 154,201707 s: segno corretto e primaria >10°.
- Yaw nella finestra circa −2,29°…+0,83°; roll circa −1,15°…+3,93°.
  Escursioni incrociate entro `max(10°, 60% primaria)`.
- Posa al ritorno a 159,013720 s: yaw −2,196875°, pitch −3,191290°,
  roll −0,319619°. Il ritorno fisico non è misurato con riferimento ottico.
- API ancora in tracking con posa valida e nessun `last_error` al controllo.

Roll-right e confronto controllato indossato/non indossato restano pendenti;
M3 non è completa.

## Roll verso destra — PASS

- Stesso CSV; movimento guidato confermato dall'utente.
- Recenter indossato a 222,590811 s: count 4, posa identità.
- Finestra analizzata: 222,590811–228,335825 s, count 4 costante,
  campioni validi, `sensor_worn=1`, IR raw 1023.
- Roll minimo −45,193804° a 226,211816 s: segno corretto e primaria >10°.
- Yaw circa −1,75°…+2,46°; pitch circa −9,19°…+2,14°.
  Escursioni incrociate entro il limite di circa 27,12° (60% primaria).
- Al ritorno il roll attraversa lo zero: −0,368724° a 228,019815 s,
  poi raggiunge +7,664942° a 228,335825 s. Ritorno con overshoot;
  questa prova non certifica accuratezza angolare o drift statico.
- API ancora in tracking con posa valida e nessun `last_error` al controllo.

Le tre prove guidate confermano i segni fisici yaw-right negativo,
pitch-up positivo, roll-right negativo. Rimangono il confronto controllato
di presenza e gli altri gate hardware documentati; M3 resta IN_PROGRESS.

## Presenza — movimento non indossato rilevato

- Prova guidata non indossata confermata dall'utente; stesso CSV.
- Recenter count 5 a 318,653950 s; finestra di rotazione e ritorno
  321,338954–328,309976 s: `sensor_worn=0` in tutti i campioni letti,
  IR raw 167–187, posa valida e tracking attivo.
- Yaw raggiunge −57,253829° a 322,978950 s, poi torna a −3,625358°
  a 328,309976 s. Gyro Y grezzo in unità fisiche −1,2411 rad/s
  a 321,959966 s e +1,0773 rad/s a 327,073961 s.
- Il flusso gyro e la posa rispondono quindi al movimento anche con
  flag non indossato in questa sessione: nessuna soppressione totale osservata.
- Stato F0 disponibile al controllo successivo: acceso, cinema,
  mask 85, `worn=false`, `vr_active=0`. È l'ultimo stato ricevuto,
  non una misura F0 sincronizzata a ogni campione della rotazione.
- API ancora in tracking, posa valida, nessun `last_error`.

Resta la ripetizione guidata indossata per completare il confronto;
le ampiezze/velocità manuali non sono riferimenti calibrati e non consentono
di certificare identica sensibilità nelle due condizioni.

## Presenza — ripetizione indossata e confronto qualitativo PASS

- Ripetizione guidata confermata dall'utente; stesso CSV.
- Recenter count 7 a 415,257082 s. Finestra analizzata
  423,916087–436,407111 s: `sensor_worn=1`, IR raw 1023,
  tracking e posa validi nei campioni letti.
- Yaw minimo −46,795397° a 428,542100 s; gyro Y −0,3024 rad/s
  a 427,107097 s e +0,71715 rad/s a 431,585101 s.
- Ritorno attraverso yaw zero a 433,322100 s; dopo circa 3 s,
  a 436,407111 s: yaw +3,397466°, pitch −4,234695°, roll +1,643206°.
- Confronto qualitativo completato: movimento gyro e orientamento presenti
  sia non indossato sia indossato, con flag di presenza distinti. Non osservata
  soppressione totale del gyro nel caso non indossato. Velocità e ampiezze
  manuali diverse non permettono un confronto quantitativo di sensibilità.
- L'ultimo F0 letto dopo la prova indica `worn=false`, coerente con il
  successivo stato sensore non indossato; non viene usato come prova F0
  contemporanea alla rotazione indossata.

Chiusi i gate delle tre prove guidate assi e del confronto qualitativo
di presenza. Restano da verificare/documentare gli ulteriori gate hardware
(recenter yaw, reale scollegamento durante tracking, ripetizioni termiche
secondo la procedura IMU); M3 resta IN_PROGRESS.

## Scollegamento USB reale durante tracking — PASS invalidazione

- Dopo riapertura GUI PID 9908, capture nativa PID 17960;
  log/CSV `logs/psvr_diag_20261004T022401.970Z_17960`.
- Prima dello scollegamento: API `running=true`, `phase=tracking`,
  `pose_valid=true`, `fresh=true`, nessun errore di calibrazione.
- Scollegamento fisico confermato dall'utente. Alle 02:24:41.509Z il log
  registra `IMU HID read failed`; handles chiusi e shutdown exit 3.
- Capture fino alla perdita: 39469 report, 78938 campioni, 62538 integrazioni,
  31,269039 s di tracking; 2 rollover, zero gap/duplicati/malformati/
  discontinuità di sequenza prima della perdita.
- Dopo la perdita: GUI/API ancora rispondente, `phase=tracking-lost`,
  `running=false`, `busy=false`, `worker_pid=null`, `pose_valid=false`,
  `fresh=false`, `latest_pose=null`, EOF 1. Nessuna posa obsoleta valida.
- Il messaggio `Worker terminato con codice 3` è l'esito della perdita I/O;
  la verifica PASS riguarda rilevamento e invalidazione, non una capture exit 0.

Ripartenza dopo ricollegamento ancora da verificare. Recenter yaw e
ripetizioni termiche restano pendenti; M3 resta IN_PROGRESS.

## Ripartenza dopo ricollegamento USB — PASS

- Ricollegamento e Start confermati dall'utente, stessa GUI PID 9908;
  nuovo worker PID 22512, log/CSV
  `logs/psvr_diag_20261004T022605.447Z_22512`.
- API `running=true`, `phase=tracking`, `pose_valid=true`, `fresh=true`,
  `last_error=null`, `last_calibration_error=null`.
- Nuova calibrazione 16000 campioni / 8000,010 ms; gyro STD
  (0,553360; 0,578448; 0,622185) dps, half-mean shift
  (0,007940; 0,000272; 0,010657) dps, entro i gate.
- Due letture API confermano progressione: 26,584533 s / 26585 report,
  poi 43,355555 s / 43356 report, posa valida e fresca in entrambe.
  Frequenza alla prima lettura circa 1000 report/s e 2000 campioni/s.
- Ripartenza manuale con Start verificata; non è un test di auto-reconnect.

Restano recenter yaw e ripetizioni termiche; M3 resta IN_PROGRESS.
