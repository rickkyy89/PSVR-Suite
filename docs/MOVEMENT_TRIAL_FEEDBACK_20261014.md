# PSVR — feedback reale del test di movimento, 2026-10-14

## Decisione

**Kalman ottimizzato non accettato per l'uso reale. One Euro 2,5 Hz/β8 resta la
baseline e il riferimento operativo.** Il vantaggio statico misurato il giorno
precedente non compensa la perdita di continuità osservata durante l'uso.

Feedback dell'utente, verbatim:

> one euro decisamente migliore come solidità di tracking, quando scatta,
> scatta più violentemente, ma il tracking non viene perso quasi mai

Interpretazione: priorità alla disponibilità e solidità del tracking, mantenendo
separato il problema dei rari salti più grandi. Non rendere valido un dato
invalido per nascondere un flash; non promuovere il Kalman sulla sola riduzione
dei picchi o sulla quiete frontale.

## Prova e condizioni

- SteamVR reale, GUI camera, preset/rig/calibrazione invariati; confronto manuale
  selezionando `kalman` e `one_euro` nella stessa GUI.
- Primo startup: calibrazione IMU fallita per variazione del giroscopio negli
  8 secondi iniziali. Successiva riattivazione normale; errore registrato,
  non attribuito al filtro posizione.
- Modalità cinema segnalata dall'utente e corretta esplicitamente. Comando
  `--vr-mode`: A0 accepted e **F0 fresca `power=on,mode=vr,vr_active=1`**, exit 0.
  Driver invariato/passivo: non effettua automaticamente questo comando.
- Registrazioni: `logs/movement_camera_20261014_0034.jsonl`, telemetry `_0034`,
  `_0040`, `movement_telemetry_vr_20261014_0043.jsonl`. Quest'ultima ha durata
  limitata; la camera resta aperta finché l'utente chiude la GUI.
- Nessun intervento sul filtro, reset, ricalibrazione, configurazioni o
  applicazioni durante l'analisi del presente feedback. Test live lasciato
  nello stato scelto dall'utente, non dedotto dagli argomenti CLI iniziali.

## Riscontri iniziali dai log

Snapshot read-only del log ancora aperto:
[movement_feedback_20261014_0052_segmented.json](../logs/movement_feedback_20261014_0052_segmented.json).
Include solo righe complete entro la dimensione del file osservata all'apertura.
Esclusi startup/IMU non valida/allineamento assente dalla tabella seguente.
**Movimenti e intervalli differenti: non è un A/B matched, né una misura di lag.**

| Misura | Kalman ottimizzato live | One Euro live |
|---|---:|---:|
| Osservazioni con posa ottica valida | 25.817 | 12.139 |
| Ottica valida ma output non pubblicato valido | **1.856 (7,19%)** | **0** |
| Passo massimo contiguo pubblicato | 24,77 mm | 31,16 mm |
| Passo p99 | 9,65 mm | 8,67 mm |
| Intervallo invalido completato massimo | 5,09 s | 0,240 s |

Non confrontare le frazioni sull'intero gruppo: alcuni intervalli One Euro hanno
il visore fuori dalla vista della camera. La condizione informativa è **ottica
valida → output del filtro**, che conferma invalidazioni aggiuntive del Kalman.
Validità pubblicata non equivale a validità OpenVR, dove interviene il grace.
Passi e gap non vengono concatenati attraverso switch, reset, epoche o sessioni.
Due test automatici verificano separazione delle perdite ottiche dagli scarti e
lettura di snapshot parziali senza inventare gap fra filtri inattivi: **2 PASS**.

Distribuzione delle 1.856 pose ottiche valide trattenute dal Kalman:

| LED associati | Trattenute | Ottiche valide nel gruppo |
|---|---:|---:|
| 2 | **545** | 545 |
| 3 | **782** | 2.670 |
| 4 | **508** | 5.185 |
| 5 | 21 | 17.130 |

Diagnostica Kalman: 1.280 frame `acquiring`, 280 `predicting`, 573 `lost`,
30 rifiuti `posterior_position_step`, 1 `implied_speed`; 545 misure con
`insufficient_leds`. Le cifre non dimostrano che tutto dipenda dal Mahalanobis:
tracking parziale, requisiti di affidabilità per il riaggancio e policy di
pubblicazione `driver_hold` sono candidati importanti.

## Priorità della prossima revisione

1. **Continuità prima del solo jitter:** contare separatamente ottica persa,
   aggiornamento non osservabile, gate, riacquisizione e IMU indisponibile.
2. Rivalutare i casi a 2/3 LED dai Jacobiani/condizionamento e dal prior IMU:
   una misura a bassa confidenza non deve diventare ad alta confidenza, ma il
   rifiuto assoluto di tutte le pose a due LED può eliminare dati utili. Nessuna
   deroga ad ambiguità o invalidità ottica senza evidenza.
3. Verificare riaggancio con tre LED e misure a quattro LED marginali: non
   confondere un processo di acquisizione prolungato con un outlier isolato.
4. Isolare effetto della policy `driver_hold` e budget camera/driver, senza
   reintrodurre occultamente un hold indefinito o il cumulo 80+80 ms.
5. Conservare il vantaggio sugli outlier, ma misurare contemporaneamente
   disponibilità, durata delle invalidità, falsi rifiuti, lag e salti al riaggancio.

La nuova revisione dovrà superare il confronto fisico con One Euro; nessuna
modifica automatica al test in corso è stata applicata in risposta al feedback.
Le evidenze statiche restano valide nel loro ambito, **non accettazione dinamica**.
