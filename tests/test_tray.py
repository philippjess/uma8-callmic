"""TrayApp offscreen mit Attrappen für PipeWire, systemd und Aufnahme: Nachführung nur während einer Aufnahme,
Live-Werte ohne und mit Arbeitsplatz-Profil. Schreibt nur ins Temp-Verzeichnis."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from uma8_callmic import constants as K
from uma8_callmic import pwctl
from uma8_callmic import tray as tray_mod
from uma8_callmic.config import Config, profile_defaults, save
from uma8_callmic.params import NULL_KEYS, all_params
from uma8_callmic.tracker import Zone


def graph(in_use: bool) -> list[dict]:
    """pw-dump: UMA-8 (Raw), Kette, AEC, „UMA-8 Call Mic“ und ein Anruf-Programm, das (nicht) aufnimmt."""
    node = lambda i, name, state="idle": {"id": i, "type": "PipeWire:Interface:Node",  # noqa: E731
                                          "info": {"props": {"node.name": name}, "state": state}}
    objs = [{"id": 1, "type": "PipeWire:Interface:Device",
             "info": {"props": {"device.vendor.id": "0x2752", "device.product.id": "0x001d"}}},
            node(10, K.CAPTURE_NODE), node(11, K.AEC_NODE), node(12, K.SOURCE_NODE),
            node(20, "Anruf", "running" if in_use else "suspended")]
    if in_use:
        objs.append({"id": 30, "type": "PipeWire:Interface:Link", "info": {"output-node-id": 12, "input-node-id": 20}})
    return objs


class FakeCapture:
    def __init__(self, target, seconds):
        self.target, self.alive, self.closed = target, True, False

    def latest(self, frames):
        return None

    def close(self):
        self.closed, self.alive = True, False


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(qapp, tmp_path, monkeypatch):
    state = {"in_use": False, "params": [], "captures": []}
    monkeypatch.setattr(K, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(K, "dfn_plugin", lambda: Path(__file__))
    monkeypatch.setattr(K, "beam_plugin", lambda: Path(__file__))
    monkeypatch.setattr(tray_mod.chainconf, "write", lambda cfg: False)
    monkeypatch.setattr(pwctl, "sync_autostart", lambda enabled: False)
    monkeypatch.setattr(pwctl, "ensure_service_enabled", lambda: False)
    monkeypatch.setattr(pwctl, "restart_chain", lambda: None)
    monkeypatch.setattr(pwctl, "service_active", lambda: True)
    monkeypatch.setattr(pwctl, "raw_source", lambda: K.RAW_DEVICE)
    monkeypatch.setattr(pwctl, "dump", lambda: graph(state["in_use"]))
    monkeypatch.setattr(pwctl, "set_params", lambda node, p: state["params"].append((node, dict(p))))

    def capture(target, seconds):
        cap = FakeCapture(target, seconds)
        state["captures"].append(cap)
        return cap
    monkeypatch.setattr(tray_mod, "open_capture", capture)

    def start(cfg: Config):
        save(cfg, K.CONFIG_FILE)
        app = tray_mod.TrayApp(qapp)
        app.track_gate.hold = 0.0     # Nachlauf (2 s) im Test sofort
        state["app"] = app
        return app
    state["start"] = start
    yield state
    if "app" in state:
        state["app"].timer.stop()
        state["app"].shutdown()


def test_tracking_runs_only_while_mic_in_use(env):
    app = env["start"](Config(direction_mode="tracking", calibrated=True, calibrated_azimuth=200.0,
                              geometry_checked=True))
    assert env["captures"] == [] and app.tracker_thread is None          # niemand nimmt auf
    assert "ruht ohne Aufnahme" in app.tray.toolTip()
    env["in_use"] = True
    app.refresh()
    assert len(env["captures"]) == 1 and env["captures"][0].target[0] == K.AEC_NODE
    assert app.tracker.zone is None                                      # ohne Profil wie bisher
    app._apply_tracked(123.0)
    assert env["params"][-1][1]["beam:Azimuth (deg)"] == 123.0
    env["in_use"] = False
    app.refresh()
    assert env["captures"][0].closed and app.tracker_thread is None and app.tracked == 123.0
    env["in_use"] = True
    app.refresh()                                                       # neue Aufnahme, Strahl bleibt
    assert len(env["captures"]) == 2 and app.tracker.current == 123.0
    app.apply_options({"direction_mode": "calibrated"})
    assert env["captures"][1].closed and app.tracked is None and len(env["captures"]) == 2


def test_failed_pw_dump_keeps_tracking_state(env, monkeypatch):
    env["in_use"] = True
    app = env["start"](Config(direction_mode="tracking", geometry_checked=True))
    assert app.tracker_thread is not None

    def broken():
        raise RuntimeError("pw-dump hängt")
    monkeypatch.setattr(pwctl, "dump", broken)
    app.refresh()
    assert app.tracker_thread is not None and app.mic_in_use


def test_no_profile_applies_exactly_the_old_params(env):
    env["in_use"] = True
    app = env["start"](Config(geometry_checked=True))
    node, sent = env["params"][-1]                                        # Kette erkannt → alle Werte
    assert node == 10 and sent == all_params(app.cfg) and not any("Null" in k for k in sent)


def test_profile_is_applied_live_and_cleared(env):
    env["in_use"] = True
    app = env["start"](Config(direction_mode="tracking", calibrated=True, calibrated_azimuth=200.0,
                              geometry_checked=True))
    app.apply_profile({"speakers": [[100.0, 5.0], [300.0, 5.0]], "null_weight_db": 10.0, "talker_zone_deg": 40.0})
    sent = env["params"][-1][1]
    assert [sent[k] for k in NULL_KEYS] == [100.0, 5.0, 300.0, 5.0, 10.0]
    assert app.tracker.zone == Zone(200.0, 40.0, (100.0, 300.0))
    assert "speakers = [[100.0, 5.0], [300.0, 5.0]]" in K.CONFIG_FILE.read_text()
    app.calibrated(210.0, 20.0)                                          # Zone folgt der Kalibrierung
    assert app.tracker.zone.center == 210.0
    app.apply_profile(profile_defaults())
    sent = env["params"][-1][1]
    assert [sent[k] for k in NULL_KEYS] == [0.0] * 5 and app.tracker.zone is None


def test_menu_has_new_entries(env):
    app = env["start"](Config(geometry_checked=True))
    texts = [a.text() for a in app.menu.actions()]
    assert "Arbeitsplatz einmessen…" in texts and "Platzierung…" in texts
