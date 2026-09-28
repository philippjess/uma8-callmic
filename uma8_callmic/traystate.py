"""Icon-Zustand und Tooltip-Text des Tray-Icons (ohne Qt, testbar)."""
from __future__ import annotations

from .config import Config
from .pwctl import Status

MODE_TEXT = {"calibrated": "Richtung: kalibriert", "manual": "Richtung: manuell",
             "tracking": "Richtung: automatisch", "omni": "Richtung: alle"}


def icon_state(status: Status, active: bool) -> str:
    if status.problem:
        return "error"
    return "active" if active else "inactive"


def tooltip(status: Status, cfg: Config, tracked: float | None = None, warnings=()) -> str:
    lines = ["UMA-8 Call Mic"]
    if status.problem:
        lines.append("⚠ " + status.problem)
    else:
        lines.append("Aktiv: Beamforming + Rauschunterdrückung" if cfg.active else "Deaktiviert: Rohsignal")
        mode = MODE_TEXT[cfg.direction_mode]
        if cfg.direction_mode == "calibrated":
            mode += f" ({cfg.calibrated_azimuth:.0f}°)" if cfg.calibrated else " (noch nicht kalibriert)"
        elif cfg.direction_mode == "manual":
            mode += f" ({cfg.manual_azimuth:.0f}°)"
        elif cfg.direction_mode == "tracking" and tracked is not None:
            mode += f" ({tracked:.0f}°)"
        lines.append(mode)
    lines += [f"Hinweis: {w}" for w in warnings]
    return "\n".join(lines)
