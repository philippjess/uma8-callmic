import math
import re
import subprocess
import time

import pytest
from spajson import loads

from uma8_callmic import constants as K
from uma8_callmic.chainconf import LINEAR_MAX_MULT, gain_stages, render, write
from uma8_callmic.config import Config

CHAIN_MODULES = ("libpipewire-module-filter-chain", "libpipewire-module-echo-cancel")


def modules(cfg: Config) -> list[dict]:
    """Filterketten- und Echo-Cancel-Module der erzeugten Konfiguration, in Ladereihenfolge."""
    return [m for m in loads(render(cfg))["context.modules"] if m["name"] in CHAIN_MODULES]


@pytest.mark.parametrize("echo", [True, False])
def test_render_fills_all_placeholders(echo):
    text = render(Config(echo_cancel=echo))
    assert "@" not in text
    assert f'plugin = "{K.beam_plugin()}"' in text
    assert f'plugin = "{K.dfn_plugin()}"' in text
    assert '"Azimuth (deg)" = 0' in text
    assert '"Attenuation Limit (dB)" = 30' in text
    assert text.count("{") == text.count("}")
    assert text.count("[") == text.count("]")


def test_render_reflects_active_state():
    on = re.search(r'label = mixer\s+control = \{ (.*?) \}', render(Config(active=True))).group(1)
    off = re.search(r'label = mixer\s+control = \{ (.*?) \}', render(Config(active=False))).group(1)
    assert on == '"Gain 1" = 1 "Gain 2" = 0'
    assert off == '"Gain 1" = 0 "Gain 2" = 1'


@pytest.mark.parametrize("echo", [True, False])
def test_source_never_suspends(echo):
    """Jedes Aufwachen leckt einen DeepFilterNet-Thread, also darf die Quelle nicht schlafen."""
    playback = modules(Config(echo_cancel=echo))[-1]["args"]["playback.props"]
    assert playback["node.name"] == K.SOURCE_NODE and playback["media.class"] == "Audio/Source"
    assert playback["session.suspend-timeout-seconds"] == "0" and playback["priority.session"] == "2500"


def test_without_echo_cancel_main_chain_reads_raw_device():
    text = render(Config(echo_cancel=False))
    (main,) = modules(Config(echo_cancel=False))
    cap = main["args"]["capture.props"]
    assert main["name"] == "libpipewire-module-filter-chain"
    assert cap["node.name"] == K.CAPTURE_NODE and cap["target.object"] == K.RAW_DEVICE
    assert cap["audio.channels"] == "8" and cap["audio.position"] == list(K.RAW_POSITIONS)
    assert cap["node.passive"] == "true" and cap["stream.dont-remix"] == "true"
    assert main["args"]["filter.graph"]["inputs"] == [f"beam:In {i}" for i in range(7)] + [None]
    assert K.PRE_NODE not in text and K.AEC_NODE not in text and "echo-cancel" not in text
    assert '"Gain (dB)" = 30 ' in text


def _chain_gain(graph: dict, start: str) -> tuple[float, str]:
    """Folgt den Links ab einem Graph-Eingang: Produkt der „Mult“-Werte und erreichter Ausgang."""
    nodes = {n["name"]: n for n in graph["nodes"]}
    links = {link["output"]: link["input"] for link in graph["links"]}
    port, gain = start, 1.0
    while True:
        node = nodes[port.split(":")[0]]
        assert node["type"] == "builtin" and node["label"] == "linear"
        mult = float(node["control"]["Mult"])
        assert 0 < mult <= LINEAR_MAX_MULT  # PipeWire klemmt „Mult“ still auf ±10
        gain *= mult
        out = node["name"] + ":Out"
        if out not in links:
            return gain, out
        port = links[out]


def test_echo_cancel_chain_structure():
    cfg = Config()
    assert cfg.echo_cancel is True
    pre, aec, main = modules(cfg)
    assert [m["name"] for m in (pre, aec, main)] == ["libpipewire-module-filter-chain",
                                                     "libpipewire-module-echo-cancel",
                                                     "libpipewire-module-filter-chain"]
    mics = list(K.MIC_POSITIONS)
    assert mics == [f"AUX{i}" for i in range(7)]

    # Vorverstärkung: liest das Gerät wie bisher die Hauptkette, Kanal 7 fällt weg, 7 Kanäle intern
    cap, out = pre["args"]["capture.props"], pre["args"]["playback.props"]
    assert cap["target.object"] == K.RAW_DEVICE and cap["audio.channels"] == "8"
    assert cap["audio.position"] == list(K.RAW_POSITIONS)
    assert cap["node.passive"] == "true" and cap["stream.dont-remix"] == "true"
    assert out["node.name"] == K.PRE_NODE and out["media.class"] == "Audio/Source/Internal"
    assert out["audio.channels"] == "7" and out["audio.position"] == mics
    graph = pre["args"]["filter.graph"]
    assert len(graph["inputs"]) == 8 and graph["inputs"][7] is None and len(graph["outputs"]) == 7
    for ch in range(7):
        gain, reached = _chain_gain(graph, graph["inputs"][ch])
        assert reached == graph["outputs"][ch]
        assert math.isclose(20 * math.log10(gain), K.AEC_PRE_GAIN_DB, abs_tol=1e-3)

    # Echounterdrückung: Referenz = Monitor der Standardausgabe, NS und AGC aus
    a = aec["args"]
    assert a["monitor.mode"] == "true"
    assert a["capture.props"]["target.object"] == K.PRE_NODE and a["capture.props"]["node.passive"] == "true"
    assert a["capture.props"]["audio.position"] == mics
    assert a["source.props"]["node.name"] == K.AEC_NODE
    assert a["source.props"]["media.class"] == "Audio/Source/Internal"
    assert a["source.props"]["audio.position"] == mics
    # Referenz: WirePlumber verbindet sie nie (sonst weckte jede Wiedergabe die Kette), das macht reflink
    ref = a["sink.props"]
    assert ref["node.name"] == K.AEC_REF_NODE and ref["node.autoconnect"] == "false"
    assert ref["audio.position"] == ["FL", "FR"] and "target.object" not in ref
    assert a["aec.args"] == {"webrtc.high_pass_filter": "true", "webrtc.noise_suppression": "false",
                             "webrtc.gain_control": "false"}

    # Hauptkette: 7 Kanäle aus der AEC, ohne null-Eingang
    cap = main["args"]["capture.props"]
    assert cap["node.name"] == K.CAPTURE_NODE and cap["target.object"] == K.AEC_NODE
    assert cap["audio.channels"] == "7" and cap["audio.position"] == mics and cap["node.passive"] == "true"
    assert main["args"]["filter.graph"]["inputs"] == [f"beam:In {i}" for i in range(7)]

    names = [m["args"][k]["node.name"] for m in (pre, aec, main) for k in m["args"] if k.endswith(".props")]
    assert len(names) == len(set(names)) == 7 and all(n.startswith("uma8_callmic") for n in names)


@pytest.mark.parametrize("echo", [True, False])
def test_ref_linker_starts_with_chain_only_with_echo_cancel(echo, monkeypatch):
    monkeypatch.setattr(K.shutil, "which", lambda name: "/usr/bin/uma8-callmic")
    conf = loads(render(Config(echo_cancel=echo)))
    if echo:
        assert conf["context.exec"] == [{"path": "/usr/bin/uma8-callmic", "args": ["--ref-linker"]}]
    else:
        assert "context.exec" not in conf


def test_echo_cancel_moves_gain_before_aec():
    text = render(Config(gain_db=30.0))
    assert '"Gain (dB)" = 6 ' in text
    assert "Audio/Source/Virtual" not in text  # stürzt PipeWire 1.6.9 bei Stream-Knoten ab


def test_gain_stages():
    assert gain_stages(24.0) == pytest.approx([10 ** 0.6] * 2)
    assert gain_stages(20.0) == pytest.approx([10.0])
    assert gain_stages(0.0) == [1.0]
    for db in (6.0, 24.0, 41.0, 60.0):
        stages = gain_stages(db)
        assert max(stages) <= LINEAR_MAX_MULT and math.isclose(20 * math.log10(math.prod(stages)), db)


def test_template_comes_from_package():
    assert K.DATA_DIR.parent == K.PKG_DIR
    assert (K.DATA_DIR / "uma8-callmic.conf.in").is_file()


@pytest.fixture
def ladspa_dirs(tmp_path, monkeypatch):
    """Drei leere Suchorte in der Reihenfolge von K.LADSPA_DIRS (lokal, RPM, /usr/lib)."""
    dirs = tuple(tmp_path / name for name in ("local", "lib64", "lib"))
    for d in dirs:
        d.mkdir()
    monkeypatch.setattr(K, "LADSPA_DIRS", dirs)
    monkeypatch.delenv("UMA8_BEAM_PLUGIN", raising=False)
    monkeypatch.delenv("UMA8_DFN_PLUGIN", raising=False)
    return dirs


def test_plugin_search_order(ladspa_dirs):
    local, lib64, lib = ladspa_dirs
    assert K.dfn_plugin() == lib64 / K.DFN_SO  # nirgends installiert: RPM-Ort, Tray meldet das Fehlen
    (lib / K.DFN_SO).touch()
    assert K.dfn_plugin() == lib / K.DFN_SO
    (lib64 / K.DFN_SO).touch()
    assert K.dfn_plugin() == lib64 / K.DFN_SO
    (local / K.DFN_SO).touch()
    assert K.dfn_plugin() == local / K.DFN_SO


def test_plugin_env_override_wins(ladspa_dirs, monkeypatch, tmp_path):
    (ladspa_dirs[0] / K.BEAM_SO).touch()
    monkeypatch.setenv("UMA8_BEAM_PLUGIN", str(tmp_path / "eigenes.so"))
    assert K.beam_plugin() == tmp_path / "eigenes.so"  # auch wenn sie fehlt: Fehler soll sichtbar werden


def test_render_resolves_plugins_at_render_time(ladspa_dirs):
    local, lib64, _ = ladspa_dirs
    (lib64 / K.BEAM_SO).touch()
    (lib64 / K.DFN_SO).touch()
    assert f'plugin = "{lib64 / K.BEAM_SO}"' in render(Config())
    (local / K.BEAM_SO).touch()
    text = render(Config())
    assert f'plugin = "{local / K.BEAM_SO}"' in text
    assert f'plugin = "{lib64 / K.DFN_SO}"' in text


def test_write_reports_changes(tmp_path):
    path = tmp_path / "uma8-callmic.conf"
    assert write(Config(), path) is True
    assert write(Config(), path) is False
    assert write(Config(gain_db=40.0), path) is True


@pytest.mark.integration
@pytest.mark.parametrize("echo", [True, False])
def test_config_starts_in_pipewire(tmp_path, echo):
    """Probelauf: braucht installiertes Plugin (install.sh) und laufendes PipeWire. Niedrige Priorität, damit die
    Probe-Quelle nie Standardmikrofon wird (fehlt das konfigurierte, wählt WirePlumber nach Priorität)."""
    path = tmp_path / "probe.conf"
    text = render(Config(echo_cancel=echo)).replace('"uma8_callmic', '"uma8_callmic_probe')
    text = text.replace("priority.session = 2500", "priority.session = 1")
    # Ohne Referenz-Helfer: dessen Prozess überlebte die Probe (doppelter fork, kein systemd-Dienst)
    path.write_text(re.sub(r"\ncontext\.exec = \[.*?\n\]\n", "\n", text, flags=re.S))
    proc = subprocess.Popen(["pipewire", "-c", str(path)], stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    proc.terminate()
    _, err = proc.communicate(timeout=5)
    assert "error" not in err.lower(), err
