"""LOCO controller · i DualSense in Home Assistant sulla chiavetta Bluetooth dell'orologio (26 novembre 2026, ADR 0220).

Morgan: «continua a scrivere la cosa di HA [...] stai attento a non fare nulla all'orologio». Il sistema di HA non ha
il driver del DualSense, quindi lo fa questo programma: abbina il DualSense con BlueZ (D-Bus), lascia i canali HID a
bluetoothd che ne fa un /dev/hidrawN, e lì parla il protocollo di `dualsense.py`. In HA tutto arriva da MQTT
discovery: un dispositivo «LOCO controller» col pulsante per abbinare e uno per ogni controller con batteria, tasti
(come eventi), stick, grilletti, giroscopio, luce, LED del giocatore, LED del microfono, vibrazione, spegni, dimentica.

L'orologio usa la stessa chiavetta (add-on «LOCO orologio»). Qui non si tocca niente di suo:
- niente agente «di base» (resta il suo): l'abbinamento lo chiediamo noi e BlueZ usa il nostro agente solo per quello
- `Pairable` si accende solo nei 90 secondi dell'abbinamento e torna com'era (l'orologio lo tiene spento)
- mai Powered, Discoverable, Alias, Class della chiavetta; la ricerca solo nella finestra di abbinamento
- si toglie (Dimentica) solo un indirizzo che è nostro e che BlueZ dice essere un DualSense
- i canali HID li tiene bluetoothd e non c'entrano con la porta seriale dell'orologio (RFCOMM); noi apriamo solo
  l'hidraw che ha l'indirizzo di un nostro DualSense

Nessun tasto comanda niente da qui: i tasti diventano eventi in HA e cosa fanno lo decidono le automazioni e LOCO. Mai
cancello, allarme, serrature, sirene o telecamere (FORBIDDEN)."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib import request as _rq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dualsense as ds  # noqa: E402
import switchpro as sp  # noqa: E402

MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
BASE = "loco/controller"
DISCOVERY = "homeassistant"
AZZURRO = (36, 118, 254)                  # gli occhi di LOCO
LUNGO_S = 0.8                             # un tasto tenuto così è «lungo» (il PS lungo farà ascoltare LOCO)
FINESTRA_S = 90
NOMI_LED = ("Spenti", "1", "2", "3", "4", "5")


def scrivi(riga: str) -> None:
    print(f"[controller] {riga}", flush=True)


# --- le parti senza Bluetooth (provate in core/tests/test_controller_26nov.py) ----------------------------------------

class Tastiera:
    """Dai tasti premuti adesso agli eventi: «croce» quando lo premi, «croce_lungo» quando lo tieni LUNGO_S"""
    def __init__(self) -> None:
        self.giu: dict[str, float] = {}
        self.lunghi: set[str] = set()

    def passa(self, premuti: frozenset[str], adesso: float) -> list[str]:
        eventi = [t for t in sorted(premuti) if t not in self.giu]
        for t in eventi:
            self.giu[t] = adesso
        for t in list(self.giu):
            if t not in premuti:
                del self.giu[t]
                self.lunghi.discard(t)
            elif t not in self.lunghi and adesso - self.giu[t] >= LUNGO_S:
                self.lunghi.add(t)
                eventi.append(f"{t}_lungo")
        return eventi


# i nomi in HA dei sensori dei tasti (uno per tasto: acceso finché lo tieni premuto)
NOMI_TASTI = {"croce": "Croce", "cerchio": "Cerchio", "quadrato": "Quadrato", "triangolo": "Triangolo",
              "l1": "L1", "r1": "R1", "l2": "L2", "r2": "R2", "l3": "L3 (stick sinistro)", "r3": "R3 (stick destro)",
              "create": "Create", "options": "Options", "ps": "PS", "touchpad": "Touchpad", "microfono": "Microfono",
              "su": "Su", "giu": "Giù", "sinistra": "Sinistra", "destra": "Destra"}


# lo spegnimento da solo quando non lo usi (come la PS5): si sceglie in HA, per ogni controller
RIPOSO = {"Mai": 0, "5 minuti": 300, "10 minuti": 600, "15 minuti": 900, "20 minuti": 1200, "30 minuti": 1800,
          "1 ora": 3600}
RIPOSO_DI_BASE = "10 minuti"                # come la PS5


def in_uso(s: ds.Stato) -> bool:
    """Qualcuno lo sta usando: un tasto, uno stick fuori dalla zona morta, un grilletto, un dito sul touchpad. Il
    giroscopio no: un controller appoggiato sul tavolo trema da solo"""
    return bool(s.premuti) or any(asse(v) for v in (*s.stick_sx, *s.stick_dx)) or s.l2 > 12 or s.r2 > 12 \
        or s.tocchi[0] is not None


def tasti_cambiati(prima: frozenset[str], adesso: frozenset[str]) -> dict[str, bool]:
    """Solo i tasti che sono cambiati: premuto (True) o lasciato (False)"""
    return {k: k in adesso for k in sorted(prima ^ adesso)}


def tasti_di(tipo: str = "dualsense") -> tuple[str, ...]:
    return sp.TUTTI_I_TASTI if tipo == "switch" else ds.TUTTI_I_TASTI


def tipi_di_evento(tipo: str = "dualsense") -> list[str]:
    t = tasti_di(tipo)
    return list(t) + [f"{x}_lungo" for x in t]


def asse(v: int) -> int:
    """0-255 col centro a 128 → -100..100, con una piccola zona morta (gli stick non tornano mai a 128 preciso)"""
    x = (v - 128) / 127 * 100
    x = 0 if abs(x) < 6 else x
    return max(-100, min(100, round(x)))


def grandezze(s: ds.Stato) -> dict[str, float]:
    """Quello che va in HA come numeri. y degli stick: verso l'alto positivo"""
    return {"stick_sx_x": asse(s.stick_sx[0]), "stick_sx_y": -asse(s.stick_sx[1]),
            "stick_dx_x": asse(s.stick_dx[0]), "stick_dx_y": -asse(s.stick_dx[1]),
            "l2": round(s.l2 / 255 * 100), "r2": round(s.r2 / 255 * 100),
            "giro_x": round(s.giroscopio[0] / 1024, 1), "giro_y": round(s.giroscopio[1] / 1024, 1),
            "giro_z": round(s.giroscopio[2] / 1024, 1),
            "acc_x": round(s.accelerometro[0] / 8192, 2), "acc_y": round(s.accelerometro[1] / 8192, 2),
            "acc_z": round(s.accelerometro[2] / 8192, 2)}


def frecce_stick(stick_sx: tuple, prima: frozenset) -> frozenset:
    """Lo stick sinistro come le frecce (Morgan, 26 notte: «navigare nei menu dappertutto con l'analogico sinistro allo
    stesso modo delle freccette»): la direzione più spinta, oltre il 55%; una già accesa resta fino al 35% (niente
    tremolio a metà). Valgono come le frecce vere: sensori di HA, automazione dell'Apple TV, schermi"""
    x, y = asse(stick_sx[0]), -asse(stick_sx[1])
    if abs(x) >= abs(y):
        d, v = ("destra" if x > 0 else "sinistra"), abs(x)
    else:
        d, v = ("su" if y > 0 else "giu"), abs(y)
    if v >= 55 or (d in prima and v >= 35):
        return frozenset({d})
    return frozenset()


class Limitatore:
    """Il controller manda centinaia di rapporti al secondo: in HA va un valore ogni `ogni` secondi al massimo e solo
    se è cambiato abbastanza (`soglia`), e l'ultimo valore arriva sempre (a riposo dopo un giro ci finisce)"""
    def __init__(self, ogni: float, soglia: float) -> None:
        self.ogni, self.soglia = ogni, soglia
        self.mandato: dict[str, float] = {}
        self.quando: dict[str, float] = {}

    def da_mandare(self, valori: dict[str, float], adesso: float) -> dict[str, float]:
        fuori = {}
        for k, v in valori.items():
            prima = self.mandato.get(k)
            if prima is not None and (v == prima or adesso - self.quando.get(k, 0) < self.ogni):
                continue
            if prima is not None and abs(v - prima) < self.soglia and v != 0:
                continue
            fuori[k] = v
            self.mandato[k], self.quando[k] = v, adesso
        return fuori


# i tipi di controller (27 novembre, ADR 0224): il DualSense e i controller per Switch (il DragonShock Nebula di
# Morgan) che si presentano come un Pro Controller di Nintendo. Nomi in HA e prefisso delle entità
TIPI = {"dualsense": {"nome": "DualSense", "prefisso": "dualsense", "marca": "Sony", "modello": "DualSense"},
        "switch": {"nome": "Controller Switch", "prefisso": "controller_switch", "marca": "Nintendo (o compatibile)",
                   "modello": "Pro Controller"}}


def tipo_da(nome: str = "", modalias: str = "") -> str | None:
    """Che controller è, dal nome nella ricerca o dal modalias di BlueZ. None = non è dei nostri"""
    if ds.e_un_dualsense(nome, modalias):
        return "dualsense"
    if sp.e_un_pro_controller(nome, modalias):
        return "switch"
    return None


def oggetto(mac: str, tipo: str = "dualsense") -> str:
    return ("switch_" if tipo == "switch" else "dualsense_") + mac.replace(":", "").lower()


def nome_di(c: dict) -> str:
    return f"{TIPI.get(c.get('tipo') or 'dualsense', TIPI['dualsense'])['nome']} {c.get('numero', '?')}"


def configurazioni(mac: str, numero: int, tipo: str = "dualsense") -> list[tuple[str, dict]]:
    """Le entità MQTT discovery di un controller: (topic di configurazione, configurazione)"""
    o = oggetto(mac, tipo)
    t = f"{BASE}/{o}"
    ti = TIPI.get(tipo, TIPI["dualsense"])
    sw = tipo == "switch"
    disp = {"identifiers": [o], "name": f"{ti['nome']} {numero}", "manufacturer": ti["marca"], "model": ti["modello"],
            "connections": [["bluetooth", mac.lower()]], "via_device": "loco_controller"}
    vivo = [{"topic": f"{BASE}/vivo"}, {"topic": f"{t}/vivo"}]

    def e(tipo_e: str, chiave: str, nome: str, **altro) -> tuple[str, dict]:
        c = {"name": nome, "unique_id": f"{o}_{chiave}",
             "default_entity_id": f"{tipo_e}.{ti['prefisso']}_{numero}_{chiave}",
             "device": disp, "availability": vivo, "availability_mode": "all"}
        c.update(altro)
        return f"{DISCOVERY}/{tipo_e}/{o}/{chiave}/config", c

    out = [
        e("binary_sensor", "collegato", "Collegato", state_topic=f"{t}/collegato", device_class="connectivity",
          availability=[{"topic": f"{BASE}/vivo"}], availability_mode="any", payload_on="si", payload_off="no"),
        e("sensor", "batteria", "Batteria", state_topic=f"{t}/batteria", device_class="battery",
          unit_of_measurement="%", state_class="measurement"),
        e("binary_sensor", "in_carica", "In carica", state_topic=f"{t}/in_carica", device_class="battery_charging",
          payload_on="si", payload_off="no"),
        e("binary_sensor", "cuffie", "Cuffie", state_topic=f"{t}/cuffie", device_class="plug", payload_on="si",
          payload_off="no", entity_category="diagnostic"),
        e("binary_sensor", "microfono_spento", "Microfono spento", state_topic=f"{t}/microfono_spento",
          payload_on="si", payload_off="no", icon="mdi:microphone-off"),
        e("event", "tasti", "Tasti", state_topic=f"{t}/tasti", event_types=tipi_di_evento(tipo),
          icon="mdi:gamepad-variant"),
        # 27 nov (ADR 0224): dove va questo controller, ognuno il suo (l'automazione dell'Apple TV guarda questo)
        e("sensor", "destinazione", "Destinazione", state_topic=f"{t}/destinazione",
          json_attributes_topic=f"{t}/destinazione/attributi", icon="mdi:target"),
        e("light", "luce", "Luce", schema="json", command_topic=f"{t}/luce/set", state_topic=f"{t}/luce",
          supported_color_modes=["rgb"], brightness=True, icon="mdi:led-strip"),
        e("select", "giocatore", "LED giocatore", command_topic=f"{t}/giocatore/set", state_topic=f"{t}/giocatore",
          options=list(NOMI_LED), icon="mdi:numeric"),
        e("switch", "led_microfono", "LED microfono", command_topic=f"{t}/led_microfono/set",
          state_topic=f"{t}/led_microfono", payload_on="si", payload_off="no", icon="mdi:microphone-message"),
        e("select", "grilletto_sx", "Grilletto L2", command_topic=f"{t}/grilletto_sx/set",
          state_topic=f"{t}/grilletto_sx", options=list(ds.GRILLETTO), icon="mdi:pistol"),
        e("select", "riposo", "Spegnimento automatico", command_topic=f"{t}/riposo/set", state_topic=f"{t}/riposo",
          options=list(RIPOSO), icon="mdi:timer-sleep", entity_category="config"),
        e("select", "grilletto_dx", "Grilletto R2", command_topic=f"{t}/grilletto_dx/set",
          state_topic=f"{t}/grilletto_dx", options=list(ds.GRILLETTO), icon="mdi:pistol"),
        e("button", "vibra", "Vibra", command_topic=f"{t}/vibra", payload_press="corta", icon="mdi:vibrate"),
        e("button", "vibra_forte", "Vibra forte", command_topic=f"{t}/vibra", payload_press="forte",
          icon="mdi:vibrate"),
        e("button", "spegni", "Spegni", command_topic=f"{t}/spegni", payload_press="si", icon="mdi:power",
          availability=vivo),
        e("button", "dimentica", "Dimentica", command_topic=f"{t}/dimentica", payload_press="si",
          icon="mdi:link-variant-off", entity_category="config", availability=[{"topic": f"{BASE}/vivo"}],
          availability_mode="any"),
    ]
    if sw:
        # il Pro Controller non ha cuffie, microfono, LED del microfono né grilletti adattivi
        via = {"cuffie", "microfono_spento", "led_microfono", "grilletto_sx", "grilletto_dx"}
        out = [x for x in out if x[0].split("/")[-2] not in via]
    nomi = sp.NOMI if sw else NOMI_TASTI
    for k in tasti_di(tipo):
        out.append(e("binary_sensor", f"tasto_{k}", f"Tasto {nomi.get(k, k)}", state_topic=f"{t}/tasto/{k}",
                     payload_on="si", payload_off="no", icon="mdi:gamepad-circle"))
    for k, nome, unita, acceso in (("stick_sx_x", "Stick sinistro X", "%", True), ("stick_sx_y", "Stick sinistro Y", "%", True),
                                   ("stick_dx_x", "Stick destro X", "%", True), ("stick_dx_y", "Stick destro Y", "%", True),
                                   ("l2", "Grilletto L2", "%", True), ("r2", "Grilletto R2", "%", True),
                                   ("giro_x", "Giroscopio X", "°/s", False), ("giro_y", "Giroscopio Y", "°/s", False),
                                   ("giro_z", "Giroscopio Z", "°/s", False), ("acc_x", "Accelerometro X", "g", False),
                                   ("acc_y", "Accelerometro Y", "g", False), ("acc_z", "Accelerometro Z", "g", False)):
        out.append(e("sensor", k, nome, state_topic=f"{t}/{k}", unit_of_measurement=unita, state_class="measurement",
                     enabled_by_default=acceso, icon="mdi:axis-arrow" if k.startswith(("giro", "acc")) else "mdi:gamepad"))
    return out


# la taratura dei colori della barra di luce (Morgan, 26 notte: «creami su HA una dashboard [...] che mi fa calibrare
# ogni colore e lo vedo direttamente sul controller»): i 12 colori di LOCO (timer e occhi), di partenza come i segnapasso
# (loco-self world/segnapasso.yaml `colori_timer`). Scegli il colore, lo cambi dalla luce «Taratura», il controller lo
# mostra subito; la tabella sta in /data e nel sensore «Taratura». LOCO la prende da lì (`colori_controller`)
TARATURA_BASE = {"blu": "#0030ff", "azzurro": "#00b2ff", "ciano": "#00fffa", "verde": "#00ff30", "giallo": "#ffd000",
                 "ambra": "#ffb600", "arancione": "#ff7826", "rosso": "#ff0008", "rosa": "#ff84bb",
                 "magenta": "#ff00c0", "viola": "#c907ff", "bianco": "#ffffff"}
NESSUNO = "nessuno"
TARATURA_S = 600


def hex_rgb(h: str) -> tuple[int, int, int]:
    n = int(h[1:], 16)
    return ((n >> 16) & 255, (n >> 8) & 255, n & 255)


def rgb_hex(c) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in c)


def configurazioni_centro() -> list[tuple[str, dict]]:
    disp = {"identifiers": ["loco_controller"], "name": "LOCO controller", "manufacturer": "LOCO",
            "model": "Add-on di HA (ADR 0220)"}
    vivo = [{"topic": f"{BASE}/vivo"}]
    return [
        (f"{DISCOVERY}/button/loco_controller/abbina/config",
         {"name": "Abbina controller", "unique_id": "loco_controller_abbina", "default_entity_id": "button.loco_controller_abbina",
          "command_topic": f"{BASE}/abbina", "payload_press": "si", "device": disp, "availability": vivo,
          "icon": "mdi:bluetooth-connect"}),
        (f"{DISCOVERY}/sensor/loco_controller/stato/config",
         {"name": "Stato", "unique_id": "loco_controller_stato", "default_entity_id": "sensor.loco_controller_stato",
          "state_topic": f"{BASE}/stato", "device": disp, "availability": vivo, "icon": "mdi:bluetooth"}),
        # dove va il controller adesso, lo decide LOCO (ADR 0221): appletv, loco, telecamere, windows. L'automazione
        # dell'Apple TV comanda solo con «appletv»
        (f"{DISCOVERY}/sensor/loco_controller/destinazione/config",
         {"name": "Destinazione", "unique_id": "loco_controller_destinazione",
          "default_entity_id": "sensor.loco_controller_destinazione", "state_topic": f"{BASE}/destinazione",
          # quale schermo, quando comanda LOCO (27 nov: con LOCO sulla TV triangolo fa ricomparire l'Apple TV, sul Nest no)
          "json_attributes_topic": f"{BASE}/destinazione/attributi",
          "device": disp, "availability": vivo, "icon": "mdi:target"}),
        # il Nest della cucina nel giro di Options (Morgan: «mettimi un interruttore in HA che lo mette e toglie dal giro,
        # non serve quasi mai»): lo legge il ponte loco-controller e lo dice al kernel
        (f"{DISCOVERY}/switch/loco_controller/nest_giro/config",
         {"name": "Nest della cucina nel giro", "unique_id": "loco_controller_nest_giro",
          "default_entity_id": "switch.loco_controller_nest_nel_giro", "command_topic": f"{BASE}/nest_giro/set",
          "state_topic": f"{BASE}/nest_giro", "payload_on": "si", "payload_off": "no", "device": disp,
          "availability": vivo, "icon": "mdi:google-nest"}),
        (f"{DISCOVERY}/select/loco_controller/taratura_colore/config",
         {"name": "Taratura colore", "unique_id": "loco_controller_taratura_colore",
          "default_entity_id": "select.loco_controller_taratura_colore", "command_topic": f"{BASE}/taratura/colore/set",
          "state_topic": f"{BASE}/taratura/colore", "options": [NESSUNO, *TARATURA_BASE], "device": disp,
          "availability": vivo, "icon": "mdi:palette"}),
        (f"{DISCOVERY}/light/loco_controller/taratura_luce/config",
         {"name": "Taratura luce", "unique_id": "loco_controller_taratura_luce",
          "default_entity_id": "light.loco_controller_taratura_luce", "schema": "json",
          "command_topic": f"{BASE}/taratura/luce/set", "state_topic": f"{BASE}/taratura/luce",
          "supported_color_modes": ["rgb"], "brightness": False, "device": disp, "availability": vivo,
          "icon": "mdi:palette-swatch"}),
        (f"{DISCOVERY}/sensor/loco_controller/taratura/config",
         {"name": "Taratura", "unique_id": "loco_controller_taratura", "default_entity_id": "sensor.loco_controller_taratura",
          "state_topic": f"{BASE}/taratura/stato", "json_attributes_topic": f"{BASE}/taratura/tabella",
          "device": disp, "availability": vivo, "icon": "mdi:palette-advanced"}),
        (f"{DISCOVERY}/button/loco_controller/taratura_ripristina/config",
         {"name": "Taratura ripristina", "unique_id": "loco_controller_taratura_ripristina",
          "default_entity_id": "button.loco_controller_taratura_ripristina", "command_topic": f"{BASE}/taratura/ripristina",
          "payload_press": "si", "device": disp, "availability": vivo, "icon": "mdi:restore"}),
        (f"{DISCOVERY}/button/loco_controller/taratura_fine/config",
         {"name": "Taratura fine", "unique_id": "loco_controller_taratura_fine",
          "default_entity_id": "button.loco_controller_taratura_fine", "command_topic": f"{BASE}/taratura/fine",
          "payload_press": "si", "device": disp, "availability": vivo, "icon": "mdi:check-bold"}),
    ]


# --- il canale veloce verso LOCO (ADR 0221) ------------------------------------------------------------------------
# Lo stato intero del controller su MQTT, fuori da HA: il ponte loco-controller su pc-01 lo porta allo schermo che lo
# usa. A ogni cambio (stick e grilletti in numeri interi, accelerometro a quarti di g, il dito in millesimi), al massimo
# VELOCE_HZ volte al secondo, e una volta al secondo da fermo (così lo schermo sa che c'è)
VELOCE_HZ = 60
DA_LOCO_SCADE_S = 30.0          # senza notizie da LOCO da tanto: torna tutto come se LOCO non ci fosse (Apple TV)


def veloce(s: ds.Stato, numero: int) -> dict:
    g = grandezze(s)
    toc = [[round(p[0] / 1919, 3), round(p[1] / 1079, 3)] if p else None for p in s.tocchi]
    return {"n": numero, "tasti": sorted(s.premuti),
            "stick": [int(g["stick_sx_x"]), int(g["stick_sx_y"]), int(g["stick_dx_x"]), int(g["stick_dx_y"])],
            "l2": int(g["l2"]), "r2": int(g["r2"]),
            "acc": [round(a / 8192 * 4) / 4 for a in s.accelerometro], "tocco": toc}


def luce_da_loco(c: dict) -> dict | None:
    """{luce: [r, g, b], giocatore, led_microfono} di LOCO → le parti di `voluto`. Sempre al 100% (Morgan)"""
    if not isinstance(c, dict) or not c:
        return None
    luce = c.get("luce")
    out = {}
    if isinstance(luce, list) and len(luce) == 3 and all(isinstance(v, int) for v in luce):
        rgb = tuple(max(0, min(255, v)) for v in luce)
        out["luce"] = {"acceso": any(rgb), "colore": rgb if any(rgb) else AZZURRO, "luminosita": 255}
    if isinstance(c.get("giocatore"), int) and 0 <= c["giocatore"] <= 5:
        out["giocatore"] = c["giocatore"]
    if isinstance(c.get("led_microfono"), bool):
        out["led_microfono"] = c["led_microfono"]
    return out


def luce_da_comando(payload: dict, adesso: dict) -> dict:
    """Il comando JSON della luce di HA (state ON/OFF, color {r,g,b}, brightness 0-255) sullo stato di prima"""
    nuovo = dict(adesso)
    if payload.get("state") == "OFF":
        nuovo["acceso"] = False
        return nuovo
    nuovo["acceso"] = True
    c = payload.get("color") or {}
    if all(k in c for k in "rgb"):
        nuovo["colore"] = (int(c["r"]), int(c["g"]), int(c["b"]))
    if "brightness" in payload:
        nuovo["luminosita"] = max(0, min(255, int(payload["brightness"])))
    return nuovo


def rgb_vero(luce: dict) -> tuple[int, int, int]:
    if not luce.get("acceso", True):
        return (0, 0, 0)
    k = luce.get("luminosita", 255) / 255
    return tuple(round(v * k) for v in luce.get("colore", AZZURRO))


# --- il controller collegato -----------------------------------------------------------------------------------------
# Come arriva (provato il 26 sera con btmon): i canali HID li prende bluetoothd, il Bluetooth di HA, che li ascolta
# sull'indirizzo preciso della chiavetta (vince sempre su chi ascolta «su tutte», come facevamo noi) e crea da sé il
# dispositivo HID (uhid). Quindi non si combatte con lui: il controller diventa un /dev/hidrawN del sistema, e noi
# leggiamo e scriviamo lì. bluetoothd toglie e mette l'intestazione HIDP (0xA1/0xA2), il resto è uguale

HIDRAW = Path("/sys/class/hidraw")


def trova_hidraw(mac: str, radice: Path = HIDRAW) -> str | None:
    """Il /dev/hidrawN di quel controller: bluetoothd mette l'indirizzo del controller in HID_UNIQ"""
    m = mac.lower()
    for d in sorted(radice.glob("hidraw*")):
        try:
            ue = (d / "device" / "uevent").read_text()
        except OSError:
            continue
        v = dict(r.split("=", 1) for r in ue.splitlines() if "=" in r)
        hid = v.get("HID_ID", "").upper()
        if v.get("HID_UNIQ", "").strip().lower() == m and (":0000054C:00000CE6" in hid or ":0000054C:00000DF2" in hid
                                                            or ":0000057E:00002009" in hid):
            return f"/dev/{d.name}"
    return None


def hidiocgfeature(n: int) -> int:
    """HIDIOCGFEATURE(n) di <linux/hidraw.h>: _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x07, n)"""
    return (3 << 30) | (n << 16) | (ord("H") << 8) | 0x07


class Sessione:
    """Un DualSense collegato: il suo hidraw, la lettura dei rapporti e le uscite"""
    def __init__(self, centro: "Centro", mac: str, fd: int, percorso: str = "") -> None:
        self.c, self.mac, self.fd, self.percorso = centro, mac, fd, percorso
        self.o = centro.ogg(mac)
        self.seq = 0
        self.lock = threading.Lock()
        self.tastiera = Tastiera()
        self.lento = Limitatore(0.1, 2)              # dieci valori al secondo quando si muove, niente da fermo
        self.vivo = True
        self.chiuso = False
        self.ultimo: ds.Stato | None = None
        self.frecce: frozenset = frozenset()          # le frecce che fa lo stick sinistro adesso
        self.premuti: frozenset | None = None         # i tasti veri più le frecce dello stick
        self.veloce_t = 0.0
        self.veloce_ultimo: dict | None = None
        self.usato = time.time()                          # l'ultima volta che qualcuno l'ha toccato
        self.a_riposo = False

    def manda(self, grezzo: ds.Uscita | None = None, **cosa) -> None:
        """Ogni pacchetto porta tutto quello che si vuole (luce, LED): cambiare una cosa non spegne le altre"""
        voluto = self.c.voluto(self.mac)
        u = grezzo or ds.Uscita(luce=rgb_vero(voluto["luce"]), giocatore=voluto["giocatore"],
                                led_microfono=voluto["led_microfono"], grilletto_sx=voluto["grilletto_sx"],
                                grilletto_dx=voluto["grilletto_dx"], **cosa)
        with self.lock:
            try:
                os.write(self.fd, ds.scrivi(u, self.seq)[1:])     # senza 0xA2: lo mette bluetoothd
            except OSError as e:
                scrivi(f"{self.mac}: non riesco a scrivere ({e})")
            self.seq = (self.seq + 1) % 16

    def feature(self, numero: int, nome: str) -> None:
        """Un rapporto «feature» (lo chiede bluetoothd per noi sul canale di controllo)"""
        import fcntl
        buf = bytearray(64)
        buf[0] = numero
        try:
            n = fcntl.ioctl(self.fd, hidiocgfeature(len(buf)), buf, True)
            scrivi(f"{self.mac}: {nome} letto ({n} byte)")
        except OSError as e:
            scrivi(f"{self.mac}: {nome} non letto ({e})")

    def chiedi_calibrazione(self) -> None:
        """Il rapporto 0x05: da lì il controller manda il rapporto pieno 0x31"""
        self.feature(ds.CALIBRAZIONE, "calibrazione")

    def presentati(self) -> None:
        """Le tre domande che fa il driver di Linux appena il DualSense arriva, nello stesso ordine: abbinamento (0x09),
        calibrazione (0x05), firmware (0x20)"""
        self.feature(0x09, "abbinamento")
        self.chiedi_calibrazione()
        self.feature(0x20, "firmware")

    def vibra_piano(self, ms: int) -> None:
        """Il ronzio delle fusa (LOCO accarezzato dal touchpad, 26 notte): solo il motore leggero, piano"""
        def giro():
            self.manda(vibra=(70, 0))
            time.sleep(max(0.05, min(1.0, ms / 1000)))
            self.manda(vibra=(0, 0))
        threading.Thread(target=giro, daemon=True).start()

    def vibra(self, forte: bool) -> None:
        def giro():
            self.manda(vibra=(255, 255) if forte else (120, 0))
            time.sleep(0.6 if forte else 0.25)
            self.manda(vibra=(0, 0))
        threading.Thread(target=giro, daemon=True).start()

    def avvia(self) -> None:
        threading.Thread(target=self.leggi, daemon=True).start()
        self.presentati()
        self.manda(grezzo=ds.Uscita(spegni_animazione=True))   # via l'animazione dell'accensione, come il driver
        time.sleep(0.1)
        self.manda()                                      # poi la luce e il numero
        self.c.pubblica(f"{BASE}/{self.o}/vivo", "online", True)
        self.c.pubblica(f"{BASE}/{self.o}/collegato", "si", True)
        self.c.pubblica_voluto(self.mac)

    def leggi(self) -> None:
        import select
        fermo = corti = visti = pieni = 0
        while self.vivo:
            try:
                pronto, _, _ = select.select([self.fd], [], [], 1.0)
                if not pronto:
                    fermo += 1
                    if fermo in (3, 10) and pieni == 0:
                        scrivi(f"{self.mac}: niente dopo {fermo} s ({visti} rapporti corti), lo richiedo")
                        threading.Thread(target=self.ritenta, daemon=True).start()
                    self.forse_a_riposo(time.time())
                    if fermo == 20 and visti == 0:            # collegato e muto: lo sblocca solo una riaccensione
                        self.c.stato(f"{self.nome()} collegato ma muto: spegnilo e riaccendilo")
                    continue
                p = os.read(self.fd, 128)
            except (OSError, ValueError) as e:            # il controller si è staccato: hidraw sparisce
                scrivi(f"{self.mac}: hidraw chiuso ({e})")
                break
            if not p:
                scrivi(f"{self.mac}: hidraw finito")
                break
            fermo = 0
            visti += 1
            if visti <= 3 or visti in (100, 1000):
                scrivi(f"{self.mac}: rapporto {visti}, {len(p)} byte ({p[:4].hex()})")
            s = self.decodifica(p)
            if s is None:
                corti += 1
                if corti in (250, 2500):                  # ancora il rapporto corto: la richiesta non è arrivata
                    threading.Thread(target=self.ritenta, daemon=True).start()
                continue
            corti = 0
            pieni += 1
            if pieni % 250 == 0:                          # circa una volta al secondo
                self.forse_a_riposo(time.time())
            if pieni == 1:
                self.c.stato(f"{self.nome()} collegato")
            self.passa(s, time.time())
        self.chiudi()

    def forse_a_riposo(self, adesso: float) -> bool:
        """Fermo da più del tempo scelto: si spegne (una volta sola)"""
        scelto = RIPOSO.get(self.c.voluto(self.mac)["riposo"], 0)
        if not scelto or self.a_riposo or adesso - self.usato < scelto:
            return False
        self.a_riposo = True
        self.c.stato(f"{self.nome()} spento: fermo da {self.c.voluto(self.mac)['riposo']}")
        threading.Thread(target=self.c.spegni, args=(self.mac,), daemon=True).start()
        return True

    def passa(self, s: ds.Stato, adesso: float) -> None:
        if in_uso(s):
            self.usato = adesso
        t = f"{BASE}/{self.o}"
        prima = self.ultimo
        self.frecce = frecce_stick(s.stick_sx, self.frecce)
        premuti = s.premuti | self.frecce
        for k, giu in tasti_cambiati(self.premuti or frozenset(), premuti).items():
            self.c.pubblica(f"{t}/tasto/{k}", "si" if giu else "no", True)       # prima dei sensori lenti
        if prima is None:                                 # all'arrivo: tutti gli altri lasciati
            for k in self.tasti:
                if k not in premuti:
                    self.c.pubblica(f"{t}/tasto/{k}", "no", True)
        self.premuti = premuti
        for ev in self.tastiera.passa(premuti, adesso):
            self.c.pubblica(f"{t}/tasti", json.dumps({"event_type": ev}), False)
        if prima is None or (s.batteria, s.in_carica, s.carica_piena) != (prima.batteria, prima.in_carica, prima.carica_piena):
            self.c.pubblica(f"{t}/batteria", str(s.batteria), True)
            self.c.pubblica(f"{t}/in_carica", "si" if s.in_carica else "no", True)
        if prima is None or s.cuffie != prima.cuffie:
            self.c.pubblica(f"{t}/cuffie", "si" if s.cuffie else "no", True)
        if prima is None or s.microfono_spento != prima.microfono_spento:
            self.c.pubblica(f"{t}/microfono_spento", "si" if s.microfono_spento else "no", True)
        for k, v in self.lento.da_mandare(grandezze(s), adesso).items():
            self.c.pubblica(f"{t}/{k}", str(v), False)
        v = veloce(s, self.c.numero(self.mac) or 1)
        if (v != self.veloce_ultimo and adesso - self.veloce_t >= 1 / VELOCE_HZ) or adesso - self.veloce_t >= 1.0:
            self.veloce_t, self.veloce_ultimo = adesso, v
            self.c.pubblica(f"{t}/veloce", json.dumps(v, separators=(",", ":")), False)
        self.ultimo = s

    # --- quello che cambia da un tipo di controller all'altro (il DualSense qui, il Pro Controller sotto)
    tasti = ds.TUTTI_I_TASTI

    def nome(self) -> str:
        return nome_di(self.c.trova(self.mac) or {})

    def decodifica(self, p: bytes) -> ds.Stato | None:
        return ds.leggi(bytes((ds.HIDP_DATI_ENTRATA,)) + p)  # il CRC conta anche l'intestazione 0xA1

    def ritenta(self) -> None:
        self.chiedi_calibrazione()

    def chiudi(self) -> None:
        if self.chiuso:
            return
        self.chiuso = True
        self.vivo = False
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.c.staccato(self.mac, self)


class SessioneSwitch(Sessione):
    """Un controller per Switch (il Pro Controller e chi si presenta come lui, ADR 0224): stessi eventi, stesse entità
    e stesso canale veloce del DualSense, con i nomi dei tasti di LOCO (switchpro.py). Niente barra di luce: la luce
    accende o spegne il cerchio del tasto Home; i LED del giocatore sono i 4 sotto; la vibrazione è la HD rumble"""
    tasti = sp.TUTTI_I_TASTI

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.n = 0
        self.stick = (sp.Stick(), sp.Stick())
        self.detto: tuple | None = None               # (giocatore, home accesa) già mandati
        self.ignoti: frozenset = frozenset()
        self.semplici = 0

    def sotto(self, cmd: int, arg: bytes = b"", vib: bytes | None = None) -> None:
        with self.lock:
            try:
                os.write(self.fd, sp.sottocomando(self.n, cmd, arg, vib))
            except OSError as e:
                scrivi(f"{self.mac}: non riesco a scrivere ({e})")
            self.n = (self.n + 1) % 16
        time.sleep(0.05)                              # il controller risponde a uno per volta

    def presentati(self) -> None:
        for cmd, arg in sp.avvio():
            self.sotto(cmd, arg)

    def ritenta(self) -> None:
        self.presentati()
        self.detto = None
        self.manda()

    def manda(self, grezzo=None, **cosa) -> None:
        if grezzo is not None:                        # l'animazione dell'accensione del DualSense: qui non c'è
            return
        voluto = self.c.voluto(self.mac)
        ora = (int(voluto["giocatore"]), any(rgb_vero(voluto["luce"])))
        if ora != self.detto:
            self.detto = ora
            self.sotto(sp.LED_GIOCATORE, sp.led_giocatore(ora[0]))
            self.sotto(sp.LUCE_HOME, sp.luce_home(ora[1]))
        v = cosa.get("vibra")
        if v is not None:
            forza = max(int(v[0]), int(v[1])) / 255
            with self.lock:
                try:
                    os.write(self.fd, sp.solo_vibra(self.n, sp.vibra(forza, forza)))
                except OSError as e:
                    scrivi(f"{self.mac}: vibrazione non mandata ({e})")
                self.n = (self.n + 1) % 16

    def decodifica(self, p: bytes) -> ds.Stato | None:
        g = sp.leggi(p)
        if g is None:
            return None
        if g.semplice:
            self.semplici += 1
            if self.semplici in (120, 1200):          # ancora il rapporto semplice: il modo pieno non è arrivato
                threading.Thread(target=self.ritenta, daemon=True).start()
        if g.ignoti != self.ignoti:
            # i tasti di dietro del Nebula (Morgan: «nessun software è riuscito a usarli»): se mandano qualcosa, è qui
            if g.ignoti and os.environ.get("CONTROLLER_DIAGNOSI") == "true":
                scrivi(f"{self.mac}: bit sconosciuti accesi: {', '.join(sorted(g.ignoti))} (rapporto {p[:13].hex()})")
            self.ignoti = g.ignoti
        return ds.Stato(stick_sx=self.stick[0].a_otto_bit(g.stick_sx), stick_dx=self.stick[1].a_otto_bit(g.stick_dx),
                        l2=255 if "l2" in g.premuti else 0, r2=255 if "r2" in g.premuti else 0, premuti=g.premuti,
                        giroscopio=tuple(round(v * 62.4) for v in g.giroscopio),   # come il DualSense: 1024 = 1 °/s
                        accelerometro=tuple(v * 2 for v in g.accelerometro),       # 8192 = 1 g
                        batteria=g.batteria, in_carica=g.in_carica, carica_piena=g.batteria == 100 and g.in_carica)

    def vibra_piano(self, ms: int) -> None:
        def giro():
            self.manda(vibra=(90, 0))
            time.sleep(max(0.05, min(1.0, ms / 1000)))
            self.manda(vibra=(0, 0))
        threading.Thread(target=giro, daemon=True).start()


# --- il centro: Bluetooth, MQTT, stato ----------------------------------------------------------------------------

class Centro:
    def __init__(self, dati: Path) -> None:
        self.dati = dati
        self.file = dati / "controller.json"
        try:
            self.cfg = json.loads(self.file.read_text())
        except (OSError, ValueError):
            self.cfg = {"controller": []}
        self.sessioni: dict[str, Sessione] = {}
        self.lock = threading.Lock()
        self.abbina_fino = 0.0
        self.mq = None
        self.bus = None
        self.scheda = ""
        self.da_loco: dict = {}                           # l'ultimo stato mandato da LOCO (luce, LED, destinazione)
        self.da_loco_t = 0.0
        self.prova: tuple | None = None                   # (rgb, scade): un colore da provare, sopra a tutto (taratura)
        self.taratura_scelto = NESSUNO
        self.taratura: dict[str, str] = dict(TARATURA_BASE)
        try:
            for k, v in json.loads((dati / "taratura.json").read_text()).items():
                if k in TARATURA_BASE and isinstance(v, str) and len(v) == 7 and v.startswith("#"):
                    self.taratura[k] = v.lower()
        except (OSError, ValueError, AttributeError):
            pass
        self.nest_giro = (self.cfg.get("nest_giro") is True)

    # la taratura dei colori e il Nest nel giro (26 notte)
    def pubblica_taratura(self) -> None:
        diversi = [k for k, v in self.taratura.items() if v != TARATURA_BASE[k]]
        self.pubblica(f"{BASE}/taratura/colore", self.taratura_scelto, True)
        self.pubblica(f"{BASE}/taratura/tabella", json.dumps({**self.taratura, "cambiati": diversi}), True)
        self.pubblica(f"{BASE}/taratura/stato", f"{len(diversi)} cambiati" if diversi else "come i segnapasso", True)
        if self.taratura_scelto in self.taratura:
            r, g, b = hex_rgb(self.taratura[self.taratura_scelto])
            self.pubblica(f"{BASE}/taratura/luce", json.dumps({"state": "ON", "color_mode": "rgb",
                                                                 "color": {"r": r, "g": g, "b": b}}), True)
        else:
            self.pubblica(f"{BASE}/taratura/luce", json.dumps({"state": "OFF"}), True)
        self.pubblica(f"{BASE}/nest_giro", "si" if self.nest_giro else "no", True)

    def taratura_mostra(self) -> None:
        """Il colore scelto sulla barra di luce, sopra a tutto, per 10 minuti (o niente)"""
        prima = self.istantanea()
        self.prova = ((hex_rgb(self.taratura[self.taratura_scelto]), time.time() + TARATURA_S)
                      if self.taratura_scelto in self.taratura else None)
        self.applica_loco(prima)

    def tara(self, cosa: str, testo: str) -> None:
        if cosa == "colore/set" and (testo == NESSUNO or testo in self.taratura):
            self.taratura_scelto = testo
        elif cosa == "luce/set" and self.taratura_scelto in self.taratura:
            try:
                d = json.loads(testo)
            except ValueError:
                return
            col = d.get("color") if isinstance(d, dict) else None
            if d.get("state") == "OFF":
                self.taratura_scelto = NESSUNO
            elif isinstance(col, dict) and all(isinstance(col.get(x), (int, float)) for x in "rgb"):
                self.taratura[self.taratura_scelto] = rgb_hex((col["r"], col["g"], col["b"]))
                self.salva_taratura()
        elif cosa == "ripristina" and self.taratura_scelto in self.taratura:
            self.taratura[self.taratura_scelto] = TARATURA_BASE[self.taratura_scelto]
            self.salva_taratura()
        elif cosa == "fine":
            self.taratura_scelto = NESSUNO
        else:
            return
        self.taratura_mostra()
        self.pubblica_taratura()

    def salva_taratura(self) -> None:
        f = self.dati / "taratura.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.taratura, indent=1))
        tmp.replace(f)

    # stato salvato
    def salva(self) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, indent=1))
        tmp.replace(self.file)

    def nostri(self) -> list[dict]:
        return self.cfg.setdefault("controller", [])

    def trova(self, mac: str) -> dict | None:
        return next((c for c in self.nostri() if c["mac"] == mac), None)

    def numero(self, mac: str) -> int:
        return (self.trova(mac) or {}).get("numero", 0)

    def tipo(self, mac: str) -> str:
        return (self.trova(mac) or {}).get("tipo") or "dualsense"

    def ogg(self, mac: str) -> str:
        return oggetto(mac, self.tipo(mac))

    def voluto(self, mac: str) -> dict:
        c = self.trova(mac) or {}
        return {"luce": c.get("luce") or {"acceso": True, "colore": AZZURRO, "luminosita": 255},
                "giocatore": c.get("giocatore", c.get("numero", 1)), "led_microfono": bool(c.get("led_microfono")),
                "grilletto_sx": c.get("grilletto_sx", "libero"), "grilletto_dx": c.get("grilletto_dx", "libero"),
                "riposo": c.get("riposo", RIPOSO_DI_BASE), **self.loco_vuole(c.get("numero", 1))}

    def mano_loco(self, numero: int = 1) -> dict:
        """Quello che LOCO dice di quel controller (27 nov, ADR 0224: «ognuno controlla il suo»): `mani[numero]`, se no
        `predefinito` (un controller che LOCO non ha ancora visto), se no lo stato intero (LOCO di prima). {} se tace"""
        d = self.da_loco
        if not d or time.time() - self.da_loco_t > DA_LOCO_SCADE_S:
            return {}
        m = (d.get("mani") or {}).get(str(numero)) if isinstance(d.get("mani"), dict) else None
        if isinstance(m, dict):
            return m
        return d.get("predefinito") if isinstance(d.get("predefinito"), dict) else d

    def loco_vuole(self, numero: int = 1) -> dict:
        """Luce, LED del giocatore e del microfono decisi da LOCO (ADR 0221), finché LOCO si fa sentire; sopra, il
        colore da provare (`loco/controller/prova`, per tarare i colori guardando il controller)"""
        v = {}
        m = self.mano_loco(numero)
        if m:
            v = luce_da_loco(m.get("controller") or {}) or {}
        if self.prova and time.time() < self.prova[1]:
            v = {**v, "luce": {"acceso": True, "colore": self.prova[0], "luminosita": 255}}
        return v

    def prova_colore(self, testo: str) -> None:
        """{"luce": [r, g, b], "secondi": n} (al massimo 10 minuti), {"luce": null} la toglie"""
        try:
            d = json.loads(testo)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        prima = self.istantanea()
        luce = d.get("luce")
        if isinstance(luce, list) and len(luce) == 3 and all(isinstance(x, int) for x in luce):
            sec = d.get("secondi", 60)
            sec = max(1.0, min(600.0, float(sec))) if isinstance(sec, (int, float)) else 60.0
            self.prova = (tuple(max(0, min(255, x)) for x in luce), time.time() + sec)
            scrivi(f"prova colore {self.prova[0]} per {sec:.0f} s")
        else:
            self.prova = None
        self.applica_loco(prima)

    def destinazione(self, numero: int = 1) -> str:
        d = self.mano_loco(numero).get("destinazione")
        return d if d in ("appletv", "loco", "telecamere", "windows") else "appletv"

    def schermo(self, numero: int = 1) -> str:
        """Lo schermo che il controller comanda quando la destinazione è LOCO o le telecamere, se no niente"""
        s = self.mano_loco(numero).get("schermo") if self.destinazione(numero) in ("loco", "telecamere") else None
        return s if isinstance(s, str) and len(s) <= 40 else ""

    def istantanea(self) -> dict:
        """Dove va e cosa vuole ognuno adesso: per mandare solo quello che cambia"""
        return {"dove": {c["mac"]: (self.destinazione(c.get("numero", 1)), self.schermo(c.get("numero", 1)))
                         for c in self.nostri()},
                "dove_1": (self.destinazione(1), self.schermo(1)),
                "voluto": {c["mac"]: self.voluto(c["mac"]) for c in self.nostri()}}

    def pubblica_destinazione(self, mac: str | None = None) -> None:
        """Quella di ognuno (`sensor.<controller>_destinazione`) e quella generale del controller 1, che resta per chi la
        guarda già (`sensor.loco_controller_destinazione`)"""
        if mac is None:
            self.pubblica(f"{BASE}/destinazione", self.destinazione(1), True)
            self.pubblica(f"{BASE}/destinazione/attributi", json.dumps({"schermo": self.schermo(1)}), True)
        for c in self.nostri():
            if mac is not None and c["mac"] != mac:
                continue
            n, o = c.get("numero", 1), self.ogg(c["mac"])
            self.pubblica(f"{BASE}/{o}/destinazione", self.destinazione(n), True)
            self.pubblica(f"{BASE}/{o}/destinazione/attributi", json.dumps({"schermo": self.schermo(n)}), True)

    def dice_loco(self, testo: str) -> None:
        """Lo stato che manda LOCO (il ponte loco-controller): si applica solo quello che cambia"""
        try:
            d = json.loads(testo)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        prima = self.istantanea()
        vecchio = {c["mac"]: (self.mano_loco(c.get("numero", 1)).get("vibra") or {}) for c in self.nostri()}
        c_vivo = bool(self.da_loco)
        self.da_loco, self.da_loco_t = d, time.time()
        for c in self.nostri():                           # il ronzio: solo al controller a cui l'ha chiesto LOCO
            vb = self.mano_loco(c.get("numero", 1)).get("vibra")
            s = self.sessioni.get(c["mac"])
            if not isinstance(vb, dict) or not s or not c_vivo or vb.get("id") == vecchio[c["mac"]].get("id"):
                continue                                  # una volta per ogni richiesta nuova (lo stato si ripete ogni 2 s)
            if vb.get("forte"):
                s.vibra(True)                             # il comando segreto riuscito
            else:
                s.vibra_piano(int(vb.get("ms") or 200))
        self.applica_loco(prima)

    def applica_loco(self, prima: dict) -> None:
        if (self.destinazione(1), self.schermo(1)) != prima.get("dove_1"):
            self.pubblica_destinazione()
            scrivi(f"destinazione: {self.destinazione(1)}")
        for c in self.nostri():
            n = c.get("numero", 1)
            if (self.destinazione(n), self.schermo(n)) != (prima.get("dove") or {}).get(c["mac"]):
                self.pubblica_destinazione(c["mac"])
                scrivi(f"{nome_di(c)}: {self.destinazione(n)}")
        for c in self.nostri():
            if self.voluto(c["mac"]) != (prima.get("voluto") or {}).get(c["mac"]):
                self.pubblica_voluto(c["mac"])
                sess = self.sessioni.get(c["mac"])
                if sess:
                    sess.manda()

    def loco_tace(self) -> None:
        """Da sorveglia: LOCO non si fa sentire da DA_LOCO_SCADE_S, si torna come se non ci fosse (una volta)"""
        if self.prova and time.time() >= self.prova[1]:
            self.prova = None
            scrivi("prova colore finita")
            if self.taratura_scelto != NESSUNO:
                self.taratura_scelto = NESSUNO
                self.pubblica_taratura()
            for c in self.nostri():
                self.pubblica_voluto(c["mac"])
                sess = self.sessioni.get(c["mac"])
                if sess:
                    sess.manda()
        if self.da_loco and time.time() - self.da_loco_t > DA_LOCO_SCADE_S:
            prima = {c["mac"]: True for c in self.nostri()}
            self.da_loco = {}
            scrivi("LOCO tace da 30 s: luce e LED come in HA, il controller all'Apple TV")
            self.pubblica_destinazione()
            for c in self.nostri():
                if prima.get(c["mac"]):
                    self.pubblica_voluto(c["mac"])
                    sess = self.sessioni.get(c["mac"])
                    if sess:
                        sess.manda()

    # MQTT
    def avvia_mqtt(self) -> None:
        import paho.mqtt.client as mqtt
        conf = mqtt_da_supervisor()
        try:
            cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="loco-controller")
        except AttributeError:                            # paho 1.x
            cl = mqtt.Client(client_id="loco-controller")
        if conf.get("username"):
            cl.username_pw_set(conf["username"], conf.get("password") or "")
        cl.will_set(f"{BASE}/vivo", "offline", retain=True)
        cl.on_connect = lambda c, *_a, **_k: self.collegato_mqtt()
        cl.on_message = lambda c, u, m: self.comando(m.topic, m.payload.decode("utf-8", "replace"))
        cl.connect(conf.get("host") or "core-mosquitto", int(conf.get("port") or 1883), 60)
        self.mq = cl
        cl.loop_start()

    def collegato_mqtt(self) -> None:
        for topic, c in configurazioni_centro():
            self.pubblica(topic, json.dumps(c), True)
        for c in self.nostri():
            self.annuncia(c)
        # solo i comandi: il canale veloce (60 al secondo) non deve tornare qui
        for s in (f"{BASE}/abbina", f"{BASE}/da_loco", f"{BASE}/prova", f"{BASE}/taratura/colore/set",
                  f"{BASE}/taratura/luce/set", f"{BASE}/taratura/ripristina", f"{BASE}/taratura/fine", f"{BASE}/nest_giro/set",
                  f"{BASE}/+/+/set", f"{BASE}/+/vibra", f"{BASE}/+/spegni",
                  f"{BASE}/+/dimentica"):
            self.mq.subscribe(s)
        self.pubblica_destinazione()
        self.pubblica_taratura()
        self.pubblica(f"{BASE}/vivo", "online", True)
        self.stato("pronto")

    def pubblica(self, topic: str, testo: str, tieni: bool) -> None:
        if self.mq is not None:
            self.mq.publish(topic, testo, qos=0, retain=tieni)

    def stato(self, testo: str) -> None:
        scrivi(testo)
        self.pubblica(f"{BASE}/stato", testo[:250], True)

    def annuncia(self, c: dict) -> None:
        for topic, conf in configurazioni(c["mac"], c["numero"], c.get("tipo") or "dualsense"):
            self.pubblica(topic, json.dumps(conf), True)
        o = self.ogg(c["mac"])
        if c["mac"] not in self.sessioni:
            self.pubblica(f"{BASE}/{o}/collegato", "no", True)
            self.pubblica(f"{BASE}/{o}/vivo", "offline", True)
        self.pubblica_voluto(c["mac"])
        self.pubblica_destinazione(c["mac"])

    def pubblica_voluto(self, mac: str) -> None:
        o, v = self.ogg(mac), self.voluto(mac)
        luce = v["luce"]
        r, g, b = luce.get("colore", AZZURRO)
        self.pubblica(f"{BASE}/{o}/luce", json.dumps({"state": "ON" if luce.get("acceso", True) else "OFF",
                                                       "color_mode": "rgb", "color": {"r": r, "g": g, "b": b},
                                                       "brightness": luce.get("luminosita", 255)}), True)
        self.pubblica(f"{BASE}/{o}/giocatore", NOMI_LED[max(0, min(5, int(v["giocatore"])))], True)
        self.pubblica(f"{BASE}/{o}/led_microfono", "si" if v["led_microfono"] else "no", True)
        self.pubblica(f"{BASE}/{o}/grilletto_sx", v["grilletto_sx"], True)
        self.pubblica(f"{BASE}/{o}/grilletto_dx", v["grilletto_dx"], True)
        self.pubblica(f"{BASE}/{o}/riposo", v["riposo"], True)

    def comando(self, topic: str, testo: str) -> None:
        parti = topic.split("/")
        if topic == f"{BASE}/da_loco":
            self.dice_loco(testo)
            return
        if topic == f"{BASE}/prova":
            self.prova_colore(testo)
            return
        if topic.startswith(f"{BASE}/taratura/"):
            self.tara(topic[len(f"{BASE}/taratura/"):], testo)
            return
        if topic == f"{BASE}/nest_giro/set" and testo in ("si", "no"):
            self.nest_giro = testo == "si"
            self.cfg["nest_giro"] = self.nest_giro
            self.salva()
            self.pubblica(f"{BASE}/nest_giro", testo, True)
            return
        if topic == f"{BASE}/abbina" and testo == "si":
            if os.environ.get("CONTROLLER_DIAGNOSI") == "true":
                threading.Thread(target=diagnosi_bluetooth, daemon=True).start()
            threading.Thread(target=self.abbina, daemon=True).start()
            return
        if len(parti) < 4 or parti[2] == "vivo":
            return
        c = next((x for x in self.nostri() if self.ogg(x["mac"]) == parti[2]), None)
        if c is None:
            return
        cosa, sess = parti[3], self.sessioni.get(c["mac"])
        if cosa == "luce" and parti[-1] == "set":
            try:
                c["luce"] = luce_da_comando(json.loads(testo), self.voluto(c["mac"])["luce"])
            except (ValueError, TypeError):
                return
        elif cosa == "giocatore" and parti[-1] == "set" and testo in NOMI_LED:
            c["giocatore"] = NOMI_LED.index(testo)
        elif cosa in ("grilletto_sx", "grilletto_dx") and parti[-1] == "set" and testo in ds.GRILLETTO:
            c[cosa] = testo
        elif cosa == "riposo" and parti[-1] == "set" and testo in RIPOSO:
            c["riposo"] = testo
            if sess:
                sess.usato = time.time()                  # il conto riparte da adesso
        elif cosa == "led_microfono" and parti[-1] == "set" and testo in ("si", "no"):
            c["led_microfono"] = testo == "si"
        elif cosa == "vibra" and testo in ("corta", "forte"):
            if sess:
                sess.vibra(testo == "forte")
            return
        elif cosa == "spegni" and testo == "si":
            self.spegni(c["mac"])
            return
        elif cosa == "dimentica" and testo == "si":
            self.dimentica(c["mac"])
            return
        else:
            return
        self.salva()
        self.pubblica_voluto(c["mac"])
        if sess:
            sess.manda()

    # Bluetooth: D-Bus
    def avvia_dbus(self) -> None:
        import dbus
        import dbus.service
        from dbus.mainloop.glib import DBusGMainLoop, threads_init
        threads_init()
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        me = self

        class Agente(dbus.service.Object):
            """Solo per gli abbinamenti che chiediamo noi, solo un DualSense, solo nella finestra («Just Works»)"""
            def _ok(self, dev) -> None:
                if not me.si_abbina(str(dev)):
                    raise dbus.DBusException("no", name="org.bluez.Error.Rejected")

            @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
            def RequestConfirmation(self, dev, _n):  # noqa: N802
                self._ok(dev)

            @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
            def RequestAuthorization(self, dev):  # noqa: N802
                self._ok(dev)

            @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
            def AuthorizeService(self, dev, _uuid):  # noqa: N802
                self._ok(dev)

            @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
            def Cancel(self):  # noqa: N802
                pass

            @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
            def Release(self):  # noqa: N802
                pass

        Agente(self.bus, "/loco/controller/agente")
        dbus.Interface(self.bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1").RegisterAgent(
            "/loco/controller/agente", "NoInputNoOutput")      # mai RequestDefaultAgent: quello è dell'orologio
        self.scheda = self.scegli_scheda()
        scrivi(f"Bluetooth: {self.scheda}")
        for c in self.nostri():
            i = self.info(self.dev_path(c["mac"]))
            scrivi(f"{nome_di(c)} ({c['mac']}): " + (
                f"Paired={bool(i.get('Paired'))} Bonded={bool(i.get('Bonded'))} Trusted={bool(i.get('Trusted'))}"
                if i else "BlueZ non lo conosce più: va riabbinato"))

    def scegli_scheda(self) -> str:
        """La chiavetta già accesa (quella dell'orologio). Non si accende niente: se è spenta l'ha spenta qualcuno"""
        import dbus
        om = dbus.Interface(self.bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        voluta = os.environ.get("CONTROLLER_SCHEDA", "").strip()
        schede = sorted((str(p), ifs["org.bluez.Adapter1"]) for p, ifs in om.GetManagedObjects().items()
                        if "org.bluez.Adapter1" in ifs)
        if voluta:
            schede = [x for x in schede if x[0].endswith("/" + voluta)] or schede
        for p, props in schede:
            if bool(props.get("Powered")):
                return p
        raise SystemExit("nessuna chiavetta Bluetooth accesa")

    def proprieta(self, path: str, iface: str):
        import dbus
        return dbus.Interface(self.bus.get_object("org.bluez", path), "org.freedesktop.DBus.Properties")

    def dev_path(self, mac: str) -> str:
        return f"{self.scheda}/dev_{mac.replace(':', '_')}"

    def info(self, path: str) -> dict:
        try:
            return dict(self.proprieta(path, "org.bluez.Device1").GetAll("org.bluez.Device1"))
        except Exception:  # noqa: BLE001
            return {}

    def si_abbina(self, dev_path: str) -> bool:
        if time.time() > self.abbina_fino:
            return False
        i = self.info(dev_path)
        return tipo_da(str(i.get("Name", "")), str(i.get("Modalias", ""))) is not None

    def abbina(self) -> None:
        if time.time() < self.abbina_fino:
            return
        import dbus
        self.abbina_fino = time.time() + FINESTRA_S
        pr = self.proprieta(self.scheda, "org.bluez.Adapter1")
        pairable_prima = bool(pr.Get("org.bluez.Adapter1", "Pairable"))
        ad = dbus.Interface(self.bus.get_object("org.bluez", self.scheda), "org.bluez.Adapter1")
        cercando = False
        try:
            pr.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True))   # se no non resta abbinato (bondable)
            self.stato("cerco un controller: DualSense con Create e PS tenuti finché lampeggia, quello per Switch col "
                       "tasto di abbinamento")
            ad.SetDiscoveryFilter({"Transport": "bredr"})
            ad.StartDiscovery()
            cercando = True
            mac = self.cerca(60)
            ad.StopDiscovery()
            cercando = False
            if not mac:
                self.stato("nessun controller in abbinamento trovato")
                return
            self.stato(f"trovato {mac}, mi abbino")
            if bool(self.info(self.dev_path(mac)).get("Paired")):
                # una chiave vecchia (controller abbinato altrove nel frattempo): si toglie e si rifà, come l'orologio
                ad.RemoveDevice(self.dev_path(mac))
                ad.StartDiscovery()
                cercando = True
                mac = self.cerca(20) or mac
                ad.StopDiscovery()
                cercando = False
            prima_i = self.info(self.dev_path(mac))
            scrivi(f"{mac}: si presenta come «{prima_i.get('Name')}», {prima_i.get('Modalias')}, classe "
                   f"{hex(int(prima_i.get('Class') or 0))}")
            # 27 nov (il Nebula): «Page Timeout» e «NoReply» al primo colpo. Si riprova fino a tre volte, ognuna dopo
            # averlo rivisto nella ricerca (vuol dire che è ancora in abbinamento), e prima di riprovare si fida
            # (Trusted): se è lui a cercare noi, bluetoothd lo lascia entrare
            for tentativo in range(3):
                try:
                    self.proprieta(self.dev_path(mac), "org.bluez.Device1").Set("org.bluez.Device1", "Trusted",
                                                                               dbus.Boolean(True))
                    dev = dbus.Interface(self.bus.get_object("org.bluez", self.dev_path(mac)), "org.bluez.Device1")
                    dev.Pair(timeout=60)
                    break
                except dbus.DBusException as e:
                    if "AlreadyExists" in str(e.get_dbus_name()) or bool(self.info(self.dev_path(mac)).get("Paired")):
                        break
                    scrivi(f"{mac}: abbinamento {tentativo + 1} non riuscito ({e.get_dbus_name()})")
                    if tentativo == 2 or time.time() > self.abbina_fino:
                        raise
                    self.stato(f"riprovo ({tentativo + 2} di 3): tienilo in abbinamento")
                    ad.StartDiscovery()
                    cercando = True
                    rivisto = self.cerca(15, mac)
                    ad.StopDiscovery()
                    cercando = False
                    if not rivisto:
                        raise
            self.proprieta(self.dev_path(mac), "org.bluez.Device1").Set("org.bluez.Device1", "Trusted",
                                                                       dbus.Boolean(True))
            i = self.info(self.dev_path(mac))
            scrivi(f"{mac}: Paired={bool(i.get('Paired'))} Bonded={bool(i.get('Bonded'))} "
                   f"Trusted={bool(i.get('Trusted'))} (senza Bonded alla riaccensione non si ricollega)")
            c = self.trova(mac)
            tipo = tipo_da(str(i.get("Name", "")), str(i.get("Modalias", ""))) or "dualsense"
            if c is None:
                numeri = {x["numero"] for x in self.nostri()}
                c = {"mac": mac, "numero": next(n for n in range(1, 10) if n not in numeri), "tipo": tipo}
                c["giocatore"] = min(c["numero"], 5)
                self.nostri().append(c)
                self.salva()
            scrivi(f"{mac}: {tipo} ({i.get('Name')}, {i.get('Modalias')})")
            self.annuncia(c)
            for _ in range(200):                          # dopo l'abbinamento si collega lui (bluetoothd lo accoglie)
                if mac in self.sessioni:
                    break
                time.sleep(0.1)
            if mac in self.sessioni:
                self.stato(f"{nome_di(c)} abbinato e collegato")
            else:
                self.stato(f"{nome_di(c)} abbinato: premi PS (o Home) per collegarlo")
        except Exception as e:  # noqa: BLE001
            self.stato(f"abbinamento non riuscito: {e}")
        finally:
            if cercando:
                try:
                    ad.StopDiscovery()
                except Exception:  # noqa: BLE001
                    pass
            try:
                pr.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(pairable_prima))
            except Exception as e:  # noqa: BLE001
                scrivi(f"Pairable non rimesso com'era: {e}")
            self.abbina_fino = 0.0

    def cerca(self, secondi: float, solo: str | None = None) -> str | None:
        import dbus
        om = dbus.Interface(self.bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        fino = time.time() + secondi
        while time.time() < fino:
            for p, ifs in om.GetManagedObjects().items():
                d = ifs.get("org.bluez.Device1")
                if not d or not str(p).startswith(self.scheda + "/"):
                    continue
                mac = str(d.get("Address", "")).upper()
                if MAC.match(mac) and d.get("RSSI") is not None and mac not in self.sessioni \
                        and (solo is None or mac == solo) \
                        and tipo_da(str(d.get("Name", "")), str(d.get("Modalias", ""))):
                    return mac                            # visto adesso nella ricerca: è lui in abbinamento
            time.sleep(1)
        return None

    def spegni(self, mac: str) -> None:
        """Si stacca: il DualSense, lasciato dal suo computer, si spegne (come quando si spegne la PS5)"""
        s = self.sessioni.get(mac)
        if s:
            s.chiudi()
        try:
            import dbus
            dbus.Interface(self.bus.get_object("org.bluez", self.dev_path(mac)), "org.bluez.Device1").Disconnect()
        except Exception:  # noqa: BLE001
            pass

    def dimentica(self, mac: str) -> None:
        c = self.trova(mac)
        if c is None:
            return
        i = self.info(self.dev_path(mac))
        self.spegni(mac)
        if i and tipo_da(str(i.get("Name", "")), str(i.get("Modalias", ""))):
            try:
                import dbus
                dbus.Interface(self.bus.get_object("org.bluez", self.scheda), "org.bluez.Adapter1").RemoveDevice(
                    self.dev_path(mac))
            except Exception as e:  # noqa: BLE001
                scrivi(f"{mac} non tolto dal Bluetooth: {e}")
        for topic, _c in configurazioni(mac, c["numero"], c.get("tipo") or "dualsense"):
            self.pubblica(topic, "", True)                 # le entità spariscono da HA
        self.cfg["controller"] = [x for x in self.nostri() if x["mac"] != mac]
        self.salva()
        self.stato(f"{nome_di(c)} dimenticato")

    # il controller collegato = il suo hidraw
    def sorveglia(self) -> None:
        """Ogni secondo: un nostro DualSense che ha il suo hidraw e non ha la sessione la apre. Se BlueZ lo dà collegato
        e l'hidraw non viene, lo si scrive una volta (con quello che serve a capire)"""
        detto: set[tuple[str, str]] = set()
        connesso_da: dict[str, float] = {}
        while True:
            try:
                self.loco_tace()
            except Exception as e:  # noqa: BLE001
                scrivi(f"loco_tace: {e}")
            for c in list(self.nostri()):
                mac = c["mac"]
                if mac in self.sessioni:
                    connesso_da.pop(mac, None)
                    continue
                percorso = trova_hidraw(mac)
                if percorso is None:
                    detto.discard((mac, "permesso"))
                    if bool(self.info(self.dev_path(mac)).get("Connected")):
                        da = connesso_da.setdefault(mac, time.time())
                        if time.time() - da > 15 and (mac, "hidraw") not in detto:
                            detto.add((mac, "hidraw"))
                            uhid = "c'è" if os.path.exists("/dev/uhid") else "manca"
                            self.stato(f"{nome_di(c)} collegato al Bluetooth ma il sistema non lo presenta "
                                       f"(nessun hidraw; uhid {uhid})")
                            for d in sorted(HIDRAW.glob("hidraw*")):
                                try:
                                    scrivi(f"  {d.name}: " + " ".join((d / "device" / "uevent").read_text().split()))
                                except OSError:
                                    pass
                    else:
                        connesso_da.pop(mac, None)
                        detto.discard((mac, "hidraw"))
                    continue
                try:
                    fd = os.open(percorso, os.O_RDWR)
                except OSError as e:                      # il permesso lo dà il Supervisor qualche secondo dopo
                    if (mac, "permesso") not in detto:
                        detto.add((mac, "permesso"))
                        scrivi(f"{mac}: {percorso} c'è ma non si apre ancora ({e}), riprovo")
                    continue
                sess = (SessioneSwitch if c.get("tipo") == "switch" else Sessione)(self, mac, fd, percorso)
                with self.lock:
                    self.sessioni[mac] = sess
                scrivi(f"{mac} collegato ({percorso})")
                threading.Thread(target=sess.avvia, daemon=True).start()
            time.sleep(1)

    def staccato(self, mac: str, sess: Sessione) -> None:
        with self.lock:
            if self.sessioni.get(mac) is not sess:
                return                                    # se n'è già aperta un'altra: quella vale
            del self.sessioni[mac]
        o = self.ogg(mac)
        for k in tasti_di(self.tipo(mac)):                # staccato con un tasto giù: non resta «premuto» in HA
            self.pubblica(f"{BASE}/{o}/tasto/{k}", "no", True)
        self.pubblica(f"{BASE}/{o}/collegato", "no", True)
        self.pubblica(f"{BASE}/{o}/vivo", "offline", True)
        scrivi(f"{mac} staccato")


def diagnosi_bluetooth(secondi: float = 120, righe: int = 600) -> None:
    """Il traffico Bluetooth vero (btmon) nel registro, per capire cosa arriva dal controller: solo le righe che
    contano (collegamenti, canali L2CAP, dati in arrivo) e solo per un po'. Serve l'opzione `diagnosi`"""
    import subprocess
    try:
        pr = subprocess.Popen(["btmon", "-T"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              errors="replace")
    except OSError as e:
        scrivi(f"diagnosi: btmon non parte ({e})")
        return
    fino, n = time.time() + secondi, 0
    chiave = ("Connection Request", "Connect Complete", "Disconnect", "Encryption Change", "Link Key",
              "L2CAP: Connection", "L2CAP: Configure", "L2CAP: Disconnection", "ACL Data RX", "ACL Data TX",
              "Reject", "Authentication", "PSM", "Status:", "Reason:", "Result:", "Channel:", "HIDP")
    acl_rx = 0
    entrata = True                          # l'ultima riga «ACL Data» era in arrivo (>) o in uscita (<)
    conti: dict[str, int] = {}
    ultimo_conto = time.time()
    try:
        for riga in pr.stdout:
            if time.time() > fino or n >= righe:
                break
            r = riga.rstrip()
            if "ACL Data RX" in r:
                acl_rx += 1
                entrata = True
            elif "ACL Data TX" in r:
                entrata = False
            m = re.search(r"Channel: (\d+) len (\d+)", r)
            if m:                                         # i dati: si contano, non si scrivono uno per uno
                k = f"{'arriva' if entrata else 'parte'} canale {m.group(1)} da {m.group(2)} byte"
                conti[k] = conti.get(k, 0) + 1
                if time.time() - ultimo_conto >= 5:
                    print("[btmon] in 5 s: " + ", ".join(f"{v}× {k}" for k, v in sorted(conti.items())), flush=True)
                    conti, ultimo_conto = {}, time.time()
                continue
            if "ACL Data" in r or r.strip() == "Status: Success (0x00)":
                continue
            if any(k in r for k in chiave):
                n += 1
                print(f"[btmon] {r.strip()[:180]}", flush=True)
    finally:
        pr.kill()
        scrivi(f"diagnosi finita: {acl_rx} pacchetti ACL in arrivo")


def mqtt_da_supervisor() -> dict:
    """Host e credenziali di Mosquitto dal Supervisor (services: mqtt:need)"""
    tok = os.environ.get("SUPERVISOR_TOKEN", "")
    if not tok:
        return {}
    r = _rq.Request("http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {tok}"})
    with _rq.urlopen(r, timeout=10) as f:
        return (json.load(f) or {}).get("data") or {}


def main() -> int:
    c = Centro(Path(os.environ.get("CONTROLLER_DATI", "/data")))
    c.avvia_dbus()
    c.avvia_mqtt()
    threading.Thread(target=c.sorveglia, daemon=True).start()
    if os.environ.get("CONTROLLER_DIAGNOSI") == "true":          # anche all'avvio: si vede la riaccensione col PS
        threading.Thread(target=diagnosi_bluetooth, args=(300,), daemon=True).start()
    from gi.repository import GLib
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
