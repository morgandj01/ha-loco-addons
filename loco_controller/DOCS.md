# LOCO controller

I **DualSense** (PS5) in Home Assistant, senza PS5. Usa la chiavetta Bluetooth (classico, BR/EDR) che HA già vede e che
usa anche l'add-on «LOCO orologio»: i due stanno insieme e questo non tocca niente dell'orologio (non prende l'agente
di base, non accende, non rinomina e non rende visibile la chiavetta, apre l'abbinamento solo nei suoi 90 secondi).

Il Bluetooth di HA accoglie il controller e ne fa un dispositivo HID (`/dev/hidrawN`), ma il sistema non ha il driver
del DualSense: l'add-on legge e scrive quel dispositivo e parla il suo protocollo (lo stesso del driver di Linux). Microfono e altoparlante del
controller col Bluetooth non vanno da nessuna parte fuori dalla PS5.

## Abbinare
1. Sul controller tieni premuti **Create** (il tastino a sinistra del touchpad) e **PS** finché la luce lampeggia veloce
2. In HA premi **LOCO controller › Abbina controller**: «Stato» dice cosa sta facendo
3. Compare il dispositivo **DualSense 1** (poi 2…). Le volte dopo basta premere PS

## Cosa c'è in HA (MQTT discovery, serve Mosquitto)
- **Tasti**: un'entità evento con un tipo per tasto (`croce`, `cerchio`, `quadrato`, `triangolo`, `l1`, `r1`, `l2`,
  `r2`, `l3`, `r3`, `create`, `options`, `ps`, `touchpad`, `microfono`, `su`, `giu`, `sinistra`, `destra`) e il suo
  `_lungo` quando lo tieni premuto 0,8 secondi
- **Batteria**, **In carica**, **Collegato**, **Cuffie**, **Microfono spento**
- **Stick** (-100…100, su positivo) e **grilletti** (0…100): al massimo due valori al secondo
- **Giroscopio** e **accelerometro**: spenti di base, si accendono dall'entità
- **Luce** (colore e luminosità), **LED giocatore** (spenti, 1…5), **LED microfono**
- **Grilletto L2** e **Grilletto R2** (i grilletti adattivi): libero, duro, molla, pistola (duro in mezzo e poi scatta),
  mitraglietta e mitragliatrice (vibrano mentre li tieni)
- **Vibra**, **Vibra forte**, **Spegni**, **Dimentica**

`scheda`: la chiavetta da usare (es. `hci0`); vuoto = quella già accesa.

`diagnosi`: scrive nel registro il traffico Bluetooth vero (btmon) all'avvio e a ogni abbinamento. Per questo l'add-on
chiede NET_ADMIN e NET_RAW; quando i controller vanno si spegne e poi si toglie.
