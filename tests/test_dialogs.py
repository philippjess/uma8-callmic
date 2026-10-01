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
    assert dlg.t60.isEnabled() and dlg.strength.isEnabled() and dlg.beamformer.isEnabled()
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


def test_level_dbfs():
    assert abs(level_dbfs(np.full(100, 0.5)) - (-6.02)) < 0.01
