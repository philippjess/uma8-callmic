"""Assistent und Platzierungsansicht offscreen, mit simuliertem Arbeitsplatz (tests/desksim.py): es wird nie
wirklich abgespielt oder aufgenommen."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from desksim import SR, Clock, SceneCapture
from sim import plane_wave
from test_doa import speech_like
from uma8_callmic.array import UMA8
from uma8_callmic.config import Config, profile_defaults
from uma8_callmic.doa import angle_diff
from uma8_callmic.workspace_ui import ZONES, PlacementWindow, PolarPlot, WorkspaceWizard, scene_for, speech_filter

P = UMA8.positions()
CAL = dict(calibrated=True, calibrated_azimuth=215.0, calibrated_elevation=25.0)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def no_capture(seconds):
    raise AssertionError("darf keine echte Aufnahme öffnen")


def test_wizard_end_to_end(app):
    clock = Clock()
    cap = SceneCapture(clock, P, sources=[(115.0, 5.0), (315.0, 5.0)], seconds=60.0)
    saved = []
    wiz = WorkspaceWizard(Config(**CAL), saved.append, capture_factory=lambda s: cap, player=cap.play,
                          output_name=lambda: "Testausgabe")
    assert "1 von 4" in wiz.heading.text() and wiz.next.isEnabled()
    wiz.measure_btn.click()
    assert not wiz.next.isEnabled() and not wiz.measure_btn.isEnabled()      # während der Messung
    while wiz.runner.running and clock.now < 20:
        clock.now += 0.1
        wiz.runner.tick(clock.now)
    assert wiz.speakers is not None and cap.played == [0, 1] and cap.closed
    assert [angle_diff(d[0], a) <= 1.0 for d, a in zip(wiz.speakers.directions, (115.0, 315.0))] == [True, True]
    assert "Lautsprecher links" in wiz.sweep_info.text() and wiz.next.isEnabled()
    wiz.next.click()
    assert "Bisherige Kalibrierung: 215°" in wiz.cal_info.text() and wiz.next.isEnabled()
    wiz.next.click()
    cap.closed = False
    wiz.type_btn.click()
    cap.add_typing(213.0, 5.0, clock.now + 0.1, 6.0)
    while wiz.typing_capture is not None and clock.now < 40:
        clock.now += 0.2
        wiz._typing_tick(clock.now)
    assert wiz.keyboard is not None and angle_diff(wiz.keyboard.azimuth, 213.0) <= 3.0, wiz.type_info.text()
    wiz.next.click()
    assert wiz.next.text() == "Speichern" and wiz.nulls.isEnabled() and not wiz.nulls.isChecked()
    wiz.zone.setValue(ZONES.index(40))
    wiz.nulls.setChecked(True)
    assert "±40°" in wiz.zone_label.text() and "Lautsprecher links: 115°" in wiz.summary.text()
    assert wiz.plot.talker == (215.0, 25.0) and len(wiz.plot.speakers) == 2 and wiz.plot.keyboard
    wiz.next.click()
    u = saved[-1]
    assert u["talker_zone_deg"] == 40.0 and u["null_weight_db"] == 10.0 and len(u["speakers"]) == 2
    assert u["noise_floor_dbfs"] < -70 and len(u["speaker_levels_dbfs"]) == 2 and len(u["keyboard"]) == 2
    assert "calibrated_azimuth" not in u                         # Kalibrierung nicht wiederholt: bleibt


def test_wizard_skip_keeps_profile_and_nulls_need_speakers(app):
    saved = []
    wiz = WorkspaceWizard(Config(), saved.append, capture_factory=no_capture, player=no_capture,
                          output_name=lambda: "x")
    wiz.next.click()
    assert not wiz.next.isEnabled()                               # ohne Kalibrierung nicht weiter
    wiz._calibrated(200.0, 20.0)
    assert wiz.next.isEnabled() and "Neu kalibriert: 200°" in wiz.cal_info.text()
    wiz.next.click()
    wiz.next.click()
    assert not wiz.nulls.isEnabled()
    wiz.nulls.setChecked(True)
    wiz.next.click()
    assert saved[-1] == {"calibrated": True, "calibrated_azimuth": 200.0, "calibrated_elevation": 20.0,
                         "talker_zone_deg": 180.0, "null_weight_db": 0.0}


def test_wizard_measurement_failure_message(app):
    clock = Clock()
    cap = SceneCapture(clock, P, sources=[None, None])
    wiz = WorkspaceWizard(Config(), lambda u: None, capture_factory=lambda s: cap, player=cap.play,
                          output_name=lambda: "HDMI")
    assert "Ausgabe: HDMI" in "\n".join(w.text() for w in wiz.findChildren(type(wiz.heading)))
    wiz.measure_btn.click()
    while wiz.runner.running and clock.now < 20:
        clock.now += 0.1
        wiz.runner.tick(clock.now)
    assert wiz.speakers is None and "nichts zu hören" in wiz.sweep_info.text()
    assert wiz.measure_btn.text() == "Wiederholen"


def test_placement_window_live_and_remeasure(app):
    clock = Clock(0.0)
    cap = SceneCapture(clock, P, sources=[(115.0, 5.0), (315.0, 5.0)], seconds=60.0)
    talk = plane_wave(0.003 * speech_like(SR * 2), P, 215.0, 25.0)
    cap.x[2 * SR:4 * SR, :7] += talk.astype(np.float32)
    cfg = Config(**CAL, speakers=[[110.0, 5.0], [320.0, 5.0]], speaker_levels_dbfs=[-60.0, -62.0])
    saved = []
    win = PlacementWindow(lambda: (cfg, None), saved.append, live=False, capture=cap, player=cap.play)
    for i in range(30):  # 2 s Ruhe, 2 s Sprache, 2 s Ruhe
        clock.now = 0.2 * (i + 1)
        win.tick(clock.now)
    assert "dBFS" in win.speech_label.text() and "dBFS" in win.noise_label.text()
    assert "-60 / -62" in win.echo_label.text() and "lauter als die Lautsprecher" in win.hints.text()
    assert win.plot.srp is not None and win.plot.beam == 215.0 and win.plot.patterns
    win._ask_measure()
    assert not win.confirm.isHidden()
    win.play_btn.click()
    while win.runner.running and clock.now < 30:
        clock.now += 0.1
        win.runner.tick(clock.now)
        win.tick(clock.now)
    assert win.pending is not None and not win.adopt_btn.isHidden()
    assert [angle_diff(d[0], a) <= 1.0 for d, a in zip(win.plot.speakers, (115.0, 315.0))] == [True, True]
    win.adopt_btn.click()
    assert angle_diff(saved[-1]["speakers"][0][0], 115.0) <= 1.0 and "noise_floor_dbfs" in saved[-1]
    win.reject()                                                    # Fenster schließen: Aufnahme endet
    assert cap.closed and not win.timer.isActive()


def test_placement_window_without_device(app):
    class Silent(SceneCapture):
        def total(self):
            return 0
    clock = Clock()
    win = PlacementWindow(lambda: (Config(), None), lambda u: None, live=False, capture=Silent(clock, P))
    win.tick(7.0)
    win.tick(10.0)
    assert "Keine Daten vom UMA-8" in win.status.text() and win.plot.talker is None
    win.close()


def test_polar_plot_paints_and_mirrors(app):
    plot = PolarPlot()
    plot.resize(420, 480)
    cfg = Config(**CAL, speakers=[[115.0, 5.0], [315.0, 5.0]], keyboard=[213.0, 3.0], talker_zone_deg=40.0,
                 null_weight_db=10.0, direction_mode="tracking")
    plot.set_scene(**scene_for(cfg, 230.0), srp=(np.arange(0, 360, 5.0), np.linspace(0, 1, 72)))
    assert plot.transform() == (55.0, False) and plot.beam == 230.0
    assert not plot.grab().isNull()
    plot.set_scene(speakers=[[315.0, 5.0], [115.0, 5.0]])        # links rechts gemessen: gespiegelt
    assert plot.transform() == (55.0, True)
    plot.set_scene(user_view=False)
    assert plot.transform() == (0.0, False) and not plot.grab().isNull()
    with pytest.raises(AttributeError):
        plot.set_scene(unknown=1)
    assert scene_for(Config(direction_mode="omni"), None)["beam"] is None


def test_speech_filter():
    assert speech_filter(Config()) is None
    f = speech_filter(Config(**CAL))
    assert f(230.0) and not f(100.0)                               # ohne Sprechzone ±45°
    f = speech_filter(Config(**CAL, talker_zone_deg=20.0, speakers=[[230.0, 5.0]]))
    assert f(220.0) and not f(240.0)
    assert profile_defaults()["talker_zone_deg"] == 180.0
