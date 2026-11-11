# Raffica di gray screen a fine prova: blocco della soglia — 2026-10-15

## Esito hardware

La prova `ac_evo_coherent_20261015` è **fallita**, come riferito dall'utente:
salti iniziali, alcuni giri tranquilli nella parte centrale, poi gray screen
continui. Il miglioramento del replay precedente non costituisce accettazione.

Sorgente live: `logs/suite_tracking_20261015T185925720173Z_6a08be9b.jsonl`.
67.994 frame, 935,129 s; zero errori di acquisizione e IMU valida in ogni frame.

## Causa identificata

La variante `coherent_imu_led_v1` confrontava l'innovazione ottica totale con
una soglia di 10 gradi rispetto all'allineamento di attivazione. Nel corso della
sessione, la correzione ottica accumulata si avvicina a quella soglia. Il
frontend continua a riconoscere cinque LED, ma il gate rifiuta le pose prima
di poter aggiornare la correzione, producendo `front_orientation_conflict`.
Lo stesso gate diventa quindi un blocco che si auto-mantiene.

Questo è un errore della correzione software introdotta. Il log dimostra il
meccanismo del rifiuto; non attribuisce da solo tutta l'incoerenza accumulata
al sensore IMU anziché ad allineamento/modello ottico.

| Minuto dalla partenza tracking | Pose ottiche valide / frame | Conflitti orientamento |
|---|---:|---:|
| 2–11 | Tutti i frame di ciascun minuto | 0 |
| 12 | 4.310 / 4.500 | 190 |
| 13 | 3.304 / 4.491 | 1.093 |
| 14 | 2.144 / 4.435 | 2.275 |
| 15, parziale | 425 / 2.565 | 761 |

I 4.319 conflitti coincidono con la fase del peggioramento riferito. Il driver
registra 208 transizioni `position_missing` nella raccolta. Non sono conteggi
intercambiabili con i singoli gray screen visti dall'utente. La coda finale
contiene anche perdita di tutte le luci e va distinta dal blocco con cinque LED.

## Correzione v2

Il gate controlla ora l'innovazione **rispetto alla correzione ottica corrente**:
`target @ optical_correction.T`, non l'angolo totale di `target` rispetto
all'attivazione. La correzione può così seguire un disaccordo graduale anche
oltre dieci gradi accumulati, mantenendo velocità massima e riferimento world.
Un nuovo salto di orientamento oltre dieci gradi continua a essere rifiutato.

Le righe di log identificano `coherent_imu_led_v2` e
`innovation_reference: current_optical_correction`. Serve riaprire la suite
per caricare il codice aggiornato; un processo già aperto conserva la v1.

## Verifica sulla registrazione appena fallita

Report: `logs/ac_evo_coherent_20261015/relative-innovation-replay.json`.

| Metrica | Live v1 registrato | Replay v2 |
|---|---:|---:|
| `front_orientation_conflict` | 4.319 | 13 |
| Frame invalidi nella finestra comune prima/ultima posa valida originale | 4.580 | 241 |
| Episodi invalidi nella stessa finestra | 592 | 34 |
| Massimo gap completato dopo primo aggancio | 4,185 s | 1,066 s |

Il replay usa candidati già rilevati, non il feedback ROI su immagini live;
non dimostra scomparsa dei gray screen nelle lenti. I salti iniziali persistono:
il massimo salto pubblicato sul nuovo log passa soltanto da 63,45 a 62,63 mm,
e il percentile 99 passa da 3,60 a 3,66 mm. La v2 corregge il blocco progressivo
della soglia, **non è una soluzione completa della stabilità iniziale**.

Test di regressione: disaccordo crescente fino a 18 gradi con tracking valido
e allineamento invariato, seguito da un nuovo salto a 45 gradi che deve essere
rifiutato senza avvelenare il prior. I test correlati continuano a coprire
sessioni, recenter, stale IMU, perdite, pattern parziali e integrazione suite.

Verifica: 229 test e 217 sottotest passati sui moduli correlati; dopo
l'aggiornamento dell'identificatore diagnostico, 33 test mirati nuovamente
passati. Anche il replay completo della prima prova è stato ripetuto:
risultati numerici invariati rispetto alla variante coerente precedente.

Diagnosi per minuto e motivi driver:
`logs/ac_evo_coherent_20261015/failure-diagnosis.json`.
