# Sviluppo senza UAC — Direct Mode, 2026-10-05

## Risultato di questa fase

Nessuna nuova elevazione, acquisizione target, modifica EDID/registro o avvio
SteamVR. Il primo override HMD rimane applicato; il runtime rimane chiuso.

### Ripristino pronto e verificabile senza scrittura

- Separati controlli puri in `scripts/hmd_edid_plan.ps1`.
- Validati scope del solo Sony, identità, lunghezza/checksum EDID, blocco HMD
  e container GUID. Rimuovendo il blocco aggiunto deve ricostruirsi esattamente
  la CTA precedente: nessuna modifica accidentale di timing/audio/video ammessa.
- Cache originale e cache della nostra variante esatta ammesse per restore;
  cache o override estranei rifiutati. Valore precedente1 preservato se presente.
- Piano scritto atomicamente; journal dell'intento prima della scrittura
  registro. Recovery di Apply interrotto e completamento idempotente di Restore.
- **13 controlli sintetici PASS**, senza registro o hardware.
- Sul piano reale `logs/hmd_edid_override_20261005.json`, l'inspector read-only
  conferma cache=nostro candidato, override=nostro candidato,
  `restore_gate_accepted=true`, prevista eliminazione del solo valore1.
  **Ripristino non eseguito**; richiede ancora accesso amministratore.

### Diagnostica più affidabile

- Nuovo lettore incrementale conserva righe e UTF8 spezzati fra due scritture,
  non duplica eventi e gestisce troncamento dei log. Test sintetici PASS.
- Il riepilogo distingue errore del probe da fullscreenfalse: mancanza di
  riepilogo/IPC è stato sconosciuto, non una misura di fullscreen.
- Conteggio NVAPI lasciato inizializzato dopo un errore non è un candidato:
  successo dell'enumerazione necessario. OS-visible distinto da desktop attivo.
- Cattura read-only reale di4s completata senza avviare SteamVR.
- Test aggiunti a CTest: EDID minimo, summary, restore plan, log reader.
  Suite precedente35/35 PASS e4/4 nuovi test PASS.

## Disponibilità reale del display: nuova lettura Windows

`scripts/inspect_windows_display_core.ps1` usa solo:
`DisplayManager.Create(None)` e `GetCurrentTargets()`, poi Dispose.
Non chiama TryAcquireTarget/CreateDisplayDevice/Apply/Present.

Osservazione corrente: adapter LUID76797:0, connettore4352 del precedente Sony
senza monitor collegato secondo questa API (`connected=false`, interface path
vuoto, usage kind Standard). Il solo target connesso è LG/4354.
Il nodo PnP Sony storico è Status=Unknown, servizio monitor; ProblemCode non
disponibile, non assumere zero. Questo non dimostra un guasto fisico: è lo
stato esposto dalle API dopo l'override nella sessione attuale.

**Conclusione limitata:** Sony è fuori dai percorsi desktop attivi, ma non
abbiamo un target connesso/acquisibile esposto qui né un candidato nella lista
Direct Mode NVIDIA. Non è ancora una presentazione diretta funzionante.

## Percorsi di presentazione da distinguere

1. SteamVR-native via IVRDisplayComponent: richiede che la GPU offra un display
   diretto utilizzabile. Finché la lista rimane vuota, cambiare solo flag
   OpenVR non rende disponibile uno scanout.
2. Presentazione Windows Display Core: API pubbliche per display specializzati,
   esempio ufficiale Win32/C++WinRT/D3D:
   https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/DisplayCoreCustomCompositor
   Non richiede necessariamente costruire l'intero compositor da zero: valutare
   IVRVirtualDisplay per ricevere il backbuffer finale già compositato da SteamVR
   e un presenter proprio per ownership, texture interop, fences e scanout.
   Questo è un possibile sviluppo, **non implementato**. Prima serve un target
   verificato, connesso, non-desktop, con modi interrogabili e ownership valida.

Fonti API:
- https://learn.microsoft.com/en-us/uwp/api/windows.devices.display.core.displaymanager
- https://learn.microsoft.com/en-us/uwp/api/windows.devices.display.core.displaytarget
- https://learn.microsoft.com/en-us/uwp/api/windows.devices.display.core.displaymanager.tryacquiretarget

## Prossimo test con accesso locale

1. SteamVR chiuso. Verifica readonly del piano e dei target.
2. Restore dell'override v2/0x07 con lo script corretto e verifica della
   ricomparsa del Sony. Non dichiarare ripristino riuscito solo per exit0.
3. Prova compat v2/0x27 isolata già preparata, mantenendo identità e timing,
   elevazione UAC locale. Differisce in un bit empirico, non prescritto dal
   formato v2 Microsoft attuale. Non importare l'intero EDID di altri progetti.
4. Prima di SteamVR, ricontrolla insieme Windows desktop, NVIDIA direct lists
   e Display Core: se nessun target appare, non provare ownership del monitor LG.
5. Solo con target corretto: avvio/posa, backend, immagine nelle lenti e modo
   effettivo. I120Hz desktop precedenti non dimostrano120Hz in Direct Mode;
   l'EDID minimo non aggiunge un timing120 né altera il range descriptor.
6. Se fallisce, restore dell'override e del runtime selettivo, verificando
   UID/bounds/segnale della candidata HDMI precedente.

Comandi readonly utili:
```powershell
.\scripts\inspect_hmd_edid_plan.ps1 -PlanPath C:\Users\rickk\Desktop\PSVR\logs\hmd_edid_override_20261005.json
.\scripts\inspect_windows_display_core.ps1
.\scripts\inspect_nvidia_displays.ps1 -IncludeDirectEnumeration
```

Stato e restore amministrativo: [NATIVE_DIRECT_MODE_20261005.md](NATIVE_DIRECT_MODE_20261005.md).
