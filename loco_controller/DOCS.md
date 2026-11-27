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

### Provare un colore (0.2.1)
Per tarare i colori della barra di luce: `loco/controller/prova` con `{"luce": [r, g, b], "secondi": 60}` mette quel
colore sopra a tutto (anche sopra a LOCO) per quei secondi, al massimo 10 minuti; `{"luce": null}` lo toglie subito

### 0.2.2: stick, taratura, Nest
- **Lo stick sinistro fa le frecce**: oltre il 55% accende il tasto Su, Giù, Sinistra o Destra (resta acceso fino al
  35%), come le frecce vere: sensori, eventi e l'automazione dell'Apple TV non vedono la differenza
- **Taratura dei colori**: `select.loco_controller_taratura_colore` sceglie uno dei 12 colori di LOCO e lo accende sul
  controller per 10 minuti; `light.loco_controller_taratura_luce` lo cambia (si salva da solo in /data/taratura.json);
  «Taratura ripristina» lo rimette come i segnapasso, «Taratura fine» spegne la prova. La tabella intera è negli
  attributi di `sensor.loco_controller_taratura`
- **Nest della cucina nel giro**: `switch.loco_controller_nest_nel_giro`, lo legge LOCO per il giro di Options
- Le fusa: LOCO può chiedere un ronzio leggero (`vibra` nello stato che manda)
- 0.2.3: il ronzio forte quando LOCO lo chiede (`vibra.forte`: il comando segreto giusto)
- 0.2.4: `sensor.loco_controller_destinazione` ha l'attributo `schermo` (tv-soggiorno, nest-cucina, schermo-soggiorno) quando comanda LOCO

### 0.3.0: più controller, ognuno il suo, e i controller per Switch (ADR 0224)
- **Ognuno va dove lo manda il suo Options**: LOCO manda `mani` {n: destinazione, schermo, luce, LED, ronzio} e ogni
  controller ha il suo `sensor.<controller>_destinazione` (`sensor.dualsense_2_destinazione`…). Quello generale
  `sensor.loco_controller_destinazione` resta ed è il controller 1
- **Controller per Switch** (il Pro Controller e quelli che si presentano come lui, come il DragonShock Nebula): si
  abbinano con lo stesso pulsante (sul controller il tasto di abbinamento) e diventano **Controller Switch N**, con gli
  stessi nomi dei tasti di LOCO: A = croce, B = cerchio, X = triangolo, Y = quadrato, L/R = L1/R1, ZL/ZR = L2/R2, − =
  Create, + = Options, Home = PS, Cattura = microfono. LED del giocatore (i 4 sotto), vibrazione, la luce accende e
  spegne il cerchio del tasto Home; niente barra di luce, touchpad, microfono e grilletti adattivi
- Con `diagnosi` accesa scrive nel registro i bit del rapporto che un Pro Controller vero non usa: se i tasti di dietro
  del Nebula mandano qualcosa, si vede lì
