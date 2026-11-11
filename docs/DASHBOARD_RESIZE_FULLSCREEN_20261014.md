# Dashboard desktop e ricorrenza -202 — 14 ottobre 2026

Utente: errore -202 frequente quando ridimensiona il desktop visualizzato
nella dashboard SteamVR; episodio appena avvenuto durante questa verifica.

## Stato osservato

- `vrmonitor.txt` riporta `Errore visore (-202)` ripetutamente.
- Probe compositor: `fullscreen=false`, `can_render_scene=true` in tre
  osservazioni consecutive.
- Finestra Headset Window visibile, non minimizzata, bounds fisici
  `(1920,492,1920,1080)` coerenti con il display configurato; non foreground.
  Il riferimento `(3840,492)` presente nello script inspector è una vecchia
  costante descrittiva, non una misura corrente del display.
- Driver: display valido, orientamento valido, dati IMU freschi;
  sessione `1948308127-3110372876-32326638813900-1`, epoca 1.
- Posizione ottica mancante e posa invalida con fallback disattivato:
  problema separato dalla perdita fullscreen, non corretto da questo recupero.
- Identity resta attivo, display 90 Hz. Nessuna modifica alle impostazioni.

Nessun errore GPU-fence/DXGI/fullscreen esplicito nel segmento recente
19:51–19:57 del log compositor. Gli eventi di reconnect dashboard alle
19:51 seguono un riavvio e non dimostrano il trigger dell'episodio alle
19:55. Il collegamento con il ridimensionamento è feedback dell'utente;
la causa interna del fallimento non è ancora isolata.

## Recupero verificato

BringToFront dispatch verificato: fullscreen rimane false anche dopo la
richiesta e al termine del probe.

Riavviato esclusivamente il compositor dopo verifica del percorso e PID:

- compositor precedente PID 26824, nuovo PID 29972 alle 19:58:34;
- vrserver PID 11892, avviato alle 19:51:38, rimane invariato;
- dopo il riavvio: fullscreen true prima/100ms/finale, can_render_scene true;
- stessa sessione IMU ed epoca 1, orientamento valido, età IMU 0,2078 ms.

Non eseguita nuova calibrazione IMU né riavvio del worker camera.
La posizione ottica resta invalida al probe finale: il recupero riguarda
la presentazione fullscreen, non certifica tracking 6DoF valido.

Questo è un workaround per la sessione corrente, non una soluzione provata
contro le ricorrenze del -202. La riproduzione controllata del resize e il
monitoraggio del cambio fullscreen/focus restano necessari per la causa.

## Riproduzione confermata dopo il recupero

L'utente verifica: dopo il recupero la presentazione riparte; ridimensionando
di nuovo il desktop nella dashboard ricompare il -202. Il resize è quindi
un trigger riproducibile nella configurazione attuale, non soltanto una
coincidenza osservata una volta. La causa interna rimane da isolare.

Il probe successivo sul nuovo compositor PID 29972 rileva di nuovo
`fullscreen=false` prima/100ms/finale e `can_render_scene=true`.
Headset Window rimane visibile e non minimizzata, con gli stessi bounds
`(1920,492,1920,1080)`, style `0x96000000` ed exstyle `0x00000000`.
Non è quindi osservato un cambiamento di dimensioni o monitor della
finestra del visore. Il controllo fullscreen true ottenuto col solo
riavvio del compositor è perso dopo la nuova prova di resize.

Gli errori fence visibili nel log risalgono a precedenti sessioni
(ultimo alle 19:44:54); non attribuirli al nuovo compositor delle 19:58.
La sola assenza di nuovi errori espliciti non identifica il meccanismo
DXGI/focus alla base del -202. Non modificare distorsione o filtri camera
per tentare di correggere questo difetto di presentazione.

Artefatti diagnostici: `logs/dashboard_resize_20261014_diagnostic_v2.json`;
il primo report rimane conservato, ma la sua attribuzione del reconnect
dashboard al resize è superata dalla revisione v2.
