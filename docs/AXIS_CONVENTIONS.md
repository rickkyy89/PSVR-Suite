# Convenzioni e verifica degli assi PSVR1

## Implementazione attuale

Coordinate destrorse: **+X destra, +Y alto, −Z avanti**. Quaternion WXYZ,
body→world; angoli diagnostici Y-X-Z. In queste coordinate:

| Movimento | Angolo atteso | Grafico |
|---|---|---|
| Guarda a destra | yaw negativo (asse Y) | rosso |
| Guarda in alto | pitch positivo (asse X) | verde |
| Orecchio destro verso spalla destra | roll negativo (asse Z) | blu |

I colori dei tre assi nel disegno 3D sono una legenda separata, esplicitamente
etichettata. I grafici mostrano angoli, non componenti cartesiane del quaternion.

## Fonti confrontate

- OpenHMD `85075b0c7e3c723ded2577edb79d00ee11aac339`,
  `src/drv_psvr/psvr.c` righe 42–53: gyro/accel `(raw[1],raw[0],-raw[2])`.
  Scale gyro 0.00105 rad/count, accel 9.81/16384; quaternion fusion restituito
  senza una successiva rotazione fissa di mount nel driver ispezionato.
- PSVRFramework `ccda7ca840f8f820197dd8571bbc869a02563ea4`,
  `PSVR.cs` e `BMI055Integrator.cs`: parser dei campi raw e conversioni/Euler
  proprie dell'app; non è una conferma indipendente di ogni segno di display.
- [Wiki report sensori](https://github.com/gusmanb/PSVRFramework/wiki/Report---Sensor-Data):
  nomi raw yaw/pitch/roll, yaw positivo a sinistra, pitch positivo in alto,
  roll positivo clockwise. I nomi raw non vanno confusi con una basis grafica.

Worn: bit0 del byte8; prossimità/IR: LE16 bytes55–56. Il bit1 display è
interpretato in modo discordante da codice/wiki: viene esposto **raw**, mentre
alimentazione/modo dichiarati sono da F0. Il driver OpenHMD e il parser
PSVRFramework consultati integrano i campioni senza un gate su worn/prossimità;
non dimostrano né escludono un gate interno al firmware.

## Cosa è già emerso

Il vecchio recenter completo applicava un inverso a sinistra e inclinava il
riferimento del mondo. Con neutral inclinato di 30°, un yaw globale di −30°
può apparire come yaw −26.57° e roll +14.50°. Il recenter corretto separa
heading globale e allineamento locale a destra; il test sintetico di questo
caso passa senza coupling. Questo corregge un problema di riferimento,
non certifica ancora i segni di tutti i movimenti fisici.

## Prova da completare insieme

1. Calibrare immobile sul tavolo; indossare, mettere la testa dritta e Ricentra.
2. Muovere un asse alla volta, in entrambe le direzioni: sinistra/destra,
   su/giù, inclinazione sinistra/destra. Guardare gyro raw mappato e angoli
   indipendentemente. La capture resta attiva: nessun conto alla rovescia.
3. Ripetere worn/unworn a registrazione visibile, salvando flags/IR e dati raw.
4. Solo se i dati lo dimostrano, correggere una **rotazione di basis propria**
   coerente su gyro, accel e orientamento; evitare negazioni arbitrarie.

Una rotazione di 180° attorno a Y invertirebbe X/Z preservando Y, ma le fonti
e le prove attuali **non giustificano applicarla**. M3 rimane aperta per questa
verifica; M4 non va anticipata.
