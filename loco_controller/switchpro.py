"""LOCO · il protocollo del Pro Controller di Nintendo sul Bluetooth, senza driver (27 novembre 2026, ADR 0224).

Morgan: «ho anche un dragonshock nebula (per switch) [...] ha due anelli colorati e ha anche lui lo slot giocatore, in
più ha 4 tasti programmabili dietro». I controller per Switch di altre marche si presentano alla Switch (e a noi) come
un Pro Controller: 057E:2009, nome «Pro Controller». Qui solo i byte, come per il DualSense in `dualsense.py`: leggere i
rapporti, scrivere i sottocomandi (modo pieno, giroscopio, vibrazione, LED del giocatore, luce del tasto Home). Le
posizioni sono quelle del lavoro di dekuNukem (Nintendo_Switch_Reverse_Engineering) e del driver di Linux
(drivers/hid/hid-nintendo.c), riscritte qui da quella descrizione.

Sul filo (via /dev/hidrawN di bluetoothd, senza l'intestazione HIDP):
- entrata 0x30: il rapporto pieno (dopo il sottocomando 0x03 0x30), 49 byte; 0x21 è uguale più la risposta a un
  sottocomando; 0x3F è il rapporto semplice che manda appena collegato
- uscita 0x01: [0x01, contatore 0-15, vibrazione 8 byte, sottocomando, argomenti]; 0x10: solo la vibrazione

I tasti diventano i nomi del DualSense, così il resto di LOCO (il ponte, il kernel, l'automazione dell'Apple TV, gli
schermi) non cambia: per funzione, non per posto. A conferma (croce), B indietro (cerchio), X triangolo, Y quadrato,
L/R = L1/R1, ZL/ZR = L2/R2 (tutto o niente), − = Create, + = Options, Home = PS, Cattura = il tasto del microfono (il
pulsante della cucina). Niente touchpad, niente barra di luce, niente microfono."""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass

NINTENDO, PRO_CONTROLLER = 0x057E, 0x2009
PIENO, RISPOSTA, SEMPLICE = 0x30, 0x21, 0x3F
LUNGO = 49
USCITA_SOTTOCOMANDO, USCITA_VIBRA = 0x01, 0x10
NEUTRO = bytes((0x00, 0x01, 0x40, 0x40))          # vibrazione ferma (un lato)

# rapporto pieno: (byte, bit) → nome di LOCO
TASTI = {
    (3, 0): "quadrato",   # Y
    (3, 1): "triangolo",  # X
    (3, 2): "cerchio",    # B
    (3, 3): "croce",      # A
    (3, 6): "r1",         # R
    (3, 7): "r2",         # ZR
    (4, 0): "create",     # −
    (4, 1): "options",    # +
    (4, 2): "r3",
    (4, 3): "l3",
    (4, 4): "ps",         # Home
    (4, 5): "microfono",  # Cattura
    (5, 0): "giu", (5, 1): "su", (5, 2): "destra", (5, 3): "sinistra",
    (5, 6): "l1",         # L
    (5, 7): "l2",         # ZL
}
# i bit che un Pro Controller vero non usa (SL e SR sono dei Joy-Con, 0x40 e 0x80 del byte 4 vuoti): se i tasti di dietro
# del Nebula si fanno vedere, è qui. Si scrivono nel registro con la diagnosi accesa (`spia`)
IGNOTI = {(3, 4): "sr destro", (3, 5): "sl destro", (4, 6): "byte 4 bit 6", (4, 7): "byte 4 bit 7",
          (5, 4): "sr sinistro", (5, 5): "sl sinistro"}
TUTTI_I_TASTI = ("croce", "cerchio", "quadrato", "triangolo", "l1", "r1", "l2", "r2", "l3", "r3", "create", "options",
                 "ps", "microfono", "su", "giu", "sinistra", "destra")
# i nomi scritti sui tasti, per HA
NOMI = {"croce": "A", "cerchio": "B", "triangolo": "X", "quadrato": "Y", "l1": "L", "r1": "R", "l2": "ZL", "r2": "ZR",
        "l3": "Stick sinistro", "r3": "Stick destro", "create": "Meno", "options": "Più", "ps": "Home",
        "microfono": "Cattura", "su": "Su", "giu": "Giù", "sinistra": "Sinistra", "destra": "Destra"}

# rapporto semplice (0x3F): byte 1 e 2, poi la croce come un orologio
SEMPLICE_TASTI = {(1, 0): "cerchio", (1, 1): "croce", (1, 2): "quadrato", (1, 3): "triangolo", (1, 4): "l1",
                  (1, 5): "r1", (1, 6): "l2", (1, 7): "r2", (2, 0): "create", (2, 1): "options", (2, 2): "l3",
                  (2, 3): "r3", (2, 4): "ps", (2, 5): "microfono"}
CROCE = ("su", "su_destra", "destra", "giu_destra", "giu", "giu_sinistra", "sinistra", "su_sinistra")
BATTERIA = {8: 100, 6: 70, 4: 40, 2: 15, 0: 0}


@dataclass
class Grezzo:
    """Quello che dice un rapporto, prima della calibrazione degli stick (12 bit, 0-4095)"""
    premuti: frozenset
    stick_sx: tuple[int, int]
    stick_dx: tuple[int, int]
    accelerometro: tuple[int, int, int] = (0, 0, 0)    # 4096 = 1 g (±8 g)
    giroscopio: tuple[int, int, int] = (0, 0, 0)       # 16,4 = 1 °/s (±2000)
    batteria: int = 0
    in_carica: bool = False
    ignoti: frozenset = frozenset()
    semplice: bool = False


def _stick(d: bytes) -> tuple[int, int]:
    return d[0] | (d[1] & 0x0F) << 8, d[1] >> 4 | d[2] << 4


def leggi(r: bytes) -> Grezzo | None:
    """Un rapporto letto da hidraw (col numero del rapporto davanti). None se non è dei nostri"""
    if len(r) >= 13 and r[0] in (PIENO, RISPOSTA):
        premuti = {nome for (b, bit), nome in TASTI.items() if r[b] >> bit & 1}
        ignoti = {nome for (b, bit), nome in IGNOTI.items() if r[b] >> bit & 1}
        acc = gir = (0, 0, 0)
        if r[0] == PIENO and len(r) >= 25:
            ax, ay, az, gx, gy, gz = struct.unpack_from("<6h", r, 13)
            acc, gir = (ax, ay, az), (gx, gy, gz)
        liv = r[2] >> 4
        return Grezzo(premuti=frozenset(premuti), stick_sx=_stick(r[6:9]), stick_dx=_stick(r[9:12]),
                      accelerometro=acc, giroscopio=gir, batteria=BATTERIA.get(liv & 0x0E, 0), in_carica=bool(liv & 1),
                      ignoti=frozenset(ignoti))
    if len(r) >= 12 and r[0] == SEMPLICE:
        premuti = {nome for (b, bit), nome in SEMPLICE_TASTI.items() if r[b] >> bit & 1}
        if r[3] < 8:
            premuti.update(CROCE[r[3]].split("_"))
        lx, ly, rx, ry = struct.unpack_from("<4H", r, 4)
        # 16 bit col centro a 0x8000 → 12 bit; y del rapporto semplice cresce verso il basso
        return Grezzo(premuti=frozenset(premuti), stick_sx=(lx >> 4, 4095 - (ly >> 4)),
                      stick_dx=(rx >> 4, 4095 - (ry >> 4)), semplice=True)
    return None


class Stick:
    """Il centro si prende dalle prime letture (stick lasciati) e la corsa si allarga se lo spingi più in là: i
    controller di altre marche spesso non hanno la calibrazione di fabbrica giusta, così non serve"""
    CORSA = 1350

    def __init__(self) -> None:
        self.centro: tuple[float, float] | None = None
        self.corsa = [float(self.CORSA)] * 4          # x-, x+, y-, y+

    def a_otto_bit(self, v: tuple[int, int]) -> tuple[int, int]:
        """12 bit (su = di più) → 0-255 col centro a 128 e y che cresce verso il basso, come il DualSense"""
        if self.centro is None:
            self.centro = (float(v[0]), float(v[1]))
        dx, dy = v[0] - self.centro[0], v[1] - self.centro[1]
        for i, d in ((0 if dx < 0 else 1, dx), (2 if dy < 0 else 3, dy)):
            self.corsa[i] = max(self.corsa[i], abs(d))
        x = dx / self.corsa[0 if dx < 0 else 1]
        y = dy / self.corsa[2 if dy < 0 else 3]
        return (max(0, min(255, round(128 + x * 127))), max(0, min(255, round(128 - y * 127))))


# --- l'uscita ----------------------------------------------------------------------------------------------------

def _freq(hz: float) -> int:
    return round(math.log2(hz / 10.0) * 32.0)


def _amp(a: float) -> int:
    """L'ampiezza 0-1 come la vuole il controller (la tabella di dekuNukem, in formula)"""
    if a <= 0:
        return 0
    if a > 0.23:
        return round(math.log2(a * 8.7) * 32)
    if a > 0.12:
        return round(math.log2(a * 17) * 16)
    return round(((math.log2(a) * 32) - 96) / (4 - 2 * a))


def vibra_lato(forte: float, alta_hz: float = 320.0, bassa_hz: float = 160.0) -> bytes:
    """4 byte di vibrazione per un lato; 0 = ferma (00 01 40 40)"""
    a = max(0.0, min(1.0, forte))
    hf = (_freq(alta_hz) - 0x60) * 4
    lf = _freq(bassa_hz) - 0x40
    e = _amp(a)
    hf_amp, lf_amp = e * 2, (e // 2 + 64 if e else 64)
    return bytes((hf & 0xFF, (hf_amp + (hf >> 8 & 0xFF)) & 0xFF, (lf + (lf_amp >> 8 & 0xFF)) & 0xFF, lf_amp & 0xFF))


def vibra(sx: float, dx: float) -> bytes:
    return vibra_lato(sx) + vibra_lato(dx)


def sottocomando(n: int, cmd: int, arg: bytes = b"", vib: bytes | None = None) -> bytes:
    r = bytearray(LUNGO)
    r[0], r[1] = USCITA_SOTTOCOMANDO, n & 0x0F
    r[2:10] = vib or NEUTRO * 2
    r[10] = cmd
    r[11:11 + len(arg)] = arg
    return bytes(r)


def solo_vibra(n: int, vib: bytes) -> bytes:
    r = bytearray(LUNGO)
    r[0], r[1] = USCITA_VIBRA, n & 0x0F
    r[2:10] = vib
    return bytes(r)


MODO = 0x03                  # arg 0x30: il rapporto pieno, 60 al secondo
IMU = 0x40                   # arg 1: giroscopio e accelerometro accesi
VIBRAZIONE = 0x48            # arg 1: vibrazione accesa
LED_GIOCATORE = 0x30         # arg: i 4 LED (bit 0-3 accesi, 4-7 lampeggiano)
LUCE_HOME = 0x38             # il cerchio di luce del tasto Home


def led_giocatore(g: int) -> bytes:
    """1-4 come la Switch di adesso (il giocatore 2 = i primi due LED, il 4 = tutti), 5 = tutti, 0 = spenti"""
    g = max(0, min(5, int(g)))
    return bytes(((1 << min(g, 4)) - 1,))


def luce_home(accesa: bool) -> bytes:
    """Fissa al massimo o spenta (come il driver di Linux, joycon_home_led_brightness_set)"""
    b = 0x0F if accesa else 0x00
    return bytes((0x01, b << 4, b | b << 4, 0x11, 0x11))


def avvio() -> list[tuple[int, bytes]]:
    """I sottocomandi da mandare appena si collega, in ordine"""
    return [(MODO, bytes((PIENO,))), (IMU, b"\x01"), (VIBRAZIONE, b"\x01")]


def e_un_pro_controller(nome: str = "", modalias: str = "") -> bool:
    m = (modalias or "").lower()
    if m.startswith("usb:v057ep2009"):
        return True
    return (nome or "").strip().lower() == "pro controller"
