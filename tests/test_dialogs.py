import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from uma8_callmic.array import UMA8
from uma8_callmic.config import Config
from uma8_callmic.dialogs import CalibrationDialog, GeometryDialog, OptionsDialog, level_dbfs


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_options_dialog_reports_changes(app):
    changes = []
    dlg = OptionsDialog(Config(), changes.append, lambda: None, meter=False)
    dlg.gain.setValue(42)
    dlg.direction.setCurrentIndex(2)
    assert changes[-1]["gain_db"] == 42.0
    assert changes[-1]["direction_mode"] == "tracking"
    assert not dlg.manual.isEnabled()
    dlg.direction.setCurrentIndex(1)
    assert dlg.manual.isEnabled()
    assert changes[-1]["echo_cancel"] is True
    dlg.echo.setChecked(False)
    assert changes[-1]["echo_cancel"] is False
    dlg.close()


def test_options_dialog_dereverb_controls(app):
    changes = []
    dlg = OptionsDialog(Config(), changes.append, lambda: None, meter=False)
    assert dlg.late.isChecked() and not dlg.dereverb.isChecked()  # Standard: nur das Abklingmodell
    assert dlg.t60.isEnabled() and dlg.strength.isEnabled() and dlg.beamformer.isEnabled()
    dlg.dereverb.setChecked(True)
    assert changes[-1]["dereverb"] is True
    dlg.late.setChecked(False)
    assert changes[-1]["late_reverb"] is False and not dlg.t60.isEnabled() and dlg.strength.isEnabled()
    dlg.dereverb.setChecked(False)
    assert changes[-1]["dereverb"] is False and not dlg.strength.isEnabled()
    dlg.beamformer.setCurrentIndex(1)
    assert changes[-1]["beamformer"] == "delay_and_sum"
    dlg.direction.setCurrentIndex(3)
    assert not dlg.beamformer.isEnabled()
    dlg.close()


def test_other_dialogs_construct(app):
    CalibrationDialog(UMA8, lambda az, el: None).close()
    GeometryDialog(UMA8, lambda ran, adopt: None).close()


class _LaggingCapture:
    """Liefert wie pw-record: Daten erst kurz nach dem Start, danach 0,2 s je Tick."""

    def __init__(self, lag_frames: int, stall_after: int | None = None):
        self.written, self.lag, self.stall_after, self.ticks = -lag_frames, lag_frames, stall_after, 0
        self.rng = np.random.default_rng(0)

    def advance(self):
        self.ticks += 1
        if self.stall_after is None or self.ticks <= self.stall_after:
            self.written += 9600

    def total(self):
        return max(self.written, 0)

    def latest(self, frames):
        return self.rng.normal(0, 1e-4, (frames, 8)).astype(np.float32) if self.total() >= frames else None

    def close(self):
        pass


def _run_geometry(app, cap, max_ticks=80):
    done = []
    dlg = GeometryDialog(UMA8, lambda ran, adopt: done.append(ran))
    dlg.capture, dlg.ticks = cap, 0
    for _ in range(max_ticks):
        if dlg.capture is None:
            break
        cap.advance()
        dlg._tick()
    text = dlg.info.text()
    dlg.close()
    return done, text, cap.ticks


def test_geometry_check_waits_for_full_ten_seconds(app):
    """Nach 50 Ticks fehlt pw-record noch der Anlauf (gemessen 1792 Frames); die Prüfung wartet darauf."""
    done, text, ticks = _run_geometry(app, _LaggingCapture(lag_frames=1792))
    assert done == [True] and "Zu wenig Daten" not in text and ticks == 51


def test_geometry_check_gives_up_on_stalled_capture(app):
    done, text, ticks = _run_geometry(app, _LaggingCapture(lag_frames=0, stall_after=30))
    assert done == [] and "Zu wenig Daten" in text and ticks == GeometryDialog.TICKS + GeometryDialog.GRACE_TICKS


def test_level_dbfs():
    assert abs(level_dbfs(np.full(100, 0.5)) - (-6.02)) < 0.01


def test_options_nulls_only_with_measured_speakers(app):
    changes = []
    dlg = OptionsDialog(Config(), changes.append, lambda: None, meter=False)
    assert not dlg.nulls.isEnabled() and not dlg.nulls.isChecked()
    dlg.gain.setValue(40)
    assert "null_weight_db" not in changes[-1]                  # nur, wenn hier umgeschaltet
    dlg.close()
    dlg = OptionsDialog(Config(speakers=[[100.0, 5.0]], null_weight_db=15.0), changes.append, lambda: None,
                        meter=False)
    assert dlg.nulls.isEnabled() and dlg.nulls.isChecked()
    dlg.nulls.setChecked(False)
    assert changes[-1] == {"null_weight_db": 0.0}
    dlg.nulls.setChecked(True)
    assert changes[-1] == {"null_weight_db": 15.0}              # eigenes Gewicht bleibt erhalten
    dlg.beamformer.setCurrentIndex(1)
    assert not dlg.nulls.isEnabled()                            # nur superdirektiv
    dlg.close()
    dlg = OptionsDialog(Config(speakers=[[100.0, 5.0]]), changes.append, lambda: None, meter=False)
    dlg.nulls.setChecked(True)
    assert changes[-1]["null_weight_db"] == 10.0                # empfohlener Wert
    dlg.close()


def test_options_follow_profile_saved_elsewhere(app):
    """Assistent speichert bei offenem Optionen-Dialog: Der Dialog zeigt das neue Profil und überschreibt die
    Nullstellen nicht, wenn danach ein anderer Regler bewegt wird."""
    changes = []
    cfg = Config()
    dlg = OptionsDialog(cfg, changes.append, lambda: None, meter=False)
    cfg.speakers, cfg.null_weight_db = [[100.0, 5.0], [260.0, 5.0]], 12.0
    cfg.calibrated, cfg.calibrated_azimuth = True, 180.0
    dlg.sync_profile(cfg)
    assert changes == []                                        # Anzeigen meldet nichts
    assert dlg.nulls.isEnabled() and dlg.nulls.isChecked() and "180°" in dlg.cal_label.text()
    dlg.gain.setValue(33)
    assert "null_weight_db" not in changes[-1]
    dlg.nulls.setChecked(False)
    dlg.nulls.setChecked(True)
    assert changes[-1] == {"null_weight_db": 12.0}
    cfg.speakers, cfg.null_weight_db = [], 0.0                  # Profil gelöscht
    dlg.sync_profile(cfg)
    assert not dlg.nulls.isEnabled() and not dlg.nulls.isChecked() and changes[-1] == {"null_weight_db": 12.0}
    dlg.close()


def test_calibration_collects_speech_levels(app):
    dlg = CalibrationDialog(UMA8, lambda az, el: None)
    assert dlg.speech_levels == []
    dlg.close()
