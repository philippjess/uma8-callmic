"""KDE-Tray-Anwendung: Zustand anzeigen, umschalten, Dialoge öffnen, nachführen."""
from __future__ import annotations

import logging

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import chainconf, pwctl
from . import constants as K
from .array import ArrayGeometry
from .capture import Capture
from .config import load, save
from .dialogs import CalibrationDialog, GeometryDialog, OptionsDialog
from .doa import SrpPhat, VoiceDetector
from .params import all_params, steering_params
from .tracker import Tracker, TrackerThread
from .traystate import icon_state, tooltip

log = logging.getLogger(__name__)


class TrayApp:
    def __init__(self, app):
        self.app = app
        res = load(K.CONFIG_FILE)
        self.cfg, self.cfg_broken, self.warnings = res.config, res.broken, list(res.warnings)
        self.tracked: float | None = None
        self.tracker_thread: TrackerThread | None = None
        self.tracker_capture: Capture | None = None
        self.chain_node: int | None = None
        self.dialogs: dict[str, object] = {}
        self.icons = {name: QIcon(str(K.ICON_DIR / f"{name}.svg")) for name in ("active", "inactive", "error")}

        self.tray = QSystemTrayIcon(self.icons["inactive"])
        menu = QMenu()
        self.act_active = QAction("Aktiv", menu)
        self.act_active.setCheckable(True)
        self.act_active.setChecked(self.cfg.active)
        self.act_active.toggled.connect(self.set_active)
        menu.addAction(self.act_active)
        menu.addSeparator()
        menu.addAction("Kalibrieren…", self.open_calibration)
        menu.addAction("Optionen…", self.open_options)
        menu.addSeparator()
        menu.addAction("Beenden", app.quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        chainconf.write(self.cfg)
        self.set_autostart(self.cfg.autostart)
        if pwctl.ensure_service_enabled():  # Erststart ohne install.sh, z. B. nach dem RPM
            self.tray.showMessage("UMA-8 Call Mic", "Filterkette eingerichtet. „UMA-8 Call Mic“ jetzt in den "
                                  "Audio-Einstellungen als Mikrofon wählen.")
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()
        self.update_tracking()
        if not self.cfg.geometry_checked:
            QTimer.singleShot(1500, self.open_geometry)
        elif not self.cfg.calibrated:
            self.tray.showMessage("UMA-8 Call Mic", "Bitte einmal kalibrieren: Rechtsklick → Kalibrieren…")

    # --- Zustand ---------------------------------------------------------------

    def refresh(self) -> None:
        st = pwctl.status()
        try:
            node = pwctl.find_node(pwctl.dump(), K.CAPTURE_NODE) if st.chain else None
        except Exception:  # pw-dump hängt/fehlt: Zustand beim nächsten Durchlauf erneut prüfen
            log.warning("pw-dump fehlgeschlagen", exc_info=True)
            node = None
        if node is not None and node != self.chain_node:
            self.chain_node = node
            self.apply_all()  # Kette (neu) gestartet: Live-Werte an Einstellungen angleichen
        elif node is None:
            self.chain_node = None
        self.tray.setIcon(self.icons[icon_state(st, self.cfg.active)])
        self.tray.setToolTip(tooltip(st, self.cfg, self.tracked, self.warnings))

    def _set(self, params: dict[str, float]) -> None:
        if self.chain_node is None:
            return
        try:
            pwctl.set_params(self.chain_node, params)
        except (RuntimeError, OSError) as e:
            log.warning("%s", e)

    def apply_all(self) -> None:
        self._set(all_params(self.cfg, self.tracked))

    def save_config(self) -> None:
        save(self.cfg, K.CONFIG_FILE, broken=self.cfg_broken)
        self.cfg_broken = False
        chainconf.write(self.cfg)

    # --- Aktionen --------------------------------------------------------------

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.act_active.toggle()

    def set_active(self, checked: bool) -> None:
        self.cfg.active = checked
        self.save_config()
        if self.chain_node is not None:
            try:
                pwctl.fade_mix(self.chain_node, checked)
            except (RuntimeError, OSError) as e:
                log.warning("%s", e)
        self.refresh()

    def apply_options(self, updates: dict) -> None:
        autostart_before = self.cfg.autostart
        for key, value in updates.items():
            setattr(self.cfg, key, value)
        self.save_config()
        self.apply_all()
        if self.cfg.autostart != autostart_before:
            self.set_autostart(self.cfg.autostart)
        self.update_tracking()
        self.refresh()

    def set_autostart(self, enabled: bool) -> None:
        """Autostart-Datei angleichen: legt sie auch an, wenn sie fehlt (RPM ohne install.sh)."""
        try:
            pwctl.sync_autostart(enabled)
        except OSError as e:
            log.warning("Autostart nicht angepasst: %s", e)

    def calibrated(self, azimuth: float, elevation: float) -> None:
        self.cfg.calibrated = True
        self.cfg.calibrated_azimuth = round(azimuth, 1)
        self.cfg.calibrated_elevation = round(elevation, 1)
        self.save_config()
        self._set(steering_params(self.cfg, self.tracked))
        self.refresh()

    def geometry_done(self, ran: bool, adopt: ArrayGeometry | None) -> None:
        if ran:
            self.cfg.geometry_checked = True
        if adopt is not None:
            self.cfg.center_channel, self.cfg.ring = adopt.center, list(adopt.ring)
        self.save_config()
        self.apply_all()
        if ran and not self.cfg.calibrated:
            self.tray.showMessage("UMA-8 Call Mic", "Jetzt bitte einmal kalibrieren: Rechtsklick → Kalibrieren…")

    def _show(self, key: str, factory) -> None:
        dlg = self.dialogs.get(key)
        if dlg is None or not dlg.isVisible():
            dlg = factory()
            self.dialogs[key] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def open_options(self) -> None:
        self._show("options", lambda: OptionsDialog(self.cfg, self.apply_options, self.open_geometry))

    def open_calibration(self) -> None:
        self._show("calibration", lambda: CalibrationDialog(self.cfg.geometry(), self.calibrated))

    def open_geometry(self) -> None:
        self._show("geometry", lambda: GeometryDialog(self.cfg.geometry(), self.geometry_done))

    # --- Nachführung -----------------------------------------------------------

    def _apply_tracked(self, azimuth: float) -> None:
        self.tracked = azimuth
        self._set(steering_params(self.cfg, azimuth))

    def update_tracking(self) -> None:
        want = self.cfg.direction_mode == "tracking"
        if want and self.tracker_thread is None:
            self.tracker_capture = Capture(pwctl.raw_source(), 8, seconds=3.0)
            tracker = Tracker(SrpPhat(self.cfg.geometry().positions()), VoiceDetector(), self._apply_tracked,
                              initial_azimuth=self.cfg.calibrated_azimuth, center=self.cfg.center_channel)
            self.tracker_thread = TrackerThread(self.tracker_capture, tracker)
            self.tracker_thread.start()
        elif not want and self.tracker_thread is not None:
            self._stop_tracking()

    def _stop_tracking(self) -> None:
        if self.tracker_thread:
            self.tracker_thread.stop()
            self.tracker_thread = None
        if self.tracker_capture:
            self.tracker_capture.close()
            self.tracker_capture = None
        self.tracked = None

    def shutdown(self) -> None:
        self._stop_tracking()
        for dlg in self.dialogs.values():
            dlg.close()
