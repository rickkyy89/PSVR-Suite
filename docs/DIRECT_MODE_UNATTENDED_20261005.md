# Direct Mode — implementazione delegata e ricerca senza UAC

## Stato verificato

Implementazione affidata estesamente a junior coder, poi revisionata e integrata.
**44/44 CTest PASS** sulla suite completa. Dopo le ultime correzioni dei soli
nuovi strumenti,5/5 test interessati nuovamente PASS. Nessun commit richiesto.

Nessuna elevazione, scrittura registro, ownership display, avvio SteamVR o
comando power/mode in questa fase. Restano primo override HMD applicato,
runtime sperimentale configurato e SteamVR chiuso.

Bundle reale finale:
`logs/direct_readiness_20261005T123220Z/`:
-5 sorgenti/analisi tutte `ok`;
-Sony assente dal desktop attivo;
-lista NVIDIA direct vuota;
-Display Core senza match del percorso Sony esatto; connettore4352 non connesso;
-restore gate accettato in sola lettura;
-outcome **blocked**, nessuna acquisizione/presentazione dichiarata.

## Strumenti implementati

### Probe Display Core

`scripts/inspect_windows_display_core.ps1` e modulo puro
`scripts/display_core_selection.ps1`.

- Schema versionato, timestamp, LUID/adapter-relative id, path, connessione,
  stale, UsageKind e descrittore monitor.
- Selezione per un solo percorso esatto; niente scelta per dimensione/primo
  monitor. LUID/connettore sono hint, non identità o autorizzazione.
- Distingue missing/disconnected/stale/standard/ambiguous/unknown da
  eligible_for_next_probe. Quest'ultimo non certifica rendering.
- Modi opt-in tramite lettura di stato, mai acquisizione/commit. Sul PC attuale
  `TryReadCurrentStateForModeQuery` restituisce uno stato non-read-only, rifiutato;
  fallback `TryReadCurrentStateForAllTargets` restituisce stato readonly valido.
  Entrambi i tentativi registrati, nessuna causa nascosta. Sony rimane missing.

### Bundle e classificatore

`scripts/collect_direct_readiness.ps1` raccoglie snapshot sequenziali Windows,
NVAPI, Display Core e restore gate; `tools/psvr_native_analysis/readiness.py`
classifica dati/fixture senza hardware.

Correzioni emerse nella review:
- `os_visible` NVAPI è osservazione, **non gate obbligatorio**: un display diretto
  può essere nascosto all'OS o omesso dall'inventario pubblico.
- Count da API fallita ignorato, non candidato. Una lista vendor-scoped non
  identifica da sola il prodotto6A04: ulteriore identity probe necessario.
- Duplicati, uso Standard e identità per solo connettore non selezionati.
- Probe IPC fallito significa fullscreen sconosciuto; il `reason` presente
  anche nei riepiloghi validi non invalida automaticamente la misura.
- Binding PowerShell per inspector con hashtable, parametri reali; Python
  risolto come executable Application tramite PATH, non come file relativo.

### Procedura locale generata, non eseguita

`scripts/prepare_local_direct_test.ps1` e `local_direct_test_plan.ps1`.
Artefatto finale: `logs/local_direct_test_ready_20261005b/procedure.md` +JSON.
8 fasi: letture, chiusura manuale, restore HMD admin, verifica reale, Prepare/Apply
compat separati, avvio manuale, osservazione visiva/Hz, rollback HMD→runtime.
Nessun wrapper restore+apply atomico nella sequenza che richiede restore già
verificato. Stato da journal distinto dalle prove fisiche; claims non osservati
null. File di evidenza nuovi, niente cancellazione/sovrascrittura.

## Confronto con l'implementazione PSVR pubblicata

Fonte consultata (GPL, **nessun codice o EDID completo copiato**):
https://github.com/Timocop/PSMoveServiceEx-Virtual-Device-Manager

- `Classes/ClassMonitor.vb:325–419`: override monitor EDID in registry e riavvio
  del nodo monitor, non flash EEPROM. La nostra variante modifica solo CTA1
  sull'istanza scelta, non tutti i monitor Sony.
- Risorse6A04 DIRECT: identità Sony invariata, VSDB Microsoft v2 con usage0x27.
  Il nostro primo test usa0x07 prescritto dalla documentazione Microsoft v2
  attuale; compat0x27 resta prova locale pendente.
- Il file DIRECT modifica anche seriale/data/dimensioni, rimuove FD range e
  aggiunge DTD60/120/90 e VIC63. Non importarlo integralmente:90Hz e cambi
  di geometria/identità non sono verificati sul nostro dispositivo.
- `UCVirtualMotionTrackerItem.vb:7,1827–1851`: percorso direct configurato a120Hz
  con1920x1080 e VID/PID; l'assenza del monitor viene trattata come "probably
  direct". Da sola non prova output: manteniamo verifica backend/target/immagine.

### Proposta120Hz esclusivamente offline

`tools/psvr_native_analysis/edid_120_proposal.py` + helper `edid_blocks.py`.
Input: EDID originale catturato e candidata HDMI120 misurata.

L'originale dichiara FDmax61Hz, Hmax68kHz, pixel/TMDS150MHz. La proposta:
-aggiunge soltanto VIC63,1920x1080p120 con timing noto297MHz,2200x1125;
-massimi FD120Hz/135kHz/pixel300MHz (quantizzazione10MHz);
-TMDS300MHz (quantizzazione5MHz);
-preserva VID/PID, seriale, dimensioni, DTD esistenti e tutti gli altri blocchi,
  incluso VSDB HMD; audit dei byte e degli offset logici prima/dopo lo shift.

**Non applicata, non apply-ready né certificazione di stabilità**. Primo artefatto
`out/edid_120_proposal_20261005.json` conserva la proposta iniziale; il generatore
aggiornato distingue insertion/field_shift da byte_diff fisico. Non mescolare
timing e classificazione nella prima prova causale locale.

## Selezione backend SteamVR: evidenza statica limitata

Analisi readonly del vrcompositor2.17.10, SHA256:
`8f518bf734b7303aec4e39da8c2f0c83cc6f36f066a38d1a28c4b8aefe3cc7d8`.
FunzioneRVA0x3BC40..0x3CC6F, xref stringa desktop0x3BD26:
-casi separati per display virtuale, driver-direct e percorso normale;
-nel normale la scelta della stringa desktop/direct segue un getter nel
  contesto interno (vtable offset0xA0), non una chiamata diretta alla slot1
  di IVRDisplayComponent_003. Non abbiamo identificato il tipo interno:
  **non attribuire quel getter a un metodo pubblico senza prova**;
-il ramo desktop cerca l'output DXGI e può restituire422/InvalidOutputDesktop
  (0x1A6) se l'output non esiste. È coerente con i log osservati dopo hide.

La proprietà desktop del nostro driver da sola non dimostra che il runtime
abbia un display diretto acquistabile. Nessuna modifica del binario/injection.

## Presenter proprio: contratto studiato, rendering non implementato

SDK effettivo2.15.6: `IVRVirtualDisplay_002`, `PresentInfo_t`32byte, offsets
0/8/16/24, handle64bit, `Present(const PresentInfo_t*,uint32_t)`.
`driver/virtual_display_contract.hpp` e test C++ verificano il contratto e
copiano solo il prefisso valido; buffer corto, enum ignoto, handlezero e tempo
nonfinito rifiutati senza alterare destinazione. Stub soltanto nel test,
mai registrato al runtime; nessun vsync o scanout inventato.

Architettura da validare: HMD fornisce posa/ottica/geometry a SteamVR, dispositivo
DisplayRedirect riceve backbuffer finale tramite IVRVirtualDisplay; presenter
separato importa/copia texture e gestisce Windows Display Core/fences/vblank.
SteamVR conserva compositing/distortion; IVRDriverDirectModeComponent, invece,
richiederebbe un percorso di compositing nostro. Registrazione di due dispositivi
e scelta output virtuale sono ancora ipotesi d'integrazione, non test runtime.

Manifest `redirectsDisplay`, device classDisplayRedirect, component_002,
ownership texture e timing virtual-vsync distinti da conteggio dei Present.
MinGW/MSVC: il problema noto è member-aggregate sret di ComputeDistortion;
non inventare slot destructor per interfacce senza virtual destructor. Il
nuovo harness non prova la boundary con l'host MSVC.

Fonti:
- SDK pinned `C:\Users\rickk\AppData\Local\Temp\opencode\psvr-openvr\headers\openvr_driver.h:4066–4091`
- https://github.com/ValveSoftware/openvr/blob/master/docs/Driver_API_Documentation.md
- https://github.com/ValveSoftware/virtual_display (sample vecchio: non copiare firma Present precedente)
- https://github.com/microsoft/Windows-classic-samples/tree/main/Samples/DisplayCoreCustomCompositor

Prerequisito comune: target Sony reale, connesso e identificato esattamente,
con modi interrogabili e ownership possibile. Un presenter non crea da solo
il percorso di scanout oggi assente. Prossimo test locale: restore verificato,
compat isolata, disponibilità backend; poi eventualmente proposta120 separata.

## Ulteriore lavoro autonomo — matrice causale e lifecycle

Alla richiesta di proseguire senza intervento locale, due junior coder hanno
implementato moduli offline separati; integrazione e review successive hanno
aggiunto due test CTest. **Suite completa aggiornata: 46/46 PASS**.
Nessun avvio SteamVR, deploy del driver o intervento sul display in questa fase.
Il bundle live di riferimento rimane quello delle12:32, non una nuova misura.

### Matrice EDID sui dati salvati

`tools/psvr_native_analysis/edid_trial_matrix.py`,41 test Python, riusa i
generatori fail-closed. Evidenza nuova:
`out/edid_trial_matrix_20261005.json` (scrittura esclusiva, nessuna sovrascrittura).
Sei celle: originale, HMD07, HMD27, ciascuna senza/con proposta120; sette
confronti causali. Tutti i15 invarianti calcolati risultano veri:
-checksum validi, identità/seriale/nome conservati;
-base invariato o sole modifiche ai massimi FD e checksum;
-coppie07→27: solo byte uso e checksum, anche nella colonna120;
-coppie base→120: classificazione HMD conservata;
-nessun byte divergente senza spiegazione nei confronti selezionati.

L'autoconsistenza del piano viene verificata rigenerando il CTA07 dal raw
catturato e GUID. Non è una lettura attuale del registro. Hex e hash permettono
review delle proposte, non sono piani di applicazione. Un input candidato
rifiutato blocca le celle120; una cattura incoerente blocca la matrice.
Etichette EDID e forme JSON sono state corrette in review; i verdict distinguono
assenza di cambiamenti nei campi proiettati da effettiva uguaglianza dei byte.
Le spiegazioni degli shift restano affiancate all'audit semantico del generatore:
un valore numerico coincidente non basta a provare conservazione di un campo.

### Lifecycle dei frame, ancora senza backend

`driver/virtual_display_lifecycle.hpp` e
`tests/virtual_display_lifecycle_tests.cpp`: array limitato a64 slot, metadata
copiati, handle preso in prestito, ticket con generazione, drop-newest quando
pieno. Completamenti duplicati/tardivi/out-of-order distinti. Nessuna release
di texture: su disconnessione il suo lifetime diventa sconosciuto, non libero.
Slot terminali conservati fino a reclaim esplicito; reset invalida vecchi ticket.
Il peek è ispezione, non dequeue: il futuro dispatcher deve evitare invii doppi.

Review e regressioni ulteriori:
-vsync richiede token della sessione catturato alla sottoscrizione; reset,
  disconnessione e failure lo invalidano. Un callback della sessione vecchia
  non può autenticare il nuovo flusso neppure prima del primo evento;
-timestamp e contatore hardware devono avanzare; fence completion non è
  vblank e nFrameId non è contatore hardware;
-delta temporale fuori dominio, overflow double e conversione float fuori
  range restituiscono indisponibile, con output azzerati;
-nessun clock letto o vblank sintetizzato: eventi esterni sono input dei test,
  non osservazioni hardware effettuate da questo modulo.

Compilazione MinGW e harness PASS. Core non derivato da IVRVirtualDisplay e
non registrato al runtime. Import texture, fences OS, acquisizione del target,
scanout e boundary ABI con host MSVC restano da realizzare/validare. Il lavoro
riduce i rischi logici del futuro presenter, non risolve la disponibilità Sony.

## Confine DLL del virtual display — 47/47 CTest PASS

Ulteriore richiesta di lavoro autonomo: verificata la disponibilità della
toolchain, senza trovare MSVC/clang-cl su PATH o nelle installazioni standard
controllate. Non è una ricerca esaustiva dell'intero disco. Implementato con
junior coder e revisionato un harness test-only Windows x64:
- `tests/virtual_display_boundary_fixture.cpp`: DLL con implementazione SDK002;
- `tests/virtual_display_boundary_api.hpp`: trace/probe POD con layout fissato;
- `tests/virtual_display_boundary_tests.cpp`: EXE senza header OpenVR, carica la
  DLL ed effettua chiamate tramite firme degli slot riscritte indipendentemente.

Verifiche: ordine Present/Wait/GetTime, this, ritorno bool, handle/frame64bit
con bit alto, offset32byte, output guardati, rifiuto di input invalidi senza
alterare l'ultimo frame accettato. Distruzione tramite export della DLL sul
tipo concreto, prima dell'unload; mai delete attraverso l'interfaccia base.

Regressioni aggiunte in review:
- canarie verificano assenza di scritture, non di letture. Due pagine di memoria
  privata del processo, seconda PAGE_NOACCESS: prefisso32 a fine prima pagina
  con size dichiarata64 passa senza leggere la coda; source in pagina
  inaccessibile con size0/16/31 viene rifiutato prima di qualsiasi lettura.
  Un over-read farebbe fallire il processo di test, non viene intercettato;
- journal circolare esportato in ordine cronologico, test oltre il wrap con
 17/18 chiamate e ultimi slot differenti;
- rimosso il falso assunto GCC>=13 equivale a C++ ABI MSVC: convenzione di
  chiamata Windows x64 e compatibilità C++ completa sono fatti distinti.

Fixture ed EXE compilati separatamente con MinGW, IPO disabilitata, nessun link
al driver di produzione. **Suite completa47/47 PASS**, build senza warning.
Wait/GetTime nella fixture contengono soltanto dati sintetici dichiarati.
È verifica comportamentale attraverso DLL, **non** prova con compilatore MSVC,
host SteamVR o scanout; questi restano pendenti. Nessun deploy/avvio runtime,
registro, power/mode o target GPU toccato in questa fase.

## Prova aggiuntiva90Hz richiesta dall'utente

Preparazione separata in [PSVR_90HZ_TRIAL_20261005.md](PSVR_90HZ_TRIAL_20261005.md)
e `configurations/candidates/hdmi_native_90hz_trial.json`: timing storici
1920×1080p90,222,750MHz,2200×1125, senza applicazione. Checker offline32 test
e suite completa **48/48 PASS**. Prima desktop90 su baseline ripristinata,
poi eventuale ramo diretto solo con target/modo effettivi disponibili.
Il90 resta escluso dai generatori della propostaEDID120 e della sua matrice:
il nuovo trial non modifica quelle evidenze né inventa un VIC1080p90.
