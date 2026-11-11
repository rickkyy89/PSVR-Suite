# Direct Mode nativa SteamVR/NVIDIA — esperimento non riuscito

## Aggiornamento successivo: HMD escluso dal desktop, presentazione ancora bloccata

Il resto del documento conserva le prime prove; lo stato più recente è qui.

- Analisi statica read-only di vrserver2.17.10 (SHA256
  `d8793f286620c0fddaf2cc8b6293c5a3fd1ae7a5c892a7f01450a59fdb2baf5b`):
  percorso NVIDIA di visibilità a RVA0xB31F0, wrapper0x1FB910, chiamata privata
  enable0x1AC2B1E7 con vendor e flags0. Enumerazione privata0x602F60D8,4 argomenti,
  record opachi16byte, modes0/1. Nessuna modifica del binario o injection.
  La classificazione Sony a RVA0x1FB6C0 ha un ramo che restituisce stato2
  anche con enumerazione vuota: successo della richiesta non prova un target.
- Probe diretto opzionale: tutti i modes0/1 danno status0/count0 sia per
  D94D sia per4DD9. Nella cattura prima dell'override,15 record/30s confermano
  Sony sul desktop,120Hz e, dopo8s, pose3/3 valide, fullscreenfalse. −202 già
  all'avvio: non sempre perdita successiva a un fullscreen iniziale valido.
- Sensore ripristinato tramite comando power-on accettato e F0 fresco:
 2002 pacchetti validi in2s (~1000Hz). Le prime sessioni avevano zero pacchetti;
  stati F0 accodati non erano prova sufficiente dello stato corrente.
- Trovata specifica Microsoft pubblica:
  https://learn.microsoft.com/en-us/windows-hardware/drivers/display/specialized-monitors-edid-extension
  VSDB CTA Microsoft CA125C/version2/uso VR7 identifica un HMD non-desktop.
  Un progetto PSVR pubblicato usa la stessa famiglia di blocco:
  https://github.com/Timocop/PSMoveServiceEx-Virtual-Device-Manager/issues/32
  Fonte consultata per verifica, nessun codice GPL o EDID completo importato.
- Implementazione indipendente minima `hmd_edid.py`: inserisce22byte nel
  blocco CTA1, conserva il blocco base intero, gli altri blocchi, identity,
  audio/video e timing esistenti; checksum ricalcolato. Container ID del
  monitor Windows reale.4 test sintetici PASS (incluse varianti/reiezioni).
- Override applicato con elevazione al solo
  `DISPLAY\SNY6A04\5&625d12c&1&UID4352`, valore EDID_OVERRIDE1; niente firmware,
  cambio VID/PID, ACL o modifica dei timing. Backup/restore con conflitti:
  `logs/hmd_edid_override_20261005.json`, **applied=true**.
- Dopo riavvio del monitor e startup, Sony è realmente assente dai percorsi
  Windows desktop attivi. **Prima parte dell'obiettivo raggiunta**.
  La lista Direct Mode NVIDIA resta vuota. `os_visible` NVAPI significa
  esposto all'OS, non necessariamente parte del desktop; non usarlo da solo.
- SteamVR continua a selezionare desktop e fallisce con422,
  `VRInitError_Compositor_InvalidOutputDesktop`, adapter output−1. Riprova
  con Sony già fuori desktop e proprietà `Prop_IsOnDesktop_Bool=false`
  **derivata dall'inventario reale**: stesso risultato. Nessun rendering
  diretto o120Hz diretto dimostrato. IMU torna tracking dopo calibrazione.
- Variante empirica pubblicata: v2 con bit5 uso non-Microsoft impostato
  (0x27 invece0x07; la specifica corrente lo riserva in v2).
  Preparato trial isolato `try_hmd_edid_compat.ps1`, ma la seconda elevazione
  non si è completata entro120s. Variante **NON applicata**: nessun nuovo
  piano compat presente; primo override rimane attivo.

### Stato macchina attuale e ripristino

SteamVR chiuso. Primo override HMD attivo, monitor principale LG60Hz attivo;
Sony fuori dal desktop ma nessuna immagine Direct Mode verificata.
Configurazione runtime sperimentale attiva, backup:
`logs/native_direct_restore_20261005c.json`.
NON riapplicare la baseline desktop senza prima ripristinare l'override HMD.

Da PowerShell amministratore, con SteamVR chiuso:
```powershell
.\scripts\hmd_edid_override.ps1 -Action Restore -PlanPath C:\Users\rickk\Desktop\PSVR\logs\hmd_edid_override_20261005.json
```
Poi restore selettivo runtime (nessuna elevazione aggiuntiva richiesta se scrivibile):
```powershell
.\scripts\native_direct_mode.ps1 -Action Restore -BackupPath C:\Users\rickk\Desktop\PSVR\logs\native_direct_restore_20261005c.json
```
Il ritorno di UID/coordinate/120Hz sul desktop va verificato, non presunto.

Prossimo passo pendente: conferma UAC per la variante isolata o ripristino.
Il software e il piano di backup sono pronti; non dichiarare risolto il −202.

Aggiornamento UAC/remoto: utente collegato tramite Google Remote Desktop.
Una elevazione successiva ha avviato il processo ma il trial si è fermato
perché Windows aveva memorizzato l'EDID applicato nella cache `EDID` del
monitor. Il controllo restore è stato corretto per ammettere solo quel
candidato esatto, mantenendo il controllo del valore override e degli altri
blocchi. Nuovo tentativo di elevazione scaduto a120s, nessun piano compat
creato: **primo override ancora applied=true, variante non applicata**.
Restore EDID corretto ancora da eseguire/verificare con elevazione.
Nessun riavvio Windows, riavvio GPU globale o modifica delle protezioni UAC.

---

## Obiettivo e architettura

Sottrarre il PSVR1 al desktop Windows e far presentare SteamVR direttamente
al display GPU. `IVRDisplayComponent` può essere usato dalla Direct Mode
nativa di SteamVR: non è necessario implementare `IVRDriverDirectModeComponent`
e un nuovo compositor per questa strada. Quest'ultima interfaccia è un
percorso diverso, per chi gestisce compositing/presentazione nel driver.

Fonti:
- https://github.com/ValveSoftware/openvr/blob/master/docs/Driver_API_Documentation.md
- https://github.com/ValveSoftware/openvr/issues/431#issuecomment-295967573
- https://developer.nvidia.com/vrworks (Direct Mode: HMD developer program)
- https://github.com/NVIDIA/nvapi/blob/main/nvapi.h
- https://github.com/NVIDIA/nvapi/blob/main/nvapi_interface.h

## Implementazione sperimentale

### Vincolo di sicurezza B9-01 (review 2026-10-16)

Il gate nativo di `matches_display_config` verifica solo contraddizioni di
identità/EDID: un inventario vuoto continua a passare. **Non è una verifica
fail-closed di assenza dal desktop**, né prova di scanout. Il test "expected
GPU hide" mantiene questa semantica; nessuna autorizzazione più permissiva
è stata introdotta.

La condizione richiesta è la congiunzione fra il gate esistente e una query
`desktop_membership()==false` attendibile. Tuttavia, il callback attuale in
`load_display_config` cerca solo target attivi con path esatto:
`enumerate_displays(false)` conserva anche target il cui
`DisplayConfigGetDeviceInfo(GET_TARGET_NAME)` fallisce o restituisce path vuoto.
L'errore diventa warning/path assente, non eccezione; il callback può quindi
restituire `false` per un display attivo non identificabile. Un AND immediato
non eliminerebbe questo caso fail-open. Le eccezioni delle query globali sono
distinte da tali errori parziali; il catch di `IsDisplayOnDesktop` copre solo
le prime, non le identità mancanti.

Per questo la logica di produzione resta invariata e **B9-01 resta aperto**.
L'helper separato `native_desktop_absence_verified` è testabile senza hardware:
rifiuta modalità non nativa, callback assente, risultato `true` ed eccezioni;
accetta `false` solo sotto il contratto del produttore. Non viene collegato al
gate attuale. Il contratto richiesto è: `false` significa assenza confermata
del path esatto; presenza o stato sconosciuto devono produrre `true` oppure
un'eccezione. Nessun fallback per ordinali GDI, dimensioni o primo match EDID.

Prima dell'integrazione occorre rendere attendibile il produttore rispetto a
identità mancanti e query parziali e validarlo con fault injection offline;
solo allora usare il gate esistente AND l'helper. La verifica va riferita alla
topologia osservata: letture separate non sono un'istantanea atomica e non
provano disponibilità video. L'accettazione dello scanout rimane una prova
hardware distinta, non eseguita nell'intervento B9-01.

### Comportamento sperimentale preesistente

- `nativeDirectMode` opt-in, disattivato per default.
- EDID reale pubblicato con `Prop_EdidVendorID_Int32` e
  `Prop_EdidProductID_Int32`: Windows e NVAPI concordano su
  **0xD94D / 0x6A04 (55629 / 27140)**. Il vendor è nel formato byte-packed
  delle API, non 0x4DD9 nel formato testuale EISA. Nessuno spoof di PSVR2.
- Nessuna dichiarazione di un `IVRDriverDirectModeComponent` inesistente.
- Il monitoraggio che invalida la posa quando un display sparisce dal desktop
  non viene avviato per il percorso nativo: la rimozione è attesa e la
  disponibilità video è responsabilità del backend GPU/compositor.
- `IsDisplayOnDesktop` finale segue l'inventario reale. Un errore di query
  non viene presentato come rimozione riuscita. Bounds del fallback preservati.
  Assenza dal desktop, da sola, non prova scanout diretto funzionante.
- `scripts/native_direct_mode.ps1` aggiunge selettivamente i parametri e un
  nuovo elemento alla lista `direct_mode` ereditata dai default installati.
  Backup delle sole chiavi interessate, restore con controllo conflitti;
  impostazioni estranee preservate. Nessuna modifica agli originali default SteamVR.
- `scripts/inspect_nvidia_displays.ps1` consulta solo API NVAPI pubbliche:
  EDID riportato dalla GPU e flag di visibilità/connessione non cached.

## Prove reali

SteamVR2.17.10, RTX5070Ti, driver32.0.16.1714, HDMI nativa.
Il visore era in power=on, mode=vr, vr_active=1, status_mask=0x51,
worn=false al controllo passivo successivo (08:33:23 locale).

**08:13:37:** driver sperimentale caricato. `vrserver.txt`:
```
Enabling direct mode for NVIDIA [Vid=0xD94D Pid=0x6A04]...
Successfully set display visibility.
```
Ma `vrcompositor.txt` dichiara `Headset display is on desktop`, crea il
normale swap chain e riporta119.999992Hz. L'inventario Windows contiene
ancora SNY6A04 a(1920,492),1080p120. Il successo della richiesta non è
successo dell'acquisizione Direct Mode.

**08:19:30:** prova separata di selezione esplicita del backend tramite
`IsDisplayOnDesktop=false`, con origine(0,0). Ancora backend desktop nel log,
monitor Sony attivo in Windows; il compositor crea una Headset Window su
(0,0), cioè il monitor principale. **Prova fallita e modifica rimossa**:
non nascondere warning né lasciare bounds sbagliati per fingere Direct Mode.
Nessun `Prop_IsOnDesktop=false` o `Prop_DisplayDebugMode=true` impostato.

**NVAPI indipendente, dopo la chiusura:** il display GPU2147881091 riporta
vendor55629/product27140, checksum base EDID valido, active=true,
connected=true, physically_connected=true, **os_visible=true**.
Conferma dell'identità corretta e della mancata rimozione lato GPU.
Non dimostra ancora se il limite sia whitelist NVIDIA, classificazione HMD,
prerequisiti di attivazione o comportamento SteamVR.

In entrambe le sessioni la cattura IMU è terminata tracking-lost prima della
calibrazione (08:13:40 /08:19:33): nessuna nuova validazione tracking/immagine
nelle lenti. Questo è distinto dall'evidenza GPU/Desktop.

## Stato finale e ripristino

SteamVR chiuso normalmente con `vrmonitor://quit`. Override sperimentali
ripristinati selettivamente: configurazione HDMI120 precedente conservata.
Il codice opt-in rimane disponibile ma **Direct Mode NON operativa**.

Backup configurazione:
`logs/native_direct_restore_20261005.json`.
Package precedente alla modifica:
`configurations/candidates/pre_native_direct_driver_package_20261005.zip`.
Baseline storica: [WORKING_CANDIDATE_HDMI_120.md](WORKING_CANDIDATE_HDMI_120.md).

Comandi per una futura prova (SteamVR chiuso; nuovo nome backup obbligatorio):
```powershell
.\scripts\native_direct_mode.ps1 -Action Enable -DryRun -BackupPath C:\Users\rickk\Desktop\PSVR\logs\native_direct_restore_next.json
.\scripts\native_direct_mode.ps1 -Action Enable -BackupPath C:\Users\rickk\Desktop\PSVR\logs\native_direct_restore_next.json
# Dopo prova e normale chiusura SteamVR:
.\scripts\native_direct_mode.ps1 -Action Restore -BackupPath C:\Users\rickk\Desktop\PSVR\logs\native_direct_restore_next.json
```

Verifiche:7/7 test CTest interessati superati sulla versione finale;
test PowerShell isolato superato per restore selettivo, conservazione dei
settings estranei e rifiuto di un conflitto senza scrittura.

Passo aperto: capire perché la chiamata NVIDIA accettata non modifica la
visibilità e quali prerequisiti reali servano per il PSVR1. La pagina NVIDIA
colloca le API Direct Mode nel programma per sviluppatori HMD; il normale
SDK NVAPI pubblico non documenta le stesse funzioni VR private usate da
SteamVR. Non attribuire automaticamente l'insuccesso a una whitelist né
considerare un warning scomparso una prova di Direct Mode.
