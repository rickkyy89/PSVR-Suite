# Collegamento e verifica milestone 1

## Collegare PSVR1

1. Usare il **processor unit Sony** del PSVR1 con il suo alimentatore.
2. Collegare i cavi del visore alle porte anteriori del processor unit,
   utilizzando l'eventuale prolunga/adattatore originale della propria revisione.
3. Collegare la porta micro-USB posteriore del processor unit a una porta USB
   del PC con un cavo **dati**, preferibilmente senza hub per la prima prova.
4. Per M1 HDMI non è necessario. Se già collegato, GPU HDMI OUT → porta
   **HDMI PS4** del processor unit; la porta **HDMI TV** è uscita opzionale verso
   TV/monitor, non ingresso dal PC. Non collegare il visore direttamente alla GPU.
5. Lasciare i driver HID Windows sulle interfacce 4 e 5. Chiudere Trinus/iVRy,
   altri tool PSVR e SteamVR per evitare letture concorrenti.
6. Se il visore è in standby, provare prima il diagnostico; se non arrivano
   report, accenderlo con il suo pulsante fisico e ripetere. Il tool M1 non
   invia power-on/VR mode e non richiede PS4 o PS Camera.

Identificazione verificata dal sorgente: Sony VID **054C**, PID **09AF**,
MI_04 sensori, MI_05 controllo. Non distingue con certezza CUH-ZVR1/ZVR2
dal solo VID/PID: stampare stringa prodotto e USB release, non inventare modello.

## Esecuzione

Dopo la build, dalla root:

```powershell
.\out\diag\psvr_diag.exe --config .\config.json
$LASTEXITCODE
```

Per una capture più lunga e raw limitati:

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --duration-ms 5000 --raw-packets 3
```

Percorsi relativi di log risolti rispetto alla directory corrente. Il tool
senza `--config` usa default integrati, non carica silenziosamente altri file.
Configurazione/CLI non valida si ferma prima di aprire hardware.

## Output atteso (esempio, non risultato presunto)

```text
PSVR detected VID=054c PID=09af
USB interface OK (PnP enumeration; no raw USB claim)
sensor interface OK interface=4
control interface OK interface=5
sensor packet OK length=64 ...
sensor summary reports=... valid64=... report_hz=... samples_hz_estimate=...
control summary reports=0 ... (passive timeout is allowed)
MILESTONE 1 PASS ...
shutdown exit_code=0
```

I metadati PnP/HID completi precedono il riepilogo. Il controllo viene aperto
e letto passivamente con timeout: zero report non è errore. Il tool non afferma
che power/mode siano funzionanti. Ogni log ha nome unico timestamp/PID.

Exit code: **0** report sensore 64 byte + HID 4/5 aperte; **2** PSVR assente;
**3** interfacce mancanti/open/lettura fallita o più PSVR ambigui;
**4** interfacce aperte ma nessun report sensore valido; **1** configurazione,
CLI, inizializzazione, enumerazione o log falliti. `--enumerate-only` termina
0 se PSVR presente, 2 se assente, e non prova l'apertura/lettura.

## Checklist hardware

- [ ] Eseguire collegato e conservare log, exit code e output console.
- [ ] Scollegare **solo USB**, eseguire: deve terminare 2 senza crash.
- [ ] Ricollegare USB, ripetere: apertura/lettura devono riprendersi.
- [ ] Scollegare USB durante capture di 5 s: errore reale o fine capture con
  dato disponibile, nessun crash/blocco; non avanzare se shutdown è problematico.

Le prove che richiedono un gesto fisico devono essere annotate dall'utente.
I test software non sostituiscono scollegamento/replug reale.

## Test software

`build.ps1` esegue CTest. Test automatici: fixture binarie del decoder
(64 byte, due timestamp, int16 signed, sequenza, rifiuto lunghezze errate),
help, CLI sconosciuta/config mancante, configurazione non valida,
fixture di selezione interfacce (assenza, incompletezza, ambiguità).
Nessuno di questi test invia comandi o necessita SteamVR.

**Fermarsi dopo M1.** M2 richiede una nuova sessione autorizzata dopo
verifica del risultato hardware e dei limiti nel report.
