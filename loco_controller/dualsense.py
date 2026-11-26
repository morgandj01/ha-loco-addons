"""LOCO · il protocollo del DualSense (PS5) sul Bluetooth, senza driver (26 novembre 2026, ADR 0220).

Il sistema di HA non ha la parte del Bluetooth che accoglie i controller (provato il 26: «Pairing successful» e poi
«br-connection-create-socket»), quindi l'add-on «LOCO controller» parla col DualSense da sé, sui due canali L2CAP
dell'HID (controllo 0x11, dati 0x13). Qui ci sono solo i byte: leggere il rapporto pieno 0x31 e scrivere quello di
uscita (luce, LED del giocatore, vibrazione, LED del microfono). Le posizioni e le costanti sono quelle del driver di
Linux (drivers/hid/hid-playstation.c, scritto da Sony: `struct dualsense_input_report`,
`dualsense_output_report_common`, `DS_OUTPUT_*`), riscritte qui da quella descrizione.

Sul filo: ogni pacchetto L2CAP comincia col byte HIDP (0xA1 = dati in ingresso, 0xA2 = dati in uscita, 0x43 = chiedi
un rapporto «feature»), poi il rapporto. Il CRC32 in fondo ai rapporti Bluetooth si calcola partendo proprio da quel
byte (il «seme» del driver), quindi è zlib.crc32 sul pacchetto intero senza gli ultimi 4 byte."""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

PSM_CONTROLLO, PSM_DATI = 0x11, 0x13
HIDP_DATI_ENTRATA, HIDP_DATI_USCITA = 0xA1, 0xA2
HIDP_CHIEDI_FEATURE, HIDP_RISPOSTA_FEATURE = 0x43, 0xA3
RAPPORTO_PIENO = 0x31                       # entrata e uscita sul Bluetooth, 78 byte senza l'intestazione HIDP
LUNGO = 78
CALIBRAZIONE = 0x05                         # chiederlo fa passare il controller dal rapporto corto 0x01 a quello pieno
SONY, DUALSENSE, DUALSENSE_EDGE = 0x054C, 0x0CE6, 0x0DF2

# la croce (buttons[0], 4 bit bassi): 0 su, poi in senso orario, 8 = niente
CROCE = ("su", "su_destra", "destra", "giu_destra", "giu", "giu_sinistra", "sinistra", "su_sinistra")
# (byte di buttons, bit) → nome del tasto, come in hid-playstation (DS_BUTTONS*)
TASTI = {
    (0, 4): "quadrato", (0, 5): "croce", (0, 6): "cerchio", (0, 7): "triangolo",
    (1, 0): "l1", (1, 1): "r1", (1, 2): "l2", (1, 3): "r2", (1, 4): "create", (1, 5): "options",
    (1, 6): "l3", (1, 7): "r3",
    (2, 0): "ps", (2, 1): "touchpad", (2, 2): "microfono",
}
TUTTI_I_TASTI = tuple(TASTI.values()) + ("su", "giu", "sinistra", "destra")
# i LED bianchi sotto il touchpad, come li accende la PS5 (player_ids del driver)
GIOCATORE = {0: 0, 1: 0b00100, 2: 0b01010, 3: 0b10101, 4: 0b11011, 5: 0b11111}


def crc_valido(pacchetto: bytes) -> bool:
    """Il pacchetto intero (col byte HIDP davanti): gli ultimi 4 byte sono il CRC32 di tutto quello che c'è prima"""
    if len(pacchetto) < 5:
        return False
    return zlib.crc32(pacchetto[:-4]) & 0xFFFFFFFF == struct.unpack_from("<I", pacchetto, len(pacchetto) - 4)[0]


@dataclass
class Stato:
    """Quello che il controller dice in un rapporto pieno"""
    stick_sx: tuple[int, int] = (128, 128)  # 0-255, 128 al centro; y cresce verso il basso
    stick_dx: tuple[int, int] = (128, 128)
    l2: int = 0                             # 0-255
    r2: int = 0
    premuti: frozenset[str] = field(default_factory=frozenset)
    giroscopio: tuple[int, int, int] = (0, 0, 0)       # grezzo, 1024 = 1 °/s
    accelerometro: tuple[int, int, int] = (0, 0, 0)    # grezzo, 8192 = 1 g
    tocchi: tuple[tuple[int, int] | None, tuple[int, int] | None] = (None, None)   # 1920×1080
    batteria: int = 0                       # percento
    in_carica: bool = False
    carica_piena: bool = False
    cuffie: bool = False
    microfono_spento: bool = False


def leggi(pacchetto: bytes) -> Stato | None:
    """Un pacchetto del canale dati. None se non è un rapporto pieno valido (il rapporto corto 0x01 lo manda prima di
    aver chiesto la calibrazione, e un CRC sbagliato si butta come fa il driver)"""
    if len(pacchetto) != 1 + LUNGO or pacchetto[0] != HIDP_DATI_ENTRATA or pacchetto[1] != RAPPORTO_PIENO:
        return None
    if not crc_valido(pacchetto):
        return None
    r = pacchetto[3:]                       # dopo HIDP, numero del rapporto e un byte di etichetta
    tasti = r[7:11]
    premuti = {nome for (b, bit), nome in TASTI.items() if tasti[b] >> bit & 1}
    croce = tasti[0] & 0x0F
    if croce < 8:
        premuti.update(p for p in CROCE[croce].split("_"))
    gx, gy, gz, ax, ay, az = struct.unpack_from("<6h", r, 15)

    def tocco(i: int):
        p = r[32 + 4 * i: 36 + 4 * i]
        if p[0] & 0x80:                     # DS_TOUCH_POINT_INACTIVE
            return None
        return (p[1] | (p[2] & 0x0F) << 8, (p[2] >> 4) | p[3] << 4)

    s0, s1 = r[52], r[53]
    carica = s0 >> 4
    return Stato(stick_sx=(r[0], r[1]), stick_dx=(r[2], r[3]), l2=r[4], r2=r[5], premuti=frozenset(premuti),
                 giroscopio=(gx, gy, gz), accelerometro=(ax, ay, az), tocchi=(tocco(0), tocco(1)),
                 batteria=100 if carica == 2 else min((s0 & 0x0F) * 10 + 5, 100),
                 in_carica=carica == 1, carica_piena=carica == 2, cuffie=bool(s1 & 0x01),
                 microfono_spento=bool(s1 & 0x04))


def chiedi_calibrazione() -> bytes:
    """Da mandare sul canale di controllo appena il controller si collega: dopo, manda solo rapporti pieni"""
    return bytes((HIDP_CHIEDI_FEATURE, CALIBRAZIONE))


@dataclass
class Uscita:
    """Quello che si vuole far fare al controller. Ogni campo None = non toccarlo"""
    luce: tuple[int, int, int] | None = None
    giocatore: int | None = None            # 0-5 (GIOCATORE)
    vibra: tuple[int, int] | None = None    # motore destro (leggero), sinistro (forte), 0-255
    led_microfono: bool | None = None
    spegni_animazione: bool = False         # la prima volta: toglie l'animazione blu dell'accensione, come il driver


def scrivi(u: Uscita, seq: int) -> bytes:
    """Il pacchetto da mandare sul canale dati (col byte HIDP 0xA2 davanti e il CRC in fondo). seq: 0-15, uno in più a
    ogni pacchetto"""
    r = bytearray(LUNGO)
    r[0] = RAPPORTO_PIENO
    r[1] = (seq & 0x0F) << 4
    r[2] = 0x10                             # DS_OUTPUT_TAG: «va messo, il perché non si sa» (dal driver)
    c = 3                                   # da qui `dualsense_output_report_common`, 47 byte
    if u.vibra is not None:
        r[c + 0] |= 0x01 | 0x02             # vibrazione compatibile + scelta della vibrazione
        r[c + 38] |= 0x04                   # valid_flag2: e la vibrazione «2» dei firmware nuovi
        r[c + 2], r[c + 3] = (max(0, min(255, int(v))) for v in u.vibra)
    if u.led_microfono is not None:
        r[c + 1] |= 0x01
        r[c + 8] = 1 if u.led_microfono else 0
    if u.luce is not None:
        r[c + 1] |= 0x04
        r[c + 44], r[c + 45], r[c + 46] = (max(0, min(255, int(v))) for v in u.luce)
    if u.giocatore is not None:
        r[c + 1] |= 0x10
        r[c + 43] = GIOCATORE.get(int(u.giocatore), 0)
    if u.spegni_animazione:
        r[c + 38] |= 0x02                   # valid_flag2: comando l'accensione della luce
        r[c + 41] = 0x02                    # DS_OUTPUT_LIGHTBAR_SETUP_LIGHT_OUT
    pacchetto = bytes((HIDP_DATI_USCITA,)) + bytes(r[:-4])
    return pacchetto + struct.pack("<I", zlib.crc32(pacchetto) & 0xFFFFFFFF)


def e_un_dualsense(nome: str = "", modalias: str = "") -> bool:
    """Dal nome che dà nella ricerca o dal modalias di BlueZ (usb:v054Cp0CE6d0100)"""
    m = (modalias or "").lower()
    if m.startswith("usb:v054cp0ce6") or m.startswith("usb:v054cp0df2"):
        return True
    return (nome or "").strip().lower() in ("dualsense wireless controller", "dualsense edge wireless controller")
