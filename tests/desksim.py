"""Simulierter Arbeitsplatz für die Tests des Arbeitsplatz-Profils: Rohaufnahme im Takt einer Testuhr, Wiedergabe
der Messdateien als ebene Wellen aus festen Lautsprecherrichtungen. Spielt und nimmt nie wirklich auf."""
import wave

import numpy as np

from sim import plane_wave
from uma8_callmic import workspace as ws

SR = 48000


class Clock:
    def __init__(self, now: float = 0.0):
        self.now = now


class FakeProc:
    """Wie subprocess.Popen: läuft bis `end` (Testuhr), dann Rückgabewert `code`."""

    def __init__(self, clock: Clock, end: float, code: int = 0):
        self.clock, self.end, self.code = clock, end, code
        self.killed = False

    def poll(self):
        return self.code if self.killed or self.clock.now >= self.end else None

    def kill(self):
        self.killed = True


def read_wav(path) -> np.ndarray:
    with wave.open(str(path)) as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(-1, w.getnchannels())
    return data.astype(float) / 32767


class SceneCapture:
    """Ersatz für capture.Capture (total, span, latest, alive, close). `play` ist der Player der Messung: liest
    die WAV-Datei und legt den hörbaren Kanal als ebene Welle aus `sources[kanal]` (None = stumm) ab jetzt +
    `latency` in die Aufnahme, mit Pegel `level_db` (RMS am Mikrofon, dBFS)."""

    def __init__(self, clock: Clock, positions: np.ndarray, sources=((100.0, 5.0), (260.0, 5.0)),
                 level_db: float = -50.0, noise_db: float = -75.0, seconds: float = 40.0, latency: float = 0.15,
                 play_code: int = 0, seed: int = 0):
        self.clock, self.positions, self.sources = clock, positions, list(sources)
        self.level_db, self.latency, self.play_code = level_db, latency, play_code
        rng = np.random.default_rng(seed)
        self.x = (rng.standard_normal((int(seconds * SR), 8)) * 10 ** (noise_db / 20)).astype(np.float32)
        self.x[:, 7] = 0.0
        self.alive, self.closed, self.played = True, False, []

    def total(self) -> int:
        return min(int(self.clock.now * SR), len(self.x))

    def span(self, start: int, end: int):
        return self.x[start:end].copy() if 0 <= start <= end <= self.total() else None

    def latest(self, frames: int):
        t = self.total()
        return self.x[t - frames:t].copy() if t >= frames else None

    def close(self):
        self.closed = True

    def add(self, signal: np.ndarray, az: float, el: float, start_s: float) -> None:
        s = int(start_s * SR)
        y = plane_wave(signal, self.positions, az, el)[: len(self.x) - s]
        self.x[s:s + len(y), :7] += y.astype(np.float32)

    def add_typing(self, az: float, el: float, start_s: float, seconds: float, level_db: float = -40.0,
                   rate: float = 6.0, seed: int = 1) -> None:
        """Tastenanschläge: kurze, abklingende Rauschimpulse (5 ms) im Abstand von ≈ 1/rate s."""
        rng = np.random.default_rng(seed)
        t = start_s
        while t < start_s + seconds:
            n = int(0.02 * SR)
            click = rng.standard_normal(n) * np.exp(-np.arange(n) / (0.005 * SR)) * 10 ** (level_db / 20)
            self.add(click, az, el, t)
            t += rng.uniform(0.5, 1.5) / rate

    def play(self, path):
        data = read_wav(path)
        ch = int(np.argmax(np.abs(data).sum(axis=0)))
        self.played.append(ch)
        if self.sources[ch] is not None:
            gain = 10 ** ((self.level_db - ws.BURST_RMS_DBFS) / 20)
            self.add(data[:, ch] * gain, *self.sources[ch], self.clock.now + self.latency)
        return FakeProc(self.clock, self.clock.now + len(data) / SR + 0.05, self.play_code)


def run_sweep(sweep, clock: Clock, until: float = 20.0, dt: float = 0.1, tick=None) -> None:
    """Testuhr vorstellen und die Messung takten, bis sie fertig ist."""
    tick = tick or sweep.step
    while clock.now < until and not sweep.done:
        clock.now = round(clock.now + dt, 6)
        tick(clock.now)
