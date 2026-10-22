# LOCO orologio

Fa da Echo all'**Echo Wall Clock** con una chiavetta Bluetooth (classico, BR/EDR) vista da Home Assistant: tiene
l'ora giusta (ogni mezz'ora e a ogni ricollegamento), calibra le lancette a 12:00, le rifasa alle 03:30 e mostra sui
segmenti i timer che gli manda LOCO. Il protocollo l'ha studiato Mike Janofsky
([echo-wall-clock](https://github.com/mpjanofsky/echo-wall-clock)); qui è riscritto da capo.

Ascolta sulla porta `porta` (8483) e risponde **solo** agli indirizzi in `da` (separati da virgole) e a questa macchina.

- `POST /orologio` `{"stanza", "timer": [{"id", "fine" (ms), "pausa"?, "suona"?}], "sveglia"}`: cosa far vedere
- `GET /orologio`: come sta
- `POST /comando` `{"cosa": "stato" | "abbina" | "calibra" | "correggi" | "ora" | "prova" | "squilla" | "rifasa" | "dimentica", ...}`

`gettone`: se c'è, `/comando` vuole l'intestazione `X-Loco-Gettone`. `fuso`: il fuso orario dell'ora sulle lancette (Europe/Rome). `scheda`: l'adattatore Bluetooth (es. `hci1`); vuoto
= il primo che si accende.

**Abbinare**: tasto blu dietro l'orologio finché la luce davanti pulsa arancione, poi `{"cosa": "abbina"}`. Accetta
abbinamenti solo in quei 90 secondi e solo da un orologio Echo. Mai scritto sul canale 2 (firmware) dell'orologio.
