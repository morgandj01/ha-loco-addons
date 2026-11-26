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
- **Un sensore per tasto** (`binary_sensor.dualsense_N_tasto_croce`, `..._tasto_l1`…): acceso finché lo tieni
  premuto, quindi si vedono due tasti insieme e ogni pressione, anche ripetuta in fretta. Per le automazioni sono
  meglio dell'evento
- **Batteria**, **In carica**, **Collegato**, **Cuffie**, **Microfono spento**
- **Stick** (-100…100, su positivo) e **grilletti** (0…100): fino a dieci valori al secondo mentre si muovono, niente da fermi
- **Giroscopio** e **accelerometro**: spenti di base, si accendono dall'entità
- **Luce** (colore e luminosità), **LED giocatore** (spenti, 1…5), **LED microfono**
- **Grilletto L2** e **Grilletto R2** (i grilletti adattivi): libero, duro, molla, pistola (duro in mezzo e poi scatta),
  mitraglietta e mitragliatrice (vibrano mentre li tieni)
- **Vibra**, **Vibra forte**, **Spegni**, **Dimentica**
- **Spegnimento automatico** (per ogni controller): Mai, 5, 10, 15, 20, 30 minuti o 1 ora senza toccarlo e si
  spegne, come sulla PS5 (di base 10 minuti). Conta tasti, stick, grilletti e touchpad, non il giroscopio

`scheda`: la chiavetta da usare (es. `hci0`); vuoto = quella già accesa.

`diagnosi`: scrive nel registro il traffico Bluetooth vero (btmon) all'avvio e a ogni abbinamento. Per questo l'add-on
chiede NET_ADMIN e NET_RAW; quando i controller vanno si spegne e poi si toglie.

## Con LOCO (ADR 0221)
Dalla 0.2.0 il controller è il telecomando di casa: l'add-on manda lo stato intero del controller su MQTT
(`loco/controller/<dualsense>/veloce`, fuori dagli stati di HA) e LOCO (il ponte `loco-controller` su pc-01) lo porta
allo schermo giusto. LOCO decide anche **luce e LED** (`loco/controller/da_loco`) e **dove va il controller**:
`sensor.loco_controller_destinazione` = `appletv`, `loco`, `telecamere` o `windows`. L'automazione dell'Apple TV comanda
solo con `appletv`. Se LOCO tace da 30 secondi torna tutto come prima: Apple TV e la luce scelta qui in HA.
