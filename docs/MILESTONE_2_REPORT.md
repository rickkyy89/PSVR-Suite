# Report milestone 2 — 2026-10-03

## Risultato: PASS hardware

Verificati sul PSVR reale **power-off, power-on, VR mode e cinema mode**:
pacchetto USB documentato trasferito per intero, risposta A0 con codice 0,
report F0 successivo con stato richiesto. Tutti i cambi terminati con **exit 0**.
Il visore è stato lasciato **acceso in cinema mode**, come a inizio prove.
Capture passiva sensori dopo i cambi: 2002 report da 64 byte validi in 2 s,
nessuno rifiutato, nessuna discontinuità. Nessuna milestone successiva avviata.

## Trasporto Windows e cambio driver

L'utente ha autorizzato WinUSB sulla sola interfaccia controllo 5 e ha eseguito
manualmente Replace Driver con Zadig. Verificato via PnP prima dei comandi:

| Nodo | Prima | Dopo |
|---|---|---|
| MI_04 sensori | HidUsb / input.inf | **HidUsb / input.inf** |
| MI_05 controllo | HidUsb / input.inf | **WinUSB / oem88.inf** |
| Composite | usbccgp / usb.inf | invariato |
| MI_01 audio | usbaudio / wdma_usb.inf | invariato |

Inventario originale: `logs/driver_snapshot_20261003T192252278_9956.json`
(nome in ora locale; log del diagnostico in UTC). Originale MI_05 provider
Microsoft, versione 10.0.26100.9444. Script corretto per PowerShell 5.1:
`$PSScriptRoot` non inizializzato nel default di un parametro, quindi il percorso
default è ora calcolato nel corpo. Inventario riuscito dopo una correzione;
non è stato esportato il driver inbox Windows come se fosse un pacchetto OEM.
Inventario dopo installazione conservato anche in
`logs/driver_snapshot_20261003T195124600_19020.json`.

Zadig 2.9 dal sito ufficiale, firma **Akeo Consulting**, stato Authenticode
**Valid**; SHA-256 osservato:
`4ecaa95df3da3621486a043aef8b3050b8bafe7c901402871e816229ef82039b`.
Procedura e ripristino in [WINUSB_CONTROL.md](WINUSB_CONTROL.md).
Il ripristino HID non è stato eseguito: il binding WinUSB resta utile al progetto.

## Backend e protocollo

WinUSB nativo (`WinUsb_Initialize/QueryInterfaceSettings/QueryPipe/ReadPipe/WritePipe`).
Nessuna libusb distribuita né modifica automatica driver. GUID/path ricavati
dal registro e SetupAPI per l'esatto instance ID PSVR MI_05; rifiuto di ambiguità.
Descriptor letto: interface=5, alternate=0, class=3, due endpoint:
**0x84 interrupt IN / 0x04 interrupt OUT**, max packet=64. Nessun cambio alternate.

Pacchetti completi, senza prefisso HID e senza troncamento a feature=5:

| Azione | Byte USB OUT |
|---|---|
| power-on | `17 00 aa 04 01 00 00 00` |
| power-off | `17 00 aa 04 00 00 00 00` |
| vr-mode | `23 00 aa 04 01 00 00 00` |
| cinema-mode | `23 00 aa 04 00 00 00 00` |

Riferimenti già analizzati: OpenHMD `85075b0`, OpenPSVR `cb86fd7`, libpsvr
`7858e9c`. Header gp_id=0 corrisponde a libpsvr; OpenHMD power-on usa 0x76,
ma non è necessario cambiare il formato del pacchetto per WinUSB.
Power-off spegne il **visore**, non il processor unit (nessun comando 0x13).
Mode non accende automaticamente il visore, power-on non forza VR.

### Correzione basata su evidenza, prima delle scritture

La prima osservazione passiva ha ricevuto:

```text
f0 00 aa 10 55 22 00 00 00 ff 00 00 02 00 00 00 00 00 00 00
```

F0 ha payload **16** (frame 20), mentre `libpsvr/Device_Status.h` rappresenta
solo i primi 8 byte. La prima implementazione rifiutava il frame e terminava 6,
senza scritture. Consultati sorgente e wiki di PSVRFramework:
`ccda7ca840f8f820197dd8571bbc869a02563ea4`, `PSVRFramework/PSVR.cs`,
[wiki F0](https://github.com/gusmanb/PSVRFramework/wiki/Report-0xF0---PSVR-Status)
e [formato/endpoint](https://github.com/gusmanb/PSVRFramework/wiki/PSVR-Control:--USB-Command-Response-Format).
Decoder corretto a payload16; fixture reale aggiunta ai test.
Nessun sorgente/binario PSVRFramework importato nel progetto.

Mask byte4: bit0 power, bit1 worn, bit2 cinema. Byte11: VR active (0 cinema,
1 VR, 3 sconosciuto). A0: 64 byte/payload60, command ID byte4 e result byte5.
0 accettato; una risposta negativa per il comando inviato termina 3.

Verifica finale: **write 8 byte + A0 corrispondente accettato + F0 nuovo coerente**,
indipendentemente dall'ordine A0/F0. Drenaggio passivo limitato prima della
scrittura evita di usare stati accodati precedenti. Timeout su tutte le I/O,
nessuna ripetizione automatica del comando, reset, firmware o feature report.

## Evidenza console/log

Log completi in `logs/` (esclusi da Git); estratti senza path macchina-specifici:

| Prova | Log | Stato finale / esito |
|---|---|---|
| Descriptor WinUSB | `psvr_diag_20261003T173219.300Z_8872.log` | pipe corretti, nessun comando, 0 |
| Primo F0 / decoder parziale | `psvr_diag_20261003T173226.443Z_15936.log` | payload16 rifiutato, 6, nessuna scrittura |
| Status senza nuovo evento | `psvr_diag_20261003T173528.505Z_20456.log` | stato sconosciuto, 6 |
| Power-off | `psvr_diag_20261003T173616.910Z_20408.log` | A0=0, power off, mask64, 0 |
| Power-on | `psvr_diag_20261003T173635.914Z_6180.log` | A0=0, power on, mask15, 0 |
| VR | `psvr_diag_20261003T173650.585Z_9296.log` | mask51 / vr_active1, 0 |
| Cinema | `psvr_diag_20261003T173701.381Z_11740.log` | mask55 / vr_active0, 0 |
| VR con gate ACK+stato finale | `psvr_diag_20261003T173927.393Z_23832.log` | A0 + F0 richiesti, 0 |
| Cinema con gate finale | `psvr_diag_20261003T173935.337Z_18868.log` | A0 + F0 richiesti, 0 |
| Regressione sensori M1 | `psvr_diag_20261003T173943.516Z_12852.log` | 2002/2002 validi, 0 |

Power-on osservato: prima F0 con power off (mask24), poi **power on mask15**
dopo ~2.03 s. Nessun successo dichiarato sul solo ACK ricevuto subito.
Power-off: nuovo F0 power off ~5 ms dopo il trasferimento.

VR, estratto della verifica finale:

```text
command requested action=vr-mode full_usb_packet=23 00 aa 04 01 00 00 00
command transfer OK bytes=8 state_verified=false (waiting for fresh F0 status)
command response result_code=0 (0=accepted; not a display-state verification)
control status power=on mode=cinema worn=false vr_active=1 status_mask=0x55
control status power=on mode=vr worn=false vr_active=1 status_mask=0x51
MILESTONE 2 action verified: vr-mode (accepted A0 response + fresh F0 state after successful transfer)
shutdown exit_code=0
```

Lo stato transitorio aggiorna VR active prima del bit cinema; il tool attende
che concordino (~1.09 s per VR, ~1.15 s per cinema nelle verifiche finali).
Capture sensori dopo cinema: 2002 validi, 0 rifiuti/discontinuità,
report_hz=1000.55, samples_hz_estimate=2001.10. Le ultime modifiche ai messaggi
di capture chiariscono che MI_05 è WinUSB, non una seconda HID.

## Build e test

Stessa toolchain M1: w64devkit x64 2.10.0, GCC 16.2.0, CMake 4.4.3, Ninja.
Build riuscite senza errori di compilazione; **9/9 CTest passati**.
Test aggiunti: pacchetti whitelisted, F0 reale/short/magic/length, VR unknown,
ACK da solo/F0 da solo non verificano un comando, A0, conflitti CLI,
azione con enumerate-only, dry-run senza USB. Eseguibile versionato 0.2.0
installato in `out/diag/psvr_diag.exe` con config e licenze.
VS 2022/Windows 10 non testati. Nessuna struttura SteamVR introdotta.

## Uso e limiti

```powershell
.\out\diag\psvr_diag.exe --config .\config.json --power-on
.\out\diag\psvr_diag.exe --config .\config.json --vr-mode
.\out\diag\psvr_diag.exe --config .\config.json --cinema-mode
.\out\diag\psvr_diag.exe --config .\config.json --power-off
```

Un comando per esecuzione, controllare `$LASTEXITCODE` dopo ciascuno. Stato
verificato=0; WinUSB necessario=5; stato sconosciuto/non verificato=6.
`--status` osserva solo nuovi eventi F0: può ritornare 6 se non cambiano
prossimità, volume, power o mode. Un comando già soddisfatto può produrre solo
ACK; non si inventa un comando query né si usa cache come prova di stato corrente.
`--control-info` descrittori soli; `--power-on --dry-run` audit byte senza hardware.

Non verificati: hot-unplug durante I/O, Ctrl+C, ripristino fisico driver HID,
visione/geometria/refresh HDMI, tracking/fusion, SteamVR. La verifica mode qui
è il protocollo/stato dichiarato dal dispositivo, non un test stereoscopico.
Prossima sessione **M3: IMU/orientamento 3DoF e recenter**, mantenendo la build
funzionante e senza iniziare SteamVR finché il tracking non è verificato.
