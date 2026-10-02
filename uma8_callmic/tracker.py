"""Nachführung: schätzt bei Sprache die Richtung und stellt den Strahl nach."""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .doa import angle_diff, circular_mean

log = logging.getLogger(__name__)

#: Schätzungen so nah (Azimut) an einem gemessenen Lautsprecher zählen nicht
SPEAKER_EXCLUSION_DEG = 20.0
#: Lautsprecher so nah an der kalibrierten Sprechrichtung werden nicht ausgeschlossen (sonst fände die
#: Nachführung den Sprecher nie); der Einmess-Assistent warnt dann
SPEAKER_TALKER_MIN_DEG = SPEAKER_EXCLUSION_DEG + 10.0


@dataclass(frozen=True)
class Zone:
    """Erlaubte Richtungen der Nachführung (Arbeitsplatz-Profil): ±half_width um center (≥ 180 = alle),
    ohne ±avoid_deg um jeden Azimut in `avoid` (Lautsprecher)."""
    center: float = 0.0
    half_width: float = 180.0
    avoid: tuple[float, ...] = ()
    avoid_deg: float = SPEAKER_EXCLUSION_DEG

    def accepts(self, azimuth: float) -> bool:
        if self.half_width < 180.0 and angle_diff(azimuth, self.center) > self.half_width:
            return False
        return all(angle_diff(azimuth, a) > self.avoid_deg for a in self.avoid)


def zone_for(cfg) -> Zone | None:
    """Zone aus dem Profil; None ohne Profil, dann führt die Nachführung genau wie bisher nach."""
    limited = cfg.calibrated and cfg.talker_zone_deg < 180.0
    avoid = tuple(float(az) for az, _ in cfg.speakers
                  if not cfg.calibrated or angle_diff(az, cfg.calibrated_azimuth) > SPEAKER_TALKER_MIN_DEG)
    if not limited and not avoid:
        return None
    return Zone(float(cfg.calibrated_azimuth), float(cfg.talker_zone_deg) if limited else 180.0, avoid)


class Tracker:
    def __init__(self, estimator, vad, apply: Callable[[float], None], initial_azimuth: float = 0.0,
                 center: int = 0, hysteresis: float = 15.0, window: int = 5, min_confidence: float = 0.05,
                 zone: Zone | None = None):
        self.estimator, self.vad, self.apply = estimator, vad, apply
        self.current = initial_azimuth
        self.center = center
        self.hysteresis, self.min_confidence = hysteresis, min_confidence
        self.history: deque[float] = deque(maxlen=window)
        #: darf aus einem anderen Thread ersetzt werden (Profil gespeichert)
        self.zone = zone

    def feed(self, block: np.ndarray) -> float | None:
        if not self.vad.is_speech(block[:, self.center]):
            return None
        r = self.estimator.estimate(block[:, :7])
        if r.confidence < self.min_confidence:
            return None
        zone = self.zone
        if zone is not None and not zone.accepts(r.azimuth):
            return None
        self.history.append(r.azimuth)
        if len(self.history) < 3:
            return None
        target = circular_mean(self.history)
        if angle_diff(target, self.current) <= self.hysteresis:
            return None
        self.current = target
        self.apply(target)
        return target


class TrackerThread(threading.Thread):
    def __init__(self, capture, tracker: Tracker, block_frames: int = 9600, interval: float = 0.2):
        super().__init__(daemon=True)
        self.capture, self.tracker = capture, tracker
        self.block_frames, self.interval = block_frames, interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            if not self.capture.alive:  # Quelle weg: den letzten Puffer nicht immer wieder auswerten
                continue
            block = self.capture.latest(self.block_frames)
            if block is None:
                continue
            try:
                self.tracker.feed(block)
            except Exception:  # Nachführung darf nie die Anwendung beenden
                log.exception("Nachführung fehlgeschlagen")

    def stop(self) -> None:
        self._stop_event.set()
