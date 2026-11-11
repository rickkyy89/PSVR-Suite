# Prova locale PSVR1 a 90 Hz — preparata, non eseguita

Richiesta utente: «Predisponi anche un test con refresh del visore 90hz».
Manifest separato: `configurations/candidates/hdmi_native_90hz_trial.json`.
Nessun modo90 creato, nessuna impostazione runtime o EDID applicata durante
la preparazione. Il candidato120 e le sue evidenze restano distinti.
Checker offline verificato con32 test; suite integrata **48/48 CTest PASS**.

## Timing manuale da provare

| Campo | Orizzontale | Verticale |
|---|---:|---:|
| Pixel attivi | 1920 | 1080 |
| Front porch | 88 | 4 |
| Sync width | 44 | 5 |
| Back porch | 148 | 36 |
| Totale | 2200 | 1125 |
| Polarità | positiva | positiva |

**90 Hz, progressivo, pixel clock222,750 MHz; frequenza orizzontale101,250 kHz.**
Il clock diviso per2200×1125 dà esattamente90 Hz. Fonte: tabella storica in
`PSVR_HIGH_REFRESH_TEST.md`, archivio PSVRFramework con hash nel manifest.
Il profilo sorgente è SNYB403: prendiamo soltanto i timing, non identità/seriale
o EDID completo. Non è una certificazione del nostro Sony SNY6A04.

## Ordine della prova

### 0. Tornare a una baseline desktop verificata

Questa prova90 usa prima il ramo desktop HDMI nativo, per isolare il refresh.
Il primo override HMD attualmente registrato nasconde Sony: eseguire con
presenza locale il ripristino già predisposto, e verificare la riapparizione.
Con SteamVR chiuso:

```powershell
& .\scripts\hmd_edid_override.ps1 -Action Restore -PlanPath .\logs\hmd_edid_override_20261005.json
```

Il comando richiede PowerShell amministratore e si usa solo se quel piano
risulta ancora applicato o in recupero. Se nel frattempo è attiva la variante
compat, ripristinare il suo piano effettivo. Poi ripristinare selettivamente
l'esperimento runtime, quando ancora attivo:

```powershell
& .\scripts\native_direct_mode.ps1 -Action Inspect
& .\scripts\native_direct_mode.ps1 -Action Restore -BackupPath .\logs\native_direct_restore_20261005c.json
```

Verificare il path esatto Sony e l'immagine desktop. Il restore usa il journal
e può rifiutare conflitti: non sostituire l'intero file steamvr.vrsettings.
La procedura Direct Mode in `logs/local_direct_test_ready_20261005b/procedure.md`
documenta il ripristino dettagliato; **per questa prova fermarsi prima di
Prepare/Apply compat**. Non cambiare EDID e refresh nello stesso passaggio.

### 1. Salvare lo stato immediatamente precedente alla prova

Creare una nuova directory `logs/refresh90_trial_<UTC>` e registrare:
-inventario completo `out/display/psvr_display.exe --json`;
-modo Sony effettivo, geometria, posizione e screenshot dei timing NVIDIA;
-valore e presenza delle sole chiavi `driver_psvrpc.displayRefreshNumerator`
  e `displayRefreshDenominator` nel file runtime attuale;
-stato di `nativeDirectMode` e identificazione EDID per documentare il ramo.

Il backup refresh va preso **dopo** il ripristino della fase0, non durante
questa preparazione. Deve distinguere valore assente da valore presente.
Non assumere120/1 o60/1: il rollback dipende dai valori realmente presenti.
L'inventario completo, non quello ridotto della capture JSONL, serve al checker.

### 2. Creare/testare il modo90 sul solo Sony

SteamVR resta chiuso. Pannello NVIDIA → Modifica risoluzione → Sony/SIE HMD →
Personalizza → Crea risoluzione personalizzata → timing **Manuale**.
Inserire la tabella sopra; verificare screenshot/target prima di Test.
Mantenere colore, scaling, cavi e altri parametri della baseline.

Se l'immagine non compare correttamente, lasciare il ripristino offerto dal
dialogo senza confermare; annotare l'esito. Se compare, confermare/salvare e
selezionare90. Non basta la voce90 nella UI: fare la lettura successiva.

### 3. Gate della misura GPU prima di configurare SteamVR

Salvare un nuovo inventario completo come `inventory_after_90.json` nella
directory della prova. Il comando che legge i dati è:

```powershell
& .\out\display\psvr_display.exe --json
```

Con quel JSON salvato, valutare offline (sostituire `<UTC>` con la directory
effettiva; il report deve essere un file nuovo):

```powershell
python -B .\tools\psvr_native_analysis\refresh90_trial.py --inventory '.\logs\refresh90_trial_<UTC>\inventory_after_90.json' --out '.\logs\refresh90_trial_<UTC>\assessment_after_90.json'
```

Procedere soltanto con `desktop_90_ready_for_runtime_configuration`:
-una sola corrispondenza del path Sony esatto, identitàD94D:6A04 valida;
-source e target1920×1080, attivo/disponibile, non clone, non scalato/ruotato;
-progressivo sia path che signal;
-refresh path **e** signal equivalenti esattamente a90/1 (180/2 ammesso).

Il campo `hz` arrotondato viene ignorato. Exit0 significa report prodotto,
non necessariamente gate superato. Sony assente blocca questo ramo desktop,
non dimostra il fallimento di un eventuale ramo diretto.

### 4. Allineare soltanto il refresh del driver e avviare manualmente

Dopo un gate positivo e una ricattura fresca con SteamVR ancora chiuso,
modificare soltanto queste due chiavi della sezione `driver_psvrpc`:

```json
{
  "displayRefreshNumerator": 90,
  "displayRefreshDenominator": 1
}
```

Questo è un frammento da unire, **non** il file runtime completo. Usare il
backup fresco della fase1 per il restore, preservando tutte le altre chiavi.
Non abilitare Direct Mode o modificare power/standby per questa comparazione.
Se geometria/posizione differiscono dalla baseline del driver, fermarsi e
risolvere la discrepanza come passaggio separato prima del test runtime.

Avviare SteamVR manualmente, calibrazione8 secondi fermo, poi recenter.
Verificare log della sessione nuova: frequenza del compositor nominale90
(circa11,111 ms/frame), assenza di errore422/−202, stato fullscreen e pose.
La proprietà DisplayFrequency90 dimostra configurazione, non scanout fisico.

### 5. Osservazione breve e prolungata

Registrare immagine in entrambi gli occhi, movimento/recenter e schermate
rosse o perdita di fullscreen. Prima controllo breve, poi almeno5 minuti
se l'esito iniziale è positivo, includendo un normale togli/rimetti del visore.
Non attribuire automaticamente eventuali errori allo standby.

Capture read-only, runtime già avviato manualmente, file nuovi:

```powershell
& .\scripts\capture_direct_mode_transition.ps1 -DurationSeconds 180 -IntervalSeconds 2 -OutputPath '.\logs\refresh90_trial_<UTC>\runtime_90_a.jsonl'
& .\scripts\capture_direct_mode_transition.ps1 -DurationSeconds 180 -IntervalSeconds 2 -OutputPath '.\logs\refresh90_trial_<UTC>\runtime_90_b.jsonl'
```

Nessun `-StartSteamVR` in queste capture. Salvare anche inventari completi
durante/dopo, log e osservazioni dell'utente. Fullscreen/pose/GPU90 positivi
non certificano assenza di frame skipping o cadence reale del pannello.

### 6. Rollback e confronto120

Chiudere SteamVR; ripristinare le due chiavi refresh dai valori effettivi
della fase1 (rimuovere la chiave se prima assente). Tornare al modo GPU
effettivamente precedente, se ancora disponibile e verificato.60Hz originale
è fallback documentato se quel modo non è recuperabile, non restore presunto.
Eliminare solo il nuovo modo90 eventualmente creato, quando non è attivo.
Verificare inventario e runtime dopo il ripristino.

Se utile, confrontare90 e120 in due sessioni separate mantenendo stesso
backend, driver, cablaggio, ottica e configurazione di tracking. Registrare
tempi degli errori e durata osservata; non sovrascrivere il candidato120.

## Eventuale90 in Direct Mode

La prova desktop non dimostra90 in Direct Mode. Per il ramo diretto servono
prima target Sony disponibile e modi interrogabili: scegliere90 solo se il
backend lo espone realmente e verificare timing/presentazione effettivi.
Non trasformare automaticamente il VIC63 (1080p120) della propostaEDID120
in90: non c'è un VIC1080p90 da sostituire. Un'eventuale proposta DTD90 sarebbe
un esperimento distinto da progettare dopo i risultati della prova iniziale.

## Campi del verbale per ciascuna sessione

UTC/inizi-fine; hash del manifest e inventari; backend; modo GPU precedente;
refresh path/signal prima/durante/dopo; chiavi runtime prima/dopo; log della
sessione; fullscreen/pose; immagine nei due occhi; errore e tempo di comparsa;
durata senza errori; controllo rollback. Campi non misurati rimangono null.
