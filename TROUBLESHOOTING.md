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
