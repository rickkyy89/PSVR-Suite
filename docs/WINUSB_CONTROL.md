# M2 — WinUSB soltanto su PSVR controllo MI_05

L'utente ha autorizzato questo cambio nella sessione M2. Inventario precedente:
MI_05 `USB\VID_054C&PID_09AF&MI_05\6&63F9D44&0&0005`, service **HidUsb**,
INF **input.inf**, provider **Microsoft**, versione **10.0.26100.9444**.
MI_04 sensori deve restare **HidUsb**; composite **usbccgp** e audio invariati.
`scripts/snapshot_driver.ps1` salva l'inventario completo in JSON in `logs/`.
Non è un export del pacchetto: input.inf è il driver inbox Windows, già nel sistema.

## Cambio manuale con Zadig

1. Chiudere programmi PSVR e il diagnostico; lasciare processor unit alimentato
   e USB collegato. Eseguire lo snapshot prima di cambiare il driver.
2. Scaricare [Zadig 2.9](https://github.com/pbatard/libwdi/releases/download/v1.5.1/zadig-2.9.exe)
   dal [sito ufficiale](https://zadig.akeo.ie/). Firma attesa **Akeo Consulting**.
3. Aprire Zadig e scegliere **Options → List All Devices**.
4. Selezionare **PS VR Control (Interface 5)**. Nome Windows attuale può essere
   "Dispositivo di input USB (Interface 5)": controllare il campo **USB ID**,
   che deve indicare **054C : 09AF : 05**, o hardware ID con **MI_05**.
5. Il driver corrente deve essere HidUsb; destinazione **WinUSB**.
6. Usare **Replace Driver**, accettare UAC, attendere il risultato dell'installazione.
7. Confermare che MI_05 è WinUSB e MI_04 è ancora HidUsb. Il tool non cambia driver.

Non selezionare il composite device senza numero interfaccia, sensori 4,
audio 0/1, H.264 6, BulkIn 7 o Control2 8. Non usare libusbK/libusb-win32:
il backend di questo progetto usa la API WinUSB nativa.

## Ripristino HID (MI_05)

Gestione dispositivi → individuare MI_05 tramite Proprietà → Dettagli →
ID hardware `USB\VID_054C&PID_09AF&MI_05` → Aggiorna driver → Cerca i driver
nel computer → Scegli da un elenco → scegliere il driver Microsoft compatibile
**Dispositivo di input USB / HID**. Se necessario usare Disco driver con
`C:\Windows\INF\input.inf`. Verificare poi service **HidUsb**, INF **input.inf**.

Se il pacchetto Zadig continua ad avere precedenza, disinstallare il solo
dispositivo MI_05 con rimozione del suo pacchetto Zadig e ricollegare USB;
non rimuovere pacchetti di altre periferiche. Conservare l'inventario e il nome
oem*.inf WinUSB prima della rimozione. Questa operazione è manuale.

Il binario M1 al commit e3ce22e richiede HID su 4/5; il diagnostico M2 mantiene
la lettura passiva dei sensori anche con controllo WinUSB. Per tornare al vecchio
binario M1 ripristinare MI_05 a HID.
