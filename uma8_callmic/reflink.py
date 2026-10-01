"""Echo-Referenz nur verbinden, solange jemand „UMA-8 Call Mic“ aufnimmt (uma8-callmic --ref-linker).

Dauerhaft verbunden weckte die Referenz bei jeder Wiedergabe auf der Standardausgabe die ganze Kette samt UMA-8
und DeepFilterNet: PipeWire macht alles lauffähig, was über Links (auch passive) und die gemeinsame node.group des
Echo-Cancel-Moduls am laufenden Monitor hängt (context.c, run_nodes). Deshalb verbindet WirePlumber den
Referenz-Stream nicht (node.autoconnect = false), sondern dieser Helfer: Monitor der Standardausgabe → Referenz,
sobald ein Programm von uma8_callmic aufnimmt; getrennt HOLD_S nach der letzten Aufnahme; bei einem Wechsel der
Standardausgabe umgehängt. Unverbunden liefert der Stream Stille, die AEC reicht das Mikrofon dann durch.

„Nimmt auf“ heißt: ein Knoten hinter uma8_callmic läuft. Der Zustand von uma8_callmic selbst taugt nicht, denn
mit verbundener Referenz hält schon Musik die Kette am Laufen – die Referenz bliebe dann für immer verbunden.

Ereignisgesteuert über „pw-dump --monitor“, verbunden und getrennt wird mit pw-link. Gestartet per context.exec
aus der Kettenkonfiguration, endet also mit dem Dienst uma8-callmic-chain."""
from __future__ import annotations

import json
import logging
import os
import select
import signal
import subprocess
import time
from dataclasses import dataclass

from . import constants as K

log = logging.getLogger(__name__)

#: So lange nach Ende der letzten Aufnahme verbunden bleiben: Anruf-Programme öffnen das Mikrofon oft kurz neu
HOLD_S = 2.0
#: Ein angefordertes Verbinden/Trennen so lange nicht wiederholen; ist es dann nicht im Graphen, neuer Versuch
PENDING_S = 2.0
#: Pause, bevor pw-dump nach seinem Ende oder einem Fehler neu gestartet wird; verdoppelt sich bei wiederholtem
#: Scheitern bis RETRY_MAX_S (jeder Neustart liest den ganzen Graphen)
RETRY_S, RETRY_MAX_S = 2.0, 60.0
#: Fehlt der einmal gesehene Referenz-Knoten so lange, endet der Helfer (Kette ohne systemd gestartet und beendet)
GONE_S = 5.0
#: Nur für Tests: Metadaten-Objekt, dessen „default.audio.sink“ gilt (sonst „default“ von WirePlumber)
METADATA_ENV = "UMA8_REF_METADATA"
_IF = "PipeWire:Interface:"

Pair = tuple[int, int]  # (Ausgangsport des Monitors, Eingangsport der Referenz)


class Graph:
    """Abbild des Graphen aus pw-dump-Objekten, voll oder als Änderungen von --monitor.

    pw-dump schreibt geänderte Objekte vollständig, entfernte als {"id": …, "info": null}. Von Metadaten nur die
    geänderten Einträge (gelöschte Schlüssel fehlen ganz), daher werden diese zusammengeführt."""

    def __init__(self, objs=()):
        self.objs: dict[int, dict] = {}
        self.meta: dict[int, dict[str, object]] = {}
        self.update(objs)

    def update(self, objs) -> None:
        for o in objs:
            if "type" not in o:
                self.objs.pop(o["id"], None)
                self.meta.pop(o["id"], None)
                continue
            self.objs[o["id"]] = o
            if o["type"] == _IF + "Metadata":
                entries = self.meta.setdefault(o["id"], {})
                entries.update((e["key"], e.get("value")) for e in o.get("metadata") or [] if e.get("subject") == 0)

    def of(self, kind: str) -> list[dict]:
        return [o for o in self.objs.values() if o.get("type") == _IF + kind]

    def nodes(self, name: str) -> set[int]:
        return {o["id"] for o in self.of("Node") if (o["info"].get("props") or {}).get("node.name") == name}

    def state(self, node_id: int) -> str | None:
        return ((self.objs.get(node_id) or {}).get("info") or {}).get("state")

    def ports(self, node_ids: set[int], direction: str, monitor: bool = False) -> list[dict]:
        """Ports der Knoten in einer Richtung, sortiert nach port.id; monitor=True: nur Monitor-Ports."""
        found = [o for o in self.of("Port") if o["info"].get("direction") == direction
                 and (o["info"].get("props") or {}).get("node.id") in node_ids
                 and bool((o["info"].get("props") or {}).get("port.monitor")) == monitor]
        return sorted(found, key=lambda o: (o["info"]["props"].get("port.id", 0), o["id"]))

    def default_sink(self, metadata: str = "default") -> str | None:
        for o in self.of("Metadata"):
            if (o.get("props") or {}).get("metadata.name") == metadata:
                value = self.meta.get(o["id"], {}).get("default.audio.sink")
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        return None
                return value.get("name") if isinstance(value, dict) else None
        return None


def in_use(g: Graph) -> bool:
    """Nimmt gerade ein Programm von uma8_callmic auf (ein Knoten hinter der Quelle läuft)?"""
    src = g.nodes(K.SOURCE_NODE)
    return any(link["info"].get("output-node-id") in src and g.state(link["info"].get("input-node-id")) == "running"
               for link in g.of("Link"))


def _channel(port: dict) -> str | None:
    return port["info"]["props"].get("audio.channel")


def wanted(g: Graph, sink: str) -> set[Pair]:
    """Monitor-Ports der Senke → Referenz-Ports, nach Kanal. Mono-Senke: auf alle Kanäle; ohne gemeinsame
    Kanalnamen der Reihe nach."""
    ref = g.ports(g.nodes(K.AEC_REF_NODE), "input")
    mon = g.ports(g.nodes(sink), "output", monitor=True)
    if not ref or not mon:
        return set()
    if len(mon) == 1:
        return {(mon[0]["id"], r["id"]) for r in ref}
    by_channel = {_channel(m): m["id"] for m in mon}
    pairs = {(by_channel[_channel(r)], r["id"]) for r in ref if _channel(r) in by_channel}
    return pairs or {(m["id"], r["id"]) for m, r in zip(mon, ref)}


def ref_links(g: Graph) -> dict[Pair, int]:
    """Alle Links in die Referenz-Ports: (Ausgangsport, Eingangsport) → Link-ID."""
    ports = {p["id"] for p in g.ports(g.nodes(K.AEC_REF_NODE), "input")}
    return {(link["info"]["output-port-id"], link["info"]["input-port-id"]): link["id"]
            for link in g.of("Link") if link["info"].get("input-port-id") in ports}


def plan(g: Graph, active: bool, sink: str | None) -> tuple[set[Pair], set[int]]:
    """Zu erstellende Links und zu entfernende Link-IDs. Ohne bekannte Standardausgabe bleibt alles, wie es ist
    (z. B. direkt nach dem Start, bevor pw-dump die Metadaten liefert)."""
    have = ref_links(g)
    if not active:
        return set(), set(have.values())
    if sink is None:
        return set(), set()
    want = wanted(g, sink)
    return want - have.keys(), {lid for pair, lid in have.items() if pair not in want}


@dataclass
class Gate:
    """Aufnahme beginnt → sofort verbinden; endet sie, erst nach `hold` Sekunden ohne neue Aufnahme trennen."""
    hold: float = HOLD_S
    active: bool = False
    idle_since: float | None = None

    def update(self, use: bool, now: float) -> bool:
        if use:
            self.active, self.idle_since = True, None
        elif self.active:
            self.idle_since = now if self.idle_since is None else self.idle_since
            if now - self.idle_since >= self.hold:
                self.active, self.idle_since = False, None
        return self.active

    def deadline(self) -> float | None:
        return None if self.idle_since is None else self.idle_since + self.hold


def pw_link(commands: list[list[str]]) -> list[str]:
    """pw-link-Aufrufe parallel ausführen; Meldungen der fehlgeschlagenen."""
    procs = [(c, subprocess.Popen(["pw-link", *c], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True))
             for c in commands]
    errors = []
    for c, p in procs:
        try:
            err = p.communicate(timeout=5)[1]
        except subprocess.TimeoutExpired:
            p.kill()
            err = p.communicate()[1] + " (Zeitüberschreitung)"
        if p.returncode:
            errors.append(f"pw-link {' '.join(c)}: {err.strip()}")
    return errors


class Linker:
    """Entscheidet bei jeder Änderung des Graphen neu; `run` führt pw-link-Aufrufe aus (Tests: Attrappe)."""

    def __init__(self, metadata: str = "default", run=pw_link):
        self.graph, self.gate, self.metadata, self.run = Graph(), Gate(), metadata, run
        #: angeforderte, im Graphen noch nicht sichtbare Änderungen: ("add", Paar) / ("del", Link-ID) → Ablaufzeit
        self.pending: dict[tuple, float] = {}
        #: Referenz-Knoten schon gesehen; seit wann er fehlt
        self.seen, self.gone_since = False, None

    def deadline(self) -> float | None:
        gone = None if self.gone_since is None else self.gone_since + GONE_S
        times = [t for t in (self.gate.deadline(), gone, *self.pending.values()) if t is not None]
        return min(times, default=None)

    def finished(self, now: float) -> bool:
        return self.gone_since is not None and now - self.gone_since >= GONE_S

    def step(self, now: float) -> tuple[set[Pair], set[int]]:
        if self.graph.nodes(K.AEC_REF_NODE):
            self.seen, self.gone_since = True, None
        elif self.seen and self.gone_since is None:
            self.gone_since = now
        active = self.gate.update(in_use(self.graph), now)
        self.pending = {key: t for key, t in self.pending.items() if t > now}
        sink = self.graph.default_sink(self.metadata)
        add, remove = plan(self.graph, active, sink)
        add = {p for p in add if ("add", p) not in self.pending}
        remove = {lid for lid in remove if ("del", lid) not in self.pending}
        if not add and not remove:
            return add, remove
        self.pending.update({("add", p): now + PENDING_S for p in add})
        self.pending.update({("del", lid): now + PENDING_S for lid in remove})
        # Erst verbinden, dann trennen: beim Wechsel der Standardausgabe bleibt die Referenz lückenlos
        errors = self.run([["-P", str(o), str(i)] for o, i in sorted(add)]) if add else []
        errors += self.run([["-d", str(lid)] for lid in sorted(remove)]) if remove else []
        for e in errors:
            log.warning("%s", e)
        if add:
            log.info("Echo-Referenz verbunden: %s (%d Ports)", sink, len(add))
        elif not active:
            log.info("Echo-Referenz getrennt")
        return add, remove


def batches(buf: bytes) -> tuple[list[list[dict]], bytes]:
    """Vollständige JSON-Arrays aus der Ausgabe von pw-dump --monitor und der unvollständige Rest.
    pw-dump schließt jedes Array mit „]“ am Zeilenanfang; innere Klammern sind eingerückt."""
    *parts, rest = buf.split(b"\n]\n")
    return [json.loads((p + b"\n]").decode("utf-8", "replace")) for p in parts], rest


def follow(linker: Linker) -> bool:
    """Liest pw-dump --monitor und entscheidet nach jeder Änderung und jedem Zeitablauf.
    True, wenn die Kette weg ist; False, wenn pw-dump endet."""
    proc = subprocess.Popen(["pw-dump", "--monitor", "--no-colors"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    linker.graph = Graph()
    parts, tail = [], b""  # pw-dump schreibt zeilenweise: Stücke sammeln, erst am Ende eines Arrays zerlegen
    try:
        fd = proc.stdout.fileno()
        while True:
            deadline = linker.deadline()
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            changed = False
            if select.select([fd], [], [], timeout)[0]:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    return False
                parts.append(chunk)
                seam = tail + chunk  # Arrayende „\n]\n“ kann über die Stückgrenze reichen
                tail = seam[-2:]
                if b"\n]\n" in seam:
                    done, rest = batches(b"".join(parts))
                    parts = [rest]
                    for objs in done:
                        linker.graph.update(objs)
                    changed = bool(done)
            if changed or timeout is not None:
                now = time.monotonic()
                linker.step(now)
                if linker.finished(now):
                    return True
    finally:
        proc.kill()
        proc.wait()


def run(metadata: str = "default") -> int:
    """Läuft bis zum Ende des Dienstes; pw-dump wird nach Ende oder Fehler neu gestartet."""
    # Per context.exec gestartet erbt der Helfer PipeWires Signalmaske: SIGINT/SIGTERM blockiert (PipeWire liest
    # sie über signalfd). Ohne Freigabe überhörte er das SIGTERM beim Stoppen des Dienstes (gemessen: 45 s Wartezeit,
    # dann SIGABRT); pw-dump und pw-link erben die freigegebene Maske.
    signal.pthread_sigmask(signal.SIG_SETMASK, set())
    linker = Linker(metadata)
    log.info("Echo-Referenz folgt %s/default.audio.sink", metadata)
    delay = RETRY_S
    while True:
        start = time.monotonic()
        try:
            if follow(linker):
                log.info("Kette beendet, Helfer endet")
                return 0
            problem = "pw-dump beendet"
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
            problem = f"Fehler: {e}"
        delay = RETRY_S if time.monotonic() - start > RETRY_MAX_S else delay
        log.warning("%s; Neustart in %g s", problem, delay)
        time.sleep(delay)
        delay = min(2 * delay, RETRY_MAX_S)
