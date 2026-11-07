# LOCO orologio

Fa da Echo all'**Echo Wall Clock** con una chiavetta Bluetooth (classico, BR/EDR) vista da Home Assistant: tiene
l'ora giusta (ogni mezz'ora e a ogni ricollegamento), calibra le lancette a 12:00, le rifasa alle 03:30 e mostra sui
segmenti i timer che gli manda LOCO. Il protocollo l'ha studiato Mike Janofsky
([echo-wall-clock](https://github.com/mpjanofsky/echo-wall-clock)); qui è riscritto da capo.

Ascolta sulla porta `porta` (8483) e risponde **solo** agli indirizzi in `da` (separati da virgole) e a questa macchina.

- `POST /orologio` `{"stanza", "timer": [{"id", "fine" (ms), "pausa"?, "suona"?}], "sveglia", "avviso" (ms)}`: cosa far vedere (un `avviso` nuovo = luce gialla al centro, 4 pulsazioni)
- `GET /orologio`: come sta
- `POST /comando` `{"cosa": "stato" | "abbina" | "calibra" | "correggi" | "ora" | "prova" | "squilla" | "avvisa" | "rifasa" | "dimentica", ...}`

`gettone`: se c'è, `/comando` vuole l'intestazione `X-Loco-Gettone`. `fuso`: il fuso orario dell'ora sulle lancette (Europe/Rome). `scheda`: l'adattatore Bluetooth (es. `hci1`); vuoto
= il primo che si accende.

**Abbinare**: tasto blu dietro l'orologio finché la luce davanti pulsa arancione, poi `{"cosa": "abbina"}`. Accetta
abbinamenti solo in quei 90 secondi e solo da un orologio Echo. Mai scritto sul canale 2 (firmware) dell'orologio.

## Da Home Assistant (1.1.0)

Ora, abbinamento e calibrazione si fanno solo da HA (da pc-01 restano timer, sveglie, avvisi e le prove).
L'add-on legge ogni 2 secondi questi aiutanti:

- `input_button.orologio_cucina_abbina`: 60 secondi di ricerca (prima tieni premuto il tasto dietro finché la luce pulsa arancione)
- `input_button.orologio_cucina_calibra`: lancette a 12:00 per 10 minuti
- `input_button.orologio_cucina_rimanda_l_ora`: rimanda l'ora subito
- `input_button.orologio_cucina_dimentica`: toglie l'abbinamento
- `input_number.orologio_cucina_correzione`: minuti di correzione (-30…30)
- `input_boolean.orologio_cucina_rifasatura_notte`: la rifasatura delle 03:30

e scrive `sensor.orologio_cucina_stato` (collegato, staccato, in abbinamento, in calibrazione, da abbinare)
