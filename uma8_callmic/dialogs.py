"""Optionen-, Kalibrierungs- und Kanalzuordnungsdialog."""
from __future__ import annotations

from typing import Callable

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QProgressBar, QPushButton, QSlider, QVBoxLayout, QWidget)

from . import constants as K
from .array import ArrayGeometry
from .calibration import evaluate
from .capture import Capture
from .config import Config
from .doa import SrpPhat, VoiceDetector
from .geometry import check as check_geometry
from .pwctl import raw_source

DIRECTIONS = [("Kalibriert", "calibrated"), ("Manuell", "manual"),
              ("Automatisch nachführen", "tracking"), ("Alle Richtungen", "omni")]
BLOCK = 9600  # 0,2 s


def level_dbfs(block: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) + 1e-12
    return 20.0 * np.log10(rms)


def _slider(lo: int, hi: int, value: float) -> QSlider:
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(lo, hi)
    s.setValue(int(round(value)))
    return s


def _with_label(widget: QWidget, label: QLabel) -> QWidget:
    box = QWidget()
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    row.addWidget(widget, 1)
    row.addWidget(label)
    return box


class LevelMeter(QProgressBar):
    def __init__(self, lo: int = -60, hi: int = 0):
        super().__init__()
        self.setRange(lo, hi)
        self.setValue(lo)
        self.setFormat("%v dBFS")

    def show_level(self, db: float) -> None:
        self.setValue(int(max(self.minimum(), min(self.maximum(), db))))


class OptionsDialog(QDialog):
    def __init__(self, cfg: Config, on_change: Callable[[dict], None], on_check_geometry: Callable[[], None],
                 meter: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("UMA-8 Call Mic – Optionen")
        self.on_change = on_change
        form = QFormLayout()
        self.direction = QComboBox()
        for text, key in DIRECTIONS:
            self.direction.addItem(text, key)
        self.direction.setCurrentIndex([k for _, k in DIRECTIONS].index(cfg.direction_mode))
        form.addRow("Richtung", self.direction)
        cal = (f"Kalibriert: {cfg.calibrated_azimuth:.0f}°, Höhe {cfg.calibrated_elevation:.0f}°"
               if cfg.calibrated else "Noch nicht kalibriert (Tray-Menü → Kalibrieren…)")
        form.addRow("", QLabel(cal))
        self.manual = _slider(0, 359, cfg.manual_azimuth)
        self.manual_label = QLabel(f"{cfg.manual_azimuth:.0f}°")
        form.addRow("Winkel (manuell)", _with_label(self.manual, self.manual_label))
        self.dereverb = QCheckBox("Hallunterdrückung")
        self.dereverb.setChecked(cfg.dereverb)
        form.addRow("", self.dereverb)
        self.strength = _slider(0, 100, cfg.dereverb_strength * 100)
        form.addRow("Stärke", self.strength)
        self.t60 = QDoubleSpinBox()
        self.t60.setRange(0.1, 1.5)
        self.t60.setSingleStep(0.05)
        self.t60.setSuffix(" s")
        self.t60.setValue(cfg.dereverb_t60)
        form.addRow("Nachhallzeit des Raums", self.t60)
        self.noise = _slider(0, 100, cfg.noise_reduction_db)
        self.noise_label = QLabel(f"{cfg.noise_reduction_db:.0f} dB")
        form.addRow("Rauschunterdrückung", _with_label(self.noise, self.noise_label))
        self.gain = _slider(0, 60, cfg.gain_db)
        self.gain_label = QLabel(f"{cfg.gain_db:.0f} dB")
        form.addRow("Verstärkung", _with_label(self.gain, self.gain_label))
        self.meter = LevelMeter()
        form.addRow("Pegel (Ausgang)", self.meter)
        self.autostart = QCheckBox("Beim Login starten")
        self.autostart.setChecked(cfg.autostart)
        form.addRow("", self.autostart)
        geo = QPushButton("Kanalzuordnung prüfen…")
        geo.clicked.connect(on_check_geometry)
        form.addRow("", geo)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)
        for signal in (self.direction.currentIndexChanged, self.manual.valueChanged, self.dereverb.toggled,
                       self.strength.valueChanged, self.t60.valueChanged, self.noise.valueChanged,
                       self.gain.valueChanged, self.autostart.toggled):
            signal.connect(self._changed)
        self._update_enabled()
        self.capture = None
        if meter:
            self.capture = Capture(K.SOURCE_NODE, 1, seconds=1.0)
            self.timer = QTimer(self)
            self.timer.timeout.connect(self._meter_tick)
            self.timer.start(100)
        self.finished.connect(self._cleanup)

    def _update_enabled(self) -> None:
        self.manual.setEnabled(self.direction.currentData() == "manual")
        self.strength.setEnabled(self.dereverb.isChecked())
        self.t60.setEnabled(self.dereverb.isChecked())

    def _changed(self, *_):
        updates = {
            "direction_mode": self.direction.currentData(),
            "manual_azimuth": float(self.manual.value()),
            "dereverb": self.dereverb.isChecked(),
            "dereverb_strength": self.strength.value() / 100.0,
            "dereverb_t60": round(self.t60.value(), 2),
            "noise_reduction_db": float(self.noise.value()),
            "gain_db": float(self.gain.value()),
            "autostart": self.autostart.isChecked(),
        }
        self.manual_label.setText(f"{updates['manual_azimuth']:.0f}°")
        self.noise_label.setText(f"{updates['noise_reduction_db']:.0f} dB")
        self.gain_label.setText(f"{updates['gain_db']:.0f} dB")
        self._update_enabled()
        self.on_change(updates)

    def _meter_tick(self) -> None:
        block = self.capture.latest(4800) if self.capture else None
        if block is not None:
            self.meter.show_level(level_dbfs(block))

    def _cleanup(self, *_):
        if self.capture:
            self.capture.close()
            self.capture = None


class _RecordingDialog(QDialog):
    """Gemeinsame Aufnahme-Mechanik: Start, Fortschritt, Pegel, Übernehmen/Abbrechen."""

    def __init__(self, title: str, intro: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.info = QLabel(intro)
        self.info.setWordWrap(True)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.meter = LevelMeter(-100, -30)
        self.start_btn = QPushButton("Start")
        self.accept_btn = QPushButton("Übernehmen")
        self.accept_btn.setEnabled(False)
        cancel = QPushButton("Abbrechen")
        self.start_btn.clicked.connect(self.start)
        self.accept_btn.clicked.connect(self._accept)
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.start_btn)
        row.addStretch(1)
        row.addWidget(self.accept_btn)
        row.addWidget(cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(self.info)
        layout.addWidget(self.progress)
        layout.addWidget(self.meter)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.capture = None
        self.ticks = 0
        self.finished.connect(self._cleanup)

    def _begin(self, seconds: float) -> None:
        self._cleanup()
        self.ticks = 0
        self.accept_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.capture = Capture(raw_source(), 8, seconds=seconds)
        self.timer.start(200)

    def _finish(self) -> None:
        self.timer.stop()
        self._cleanup()
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Wiederholen")

    def _no_data(self) -> bool:
        if self.ticks >= 10 and self.capture.total() == 0:
            self._finish()
            self.info.setText("Keine Daten vom UMA-8 – angeschlossen und mit Raw-Firmware?")
            return True
        return False

    def _cleanup(self, *_):
        self.timer.stop()
        if self.capture:
            self.capture.close()
            self.capture = None

    def start(self):
        raise NotImplementedError

    def _tick(self):
        raise NotImplementedError

    def _accept(self):
        raise NotImplementedError


class CalibrationDialog(_RecordingDialog):
    QUIET_TICKS, SPEAK_TICKS = 10, 25  # 2 s still, 5 s sprechen

    def __init__(self, geometry: ArrayGeometry, on_accept: Callable[[float, float], None], parent=None):
        super().__init__("UMA-8 Call Mic – Kalibrieren",
                         "Setz dich hin wie beim Telefonieren. Nach dem Start: 2 Sekunden still sein, "
                         "dann 5 Sekunden normal sprechen.", parent)
        self.geometry, self.on_accept = geometry, on_accept
        self.result: tuple[float, float] | None = None

    def start(self):
        self.estimator = SrpPhat(self.geometry.positions())
        self.vad = VoiceDetector()
        self.results = []
        self.result = None
        self.info.setText("Bitte still sein …")
        self._begin(3.0)

    def _tick(self):
        self.ticks += 1
        total = self.QUIET_TICKS + self.SPEAK_TICKS
        self.progress.setValue(int(100 * min(self.ticks, total) / total))
        if self._no_data():
            return
        block = self.capture.latest(BLOCK)
        if block is None:
            return
        center = block[:, self.geometry.center]
        self.meter.show_level(level_dbfs(center))
        speech = self.vad.is_speech(center)
        if self.ticks == self.QUIET_TICKS:
            self.info.setText("Jetzt normal sprechen …")
        if self.ticks > self.QUIET_TICKS and speech:
            self.results.append(self.estimator.estimate(block[:, :7]))
        if self.ticks >= total:
            self._finish()
            outcome = evaluate(self.results)
            self.info.setText(outcome.message)
            if outcome.ok:
                self.result = (outcome.azimuth, outcome.elevation)
                self.accept_btn.setEnabled(True)

    def _accept(self):
        if self.result:
            self.on_accept(*self.result)
        self.accept()


class GeometryDialog(_RecordingDialog):
    TICKS = 50  # 10 s

    def __init__(self, default: ArrayGeometry, on_done: Callable[[bool, ArrayGeometry | None], None], parent=None):
        super().__init__("UMA-8 Call Mic – Kanalzuordnung",
                         "Prüft, welcher Kanal welches Mikrofon ist. Bitte 10 Sekunden still sein; "
                         "normales Raumgeräusch oder ein Lüfter sind gut.", parent)
        self.default, self.on_done = default, on_done
        self.detected: ArrayGeometry | None = None

    def start(self):
        self.detected = None
        self.info.setText("Messe Raumgeräusch …")
        self._begin(12.0)

    def _tick(self):
        self.ticks += 1
        self.progress.setValue(int(100 * min(self.ticks, self.TICKS) / self.TICKS))
        if self._no_data():
            return
        recent = self.capture.latest(BLOCK)
        if recent is not None:
            self.meter.show_level(level_dbfs(recent[:, self.default.center]))
        if self.ticks < self.TICKS:
            return
        block = self.capture.latest(self.TICKS * BLOCK)
        self._finish()
        if block is None:
            self.info.setText("Zu wenig Daten – bitte wiederholen.")
            return
        res = check_geometry(block, self.default)
        self.info.setText(f"{res.message} (Kontrast {res.contrast:.2f})")
        if res.detected is not None and not res.matches_default:
            self.detected = res.detected
            self.accept_btn.setText("Erkannte Zuordnung übernehmen")
            self.accept_btn.setEnabled(True)
        self.on_done(True, None)

    def _accept(self):
        if self.detected is not None:
            self.on_done(True, self.detected)
        self.accept()
