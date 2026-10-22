#!/usr/bin/env python3
"""LOCO · l'orologio da muro (l'Echo Wall Clock della cucina, 22 ottobre 2026).

Gira sul nodo che ha il Bluetooth classico vicino all'orologio (oggi mbp-01, il MacBook del soggiorno) come utente suo
(loco-orologio) e fa da Echo all'orologio: lo tiene all'ora giusta e gli fa vedere i timer di LOCO della sua stanza.

  POST /orologio {"stanza", "timer": [{"id", "fine" (ms), "pausa"?, "suona"?}], "sveglia"}   dalla pagina dello schermo
  GET  /orologio                                                                              come sta
  POST /comando  {"cosa": ...} col gettone di /etc/loco-orologio/comando.token                da `loco-orologio`

Il protocollo l'ha studiato Mike Janofsky (github.com/mpjanofsky/echo-wall-clock, PROTOCOL.md): l'orologio è un
«Alexa Gadget» su Bluetooth classico, SPP/RFCOMM canale 4; trame F0 02 00 seq … checksum F1 con F0/F1/F2 escapati;
dentro protobuf AGT (Message{Directive{Header{namespace, name}, payload}}) senza messageId, come un Echo vero. Qui è
riscritto da capo dalla descrizione, niente codice copiato.

Cosa fa:
- all'avvio, a ogni ricollegamento e ogni mezz'ora: l'ora (StateUpdate timeinfo, ora di qui scritta come locale:
  l'orologio ignora il fuso), più la correzione in minuti se le lancette sono rimaste storte
- timer: SetAlert TIMER col token del timer e la fine; in pausa o tolto DeleteAlert; quando suona timers=active ogni
  12 s finché suona (un invio = ~10 s di pulsazioni), poi cleared; sveglia che suona nella stanza = alarms=active
- calibra: le lancette a 12:00 (l'orologio va a casa sul suo sensore e poi all'ora detta) e restano lì 10 minuti per
  guardarle; rifasatura di notte alle 03:30 (12:00 e poi l'ora vera) se le lancette perdono passi
- abbinamento: solo a comando, 90 s, solo un orologio Echo (nome EchoWallClk… o EWC…); fuori da lì il MacBook non
  accetta abbinamenti da nessuno

Non tocca mai il canale 2 dell'orologio (aggiornamento del firmware).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORTA = int(os.environ.get("OROLOGIO_PORTA", "8483"))
ORIGINE = os.environ.get("OROLOGIO_ORIGINE", "https://192.168.10.193:8470")
STATO = Path(os.environ.get("OROLOGIO_STATO", "/var/lib/loco-orologio/stato.json"))
GETTONE = Path(os.environ.get("OROLOGIO_GETTONE", "/etc/loco-orologio/comando.token"))
# come add-on di Home Assistant (deploy/ha/addon-orologio): in ascolto sulla rete, ma solo da pc-01
ASCOLTO = os.environ.get("OROLOGIO_ASCOLTO", "127.0.0.1")
DA = frozenset(x.strip() for x in os.environ.get("OROLOGIO_DA", "").split(",") if x.strip())
CANALE = 4                             # i dati dei gadget; il 2 è il firmware e non si tocca mai
NOMI = re.compile(r"^(EchoWallClk|EWC)")
MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
SQUILLO_S = 12.0                       # ogni quanto si ripete «active» mentre suona (un invio = ~10 s di pulsazioni)
SQUILLO_MAX_S = 120.0
RIFASA = (3, 30)                       # di notte, quando non guarda nessuno
CALIBRA_S = 600.0
AVVISO_S = 90.0                        # un avviso più vecchio di così non accende più il giallo

# --- il protocollo (dalla descrizione di PROTOCOL.md, scritto qui) ------------------------------------------------
STX, ETX, ESC = 0xF0, 0xF1, 0xF2


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _campo(num: int, valore) -> bytes:
    v = valore.encode() if isinstance(valore, str) else bytes(valore)
    return _varint((num << 3) | 2) + _varint(len(v)) + v


def _campo_num(num: int, valore: int) -> bytes:
    return _varint(num << 3) + _varint(int(valore))


def direttiva(namespace: str, nome: str, carico: bytes = b"") -> bytes:
    """Message{1: Directive{1: Header{1 namespace, 2 name}, 2: payload}}: niente messageId né carico vuoto (l'orologio
    ignora i messaggi che non sono identici a quelli di un Echo)"""
    d = _campo(1, _campo(1, namespace) + _campo(2, nome))
    if carico:
        d += _campo(2, carico)
    return _campo(1, d)


def scopri() -> bytes:
    return direttiva("Alexa.Discovery", "Discover")


def stato(**voci: str) -> bytes:
    return direttiva("Alexa.Gadget.StateListener", "StateUpdate",
                     b"".join(_campo(1, _campo(1, k) + _campo(2, v)) for k, v in voci.items()))


def avviso(token: str, tipo: str, quando: str) -> bytes:
    return direttiva("Alerts", "SetAlert", _campo(1, token) + _campo(2, tipo) + _campo(3, quando))


def togli_avviso(token: str) -> bytes:
    return direttiva("Alerts", "DeleteAlert", _campo(1, token))


def spegni_spia() -> bytes:
    return direttiva("Notifications", "ClearIndicator")


def accendi_spia() -> bytes:
    return direttiva("Notifications", "SetIndicator", _campo_num(1, 1))


class Trame:
    """F0 | 02 | 00 | seq | escape(carico + checksum16) | F1, checksum = (02 + 00 + somma del carico) & 0xFFFF"""

    def __init__(self):
        self.seq = 1                     # un Echo comincia da 1
        self.buf: bytearray | None = None
        self.esc = False

    def trama(self, carico: bytes) -> bytes:
        ck = (0x02 + 0x00 + sum(carico)) & 0xFFFF
        corpo = bytearray()
        for b in bytes(carico) + bytes([ck >> 8, ck & 0xFF]):
            corpo += bytes([ESC, b ^ ESC]) if b in (STX, ETX, ESC) else bytes([b])
        s = self.seq
        self.seq = (self.seq + 1) & 0xFF
        while self.seq in (STX, ETX, ESC):
            self.seq = (self.seq + 1) & 0xFF
        return bytes([STX, 0x02, 0x00, s]) + bytes(corpo) + bytes([ETX])

    def leggi(self, dati: bytes) -> list[bytes]:
        """I carichi delle trame arrivate (una trama può arrivare a pezzi o insieme ad altre)"""
        out = []
        for b in dati:
            if b == STX:
                self.buf, self.esc = bytearray(), False
            elif self.buf is None:
                continue
            elif b == ETX:
                f, self.buf = self.buf, None
                if len(f) >= 5 and ((f[0] + f[1] + sum(f[3:-2])) & 0xFFFF) == ((f[-2] << 8) | f[-1]):
                    out.append(bytes(f[3:-2]))
            elif self.esc:
                self.buf.append(b ^ ESC)
                self.esc = False
            elif b == ESC:
                self.esc = True
            else:
                self.buf.append(b)
        return out


def token_di(tid: str) -> str:
    """Il token del timer per l'orologio: stringa di cifre (gli Echo usano 8-10 cifre), stabile per lo stesso timer"""
    h = re.sub(r"[^0-9a-f]", "", str(tid).lower())[:8] or "0"
    return str(1_000_000_000 + int(h, 16) % 3_000_000_000)


def ora_locale(t: float, correzione_min: float = 0.0) -> str:
    """L'ora come la vuole l'orologio: locale con le cifre giuste (il fuso lo ignora)"""
    return datetime.fromtimestamp(t + correzione_min * 60).astimezone().isoformat(timespec="seconds")


def voluto_valido(d) -> dict | None:
    """Quello che arriva dalla pagina, controllato"""
    if not isinstance(d, dict) or not isinstance(d.get("timer"), list) or len(d["timer"]) > 3:
        return None
    out = []
    for t in d["timer"]:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not re.fullmatch(r"[0-9a-zA-Z_-]{1,40}", t["id"]):
            return None
        f = t.get("fine")
        if isinstance(f, bool) or not isinstance(f, (int, float)) or not 0 < f < 10**14:
            return None
        out.append({"id": t["id"], "fine": int(f), "pausa": t.get("pausa") is True, "suona": t.get("suona") is True})
    a = d.get("avviso", 0)
    a = int(a) if isinstance(a, (int, float)) and not isinstance(a, bool) and 0 < a < 10**14 else 0
    return {"timer": out, "sveglia": d.get("sveglia") is True, "avviso": a}


# --- quello che l'orologio deve vedere, e cosa mandargli -------------------------------------------------------
class Regista:
    """Dal voluto (la pagina) e da quello che l'orologio sa già, le direttive da mandare. Senza Bluetooth: si prova da
    solo nei test"""

    def __init__(self, correzione_min: float = 0.0):
        self.correzione = correzione_min
        self.voluto = {"timer": [], "sveglia": False}
        self.mandati: dict[str, str] = {}      # token → fine mandata
        self.squilli: dict[str, float] = {}    # token → inizio dello squillo
        self.squillo_ultimo: dict[str, float] = {}
        self.sveglia_da = 0.0
        self.sveglia_ultimo = 0.0
        self.prova: dict | None = None          # un timer di prova dal comando
        self.avviso_visto = 0                    # l'ultimo avviso di LOCO (ms) già fatto vedere col giallo

    def ricollegato(self) -> None:
        self.mandati.clear()
        self.squillo_ultimo.clear()

    def passo(self, adesso: float) -> list[tuple[str, bytes]]:
        """Le direttive di questo giro (nome per il registro, carico)"""
        out: list[tuple[str, bytes]] = []
        timer = list(self.voluto["timer"])
        if self.prova and self.prova["fine"] / 1000 > adesso - 15:
            timer.append(self.prova)
        elif self.prova:
            self.prova = None
        attivi = {token_di(t["id"]): t for t in timer if not t["pausa"] and not t["suona"]
                  and t["fine"] / 1000 > adesso - 2}
        suonano = {token_di(t["id"]) for t in timer if t["suona"]}
        for tok, t in attivi.items():
            q = ora_locale(t["fine"] / 1000, self.correzione)
            if self.mandati.get(tok) != q:
                out.append((f"timer {tok} fino {q}", avviso(tok, "TIMER", q)))
                self.mandati[tok] = q
        for tok in list(self.mandati):
            if tok not in attivi:
                out.append((f"timer {tok} via", togli_avviso(tok)))
                del self.mandati[tok]
        for tok in suonano:                          # suona: pulsa ogni 12 s, al massimo 2 minuti
            da = self.squilli.setdefault(tok, adesso)
            if adesso - da < SQUILLO_MAX_S and adesso - self.squillo_ultimo.get(tok, 0) >= SQUILLO_S:
                out.append((f"timer {tok} suona", stato(timers="active")))
                self.squillo_ultimo[tok] = adesso
        for tok in list(self.squilli):
            if tok not in suonano:
                out.append((f"timer {tok} fermato", stato(timers="cleared")))
                self.squilli.pop(tok, None)
                self.squillo_ultimo.pop(tok, None)
        if self.voluto["sveglia"]:
            if not self.sveglia_da:
                self.sveglia_da = adesso
            if adesso - self.sveglia_da < SQUILLO_MAX_S and adesso - self.sveglia_ultimo >= SQUILLO_S:
                out.append(("sveglia suona", stato(alarms="active")))
                self.sveglia_ultimo = adesso
        elif self.sveglia_da:
            out.append(("sveglia fermata", stato(alarms="cleared")))
            self.sveglia_da = self.sveglia_ultimo = 0.0
        # un avviso di LOCO nella stanza (22 ottobre, Morgan «usiamo il giallo per gli avvisi»): la luce al centro
        # pulsa gialla 4 volte e si spegne da sola; uno vecchio (riavvio, ricollegamento) non si rifà
        a = int(self.voluto.get("avviso") or 0)
        if a > self.avviso_visto:
            self.avviso_visto = a
            if adesso - a / 1000 < AVVISO_S:
                out.append(("avviso: luce gialla", accendi_spia()))
        return out


# --- il Bluetooth (BlueZ via D-Bus) -------------------------------------------------------------------------------
class Orologio:
    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = self._leggi()
        self.regista = Regista(float(self.cfg.get("correzione_min") or 0))
        self.sock: socket.socket | None = None
        self.trame = Trame()
        self.collegato_da = 0.0
        self.risposto = False
        self.ultima_ora = 0.0
        self.ora_mandata_slot = ""
        self.rifasato_giorno = ""
        self.calibra_fino = 0.0
        self.abbina_fino = 0.0
        self.attesa = 0.0
        self.tentativi = 0
        self.errore = ""
        self.registro: list[str] = []
        self.bus = None
        self.scheda, self.scheda_ind = "/org/bluez/hci0", ""
        self.fermo = threading.Event()

    # stato su disco: indirizzo, correzione, rifasatura
    def _leggi(self) -> dict:
        try:
            d = json.loads(STATO.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _salva(self) -> None:
        try:
            STATO.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATO.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cfg))
            os.replace(tmp, STATO)
        except OSError as e:
            self.scrivi(f"stato non salvato: {e}")

    def scrivi(self, riga: str) -> None:
        print(f"[orologio] {riga}", flush=True)
        self.registro = (self.registro + [time.strftime("%H:%M:%S ") + riga])[-40:]

    # D-Bus
    def avvia_dbus(self) -> None:
        import dbus
        import dbus.service
        from dbus.mainloop.glib import DBusGMainLoop, threads_init
        threads_init()                                    # l'abbinamento chiama D-Bus da un altro filo
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SystemBus()
        me = self

        class Agente(dbus.service.Object):
            """Accetta solo l'orologio, e solo mentre si abbina («Just Works»: niente codice da confermare)"""
            def _ok(self, dev) -> None:
                if not me.puo_abbinare(str(dev)):
                    me.scrivi(f"abbinamento rifiutato da {dev}")
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

        class Seriale(dbus.service.Object):
            """La porta seriale che l'orologio cerca quando richiama dopo l'abbinamento (lo apriamo noi sul suo canale 4)"""
            @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
            def NewConnection(self, dev, fd, _p):  # noqa: N802
                try:
                    os.close(fd.take())
                except Exception:  # noqa: BLE001
                    pass
                me.richiamato(str(dev))

            @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
            def RequestDisconnection(self, _dev):  # noqa: N802
                pass

            @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
            def Release(self):  # noqa: N802
                pass

        self.scheda, self.scheda_ind = self._scegli_scheda()
        self.scrivi(f"Bluetooth: {self.scheda} ({self.scheda_ind})")
        Agente(self.bus, "/loco/orologio/agente")
        Seriale(self.bus, "/loco/orologio/seriale")
        mgr = dbus.Interface(self.bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
        mgr.RegisterAgent("/loco/orologio/agente", "NoInputNoOutput")
        try:
            mgr.RequestDefaultAgent("/loco/orologio/agente")
        except dbus.DBusException as e:
            self.scrivi(f"agente non di base ({e.get_dbus_name()}): va bene lo stesso")
        dbus.Interface(self.bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1").RegisterProfile(
            "/loco/orologio/seriale", "00001101-0000-1000-8000-00805f9b34fb",
            {"Name": "Serial Port", "Role": "server", "Channel": dbus.UInt16(1),
             "RequireAuthentication": False, "RequireAuthorization": False})
        self._scheda("Powered", True)
        self._scheda("Connectable", True)      # l'orologio richiama dopo l'abbinamento e ogni tanto
        self._scheda("Pairable", False)

    def _scegli_scheda(self) -> tuple[str, str]:
        """Quale Bluetooth usare: OROLOGIO_SCHEDA (es. hci1) se c'è, se no il primo che si accende davvero. Sul
        MacBook del soggiorno il chip interno non parte (22 ottobre: «BCM: Reset failed»): vince la chiavetta USB"""
        import dbus
        om = dbus.Interface(self.bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
        schede = sorted((str(p), ifs["org.bluez.Adapter1"]) for p, ifs in om.GetManagedObjects().items()
                        if "org.bluez.Adapter1" in ifs)
        voluta = os.environ.get("OROLOGIO_SCHEDA", "").strip()
        if voluta:
            schede = [x for x in schede if x[0].endswith("/" + voluta)] or schede
        for path, props in schede:
            pr = dbus.Interface(self.bus.get_object("org.bluez", path), "org.freedesktop.DBus.Properties")
            try:
                pr.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
                time.sleep(1)
                if bool(pr.Get("org.bluez.Adapter1", "Powered")):
                    return path, str(props.get("Address", ""))
            except dbus.DBusException as e:
                self.scrivi(f"{path} non si accende: {e.get_dbus_name()}")
        raise SystemExit("nessun Bluetooth che si accende")

    def _scheda(self, prop: str, valore) -> None:
        import dbus
        try:
            dbus.Interface(self.bus.get_object("org.bluez", self.scheda),
                           "org.freedesktop.DBus.Properties").Set("org.bluez.Adapter1", prop, dbus.Boolean(valore))
        except Exception as e:  # noqa: BLE001
            self.scrivi(f"Bluetooth: {prop} non impostato ({e})")

    def puo_abbinare(self, dev_path: str) -> bool:
        ind = self.cfg.get("indirizzo") or ""
        mio = ind and dev_path.endswith("dev_" + ind.replace(":", "_"))
        return bool(mio) or (time.time() < self.abbina_fino and self._nome_di(dev_path))

    def _nome_di(self, dev_path: str) -> bool:
        import dbus
        try:
            n = dbus.Interface(self.bus.get_object("org.bluez", dev_path), "org.freedesktop.DBus.Properties").Get(
                "org.bluez.Device1", "Name")
            return bool(NOMI.match(str(n)))
        except Exception:  # noqa: BLE001
            return False

    def cerca(self, secondi: float = 60) -> str | None:
        """Un orologio Echo in abbinamento (luce arancione che pulsa): il suo indirizzo"""
        import dbus
        ad = dbus.Interface(self.bus.get_object("org.bluez", self.scheda), "org.bluez.Adapter1")
        try:
            ad.SetDiscoveryFilter({"Transport": "bredr"})
        except dbus.DBusException:
            pass
        ad.StartDiscovery()
        try:
            fine = time.time() + secondi
            while time.time() < fine:
                om = dbus.Interface(self.bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager")
                for path, ifs in om.GetManagedObjects().items():
                    d = ifs.get("org.bluez.Device1")
                    # solo se sentito adesso (RSSI): uno rimasto in memoria da prima non è in abbinamento
                    if d and NOMI.match(str(d.get("Name", ""))) and "RSSI" in d:
                        return str(d.get("Address"))
                time.sleep(1)
        finally:
            try:
                ad.StopDiscovery()
            except dbus.DBusException:
                pass
        return None

    # il collegamento
    def collega(self) -> bool:
        ind = self.cfg.get("indirizzo")
        if not ind:
            return False
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.setsockopt(274, 4, struct.pack("BB", 2, 0))          # SOL_BLUETOOTH, BT_SECURITY, media (cifrato)
        if MAC.match(getattr(self, "scheda_ind", "") or ""):
            s.bind((self.scheda_ind, 0))                         # dalla scheda scelta, non da quella guasta
        s.settimeout(30)
        try:
            s.connect((ind, CANALE))
        except OSError as e:
            s.close()
            if self.errore != f"collegamento: {e}":
                self.scrivi(f"non mi collego: {e}")
            self.errore = f"collegamento: {e}"
            return False
        s.settimeout(0.3)
        with self.lock:
            self.sock, self.trame, self.risposto = s, Trame(), False
            self.collegato_da = time.time()
            self.regista.ricollegato()
            self.errore = ""
        self.scrivi(f"collegato a {ind}")
        self.manda("scopri", scopri())
        return True

    def richiamato(self, dev: str) -> None:
        """L'orologio ci richiama (luce blu al centro): il collegamento di prima è morto anche se il socket non lo sa,
        e lui aspetta ~20 s che apriamo il suo canale 4 (PROTOCOL.md §2). Si riapre subito"""
        if self.cfg.get("indirizzo", "").replace(":", "_") not in dev:
            return
        self.scrivi("l'orologio richiama: mi ricollego")
        self.stacca("richiamato")
        self.attesa, self.tentativi = 0.0, 0

    def stacca(self, perche: str) -> None:
        with self.lock:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
                self.scrivi(f"staccato: {perche}")

    def manda(self, cosa: str, carico: bytes) -> bool:
        with self.lock:
            if self.sock is None:
                return False
            try:
                self.sock.sendall(self.trame.trama(carico))
            except OSError as e:
                self.stacca(f"invio di {cosa}: {e}")
                return False
        if not cosa.startswith("ora "):
            self.scrivi(cosa)
        return True

    def manda_ora(self, adesso: float, fissa: str | None = None) -> None:
        q = fissa or ora_locale(adesso, self.regista.correzione)
        if self.manda(f"ora {q}", stato(timeinfo=q)):
            self.ultima_ora = adesso
            self.scrivi(f"ora {q}")

    def _mezzogiorno(self, adesso: float) -> str:
        d = datetime.fromtimestamp(adesso).astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        return d.isoformat(timespec="seconds")

    def giro(self) -> None:
        """Il cuore: collegarsi, leggere, mandare quello che serve"""
        while not self.fermo.is_set():
            adesso = time.time()
            if self.sock is None:
                if self.cfg.get("indirizzo") and adesso >= self.attesa:
                    if not self.collega():
                        self.tentativi += 1                # 5, 10, 20, 40… al massimo ogni 2 minuti
                        self.attesa = adesso + min(120.0, 5.0 * 2 ** min(self.tentativi - 1, 5))
                        continue
                    self.attesa, self.tentativi = 0.0, 0
                else:
                    time.sleep(0.5)
                    continue
            try:
                dati = self.sock.recv(1024) if self.sock else b""
                if self.sock is not None and not dati:
                    self.stacca("l'orologio ha chiuso")
                    continue
            except socket.timeout:
                dati = b""
            except OSError as e:
                self.stacca(f"lettura: {e}")
                continue
            for car in self.trame.leggi(dati):
                if b"Discover.Response" in car and not self.risposto:
                    self.risposto = True
                    self.scrivi("l'orologio ha risposto")
            if not self.risposto and adesso - self.collegato_da < 3:
                continue                                   # come un Echo: prima la risposta, poi il resto
            if not self.ultima_ora or self.ultima_ora < self.collegato_da:
                if adesso < self.calibra_fino:
                    self.manda_ora(adesso, self._mezzogiorno(adesso))
                else:
                    self.manda_ora(adesso)
                self.manda("spia spenta", spegni_spia())
            self._orari(adesso)
            for cosa, car in self.regista.passo(adesso):
                if not self.manda(cosa, car):
                    break

    def _orari(self, adesso: float) -> None:
        loc = datetime.fromtimestamp(adesso)
        if self.calibra_fino and adesso >= self.calibra_fino:
            self.calibra_fino = 0.0
            self.scrivi("fine calibrazione: torna l'ora vera")
            self.manda_ora(adesso)
            return
        if self.calibra_fino:
            return
        slot = loc.strftime("%Y%m%d%H") + ("a" if loc.minute < 30 else "b")
        if slot != self.ora_mandata_slot and loc.second >= 5:
            self.ora_mandata_slot = slot                  # ogni mezz'ora, come un Echo
            if adesso - self.ultima_ora > 60:
                self.manda_ora(adesso)
        oggi = loc.strftime("%Y%m%d")
        if self.cfg.get("rifasa", True) and (loc.hour, loc.minute) == RIFASA and self.rifasato_giorno != oggi:
            self.rifasato_giorno = oggi                   # a casa sul sensore e di nuovo all'ora: i passi persi
            self.scrivi("rifasatura della notte")
            self.manda_ora(adesso, self._mezzogiorno(adesso))
            self.calibra_fino = adesso + 150

    # comandi
    def comando(self, d: dict) -> dict:
        cosa = str(d.get("cosa") or "")
        if cosa == "stato":
            return self.stato()
        if cosa == "abbina":
            threading.Thread(target=self._abbina, daemon=True).start()
            return {"ok": True, "detto": "cerco l'orologio per 60 s: tieni premuto il tasto dietro finché la luce "
                                         "davanti pulsa arancione"}
        if cosa == "dimentica":
            self.stacca("dimenticato")
            if self.cfg.get("indirizzo"):
                self._rimuovi(self.cfg["indirizzo"])
            self.cfg.pop("indirizzo", None)
            self._salva()
            return {"ok": True}
        if cosa == "calibra":
            m = d.get("minuti", 10)
            m = float(m) if isinstance(m, (int, float)) and not isinstance(m, bool) and 1 <= m <= 30 else 10.0
            self.calibra_fino = time.time() + m * 60
            self.manda_ora(time.time(), self._mezzogiorno(time.time()))
            return {"ok": True, "detto": f"lancette a 12:00 per {m:g} minuti: tutte e due devono puntare dritte in su"}
        if cosa == "correggi":
            m = d.get("minuti")
            if isinstance(m, bool) or not isinstance(m, (int, float)) or not -720 <= m <= 720:
                return {"ok": False, "detto": "minuti tra -720 e 720"}
            self.cfg["correzione_min"] = float(m)
            self.regista.correzione = float(m)
            self.regista.ricollegato()                    # i timer si rimandano con la stessa correzione
            self._salva()
            self.calibra_fino = 0.0
            self.manda_ora(time.time())
            return {"ok": True, "detto": f"correzione {m:+g} minuti"}
        if cosa == "ora":
            self.calibra_fino = 0.0
            self.manda_ora(time.time())
            return {"ok": True}
        if cosa == "rifasa":
            self.cfg["rifasa"] = d.get("acceso") is not False
            self._salva()
            return {"ok": True, "rifasa": self.cfg["rifasa"]}
        if cosa == "prova":
            m = d.get("minuti", 2)
            m = float(m) if isinstance(m, (int, float)) and not isinstance(m, bool) and 0.5 <= m <= 60 else 2.0
            self.regista.prova = {"id": "prova", "fine": int((time.time() + m * 60) * 1000), "pausa": False,
                                  "suona": False}
            return {"ok": True, "detto": f"timer di prova di {m:g} minuti"}
        if cosa == "avvisa":
            self.manda("prova avviso: luce gialla", accendi_spia())
            return {"ok": True, "detto": "luce gialla al centro"}
        if cosa == "squilla":
            self.manda("prova squillo", stato(timers="active"))
            return {"ok": True}
        return {"ok": False, "detto": "comandi: stato abbina dimentica calibra correggi ora rifasa prova squilla avvisa"}

    def _abbina(self) -> None:
        self.abbina_fino = time.time() + 90
        self._scheda("Pairable", True)
        try:
            ind = self.cerca(60)
            if not ind or not MAC.match(ind):
                self.scrivi("nessun orologio in abbinamento trovato")
                return
            self.scrivi(f"trovato {ind}: mi abbino e mi collego subito, come un Echo")
            self.stacca("abbinamento")
            self._rimuovi(ind)                            # una chiave vecchia (orologio resettato) = errore 52

            self.cfg["indirizzo"] = ind
            self._salva()
            for _ in range(6):                            # il primo tentativo dopo l'abbinamento spesso torna indietro
                if self.collega():
                    self._fidato(ind)
                    return
                time.sleep(1)
            self.scrivi(f"non riesco a collegarmi: {self.errore}")
        finally:
            self.abbina_fino = 0.0
            self._scheda("Pairable", False)

    def _rimuovi(self, ind: str) -> None:
        """Toglie l'orologio e la sua chiave dal Bluetooth (BlueZ): dopo un reset dell'orologio la chiave vecchia non
        combacia più e ogni collegamento finisce con «chiave mancante» (errno 52)"""
        import dbus
        try:
            dbus.Interface(self.bus.get_object("org.bluez", self.scheda), "org.bluez.Adapter1").RemoveDevice(
                self.scheda + "/dev_" + ind.replace(":", "_"))
            self.scrivi(f"tolta la chiave vecchia di {ind}")
        except Exception:  # noqa: BLE001
            pass                                           # non c'era

    def _fidato(self, ind: str) -> None:
        import dbus
        try:
            dbus.Interface(self.bus.get_object("org.bluez", self.scheda + "/dev_" + ind.replace(":", "_")),
                           "org.freedesktop.DBus.Properties").Set("org.bluez.Device1", "Trusted", dbus.Boolean(True))
        except Exception as e:  # noqa: BLE001
            self.scrivi(f"non segnato come fidato: {e}")

    def stato(self) -> dict:
        return {"indirizzo": self.cfg.get("indirizzo"), "collegato": self.sock is not None, "risposto": self.risposto,
                "correzione_min": self.regista.correzione, "rifasa": self.cfg.get("rifasa", True),
                "calibra": self.calibra_fino > time.time(), "timer": self.regista.voluto["timer"],
                "sveglia": self.regista.voluto["sveglia"], "errore": self.errore, "registro": self.registro[-12:]}


def gestore(o: Orologio, gettone: str):
    class H(BaseHTTPRequestHandler):
        server_version = "loco-orologio"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 10

        def log_message(self, *_):
            pass

        def _origine_ok(self) -> bool:
            og = self.headers.get("Origin")
            return og is None or og == ORIGINE

        def _da_chi_ok(self) -> bool:
            """Fuori da 127.0.0.1 solo gli indirizzi di OROLOGIO_DA (pc-01); senza elenco solo questa macchina"""
            ip = self.client_address[0]
            return ip in ("127.0.0.1", "::1") or ip in DA

        def _testata(self, codice: int, corpo: bytes = b""):
            self.send_response(codice)
            if self.headers.get("Origin") == ORIGINE:
                self.send_header("Access-Control-Allow-Origin", ORIGINE)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Access-Control-Allow-Local-Network", "true")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            if corpo:
                self.wfile.write(corpo)

        def do_OPTIONS(self):  # noqa: N802
            self._testata(204 if self._origine_ok() and self._da_chi_ok() else 403)

        def do_GET(self):  # noqa: N802
            if not self._da_chi_ok():
                return self._testata(403)
            if self.path != "/orologio":
                return self._testata(404)
            s = o.stato()
            self._testata(200, json.dumps({k: s[k] for k in ("collegato", "risposto", "calibra", "errore")}).encode())

        def do_POST(self):  # noqa: N802
            if not self._da_chi_ok():
                self.close_connection = True
                return self._testata(403)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= 2048:
                self.close_connection = True
                return self._testata(400)
            corpo = self.rfile.read(n)
            try:
                d = json.loads(corpo)
            except ValueError:
                return self._testata(400)
            if self.path == "/orologio":
                if not self._origine_ok():
                    return self._testata(403)
                v = voluto_valido(d)
                if v is None:
                    return self._testata(400, b'{"ok": false}')
                o.regista.voluto = v
                return self._testata(200 if o.sock is not None else 503,
                                     b'{"ok": true}' if o.sock is not None else b'{"ok": false}')
            if self.path == "/comando":
                g = self.headers.get("X-Loco-Gettone") or ""
                # sul nodo serve il gettone; da add-on basta che arrivi da pc-01 (OROLOGIO_DA), il gettone se c'è
                da_pc = bool(DA) and self.client_address[0] in DA
                ok = hmac.compare_digest(g, gettone) if gettone else da_pc
                if self.headers.get("Origin") is not None or not ok or not isinstance(d, dict):
                    return self._testata(403)
                return self._testata(200, json.dumps(o.comando(d), ensure_ascii=False).encode())
            self._testata(404)
    return H


def main() -> int:
    try:
        gettone = GETTONE.read_text().strip()
    except OSError:
        gettone = ""
    o = Orologio()
    srv = ThreadingHTTPServer((ASCOLTO, PORTA), gestore(o, gettone))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    o.avvia_dbus()
    threading.Thread(target=o.giro, daemon=True).start()
    o.scrivi(f"in ascolto su {ASCOLTO}:{PORTA}{' solo da ' + ','.join(sorted(DA)) if DA else ''}, orologio {o.cfg.get('indirizzo') or 'non ancora abbinato'}")
    from gi.repository import GLib
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        pass
    o.fermo.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
