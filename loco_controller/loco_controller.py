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


def tipi_di_evento() -> list[str]:
    return [t for t in ds.TUTTI_I_TASTI] + [f"{t}_lungo" for t in ds.TUTTI_I_TASTI]


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


def oggetto(mac: str) -> str:
    return "dualsense_" + mac.replace(":", "").lower()


def configurazioni(mac: str, numero: int) -> list[tuple[str, dict]]:
    """Le entità MQTT discovery di un controller: (topic di configurazione, configurazione)"""
    o = oggetto(mac)
    t = f"{BASE}/{o}"
    disp = {"identifiers": [o], "name": f"DualSense {numero}", "manufacturer": "Sony", "model": "DualSense",
            "connections": [["bluetooth", mac.lower()]], "via_device": "loco_controller"}
    vivo = [{"topic": f"{BASE}/vivo"}, {"topic": f"{t}/vivo"}]

    def e(tipo: str, chiave: str, nome: str, **altro) -> tuple[str, dict]:
        c = {"name": nome, "unique_id": f"{o}_{chiave}", "default_entity_id": f"{tipo}.dualsense_{numero}_{chiave}",
             "device": disp, "availability": vivo, "availability_mode": "all"}
        c.update(altro)
        return f"{DISCOVERY}/{tipo}/{o}/{chiave}/config", c

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
        e("event", "tasti", "Tasti", state_topic=f"{t}/tasti", event_types=tipi_di_evento(), icon="mdi:gamepad-variant"),
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
    for k in ds.TUTTI_I_TASTI:
        out.append(e("binary_sensor", f"tasto_{k}", f"Tasto {NOMI_TASTI.get(k, k)}", state_topic=f"{t}/tasto/{k}",
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
    ]


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
    """Il /dev/hidrawN di quel DualSense: bluetoothd mette l'indirizzo del controller in HID_UNIQ"""
    m = mac.lower()
    for d in sorted(radice.glob("hidraw*")):
        try:
            ue = (d / "device" / "uevent").read_text()
        except OSError:
            continue
        v = dict(r.split("=", 1) for r in ue.splitlines() if "=" in r)
        hid = v.get("HID_ID", "").upper()
        if v.get("HID_UNIQ", "").strip().lower() == m and (":0000054C:00000CE6" in hid or ":0000054C:00000DF2" in hid):
            return f"/dev/{d.name}"
    return None


def hidiocgfeature(n: int) -> int:
    """HIDIOCGFEATURE(n) di <linux/hidraw.h>: _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x07, n)"""
    return (3 << 30) | (n << 16) | (ord("H") << 8) | 0x07


class Sessione:
    """Un DualSense collegato: il suo hidraw, la lettura dei rapporti e le uscite"""
    def __init__(self, centro: "Centro", mac: str, fd: int, percorso: str = "") -> None:
        self.c, self.mac, self.fd, self.percorso = centro, mac, fd, percorso
        self.o = oggetto(mac)
        self.seq = 0
        self.lock = threading.Lock()
        self.tastiera = Tastiera()
        self.lento = Limitatore(0.1, 2)              # dieci valori al secondo quando si muove, niente da fermo
        self.vivo = True
        self.chiuso = False
        self.ultimo: ds.Stato | None = None
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
                        scrivi(f"{self.mac}: niente dopo {fermo} s ({visti} rapporti corti), richiedo la calibrazione")
                        threading.Thread(target=self.chiedi_calibrazione, daemon=True).start()
                    self.forse_a_riposo(time.time())
                    if fermo == 20 and visti == 0:            # collegato e muto: lo sblocca solo una riaccensione
                        self.c.stato(f"DualSense {self.c.numero(self.mac)} collegato ma muto: spegnilo (tieni PS "
                                     f"10 secondi) e riaccendilo")
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
            s = ds.leggi(bytes((ds.HIDP_DATI_ENTRATA,)) + p)  # il CRC conta anche l'intestazione 0xA1
            if s is None:
                corti += 1
                if corti in (250, 2500):                  # ancora il rapporto corto: la calibrazione non è arrivata
                    threading.Thread(target=self.chiedi_calibrazione, daemon=True).start()
                continue
            corti = 0
            pieni += 1
            if pieni % 250 == 0:                          # circa una volta al secondo
                self.forse_a_riposo(time.time())
            if pieni == 1:
                self.c.stato(f"DualSense {self.c.numero(self.mac)} collegato")
            self.passa(s, time.time())
        self.chiudi()

    def forse_a_riposo(self, adesso: float) -> bool:
        """Fermo da più del tempo scelto: si spegne (una volta sola)"""
        scelto = RIPOSO.get(self.c.voluto(self.mac)["riposo"], 0)
        if not scelto or self.a_riposo or adesso - self.usato < scelto:
            return False
        self.a_riposo = True
        self.c.stato(f"DualSense {self.c.numero(self.mac)} spento: fermo da {self.c.voluto(self.mac)['riposo']}")
        threading.Thread(target=self.c.spegni, args=(self.mac,), daemon=True).start()
        return True

    def passa(self, s: ds.Stato, adesso: float) -> None:
        if in_uso(s):
            self.usato = adesso
        t = f"{BASE}/{self.o}"
        prima = self.ultimo
        for k, giu in tasti_cambiati(prima.premuti if prima else frozenset(), s.premuti).items():
            self.c.pubblica(f"{t}/tasto/{k}", "si" if giu else "no", True)       # prima dei sensori lenti
        if prima is None:                                 # all'arrivo: tutti gli altri lasciati
            for k in ds.TUTTI_I_TASTI:
                if k not in s.premuti:
                    self.c.pubblica(f"{t}/tasto/{k}", "no", True)
        for ev in self.tastiera.passa(s.premuti, adesso):
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
        self.ultimo = s

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

    def voluto(self, mac: str) -> dict:
        c = self.trova(mac) or {}
        return {"luce": c.get("luce") or {"acceso": True, "colore": AZZURRO, "luminosita": 255},
                "giocatore": c.get("giocatore", c.get("numero", 1)), "led_microfono": bool(c.get("led_microfono")),
                "grilletto_sx": c.get("grilletto_sx", "libero"), "grilletto_dx": c.get("grilletto_dx", "libero"),
                "riposo": c.get("riposo", RIPOSO_DI_BASE)}

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
        self.mq.subscribe(f"{BASE}/#")
        self.pubblica(f"{BASE}/vivo", "online", True)
        self.stato("pronto")

    def pubblica(self, topic: str, testo: str, tieni: bool) -> None:
        if self.mq is not None:
            self.mq.publish(topic, testo, qos=0, retain=tieni)

    def stato(self, testo: str) -> None:
        scrivi(testo)
        self.pubblica(f"{BASE}/stato", testo[:250], True)

    def annuncia(self, c: dict) -> None:
        for topic, conf in configurazioni(c["mac"], c["numero"]):
            self.pubblica(topic, json.dumps(conf), True)
        o = oggetto(c["mac"])
        if c["mac"] not in self.sessioni:
            self.pubblica(f"{BASE}/{o}/collegato", "no", True)
            self.pubblica(f"{BASE}/{o}/vivo", "offline", True)
        self.pubblica_voluto(c["mac"])

    def pubblica_voluto(self, mac: str) -> None:
        o, v = oggetto(mac), self.voluto(mac)
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
        if topic == f"{BASE}/abbina" and testo == "si":
            if os.environ.get("CONTROLLER_DIAGNOSI") == "true":
                threading.Thread(target=diagnosi_bluetooth, daemon=True).start()
            threading.Thread(target=self.abbina, daemon=True).start()
            return
        if len(parti) < 4 or parti[2] == "vivo":
            return
        c = next((x for x in self.nostri() if oggetto(x["mac"]) == parti[2]), None)
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
            scrivi(f"DualSense {c['numero']} ({c['mac']}): " + (
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
        return ds.e_un_dualsense(str(i.get("Name", "")), str(i.get("Modalias", "")))

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
            self.stato("cerco un DualSense: tieni premuti Create e PS finché lampeggia")
            ad.SetDiscoveryFilter({"Transport": "bredr"})
            ad.StartDiscovery()
            cercando = True
            mac = self.cerca(60)
            ad.StopDiscovery()
            cercando = False
            if not mac:
                self.stato("nessun DualSense in abbinamento trovato")
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
            dev = dbus.Interface(self.bus.get_object("org.bluez", self.dev_path(mac)), "org.bluez.Device1")
            dev.Pair(timeout=40)
            self.proprieta(self.dev_path(mac), "org.bluez.Device1").Set("org.bluez.Device1", "Trusted",
                                                                       dbus.Boolean(True))
            i = self.info(self.dev_path(mac))
            scrivi(f"{mac}: Paired={bool(i.get('Paired'))} Bonded={bool(i.get('Bonded'))} "
                   f"Trusted={bool(i.get('Trusted'))} (senza Bonded alla riaccensione non si ricollega)")
            c = self.trova(mac)
            if c is None:
                numeri = {x["numero"] for x in self.nostri()}
                c = {"mac": mac, "numero": next(n for n in range(1, 10) if n not in numeri)}
                c["giocatore"] = min(c["numero"], 5)
                self.nostri().append(c)
                self.salva()
            self.annuncia(c)
            for _ in range(200):                          # dopo l'abbinamento si collega lui (bluetoothd lo accoglie)
                if mac in self.sessioni:
                    break
                time.sleep(0.1)
            if mac in self.sessioni:
                self.stato(f"DualSense {c['numero']} abbinato e collegato")
            else:
                self.stato(f"DualSense {c['numero']} abbinato: premi PS per collegarlo")
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

    def cerca(self, secondi: float) -> str | None:
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
                        and ds.e_un_dualsense(str(d.get("Name", "")), str(d.get("Modalias", ""))):
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
        if i and ds.e_un_dualsense(str(i.get("Name", "")), str(i.get("Modalias", ""))):
            try:
                import dbus
                dbus.Interface(self.bus.get_object("org.bluez", self.scheda), "org.bluez.Adapter1").RemoveDevice(
                    self.dev_path(mac))
            except Exception as e:  # noqa: BLE001
                scrivi(f"{mac} non tolto dal Bluetooth: {e}")
        for topic, _c in configurazioni(mac, c["numero"]):
            self.pubblica(topic, "", True)                 # le entità spariscono da HA
        self.cfg["controller"] = [x for x in self.nostri() if x["mac"] != mac]
        self.salva()
        self.stato(f"DualSense {c['numero']} dimenticato")

    # il controller collegato = il suo hidraw
    def sorveglia(self) -> None:
        """Ogni secondo: un nostro DualSense che ha il suo hidraw e non ha la sessione la apre. Se BlueZ lo dà collegato
        e l'hidraw non viene, lo si scrive una volta (con quello che serve a capire)"""
        detto: set[tuple[str, str]] = set()
        connesso_da: dict[str, float] = {}
        while True:
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
                            self.stato(f"DualSense {c['numero']} collegato al Bluetooth ma il sistema non lo presenta "
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
                sess = Sessione(self, mac, fd, percorso)
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
        o = oggetto(mac)
        for k in ds.TUTTI_I_TASTI:                        # staccato con un tasto giù: non resta «premuto» in HA
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
