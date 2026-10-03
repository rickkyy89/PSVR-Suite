# Collegamento e verifica milestone 1/2

## Collegare PSVR1

1. Usare il **processor unit Sony** del PSVR1 con il suo alimentatore.
2. Collegare i cavi del visore alle porte anteriori del processor unit,
   utilizzando l'eventuale prolunga/adattatore originale della propria revisione.
3. Collegare la porta micro-USB posteriore del processor unit a una porta USB
   del PC con un cavo **dati**, preferibilmente senza hub per la prima prova.
4. Per M1 HDMI non è necessario. Se già collegato, GPU HDMI OUT → porta
   **HDMI PS4** del processor unit; la porta **HDMI TV** è uscita opzionale verso
   TV/monitor, non ingresso dal PC. Non collegare il visore direttamente alla GPU.
5. M1 originaria: HID su 4/5. Da M2: **HID su 4, WinUSB su 5**
   ([procedura](docs/WINUSB_CONTROL.md)). Il tool supporta entrambe le combinazioni
   per capture passiva; comandi M2 richiedono WinUSB. Chiudere Trinus/iVRy,
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

I metadati PnP/HID completi precedono il riepilogo. Il controllo viene aperto;
se il descriptor espone input, viene letto passivamente con timeout. Sul PSVR
testato **non espone input**, quindi la lettura viene saltata e loggata.
Zero report controllo non è errore. Il tool non afferma
che power/mode siano funzionanti. Ogni log ha nome unico timestamp/PID.

Exit code passivo: **0** report sensore 64 byte + sensori 4/controllo 5 aperti; **2** PSVR assente;
**3** interfacce mancanti/open/lettura fallita o più PSVR ambigui;
**4** interfacce aperte ma nessun report sensore valido; **1** configurazione,
CLI, inizializzazione, enumerazione o log falliti; **130** Ctrl+C.
`--enumerate-only` termina
0 se PSVR presente, 2 se assente, e non prova l'apertura/lettura.

## Checklist hardware

- [x] Eseguire collegato e conservare log, exit code e output console (sessione 2026-10-03).
- [x] Scollegare **solo USB**, eseguire: deve terminare 2 senza crash (verificato).
- [x] Ricollegare USB, ripetere: apertura/lettura devono riprendersi (verificato).
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

## Milestone 2 — comandi e verifica

La sessione M2 è stata autorizzata dopo M1. Con WinUSB solo su MI_05:

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --control-info
.\out\diag\psvr_diag.exe --config .\config.json --power-off
.\out\diag\psvr_diag.exe --config .\config.json --power-on
.\out\diag\psvr_diag.exe --config .\config.json --vr-mode
.\out\diag\psvr_diag.exe --config .\config.json --cinema-mode
```

Un comando alla volta, controllando exit code/log. L'apertura non invia comandi.
Power-on non abilita automaticamente VR; mode non accende il visore.
Per la prima sequenza partire dal visore acceso, spegnerlo, accenderlo e
cambiare modalità: queste transizioni sono state verificate sul PC reale.

Output atteso dopo un cambio VR:

```text
command requested action=vr-mode full_usb_packet=23 00 aa 04 01 00 00 00
command transfer OK bytes=8 state_verified=false (waiting for fresh F0 status)
command response result_code=0 (0=accepted; not a display-state verification)
control status power=on mode=vr ... vr_active=1 ...
MILESTONE 2 action verified: vr-mode (accepted A0 response + fresh F0 state after successful transfer)
shutdown exit_code=0
```

Controlli reali: power-off mask 0x64; power-on 0x15; VR 0x51/active=1;
cinema 0x55/active=0. Altri bit cambiano con cuffie/mute/prossimità:
verificare bit documentati, non l'uguaglianza del mask intero.

**5** = WinUSB MI_05 necessario, comando non inviato. **6** = ACK/stato non
confermati entro `control_timeout_ms` (default 3000, override CLI fino a 15000).
**3** = errore di interfaccia/I/O o risposta A0 negativa. Nessun retry automatico.

`--status --control-timeout-ms 10000` aspetta passivamente un evento F0;
indossare/togliere il visore durante l'osservazione può generarne uno.
Senza eventi ritorna 6 con stato sconosciuto. Un comando già soddisfatto può
restituire ACK senza nuovo F0: non è successo di verifica dello stato.
`--power-on --dry-run` stampa il pacchetto senza aprire USB. Azioni multiple
o abbinate a `--enumerate-only` sono rifiutate prima dell'hardware.

CTest ora esegue **9 test**: aggiunti wire packets, F0 reale/malformato, A0,
criterio ACK+stato, conflitti CLI e dry-run senza USB.

- [x] Cambio driver limitato a MI_05, controllato via PnP.
- [x] Power-off/on e VR/cinema con ACK e F0 dopo il comando.
- [x] Capture passiva dopo i cambi: 2002 report validi in 2 s.
- [ ] Ripristino fisico MI_05 a HID (procedura documentata).

**Fermarsi dopo M2.** Prossima sessione: M3 IMU/orientamento e recenter.

## Milestone 3 — IMU/fusion + monitor (IN_PROGRESS, rinvio docs)

M3 non è completa. Procedure, assi, gate e comandi non duplicati qui;
riferimenti canonici (altro agent):

- [docs/IMU_TESTING.md](IMU_TESTING.md) — capture `--imu`, `--axis-test`,
  `--stationary-test`, `--json-stream`, IPC `--recenter/--recalibrate/--stop-imu PID.
- [docs/MONITOR_APP.md](MONITOR_APP.md) — GUI, API readonly, grafici/state.
- [docs/MILESTONE_3_REPORT.md](MILESTONE_3_REPORT.md) — file/comandi/risultati/
  fallimenti/fix/gate di questo agent.

Sintesi: cal 8 s tavolo hands-off dopo 200 ms; build MinGW 14/14 CTest;
statico 60 s originale PASS pre-gate; statico post-fix 42000 ms PENDING.
