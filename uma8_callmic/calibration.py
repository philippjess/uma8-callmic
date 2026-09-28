"""Auswertung einer Kalibrierung aus mehreren Richtungsschätzungen."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .doa import DoaResult, angle_diff, circular_mean


@dataclass
class CalibrationOutcome:
    ok: bool
    azimuth: float
    elevation: float
    message: str


def evaluate(results: list[DoaResult], min_blocks: int = 5, min_confidence: float = 0.05,
             max_spread: float = 20.0) -> CalibrationOutcome:
    if len(results) < min_blocks:
        return CalibrationOutcome(False, 0.0, 0.0,
                                  "Zu wenig Sprache erkannt – bitte näher oder lauter sprechen und wiederholen.")
    az = circular_mean([r.azimuth for r in results])
    el = float(np.mean([r.elevation for r in results]))
    spread = float(np.mean([angle_diff(r.azimuth, az) for r in results]))
    conf = float(np.median([r.confidence for r in results]))
    if conf < min_confidence or spread > max_spread:
        return CalibrationOutcome(False, az, el,
                                  f"Richtung nicht eindeutig (Streuung {spread:.0f}°) – bitte wiederholen.")
    return CalibrationOutcome(True, az, el,
                              f"Richtung {az:.0f}°, Höhe {el:.0f}° – eindeutig (Streuung {spread:.0f}°).")
