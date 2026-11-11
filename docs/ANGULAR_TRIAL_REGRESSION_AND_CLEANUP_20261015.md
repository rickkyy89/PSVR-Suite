# Prova angolare: regressione frontale e pulizia acquisizioni

## Feedback e raccolta

L'utente riferisce molti salti durante i movimenti della testa, posizione meno
stabile della build precedente e gray screen ancora oltre circa 30 gradi.
La prova è rifiutata; non è considerata accettazione del nuovo comportamento.

Sorgente: `logs/suite_tracking_20261015T215832092177Z_d1fae425.jsonl`.
Cartella raccolta: `logs/ac_evo_angular_center_20261015/`.
La raccolta runtime si era conclusa automaticamente a 30 minuti, mentre la
suite ha continuato a registrare. L'analisi camera è stata quindi limitata
al timestamp della segnalazione di fine, salvato tramite il file `STOP`;
non considera i frame successivi. Un intervallo lungo finale senza luci non
viene conteggiato come gap completato se non seguito da recupero entro il cutoff.

Nel segmento analizzato:
- 215.690 frame con `coherent_imu_led_v2`;
- esperimento a due LED disattivato in tutti i frame;
- nessuna posa valida con soli due LED;
- 413 `front_reprojection_conflict`, 139 `Pattern non associato`, 4 ambiguità,
  16 conflitti incrementali di orientamento;
- 139 gap ottici completati dopo l'aggancio, 554 frame invalidi complessivi
  in tali gap; il massimo dura circa 0,587 s;
- i gap più lunghi restano rifiuti di riproiezione con cinque o più candidati.

Il riepilogo completo è `failure-diagnosis.json` nella cartella della prova.
Questi conteggi non sono numeri di gray screen e non sono confronti A/B di
movimenti identici rispetto alla prova precedente.

## Regressione individuata nel driver

L'abilitazione del controller angolare aveva esteso involontariamente la
gestione di recupero anche ai normali dropout ottici frontali. Con
`positionFallback3DofEnabled=false` e politica angolare attiva, una perdita
frontale impostava `degraded_`, e il successivo campione ottico veniva
riagganciato con il raccordo, anche senza essere mai entrati nel fallback
angolare. I trace della parte coperta dalla raccolta contengono 117 ingressi
`6dof_reacquiring` e 117 `position_missing`, compatibili con questa catena.

Il meccanismo è verificato nel codice e può contribuire alla sensazione di
posizione che rincorre o oscilla nei movimenti. Non dimostra che sia l'unica
causa dei salti percepiti.

### Correzione

- Con solo la politica angolare abilitata, il normale tracking frontale
  conserva ora la semantica strict precedente: niente nuovo raccordo
  dopo ogni semplice buco ottico.
- Il controller continua a memorizzare la posizione pubblicata per l'eventuale
  futuro ingresso oltre 90 gradi.
- Il raccordo di rientro viene mantenuto solo dopo un reale ingresso nella
  modalità angolare; una perdita durante quel ritorno resta gestita.
- Il fallback generico esplicitamente abilitato conserva la propria semantica.

Test di regressione: un dropout a 30 gradi seguito da misura valida deve
pubblicare la posizione secondo la semantica strict, con modo `6dof` e offset
di recupero nullo. I contratti nativi `position_continuity_contract` e
`driver_runtime_mock` passano, compresi gli scenari angolari precedenti e lo
sweep sintetico di 40 minuti. La correzione non viene presentata come verifica
hardware della stabilità.

DLL corretta installata a runtime arrestato nel pacchetto già registrato.
SHA256: `c037f0a76142e94487b3634cc7ce17cb8b6212d55cf2b9afab69e1cffabb1067`.
Backup della DLL della prova:
`logs/angular_center_install_20261015T225501Z/`.

## Problema laterale ancora aperto

Il raccordo da 90 gradi non risolve le perdite a 30–40 gradi. Nella nuova
registrazione restano gli stessi rifiuti del modello frontale e delle
associazioni miste. Non è stata alzata la soglia angolare né riattivata la
stima a due LED per mascherare tali perdite. Questa correzione riguarda la
regressione di continuità introdotta nel driver, non il riconoscimento delle
costellazioni.

## Pulizia richiesta dall'utente

Inventario iniziale dei log: circa 21,6 GiB. Eliminati **42 vecchi JSONL di
acquisizione del 14 ottobre**, per **15.367.015.715 byte**:
**14,312 GiB, circa 15,37 GB decimali** di file rimossi.

La selezione è stata esplicita e confinata alla root `logs/`: i vecchi
`suite_tracking_20261014T*.jsonl`, `optics_camera_20261014_0116.jsonl` e
`movement_camera_20261014_0034.jsonl`. Controllati assenza di tracking Git,
link/reparse point e modifiche tra inventario e cancellazione. Nessun Drive
staging attraversato. Le dimensioni sono quelle logiche dei file rimossi,
non una misura differenziale dello spazio libero del volume sotto altri writer.

Prima della cancellazione sono stati conservati percorsi, dimensioni,
timestamp e brevi metadati di inizio/fine acquisizione. Verificato che tutti
i 42 percorsi cancellati siano assenti. Manifest:
`logs/old-trial-cleanup-manifest.json`.

Conservati i log delle prove recenti utili al confronto, i report di analisi,
i file di calibrazione e i backup di ripristino del driver. La registrazione
camera della suite può crescere finché il worker resta attivo: terminare una
prova in chat non equivale a premere **Arresta VR** nella suite.
