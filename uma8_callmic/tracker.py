"""Nachführung: schätzt bei Sprache die Richtung und stellt den Strahl nach."""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable

import numpy as np

from .doa import angle_diff, circular_mean

log = logging.getLogger(__name__)


class Tracker:
    def __init__(self, estimator, vad, apply: Callable[[float], None], initial_azimuth: float = 0.0,
                 center: int = 0, hysteresis: float = 15.0, window: int = 5, min_confidence: float = 0.05):
        self.estimator, self.vad, self.apply = estimator, vad, apply
        self.current = initial_azimuth
        self.center = center
        self.hysteresis, self.min_confidence = hysteresis, min_confidence
        self.history: deque[float] = deque(maxlen=window)

    def feed(self, block: np.ndarray) -> float | None:
        if not self.vad.is_speech(block[:, self.center]):
            return None
        r = self.estimator.estimate(block[:, :7])
        if r.confidence < self.min_confidence:
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
