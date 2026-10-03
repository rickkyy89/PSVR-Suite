# Diagnosi USB/HID

## PSVR non rilevato — exit 2

Controllare alimentatore/processor unit, cavo USB dati e porta. HDMI non
determina presenza USB. Rilevare dispositivi presenti con:

```powershell
Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_054C&PID_09AF' } | Format-List Status,Class,FriendlyName,InstanceId
```

Usare `psvr_diag --enumerate-only` per inventario senza aprire 4/5.

## USB presente ma HID 4/5 assenti — exit 3

Leggere i service/problem code PnP e i path nel log. Se le interfacce sono
legate a WinUSB/libusbK da un vecchio programma, HIDAPI non le enumera.
Ripristinare per quelle sole interfacce il driver HID compatibile tramite
Gestione dispositivi, documentando driver precedente. Non cambiare il driver
del dispositivo USB composito o quello audio. Nessuna installazione automatica.

## Open/lettura fallisce

Il log HIDAPI riporta l'errore Windows. Chiudere altri software PSVR;
verificare il path/MI selezionato, permessi, disconnessione. Non è necessario
eseguire automaticamente come amministratore. Più visori/interfacce duplicati
sono considerati ambigui: collegare un solo processor unit nella prima fase.

## Interfacce aperte ma zero report — exit 4

È diverso da un errore di open. Accendere tramite pulsante fisico,
controllare collegamenti del visore, ripetere 5 secondi, confrontare log.
Il controllo può restare silenzioso senza comandi: è atteso in M1.
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

SteamVR non è coinvolto in M1. La futura diagnosi di mancato HMD partirà
da factory/init/registration in `vrserver.txt`, poi presentazione in
`vrcompositor.txt`; non da tentativi di modifica indiscriminata dei settings.
