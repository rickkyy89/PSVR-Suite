# Diagnosi USB/HID

## PSVR non rilevato — exit 2

Controllare alimentatore/processor unit, cavo USB dati e porta. HDMI non
determina presenza USB. Rilevare dispositivi presenti con:

```powershell
Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_054C&PID_09AF' } | Format-List Status,Class,FriendlyName,InstanceId
```

Usare `psvr_diag --enumerate-only` per inventario senza aprire 4/5.

## USB presente ma interfacce richieste assenti — exit 3

Leggere service/problem code PnP e path nel log. **MI_04 deve essere HID**;
**MI_05 può essere HID per capture passiva oppure WinUSB per M2**.
HIDAPI non enumera le interfacce WinUSB/libusbK. Per MI_04 legata al driver
sbagliato ripristinare HID tramite Gestione dispositivi, documentando il driver
precedente. Per controllo M2 verificare WinUSB su MI_05. Non cambiare il driver
del dispositivo USB composito o quello audio. Nessuna installazione automatica.

## Open/lettura fallisce

Il log HIDAPI riporta l'errore Windows. Chiudere altri software PSVR;
verificare il path/MI selezionato, permessi, disconnessione. Non è necessario
eseguire automaticamente come amministratore. Più visori/interfacce duplicati
sono considerati ambigui: collegare un solo processor unit nella prima fase.

## Interfacce aperte ma zero report — exit 4

È diverso da un errore di open. Accendere tramite pulsante fisico,
controllare collegamenti del visore, ripetere 5 secondi, confrontare log.
Il controllo può restare silenzioso senza comandi; sul visore di questa sessione
il descriptor ha input=0, output=0, feature=5 e il tool evita la lettura input.
Questi dati NON autorizzano a provare feature report inventati. M2 ha verificato
**WinUSB sulla sola MI_05** per i comandi documentati; MI_04 rimane HID.
Vedere `docs/WINUSB_CONTROL.md`.
Non aggiungere comandi USB casuali per far apparire dati.

## Audio/H.264/BulkIn in errore

Sono altre interfacce del composite device. M1 riporta service/problem code,
ma non le apre. Il loro errore non implica fallimento sensori/controllo.
Diagnosticarle separatamente solo se diventeranno necessarie.

## Audio muto in SteamVR (modalità VR) — cuffia OK in cinema

Sessione utente validata: in modalità cinema l'audio esce dalla cuffia del
processor unit (stream HDMI dal PC). In modalità VR/SteamVR la cuffia è
risultata muta. Il routing hardware/firmware del PU è una possibile causa,
da distinguere dall'endpoint scelto da SteamVR e dal routing dell'applicazione.
Il protocollo di controllo documentato (power
0x17, mode 0x23, LED 0x15) **non ha alcun comando audio/uscita cuffia**:
nessun ripristino dell'audio al jack del visore in VR è stato implementato
o validato in questa modifica.

Diagnosi in sola lettura, senza toccare SteamVR o i driver:

```powershell
.venv-camera\Scripts\python.exe -m tools.psvr_audio_diag          # live
.venv-camera\Scripts\python.exe -m tools.psvr_audio_diag --offline # soli fatti/guida
```

Il tool usa esclusivamente l'osservazione passiva `psvr_diag --status`
(F0; nessun comando power/mode/LED) e query PnP/CIM in lettura.
F0 assente ⇒ `pu_mode: unknown`, mai dedotto. Nota importante: l'interfaccia
USB **MI_01 "PS VR Audio"** (usbaudio) **non equivale** al 3D audio
proprietario: selezionarla come periferica di riproduzione è al più un
percorso stereo non ancora validato. L'inventario PnP del tool non determina
l'endpoint di default né il routing per-app: verificarli nelle impostazioni
audio SteamVR e con `mmsys.cpl` / mixer volume. Non cambiare il driver MI_01
(vincolo M2: audio invariato). Dettagli e coda di validazione hardware:
`docs/AUDIO_TRACKING_CAMERA_20261015.md`.

**Soluzione pratica (workaround, non fix):** poiché il jack cuffia del PU
può essere muto in VR e non esiste alcun comando host documentato per
cambiare il routing interno del PU, provare una **cuffia
separata collegata al PC** (USB o jack 3,5 mm) impostata come dispositivo
audio di SteamVR: l'audio passa dal PC, non dal PU. È un aggiramento
dell'operatore, non un ripristino dell'uscita cuffia del visore e non è
validato come fix.

## Videocamera 6DoF: scegliere la sorgente non sostituisce la calibrazione

Nella scheda **Camera**, la tendina **Videocamera 6DoF** mostra PS3 Eye,
OpenCV DirectShow, OpenCV MSMF, OpenCV (auto) e **PS4 Camera stereo**. Il valore
`camera_source` sceglie **quale camera il worker apre** lato host; non scrive in
`steamvr.vrsettings` e **non calibra**. Gli intrinseci si caricano da
`intrinsicsPath` e il worker rifiuta frame con risoluzione diversa da
quella calibrata (`"live camera resolution differs from calibration"`).

I profili restano separati. PS3 Eye usa
`configurations/pseye_blue_640x480_calibrated.json`; PS4 Camera stereo richiede
la preparazione UVC esterna e usa `configurations/ps4_stereo_640x400_suite.json`
insieme ai controlli di `configurations/ps4_suite_source.json` (exposure −7,
gain 8, brightness 1, Manual). Il pannello PS4 separato serve per acquisizione e
calibrazione; il tracking 6DoF si sceglie e si applica dalla scheda Camera della
Suite. Vedi [guida PS4](docs/PS4_CAMERA.md) e
[diagnosi front-five e limiti](docs/PS4_FRONT5_DIAGNOSTICS_20261101.md).

## Tracking che degrada dopo diversi minuti

La prossima release include una correzione della mappatura temporale IMU:
finestra mobile di 2 s al posto del minimo dell'intera sessione, timestamp
pubblicati immutabili e aumento dell'offset limitato per non rendere freschi
campioni arretrati dopo una pausa di trasporto. I test sintetici coprono
sessioni ininterrotte di 20 minuti; non dimostrano la causa del problema reale.

Ripetere una sessione 6DoF di 20–30 minuti. Se ricompaiono gli scatti,
conservare il JSONL del tracking prima di riavviare: età IMU, intervalli camera
e motivi di perdita/rifiuto consentono di distinguere clock, cattura e
associazione ottica. Il collaudo hardware lungo resta aperto.

## Build e log

La prima configurazione richiede Git/rete verso GitHub; proxy/certificati
sono indicati nell'errore FetchContent. CMake cache di una toolchain non è
compatibile con un'altra: usare le directory previste, non cache upstream.
Log non scrivibile → exit 1; usare `--log-dir` con directory scrivibile.
I log contengono path USB e possibili seriali: per un bug pubblico allegare
una copia con gli identificativi personali eventualmente oscurati.

## Comando M2 termina 5 o 6

**5:** controllo ancora HidUsb/altro driver; il comando non è stato inviato.
Verificare MI_05/service con inventario o `--enumerate-only`.
**6:** distinguere `command transfer OK`, risposta A0 e nuovo F0. ACK=0
non prova il cambio di stato. F0 è event-driven, non periodico. In VR/cinema
i due flag possono essere incoerenti per ~1 s durante la transizione; il tool
attende che concordino e che il visore sia acceso. Power-on ha richiesto ~2 s;
se la transizione è ancora in corso aumentare `--control-timeout-ms 5000`.
Se l'azione era già soddisfatta può non arrivare un nuovo F0. `--status` senza
eventi termina con stato sconosciuto, non con errore del driver.

WinUSB open fallito: codice Windows e service/INF/GUID della sola MI_05.
Il tool cerca il GUID per l'esatto instance ID, verifica interfaccia 5 e legge
i pipe dal descriptor, senza path/endpoint macchina-specifici hardcoded.

SteamVR non è coinvolto in M1/M2. La futura diagnosi di mancato HMD partirà
da factory/init/registration in `vrserver.txt`, poi presentazione in
`vrcompositor.txt`; non da tentativi di modifica indiscriminata dei settings.

## M3 — drift, recenter, prossimità, assi

- **Drift da cattivo bias ≠ recenter.** Recenter imposta solo il riferimento
  posa (full/yaw), non ricalibra il bias gyro. Se tutto deriva su tavolo dopo
  cal in movimento, rifare cal 8 s tavolo hands-off, non premere solo Ricentra.
  Caso visto: cal 2 s in movimento ⇒ residuo Y +0.648 dps ≈38.88 dpm.
- **Statico falso / prossimità non provata.** `worn` = bit0 byte 8, IR = LE16
  byte 55, bit1 display solo RAW. Worn true IR 1023 vs unworn false IR 183 visti,
  ma prossimità come CAUSA di motion mancante non dimostrata: confrontare
  indossato/non con flag raw + F0, senza inferire causa dai soli flag.
- **Etichette assi.** RHand +X destra +Y up −Z forward: yaw-destra negativo,
  pitch-su positivo, roll-destra negativo; grafici yaw(Y) rosso, pitch(X) verde,
  roll(Z) blu. Non dichiarare inversioni risolte senza test fisico misurato
  (≥10° primaria). Exit 7 = cal/qualità/capture incompleta.
