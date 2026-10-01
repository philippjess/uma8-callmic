"""Echo-Referenz-Helfer (reflink) auf aufgezeichneten pw-dump-Graphen, ohne PipeWire.

data/reflink_graphs.json stammt aus einer Testkette ohne UMA-8 (Fake-Array, Testsenken uma8t_refsink und
uma8t_refsink2; deren Metadaten-Objekt heißt hier „default“), gekürzt auf Knoten, Links und die Ports der
beteiligten Knoten:
  idle_music   Musik auf uma8t_refsink, niemand nimmt auf, Referenz unverbunden
  call         pw-record nimmt uma8_callmic auf, Referenz noch unverbunden
  call_linked  dito, Referenz mit dem Monitor von uma8t_refsink verbunden (pw-link -P)
  ended        Aufnahme beendet, Referenz noch verbunden: die Musik hält jetzt die ganze Kette am Laufen"""
import copy
import json
import os
import threading
from pathlib import Path

import pytest

from uma8_callmic import __main__ as main_mod
from uma8_callmic import constants as K
from uma8_callmic import reflink
from uma8_callmic.reflink import HOLD_S, PENDING_S, Gate, Graph, Linker, batches, in_use, plan, ref_links, wanted

GRAPHS = json.loads((Path(__file__).parent / "data/reflink_graphs.json").read_text())
SINK, SINK2 = "uma8t_refsink", "uma8t_refsink2"


def graph(name: str) -> Graph:
    return Graph(copy.deepcopy(GRAPHS[name]))


def port(g: Graph, node: str, channel: str) -> int:
    """Monitor-Port einer Senke bzw. Eingangsport der Referenz zu einem Kanal."""
    direction, monitor = ("input", False) if node == K.AEC_REF_NODE else ("output", True)
    (p,) = [p["id"] for p in g.ports(g.nodes(node), direction, monitor)
            if p["info"]["props"]["audio.channel"] == channel]
    return p


def pairs(g: Graph, sink: str) -> set[tuple[int, int]]:
    return {(port(g, sink, ch), port(g, K.AEC_REF_NODE, ch)) for ch in ("FL", "FR")}


def test_in_use_follows_recorders_not_the_source_state():
    assert not in_use(graph("idle_music"))
    assert in_use(graph("call")) and in_use(graph("call_linked"))
    ended = graph("ended")
    (src,) = ended.nodes(K.SOURCE_NODE)
    # Ohne Aufnahme läuft uma8_callmic trotzdem – geweckt von der Musik über die verbundene Referenz. Am Zustand
    # der Quelle gemessen bliebe die Referenz deshalb für immer verbunden.
    assert ended.state(src) == "running" and not in_use(ended)


def test_default_sink_from_metadata():
    g = graph("call")
    assert g.default_sink() == SINK
    assert g.default_sink("anders") is None
    meta = next(o for o in GRAPHS["call"] if o["type"].endswith("Metadata"))
    as_text = dict(meta, metadata=[dict(meta["metadata"][0], value='{"name": "x"}')])
    assert Graph([as_text]).default_sink() == "x"  # Wert als Zeichenkette statt geparstem JSON


def test_wanted_matches_monitor_channels():
    g = graph("call")
    assert wanted(g, SINK) == pairs(g, SINK)
    assert wanted(g, SINK2) == pairs(g, SINK2) and pairs(g, SINK2).isdisjoint(pairs(g, SINK))
    assert wanted(g, "gibt_es_nicht") == set()


def test_wanted_mono_sink_and_foreign_channels():
    g = graph("call")
    fr = port(g, SINK, "FR")
    mono = Graph([o for o in copy.deepcopy(GRAPHS["call"]) if o["id"] != fr])
    assert wanted(mono, SINK) == {(port(g, SINK, "FL"), port(g, K.AEC_REF_NODE, ch)) for ch in ("FL", "FR")}
    renamed = copy.deepcopy(GRAPHS["call"])
    sink_ports = {port(g, SINK, "FL"): "AUX0", fr: "AUX1"}
    for o in renamed:
        if o["id"] in sink_ports:
            o["info"]["props"]["audio.channel"] = sink_ports[o["id"]]
    assert wanted(Graph(renamed), SINK) == pairs(g, SINK)  # keine gemeinsamen Kanalnamen: der Reihe nach


def test_plan():
    call, linked, ended = graph("call"), graph("call_linked"), graph("ended")
    links = set(ref_links(linked).values())
    assert len(links) == 2 and set(ref_links(linked)) == pairs(linked, SINK)
    assert plan(call, True, SINK) == (pairs(call, SINK), set())  # Anruf beginnt: verbinden
    assert plan(linked, True, SINK) == (set(), set())  # verbunden: nichts tun
    assert plan(linked, True, SINK2) == (pairs(linked, SINK2), links)  # Standardausgabe gewechselt: umhängen
    assert plan(linked, True, None) == (set(), set())  # Standardausgabe (noch) unbekannt: nichts anfassen
    assert plan(ended, False, SINK) == (set(), links)  # Anruf vorbei: trennen
    assert plan(graph("idle_music"), False, SINK) == (set(), set())


def test_gate_links_at_once_and_unlinks_after_hold():
    gate = Gate(hold=2.0)
    assert gate.update(False, 0.0) is False and gate.deadline() is None
    assert gate.update(True, 1.0) is True
    assert gate.update(False, 2.0) is True and gate.deadline() == 4.0
    assert gate.update(True, 3.9) is True and gate.deadline() is None  # kurz neu geöffnet: bleibt verbunden
    assert gate.update(False, 5.0) is True
    assert gate.update(False, 6.9) is True
    assert gate.update(False, 7.0) is False and gate.deadline() is None


class FakePwLink:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, commands):
        self.calls += commands
        return []

    def take(self) -> list[list[str]]:
        calls, self.calls = self.calls, []
        return calls


def test_linker_call_cycle():
    run = FakePwLink()
    linker = Linker(run=run)
    linker.graph = graph("idle_music")
    linker.step(0.0)
    assert run.take() == []

    linker.graph = graph("call")
    linker.step(10.0)
    fl, fr = sorted(pairs(linker.graph, SINK))
    assert run.take() == [["-P", str(fl[0]), str(fl[1])], ["-P", str(fr[0]), str(fr[1])]]
    linker.step(10.1)  # Links noch nicht im Graphen: nicht doppelt anfordern
    assert run.take() == []
    linker.step(10.0 + PENDING_S)  # nach PENDING_S immer noch nicht da: neuer Versuch
    assert len(run.take()) == 2

    linker.graph = graph("call_linked")
    linker.step(13.0)
    assert run.take() == []

    linker.graph = graph("ended")
    linker.step(20.0)
    assert run.take() == [] and linker.deadline() == pytest.approx(20.0 + HOLD_S)
    linker.step(20.0 + HOLD_S)
    assert run.take() == [["-d", str(lid)] for lid in sorted(ref_links(linker.graph).values())]


def test_linker_relinks_before_unlinking():
    run = FakePwLink()
    linker = Linker(run=run)
    linker.graph = graph("call_linked")
    linker.step(0.0)
    assert run.take() == []
    meta = next(o for o in linker.graph.of("Metadata"))
    linker.graph.update([dict(meta, metadata=[dict(meta["metadata"][0], value={"name": SINK2})])])
    linker.step(1.0)
    calls = run.take()
    assert [c[0] for c in calls] == ["-P", "-P", "-d", "-d"]  # erst verbinden, dann trennen
    assert {(int(c[1]), int(c[2])) for c in calls[:2]} == pairs(linker.graph, SINK2)


def test_linker_restart_mid_call_adopts_links():
    """Neu gestarteter Helfer (z. B. nach kill) übernimmt bestehende Links, statt sie neu anzulegen."""
    run = FakePwLink()
    linker = Linker(run=run)
    linker.graph = graph("call_linked")
    linker.step(0.0)
    assert run.take() == []
    linker = Linker(run=run)  # Neustart nach Anrufende: verwaiste Links sofort trennen, ohne Wartezeit
    linker.graph = graph("ended")
    linker.step(0.0)
    assert sorted(c[0] for c in run.take()) == ["-d", "-d"]


def test_linker_finishes_when_chain_is_gone():
    linker = Linker(run=FakePwLink())
    linker.step(0.0)  # Kette noch nicht da: warten, nicht enden
    assert not linker.finished(100.0)
    linker.graph = graph("idle_music")
    linker.step(1.0)
    linker.graph = Graph([o for o in GRAPHS["idle_music"] if o["type"].endswith("Metadata")])
    linker.step(2.0)
    assert linker.deadline() == pytest.approx(2.0 + reflink.GONE_S) and not linker.finished(2.0)
    assert linker.finished(2.0 + reflink.GONE_S)


def test_graph_applies_monitor_changes():
    g = graph("call")
    (rec,) = g.nodes("pw-record")
    links = [o["id"] for o in g.of("Link") if o["info"]["input-node-id"] == rec]
    g.update([{"id": rec, "info": None}] + [{"id": lid, "info": None} for lid in links])  # so meldet pw-dump Entfernte
    assert not g.nodes("pw-record") and not in_use(g)
    meta = copy.deepcopy(next(o for o in g.of("Metadata")))
    meta["metadata"] = [{"subject": 0, "key": "default.audio.source", "type": "Spa:String:JSON",
                         "value": {"name": "y"}}]
    g.update([meta])  # pw-dump schreibt nur geänderte Einträge: die Standardausgabe bleibt bekannt
    assert g.default_sink() == SINK


def _monitor_text(batch_list) -> bytes:
    """Ausgabe wie pw-dump --monitor: je Änderung ein eingerücktes JSON-Array, „]“ am Zeilenanfang."""
    return b"".join(json.dumps(b, indent=2).encode() + b"\n" for b in batch_list)


@pytest.mark.parametrize("chunk", [1, 7, 4096, 1 << 20])
def test_batches_splits_monitor_output(chunk):
    sent = [GRAPHS["call"], [{"id": 5, "info": None}], GRAPHS["ended"][:3]]
    text, got, rest = _monitor_text(sent), [], b""
    for i in range(0, len(text), chunk):
        done, rest = batches(rest + text[i:i + chunk])
        got += done
    assert got == sent and rest == b""
    assert batches(b'[\n  {\n    "id": 1')[0] == []


def test_follow_reads_pw_dump_in_pieces(monkeypatch):
    """follow() mit einem pw-dump-Ersatz, der seine Ausgabe in krummen Stücken schreibt."""
    r, w = os.pipe()

    class FakeDump:
        stdout = os.fdopen(r, "rb", buffering=0)

        def kill(self):
            pass

        def wait(self):
            pass

    def writer():
        text = _monitor_text([GRAPHS["call"], [{"id": 5, "info": None}]])
        for i in range(0, len(text), 999):
            os.write(w, text[i:i + 999])
        os.close(w)

    monkeypatch.setattr(reflink.subprocess, "Popen", lambda *a, **k: FakeDump())
    run = FakePwLink()
    linker = Linker(run=run)
    thread = threading.Thread(target=writer)
    thread.start()
    assert reflink.follow(linker) is False  # pw-dump zu Ende: Neustart, nicht beenden
    thread.join()
    assert sorted(c[0] for c in run.take()) == ["-P", "-P"]
    assert linker.graph.nodes("pw-record")


def test_main_dispatches_ref_linker(monkeypatch):
    seen = []
    monkeypatch.setattr(reflink, "run", lambda metadata: seen.append(metadata) or 0)
    monkeypatch.setenv(reflink.METADATA_ENV, "uma8t")
    assert main_mod.main(["--ref-linker"]) == 0
    monkeypatch.delenv(reflink.METADATA_ENV)
    assert main_mod.main(["--ref-linker"]) == 0
    assert seen == ["uma8t", "default"]


def test_run_retries_with_backoff_and_ends_with_chain(monkeypatch):
    results = iter([ValueError("kaputt"), False, ValueError("kaputt"), True])

    def fake_follow(linker):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return r

    sleeps = []
    monkeypatch.setattr(reflink, "follow", fake_follow)
    monkeypatch.setattr(reflink.time, "sleep", sleeps.append)
    assert reflink.run("uma8t") == 0
    assert sleeps == [2.0, 4.0, 8.0]  # scheitert pw-dump immer wieder, nicht im 2-s-Takt den Graphen lesen


def test_batches_tolerates_invalid_utf8():
    (objs,), _ = batches(b'[\n  {\n    "id": 1,\n    "name": "\xff"\n  }\n]\n')
    assert objs[0]["id"] == 1
