"""Arbeitsplatz einmessen (Assistent) und Platzierungsansicht; die Messlogik liegt in workspace.py."""
from __future__ import annotations

import copy
import math
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPalette, QPen, QPolygonF
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QSlider, QStackedWidget, QVBoxLayout, QWidget)

from . import beampattern, pwctl
from . import workspace as ws
from .capture import open_capture
from .config import Config, profile_defaults
from .dialogs import BLOCK, NULLS_TEXT, NULLS_TOOLTIP, CalibrationDialog
from .doa import angle_diff
from .params import NULL_WEIGHT_DB, all_params
from .tracker import SPEAKER_EXCLUSION_DEG, Zone, zone_for

#: Farben (Kategorien 1–3 und Flächen der Referenzpalette, für helle und dunkle Oberfläche geprüft)
_THEMES = {
    False: {"surface": "#fcfcfb", "ink": "#0b0b0b", "muted": "#52514e", "grid": "#d9d8d3",
            "s1": "#2a78d6", "s2": "#eb6834", "s3": "#1baf7a"},
    True: {"surface": "#1a1a19", "ink": "#ffffff", "muted": "#c3c2b7", "grid": "#3d3d3a",
           "s1": "#3987e5", "s2": "#d95926", "s3": "#199e70"},
}
#: Bewegungsbereich der Nachführung (± Grad); der letzte Wert heißt „unbeschränkt“
ZONES = (15, 20, 25, 30, 40, 50, 60, 75, 90, 180)
PATTERN_FREQS = (1000.0, 3000.0)
NO_DATA = "Keine Daten vom UMA-8 – angeschlossen und mit Raw-Firmware?"
VOLUME_WARNING = ("Gleich spielt jeder Lautsprecher 2,5 Sekunden Rauschen, erst links, dann rechts. Stell die "
                  "Lautstärke so ein wie in einem Anruf – nicht lauter – und sei während der Messung still.")


def _raw_capture(seconds: float):
    return open_capture(pwctl.raw_target(), seconds)


def _output_name() -> str:
    objs = pwctl.try_dump()
    return (pwctl.default_sink_description(objs) if objs else None) or "unbekannt"


def zone_text(deg: float) -> str:
    return "unbeschränkt" if deg >= 180 else f"±{deg:.0f}°"


# --- Polardiagramm --------------------------------------------------------------

class PolarPlot(QWidget):
    """Richtungen im Bezugssystem des Arrays (0° rechts, gegen den Uhrzeigersinn) oder „aus deiner Sicht“:
    gedreht, sodass der Sprecher unten liegt, und gespiegelt, falls der linke Lautsprecher sonst rechts läge.
    Strahlmuster als Pegel (Außenring 0 dB, Ringe −10/−20 dB, Mitte −30 dB), Schallkarte normiert."""

    RANGE_DB = 30.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 400)
        self.talker: tuple[float, float] | None = None
        self.zone = 180.0
        self.speakers: list[list[float]] = []
        self.keyboard: list[float] | None = None
        self.beam: float | None = None
        #: Beschriftung → (Azimute, dB)
        self.patterns: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        #: Elevation, für die die Strahlmuster gelten
        self.pattern_el: float | None = None
        #: (Azimute, 0 … 1)
        self.srp: tuple[np.ndarray, np.ndarray] | None = None
        self.activity = 1.0
        self.user_view = True

    def set_scene(self, **kw) -> None:
        for key, value in kw.items():
            if not hasattr(self, key):
                raise AttributeError(key)
            setattr(self, key, value)
        self.update()

    def transform(self) -> tuple[float, bool]:
        """Drehung (°) und Spiegelung der Darstellung."""
        if not (self.user_view and self.talker):
            return 0.0, False
        rot = 270.0 - self.talker[0]
        mirror = False
        if len(self.speakers) == 2:  # zuerst gemessen = links
            left, right = (math.cos(math.radians(s[0] + rot)) for s in self.speakers)
            mirror = left > right
        return rot, mirror

    def paintEvent(self, _event) -> None:
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        t = {k: QColor(v) for k, v in _THEMES[dark].items()}
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), t["surface"])
        font = QFont(self.font())
        fh = p.fontMetrics().height()
        legend_h = 3 * fh + 8
        R = max(40.0, min(self.width(), self.height() - legend_h - fh) / 2 - 2.2 * fh)
        c = QPointF(self.width() / 2, fh + 1.6 * fh + R)
        rot, mirror = self.transform()

        def pt(az: float, r: float) -> QPointF:
            a = math.radians(az + rot)
            x, y = r * math.cos(a), r * math.sin(a)
            return QPointF(c.x() + (-x if mirror else x), c.y() - y)

        def ring(db: float) -> float:
            return R * min(max(1 + db / self.RANGE_DB, 0.0), 1.0)

        # Titel, Gitter, Azimut-Beschriftung (Array-Grad)
        p.setPen(t["ink"])
        p.drawText(QPointF(8, fh), "Aus deiner Sicht (du unten)" if rot or mirror else "Array-Sicht (0° rechts)")
        p.setPen(QPen(t["grid"], 1))
        for db in (0.0, -10.0, -20.0):
            p.drawEllipse(c, ring(db), ring(db))
        small = QFont(font)
        small.setPointSizeF(max(6.0, font.pointSizeF() * 0.8))
        p.setFont(small)
        for az in range(0, 360, 30):
            p.setPen(QPen(t["grid"], 1))
            p.drawLine(c, pt(az, R))
            q = pt(az, R + 0.9 * fh)
            p.setPen(t["muted"])
            p.drawText(QRectF(q.x() - 20, q.y() - fh / 2, 40, fh), Qt.AlignmentFlag.AlignCenter, f"{az}°")
        for db in (-10.0, -20.0):  # fest oben rechts, unabhängig von der Drehung
            a = math.radians(70.0)
            p.drawText(QPointF(c.x() + ring(db) * math.cos(a) + 3, c.y() - ring(db) * math.sin(a) - 2), f"{db:.0f} dB")
        p.setFont(font)

        def wedge(center: float, half: float, color: QColor, outline: QPen | None = None) -> None:
            steps = max(8, int(half))
            poly = QPolygonF([c] + [pt(center + d, R) for d in np.linspace(-half, half, steps)])
            p.setPen(outline or Qt.PenStyle.NoPen)
            p.setBrush(QBrush(color))
            p.drawPolygon(poly)
            p.setBrush(Qt.BrushStyle.NoBrush)

        if self.talker and self.zone < 180:
            fill = QColor(t["ink"])
            fill.setAlphaF(0.07)
            wedge(self.talker[0], self.zone, fill, QPen(t["muted"], 1, Qt.PenStyle.DashLine))
        for az, _ in self.speakers:  # aus der Nachführung ausgeschlossen
            fill = QColor(t["muted"])
            fill.setAlphaF(0.12)
            wedge(az, SPEAKER_EXCLUSION_DEG, fill)

        if self.srp is not None:
            az, v = self.srp
            color = QColor(t["s3"])
            color.setAlphaF(0.12 + 0.3 * self.activity)
            p.setPen(QPen(t["s3"], 1.5))
            p.setBrush(QBrush(color))
            p.drawPolygon(QPolygonF([pt(a, R * (0.06 + 0.94 * x)) for a, x in zip(az, v)]))
            p.setBrush(Qt.BrushStyle.NoBrush)

        colors = [t["s1"], t["s2"]]
        for (label, (az, db)), color in zip(self.patterns.items(), colors):
            p.setPen(QPen(color, 2))
            p.drawPolygon(QPolygonF([pt(a, ring(d)) for a, d in zip(az, db)]))

        if self.beam is not None:
            p.setPen(QPen(t["ink"], 2))
            tip = pt(self.beam, R)
            p.drawLine(c, tip)
            for side in (-6.0, 6.0):
                p.drawLine(tip, pt(self.beam + side, R - 14))

        def label(at: QPointF, text: str) -> None:
            w = p.fontMetrics().horizontalAdvance(text) + 6
            box = QRectF(at.x() - w / 2, at.y() + 7, w, fh)
            p.fillRect(box, t["surface"])
            p.setPen(t["ink"])
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

        p.setBrush(QBrush(t["ink"]))
        p.setPen(QPen(t["surface"], 2))
        if self.keyboard:
            q = pt(self.keyboard[0], R * 0.62)
            p.drawRect(QRectF(q.x() - 9, q.y() - 4, 18, 8))
            label(q, "Tastatur")
        names = ["L", "R"] if len(self.speakers) == 2 else ["Lautsprecher"]
        for (az, _), name in zip(self.speakers, names):
            q = pt(az, R * 0.82)
            p.setBrush(QBrush(t["ink"]))
            p.setPen(QPen(t["surface"], 2))
            p.drawRect(QRectF(q.x() - 6, q.y() - 6, 12, 12))
            label(q, name)
        if self.talker:
            q = pt(self.talker[0], R * 0.82)
            p.setBrush(QBrush(t["ink"]))
            p.setPen(QPen(t["surface"], 2))
            p.drawEllipse(q, 7, 7)
            label(q, "du")
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Legende
        y = self.height() - legend_h + fh * 0.8
        x = 8.0
        entries = [(colors[i], label_) for i, label_ in enumerate(self.patterns)]
        if self.srp is not None:
            entries.append((t["s3"], "Schall jetzt"))
        for color, text in entries:
            p.setPen(QPen(color, 3))
            p.drawLine(QPointF(x, y - fh / 3), QPointF(x + 18, y - fh / 3))
            p.setPen(t["ink"])
            p.drawText(QPointF(x + 24, y), text)
            x += 34 + p.fontMetrics().horizontalAdvance(text)
        p.setPen(t["muted"])
        p.drawText(QPointF(8, y + fh), "● du   ■ Lautsprecher   ▬ Tastatur   → Strahl   grau: Sprechzone, "
                                       "Lautsprecher-Ausschluss")
        el = "" if self.pattern_el is None else f" für Schall unter {self.pattern_el:.0f}° Höhe"
        p.drawText(QPointF(8, y + 2 * fh), f"Strahl{el}: Außenring 0 dB, Mitte −{self.RANGE_DB:.0f} dB")
        p.end()


def scene_for(cfg: Config, tracked: float | None, freqs=PATTERN_FREQS) -> dict:
    """Szene des Polardiagramms aus den Einstellungen: Marker und Strahlmuster wie im Plugin."""
    params = all_params(cfg, tracked)
    el = float(np.mean([e for _, e in cfg.speakers])) if cfg.speakers else None
    az, pats = beampattern.pattern(params, cfg.geometry().positions(), freqs, el_deg=el, center=cfg.center_channel)
    omni = cfg.direction_mode == "omni"
    return {
        "talker": (cfg.calibrated_azimuth, cfg.calibrated_elevation) if cfg.calibrated else None,
        "zone": cfg.talker_zone_deg, "speakers": [list(s) for s in cfg.speakers],
        "keyboard": list(cfg.keyboard) or None,
        "patterns": {f"Strahl {f / 1000:g} kHz": (az, db) for f, db in pats.items()},
        "pattern_el": params["beam:Elevation (deg)"] if el is None else el,
        "beam": None if omni else params["beam:Azimuth (deg)"],
    }


# --- Lautsprechermessung im GUI-Takt --------------------------------------------

class SweepRunner:
    """Führt ws.SpeakerSweep per QTimer aus: Testdateien in einem Temp-Verzeichnis, Aufnahme geliehen (`capture`)
    oder eigen (`capture_factory`), Ergebnis an `on_done(sweep)`."""

    def __init__(self, owner: QWidget, cfg: Config, on_status: Callable[[str, float], None],
                 on_done: Callable[[ws.SpeakerSweep], None], capture=None, capture_factory=_raw_capture,
                 player=ws.start_player):
        self.cfg, self.on_status, self.on_done = cfg, on_status, on_done
        self.capture, self.capture_factory, self.player = capture, capture_factory, player
        self.sweep: ws.SpeakerSweep | None = None
        self.own = None
        self.tmp: Path | None = None
        self.timer = QTimer(owner)
        self.timer.timeout.connect(lambda: self.tick(time.monotonic()))

    @property
    def running(self) -> bool:
        return self.sweep is not None

    def start(self) -> None:
        self.stop()
        self.tmp = Path(tempfile.mkdtemp(prefix="uma8-callmic-"))
        files = ws.write_bursts(self.tmp)
        capture = self.capture
        if capture is None:
            capture = self.own = self.capture_factory(12.0)
        self.sweep = ws.SpeakerSweep(capture, files, self.cfg.geometry().positions(), self.cfg.center_channel,
                                     self.player)
        self.timer.start(100)

    def tick(self, now: float) -> None:
        sweep = self.sweep
        if sweep is None:
            return
        sweep.step(now)
        self.on_status(sweep.status(), sweep.progress())
        if sweep.done:
            self.stop()
            self.on_done(sweep)

    def stop(self) -> None:
        self.timer.stop()
        if self.sweep is not None:
            self.sweep.cancel()
            self.sweep = None
        if self.own is not None:
            self.own.close()
            self.own = None
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None


def speaker_summary(m: ws.SpeakerMeasurement) -> str:
    lines = [r.message for r in m.speakers] + [f"Grundrauschen {m.noise_dbfs:.0f} dBFS"] + m.notes
    return "\n".join(lines)


# --- Assistent ----------------------------------------------------------------

class WorkspaceWizard(QDialog):
    """Lautsprecher → Sprechrichtung (Kalibrierung) → Tastatur (optional) → Ergebnis; „Speichern“ übergibt die
    geänderten Einstellungen an `on_save`. Abbrechen ändert nichts."""

    TITLES = ("Lautsprecher", "Sprechrichtung", "Tastatur (optional)", "Ergebnis")

    def __init__(self, cfg: Config, on_save: Callable[[dict], None], parent=None, capture_factory=_raw_capture,
                 player=ws.start_player, output_name: Callable[[], str] = _output_name):
        super().__init__(parent)
        self.setWindowTitle("UMA-8 Call Mic – Arbeitsplatz einmessen")
        self.cfg, self.on_save, self.capture_factory = copy.deepcopy(cfg), on_save, capture_factory
        self.speakers: ws.SpeakerMeasurement | None = None
        self.talker: tuple[float, float] | None = None
        self.speech_level: float | None = None
        self.keyboard: ws.Direction | None = None
        self.calibration: CalibrationDialog | None = None
        self.typing_capture = None
        self.typing_start: float | None = None
        self.typing_total: int | None = None
        self.runner = SweepRunner(self, self.cfg, self._sweep_status, self._sweep_done, capture_factory=capture_factory,
                                  player=player)
        self.typing_timer = QTimer(self)
        self.typing_timer.timeout.connect(lambda: self._typing_tick(time.monotonic()))

        self.heading = QLabel()
        bold = self.heading.font()
        bold.setBold(True)
        self.heading.setFont(bold)
        self.pages = QStackedWidget()
        for build in (self._speaker_page, self._talker_page, self._keyboard_page, self._result_page):
            self.pages.addWidget(build(output_name))
        self.back = QPushButton("Zurück")
        self.next = QPushButton("Weiter")
        cancel = QPushButton("Abbrechen")
        self.back.clicked.connect(lambda: self._go(self.pages.currentIndex() - 1))
        self.next.clicked.connect(self._next)
        cancel.clicked.connect(self.reject)
        nav = QHBoxLayout()
        nav.addWidget(self.back)
        nav.addStretch(1)
        nav.addWidget(self.next)
        nav.addWidget(cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(self.heading)
        layout.addWidget(self.pages, 1)
        layout.addLayout(nav)
        self.finished.connect(self._cleanup)
        self._go(0)

    # --- Seiten ---

    @staticmethod
    def _text(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        return label

    def _speaker_page(self, output_name) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(self._text(VOLUME_WARNING + " Mit Kopfhörern diesen Schritt überspringen."))
        v.addWidget(self._text(f"Ausgabe: {output_name()}"))
        self.measure_btn = QPushButton("Lautsprecher messen")
        self.measure_btn.clicked.connect(self._measure)
        self.sweep_progress = QProgressBar()
        self.sweep_progress.setRange(0, 100)
        self.sweep_info = self._text(self._current_speakers())
        v.addWidget(self.measure_btn)
        v.addWidget(self.sweep_progress)
        v.addWidget(self.sweep_info)
        v.addStretch(1)
        if self.cfg.has_profile():
            clear = QPushButton("Profil löschen")
            clear.setToolTip("Lautsprecher, Tastatur, Sprechzone und Nullstellen zurücksetzen (Kalibrierung bleibt).")
            clear.clicked.connect(self._clear_profile)
            v.addWidget(clear, 0, Qt.AlignmentFlag.AlignLeft)
        return page

    def _current_speakers(self) -> str:
        if not self.cfg.speakers:
            return "Noch keine Lautsprecher eingemessen. Überspringen behält den bisherigen Stand."
        dirs = ", ".join(f"{az:.0f}°" for az, _ in self.cfg.speakers)
        return f"Bisher eingemessen: {dirs}. Überspringen behält sie."

    def _talker_page(self, _output_name) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(self._text("Setz dich hin wie beim Telefonieren. Die Kalibrierung misst deine Sprechrichtung: "
                               "2 Sekunden still sein, dann 5 Sekunden normal sprechen."))
        self.cal_btn = QPushButton("Kalibrieren…")
        self.cal_btn.clicked.connect(self._calibrate)
        self.cal_info = self._text("")
        v.addWidget(self.cal_btn, 0, Qt.AlignmentFlag.AlignLeft)
        v.addWidget(self.cal_info)
        v.addStretch(1)
        return page

    def _keyboard_page(self, _output_name) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(self._text("Wo liegt die Tastatur? Nach dem Start 5 Sekunden normal tippen. Nur zur Anzeige: Die "
                               "Tastatur liegt meist in deiner Sprechrichtung und lässt sich nicht ausblenden, das "
                               "Tippen dämpft die Rauschunterdrückung. „Weiter“ überspringt."))
        self.type_btn = QPushButton("5 s tippen")
        self.type_btn.clicked.connect(self._start_typing)
        self.type_progress = QProgressBar()
        self.type_progress.setRange(0, 100)
        self.type_info = self._text("")
        v.addWidget(self.type_btn, 0, Qt.AlignmentFlag.AlignLeft)
        v.addWidget(self.type_progress)
        v.addWidget(self.type_info)
        v.addStretch(1)
        return page

    def _result_page(self, _output_name) -> QWidget:
        page = QWidget()
        h = QHBoxLayout(page)
        left = QVBoxLayout()
        self.plot = PolarPlot()
        self.user_view = QCheckBox("Aus deiner Sicht (du unten)")
        self.user_view.setChecked(True)
        self.user_view.toggled.connect(lambda on: self.plot.set_scene(user_view=on))
        left.addWidget(self.plot, 1)
        left.addWidget(self.user_view)
        right = QVBoxLayout()
        self.summary = self._text("")
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        right.addWidget(self.summary)
        form = QFormLayout()
        self.zone = QSlider(Qt.Orientation.Horizontal)
        self.zone.setRange(0, len(ZONES) - 1)
        self.zone.setValue(min(range(len(ZONES)), key=lambda i: abs(ZONES[i] - self.cfg.talker_zone_deg)))
        self.zone_label = QLabel()
        self.zone.valueChanged.connect(self._refresh_result)
        row = QHBoxLayout()
        row.addWidget(self.zone, 1)
        row.addWidget(self.zone_label)
        box = QWidget()
        box.setLayout(row)
        box.setToolTip("Wie weit du dich beim Sprechen bewegst. Die automatische Nachführung folgt nur innerhalb "
                       "dieses Bereichs um die kalibrierte Richtung; Richtungen der Lautsprecher (±20°) ignoriert "
                       "sie immer.")
        form.addRow("Bewegungsbereich", box)
        form.addRow("", self._text("(wirkt bei „Automatisch nachführen“)"))
        self.nulls = QCheckBox(NULLS_TEXT)
        self.nulls.setToolTip(NULLS_TOOLTIP)
        self.nulls.setChecked(self.cfg.null_weight_db > 0)
        self.nulls.toggled.connect(self._refresh_result)
        form.addRow("", self.nulls)
        right.addLayout(form)
        self.hints = self._text("")
        right.addWidget(self.hints)
        right.addStretch(1)
        h.addLayout(left, 3)
        h.addLayout(right, 2)
        return page

    # --- Ablauf ---

    def _go(self, index: int) -> None:
        index = max(0, min(index, self.pages.count() - 1))
        self.pages.setCurrentIndex(index)
        self.heading.setText(f"Schritt {index + 1} von {self.pages.count()}: {self.TITLES[index]}")
        self.back.setEnabled(index > 0)
        self.next.setText("Speichern" if index == self.pages.count() - 1 else "Weiter")
        if index == 1:
            self._show_calibration()
        if index == self.pages.count() - 1:
            self._refresh_result()
        self._update_buttons()

    def _update_buttons(self) -> None:
        busy = self.runner.running or self.typing_capture is not None
        index = self.pages.currentIndex()
        calibrated = self.talker is not None or self.cfg.calibrated
        self.next.setEnabled(not busy and (index != 1 or calibrated))
        self.back.setEnabled(not busy and index > 0)
        self.measure_btn.setEnabled(not busy)
        self.type_btn.setEnabled(not busy)

    def _next(self) -> None:
        if self.pages.currentIndex() == self.pages.count() - 1:
            self.on_save(self.updates())
            self.accept()
        else:
            self._go(self.pages.currentIndex() + 1)

    def _clear_profile(self) -> None:
        answer = QMessageBox.question(self, "Profil löschen", "Arbeitsplatz-Profil löschen? Lautsprecher, Tastatur, "
                                      "Sprechzone und Nullstellen werden zurückgesetzt.")
        if answer == QMessageBox.StandardButton.Yes:
            self.on_save(profile_defaults())
            self.accept()

    def _measure(self) -> None:
        self.speakers = None
        self.sweep_info.setText("")
        self.runner.start()
        self._update_buttons()

    def _sweep_status(self, text: str, progress: float) -> None:
        self.sweep_info.setText(text)
        self.sweep_progress.setValue(int(100 * progress))

    def _sweep_done(self, sweep: ws.SpeakerSweep) -> None:
        if sweep.result is not None and sweep.result.ok:
            self.speakers = sweep.result
            self.sweep_info.setText(speaker_summary(sweep.result))
        else:
            self.sweep_info.setText(sweep.error or "Messung fehlgeschlagen.")
        self.measure_btn.setText("Wiederholen")
        self._update_buttons()

    def _show_calibration(self) -> None:
        if self.talker is not None:
            text = f"Neu kalibriert: {self.talker[0]:.0f}°, Höhe {self.talker[1]:.0f}°."
            if self.speech_level is not None:
                text += f" Sprachpegel {self.speech_level:.0f} dBFS."
        elif self.cfg.calibrated:
            text = (f"Bisherige Kalibrierung: {self.cfg.calibrated_azimuth:.0f}°, Höhe "
                    f"{self.cfg.calibrated_elevation:.0f}°. Neu kalibrieren, wenn Mikrofon oder Sitzplatz "
                    "sich geändert haben.")
        else:
            text = "Noch nicht kalibriert."
        self.cal_info.setText(text)

    def _calibrate(self) -> None:
        dlg = CalibrationDialog(self.cfg.geometry(), self._calibrated, parent=self)
        self.calibration = dlg
        dlg.open()

    def _calibrated(self, azimuth: float, elevation: float) -> None:
        self.talker = (azimuth, elevation)
        levels = self.calibration.speech_levels if self.calibration is not None else []
        self.speech_level = float(np.median(levels)) if levels else None
        self._show_calibration()
        self._update_buttons()

    def _start_typing(self) -> None:
        self.keyboard = None
        self.typing_capture = self.capture_factory(ws.TYPING_S + 3.0)
        self.typing_start = self.typing_total = None
        self.type_info.setText("Jetzt tippen …")
        self.typing_timer.start(200)
        self._update_buttons()

    def _typing_tick(self, now: float) -> None:
        cap = self.typing_capture
        if cap is None:
            return
        if self.typing_start is None:
            self.typing_start = now
        elapsed = now - self.typing_start
        total = cap.total()
        if self.typing_total is None and total > 0:
            self.typing_total = total  # Aufnahme läuft: ab hier zählen die 5 s
            self.typing_start, elapsed = now, 0.0
        self.type_progress.setValue(int(100 * min(1.0, elapsed / ws.TYPING_S)) if self.typing_total else 0)
        if self.typing_total is None:
            if elapsed > 2.5:
                self._stop_typing(NO_DATA)
            return
        if elapsed < ws.TYPING_S:
            return
        block = cap.span(self.typing_total, total)
        if block is None or len(block) < ws.SR:
            self._stop_typing("Zu wenig Daten – bitte wiederholen.")
            return
        result = ws.analyse_keyboard(block, self.cfg.geometry().positions(), self.cfg.center_channel)
        self.keyboard = result if result.ok else None
        self._stop_typing(result.message)

    def _stop_typing(self, message: str) -> None:
        self.typing_timer.stop()
        if self.typing_capture is not None:
            self.typing_capture.close()
            self.typing_capture = None
        self.type_info.setText(message)
        self.type_btn.setText("Wiederholen")
        self._update_buttons()

    # --- Ergebnis ---

    def updates(self) -> dict:
        """Geänderte Einstellungen; nicht gemessene Teile behalten ihren bisherigen Wert."""
        u: dict = {}
        if self.speakers is not None:
            u["speakers"] = [[round(az, 1), round(el, 1)] for az, el in self.speakers.directions]
            u["speaker_levels_dbfs"] = [round(x, 1) for x in self.speakers.levels_dbfs]
            u["noise_floor_dbfs"] = round(self.speakers.noise_dbfs, 1)
        if self.talker is not None:
            u.update(calibrated=True, calibrated_azimuth=round(self.talker[0] % 360.0, 1),
                     calibrated_elevation=round(self.talker[1], 1))
        if self.speech_level is not None:
            u["speech_level_dbfs"] = round(self.speech_level, 1)
        if self.keyboard is not None:
            u["keyboard"] = [round(self.keyboard.azimuth, 1), round(self.keyboard.elevation, 1)]
        u["talker_zone_deg"] = float(ZONES[self.zone.value()])
        has_speakers = u.get("speakers", self.cfg.speakers)
        weight = self.cfg.null_weight_db if self.cfg.null_weight_db > 0 else NULL_WEIGHT_DB
        u["null_weight_db"] = weight if self.nulls.isChecked() and has_speakers else 0.0
        return u

    def effective(self) -> Config:
        cfg = copy.deepcopy(self.cfg)
        for key, value in self.updates().items():
            setattr(cfg, key, value)
        return cfg

    def _refresh_result(self, *_) -> None:
        cfg = self.effective()
        self.zone_label.setText(zone_text(cfg.talker_zone_deg))
        self.nulls.setEnabled(bool(cfg.speakers))
        self.plot.set_scene(**scene_for(cfg, None))
        lines = []
        if cfg.calibrated:
            lines.append(f"Du: {cfg.calibrated_azimuth:.0f}°, Höhe {cfg.calibrated_elevation:.0f}°")
        names = ("Lautsprecher links", "Lautsprecher rechts") if len(cfg.speakers) == 2 else ("Lautsprecher",)
        levels = cfg.speaker_levels_dbfs or [None] * len(cfg.speakers)
        for (az, el), name, level in zip(cfg.speakers, names, levels):
            rel = f", {angle_diff(az, cfg.calibrated_azimuth):.0f}° neben dir" if cfg.calibrated else ""
            lvl = f", Pegel {level:.0f} dBFS" if level is not None else ""
            lines.append(f"{name}: {az:.0f}°, Höhe {el:.0f}°{rel}{lvl}")
        if not cfg.speakers:
            lines.append("Keine Lautsprecher eingemessen.")
        if cfg.keyboard:
            lines.append(f"Tastatur: {cfg.keyboard[0]:.0f}°, Höhe {cfg.keyboard[1]:.0f}°")
        if cfg.noise_floor_dbfs is not None:
            lines.append(f"Grundrauschen: {cfg.noise_floor_dbfs:.0f} dBFS")
        talker = cfg.calibrated_azimuth if cfg.calibrated else None
        lines += ws.profile_notes(talker, cfg.speakers, cfg.keyboard or None)
        if self.speakers is not None:
            lines += self.speakers.notes
        self.summary.setText("\n".join(lines))
        echo = max(cfg.speaker_levels_dbfs) if cfg.speaker_levels_dbfs else None
        self.hints.setText(hint_text(cfg.speech_level_dbfs, cfg.noise_floor_dbfs, echo))

    def _cleanup(self, *_) -> None:
        self.runner.stop()
        if self.typing_capture is not None:
            self._stop_typing("")
        if self.calibration is not None:
            self.calibration.close()


def hint_text(speech: float | None, noise: float | None, echo: float | None) -> str:
    return "\n".join(("✓ " if h.good else "⚠ ") + h.text for h in ws.placement_hints(speech, noise, echo))


# --- Platzierungsansicht --------------------------------------------------------

class PlacementWindow(QDialog):
    """Live: Schallkarte (SRP-PHAT über den Azimut, ≈ 5 Hz) aus einer eigenen Rohaufnahme, die nur läuft, solange
    das Fenster offen ist; Strahlmuster, Marker und Pegel mit Platzierungshinweisen. `state()` liefert die
    aktuellen Einstellungen und die nachgeführte Richtung, `on_save` übernimmt eine neue Lautsprechermessung."""

    def __init__(self, state: Callable[[], tuple[Config, float | None]], on_save: Callable[[dict], None],
                 parent=None, live: bool = True, capture=None, player=ws.start_player):
        super().__init__(parent)
        self.setWindowTitle("UMA-8 Call Mic – Platzierung")
        self.state, self.on_save = state, on_save
        cfg, _ = state()
        self.capture = _raw_capture(12.0) if live and capture is None else capture
        self.analysis = ws.LiveAnalysis(cfg.geometry().positions(), cfg.center_channel)
        self.geometry_key = (cfg.center_channel, tuple(cfg.ring), cfg.ring_offset_deg, cfg.radius_mm)
        self.pattern_key = None
        self.scene: dict = {}
        self.pending: ws.SpeakerMeasurement | None = None
        self.opened: float | None = None
        self.last_total = 0
        self.runner = SweepRunner(self, cfg, self._sweep_status, self._sweep_done, capture=self.capture,
                                  player=player)

        self.plot = PolarPlot()
        self.user_view = QCheckBox("Aus deiner Sicht (du unten)")
        self.user_view.setChecked(True)
        self.user_view.toggled.connect(lambda on: self.plot.set_scene(user_view=on))
        left = QVBoxLayout()
        left.addWidget(self.plot, 1)
        left.addWidget(self.user_view)

        form = QFormLayout()
        self.speech_label, self.noise_label, self.echo_label = QLabel("—"), QLabel("—"), QLabel("—")
        form.addRow("Sprache", self.speech_label)
        form.addRow("Grundrauschen", self.noise_label)
        form.addRow("Lautsprecher", self.echo_label)
        self.hints = WorkspaceWizard._text("")
        self.status = WorkspaceWizard._text("")
        self.measure_btn = QPushButton("Lautsprecher neu messen…")
        self.measure_btn.clicked.connect(self._ask_measure)
        self.confirm = QWidget()
        c = QVBoxLayout(self.confirm)
        c.setContentsMargins(0, 0, 0, 0)
        c.addWidget(WorkspaceWizard._text(VOLUME_WARNING))
        row = QHBoxLayout()
        self.play_btn = QPushButton("Jetzt abspielen")
        self.play_btn.clicked.connect(self._measure)
        no = QPushButton("Abbrechen")
        no.clicked.connect(lambda: self.confirm.setVisible(False))
        row.addWidget(self.play_btn)
        row.addWidget(no)
        row.addStretch(1)
        c.addLayout(row)
        self.confirm.setVisible(False)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.result = WorkspaceWizard._text("")
        self.adopt_btn = QPushButton("Messung übernehmen")
        self.adopt_btn.setToolTip("Neue Lautsprecherrichtungen und Pegel ins Profil (und in die Nullstellen).")
        self.adopt_btn.setVisible(False)
        self.adopt_btn.clicked.connect(self._adopt)
        right = QVBoxLayout()
        right.addLayout(form)
        right.addWidget(self.hints)
        right.addSpacing(8)
        right.addWidget(self.measure_btn, 0, Qt.AlignmentFlag.AlignLeft)
        right.addWidget(self.confirm)
        right.addWidget(self.progress)
        right.addWidget(self.result)
        right.addWidget(self.adopt_btn, 0, Qt.AlignmentFlag.AlignLeft)
        right.addStretch(1)
        right.addWidget(self.status)
        layout = QHBoxLayout(self)
        layout.addLayout(left, 3)
        layout.addLayout(right, 2)
        self.timer = QTimer(self)
        self.timer.timeout.connect(lambda: self.tick(time.monotonic()))
        self.timer.start(200)
        self.finished.connect(self._cleanup)
        self.redraw()

    def tick(self, now: float) -> None:
        cfg, _ = self.state()
        key = (cfg.center_channel, tuple(cfg.ring), cfg.ring_offset_deg, cfg.radius_mm)
        if key != self.geometry_key:
            self.geometry_key = key
            self.analysis = ws.LiveAnalysis(cfg.geometry().positions(), cfg.center_channel)
        self.analysis.accept = speech_filter(cfg)
        self._feed(now)
        self.redraw()

    def redraw(self) -> None:
        cfg, tracked = self.state()
        pkey = repr((all_params(cfg, tracked), cfg.speakers, cfg.keyboard, cfg.talker_zone_deg, cfg.calibrated))
        if pkey != self.pattern_key:
            self.pattern_key = pkey
            self.scene = scene_for(cfg, tracked)
        srp = self.analysis.map_norm()
        scene = dict(self.scene)
        if self.pending is not None:
            scene["speakers"] = [list(d) for d in self.pending.directions]
        self.plot.set_scene(**scene, srp=None if srp is None else (self.analysis.azimuths, srp),
                            activity=self.analysis.activity())
        self._levels(cfg)

    def _feed(self, now: float) -> None:
        cap = self.capture
        if cap is None:
            return
        if self.opened is None:
            self.opened = now
        total = cap.total()
        if total == self.last_total:
            if now - self.opened > 2.5 and (total == 0 or not cap.alive):
                self.status.setText(NO_DATA)
            return
        self.last_total = total
        block = cap.latest(BLOCK)
        if block is not None:
            self.status.setText("")
            self.analysis.feed(block)

    def _levels(self, cfg: Config) -> None:
        speech, noise = self.analysis.speech_dbfs(), self.analysis.noise_dbfs()
        levels = self.pending.levels_dbfs if self.pending is not None else cfg.speaker_levels_dbfs
        echo = max(levels) if levels else None
        self.speech_label.setText("—  (sprich ein paar Sätze)" if speech is None else f"{speech:.0f} dBFS")
        if noise is None and cfg.noise_floor_dbfs is not None:
            self.noise_label.setText(f"{cfg.noise_floor_dbfs:.0f} dBFS (beim Einmessen)")
        else:
            self.noise_label.setText("—" if noise is None else f"{noise:.0f} dBFS")
        noise = cfg.noise_floor_dbfs if noise is None else noise
        self.echo_label.setText("nicht gemessen" if echo is None else
                                " / ".join(f"{x:.0f}" for x in levels) + " dBFS (bei der Lautstärke der Messung)")
        self.hints.setText(hint_text(speech, noise, echo))

    def _ask_measure(self) -> None:
        self.confirm.setVisible(True)

    def _measure(self) -> None:
        self.confirm.setVisible(False)
        if self.capture is None:
            self.result.setText(NO_DATA)
            return
        self.pending = None
        self.adopt_btn.setVisible(False)
        self.progress.setVisible(True)
        self.measure_btn.setEnabled(False)
        self.runner.start()

    def _sweep_status(self, text: str, progress: float) -> None:
        self.result.setText(text)
        self.progress.setValue(int(100 * progress))

    def _sweep_done(self, sweep: ws.SpeakerSweep) -> None:
        self.progress.setVisible(False)
        self.measure_btn.setEnabled(True)
        if sweep.result is not None and sweep.result.ok:
            self.pending = sweep.result
            self.result.setText(speaker_summary(sweep.result) + "\n(neu, noch nicht übernommen)")
            self.adopt_btn.setVisible(True)
            self.pattern_key = None
        else:
            self.result.setText(sweep.error or "Messung fehlgeschlagen.")

    def _adopt(self) -> None:
        m = self.pending
        if m is None:
            return
        self.on_save({"speakers": [[round(az, 1), round(el, 1)] for az, el in m.directions],
                      "speaker_levels_dbfs": [round(x, 1) for x in m.levels_dbfs],
                      "noise_floor_dbfs": round(m.noise_dbfs, 1)})
        self.pending = None
        self.pattern_key = None
        self.adopt_btn.setVisible(False)
        self.result.setText("Übernommen.")

    def _cleanup(self, *_) -> None:
        self.timer.stop()
        self.runner.stop()
        if self.capture is not None:
            self.capture.close()
            self.capture = None


def speech_filter(cfg: Config) -> Callable[[float], bool] | None:
    """Welche Richtungen als eigene Sprache zählen: Sprechzone und nicht bei den Lautsprechern; ohne Zone ±45°
    um die kalibrierte Richtung, ohne Kalibrierung alles außer den Lautsprechern."""
    zone = zone_for(cfg)
    if cfg.calibrated and (zone is None or zone.half_width >= 180):
        zone = Zone(cfg.calibrated_azimuth, 45.0, zone.avoid if zone else ())
    return None if zone is None else zone.accepts
