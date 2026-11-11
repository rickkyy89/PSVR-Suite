# Candidato HDMI nativo 120 Hz — non stabile certificato

2026-10-04. **M3–M6 IN_PROGRESS; prova DP PAUSED** su richiesta dell'utente.
Attendere prima di scambiare le porte; nessun cambio cavi durante la ricerca.

## Cosa conserva il candidato

Salvati `configurations/candidates/hdmi_native_120hz.json` e
`configurations/candidates/hdmi_native_120hz_driver_package.zip`.
Digest SHA256 del package:
`671B30777AD0CE4AE17766B9E438A16DF3B0A03E3AE0298157EFB97033D7FC03`.
Il salvataggio non attesta un restore eseguito né la risoluzione del −202.
Il candidato comprende override runtime selettivi, `ImuConfig`, timing
manuali NVIDIA di riferimento e note; digest/archivio servono alla tracciabilità.
**Non è un export binario dei modi NVIDIA dal registry**, né un backup
completo di SteamVR da sovrascrivere.

- Sony SNY6A04 UID4352 su GPU HDMI nativo, 1920x1080 progressivo,
  origine (1920,492); path/signal120/1 verificati prima/dopo l'avvio.
- Timing manuale di riferimento120: attivi1920/1080, front porch88/4,
  sync44/5, back porch148/36, totali2200/1125, polarità positiva/positiva,
  pixel clock297,000MHz. Identità originale Sony preservata; nessun import
  completo dell'EDID storico SNYB403.
- Avvio23:40:19 positivo: compositor nominal120Hz, fullscreen true,
  calibrazione e10/10 pose valide; riscontro utente positivo.
- **Ricorrenza nota −202**: alle23:49:31 stessi vrserver23376/compositor23424,
  10/10 pose valide/connected, fullscreen false, pauseCompositorOnStandby
  ancora false. Questo override da solo non risolve permanentemente il problema;
  standby come causa non provato. Driver desktop-esteso, non Direct Mode.

I successi a60/120Hz, entrambi gli occhi, movimento e recenter restano
evidenze parziali. Non certificano stabilità, ottica quantitativa, frame
skipping o persistenza del modo dopo rebootWindows.

Ulteriore prova focus: un solo SetForegroundWindow sullo HWND/PID/bounds
verificati ha restituito true, foreground acquisito confermato subito e
dopo100ms; la lettura successiva OpenVR è ancora fullscreen false con10/10
pose valide. Portare in primo piano da solo non è risultato sufficiente.
Questo non dimostra quale evento abbia causato la perdita iniziale.

## Ripristino selettivo — solo con successiva autorizzazione

1. **Chiudere SteamVR normalmente** e verificarne la chiusura prima di
   modificare settings. Conservare un backup corrente per confronto/rollback.
2. Verificare il **connettore fisico**, il target Sony corretto e i modi
   effettivamente disponibili. UID e coordinate possono cambiare con porta
   o topologia: non applicare ciecamente i valori del candidato a una prova DP.
3. Il JSON non reinstalla il modo NVIDIA. Se il custom120 non esiste, non
   fingere che sia ripristinato: verificare/autorizzare separatamente la prova
   manuale descritta in [PSVR_HIGH_REFRESH_TEST.md](PSVR_HIGH_REFRESH_TEST.md).
   Mantenere un monitor di controllo e il fallback60.
4. Solo dopo verifica di source/scanout, refresh path/signal e geometria reali,
   **fare merge delle sole chiavi selezionate** nel settings corrente,
   preservando tutte le altre impostazioni utente. Non sostituire l'intero
   `steamvr.vrsettings`; usare refresh/bounds coerenti con il display reale.
   Verificare anche `ImuConfig` e note, senza modifiche automatiche implicite.
5. Verificare il digest e la provenienza del package prima di un eventuale
   ripristino autorizzato. Il package non ricrea timing NVIDIA o Direct Mode.
6. Un futuro riavvio/calibrazione deve rivalidare pose, fullscreen, immagine
   nelle lenti e durata. Riapplicare il candidato **non** equivale a risolvere −202.

Questa procedura non è stata eseguita in questo aggiornamento documentale.
Evidenze e storia dei fallimenti: [RUNTIME_CHECKS_20261004.md](RUNTIME_CHECKS_20261004.md).
