"""Mehrkanal-Aufnahme über pw-record (Rohdaten auf stdout) in einen Ringpuffer."""
from __future__ import annotations

import subprocess
import threading

import numpy as np


def record_command(target: str, channels: int, sample_rate: int = 48000, positions: tuple[str, ...] = (),
                   no_fallback: bool = False) -> list[str]:
    """pw-record-Aufruf. `positions` = Kanalpositionen der Quelle: ohne sie nimmt pw-record ein Standardlayout
    (8 Kanäle = 7.1) und PipeWire mischt abweichende Positionen um. `no_fallback`: fehlt das Ziel (Kette nicht
    geladen oder neu gestartet), endet die Aufnahme, statt auf das Standardmikrofon auszuweichen."""
    cmd = ["pw-record", "--target", target, "--rate", str(sample_rate), "--channels", str(channels)]
    if positions:
        cmd += ["--channel-map", ",".join(positions)]
    if no_fallback:
        cmd += ["-P", "{ node.dont-fallback = true }"]
    return cmd + ["--format", "f32", "--raw", "-"]


class Capture:
    def __init__(self, target: str, channels: int, seconds: float = 12.0, sample_rate: int = 48000,
                 command: list[str] | None = None, positions: tuple[str, ...] = (), no_fallback: bool = False):
        self.channels = channels
        self.buf = np.zeros((int(seconds * sample_rate), channels), dtype=np.float32)
        self.written = 0
        self.lock = threading.Lock()
        cmd = command or record_command(target, channels, sample_rate, positions, no_fallback)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self) -> None:
        frame_bytes = 4 * self.channels
        pending = b""
        while True:
            chunk = self.proc.stdout.read(frame_bytes * 1024)
            if not chunk:
                break
            data = pending + chunk
            usable = len(data) - len(data) % frame_bytes
            pending = data[usable:]
            frames = np.frombuffer(data[:usable], dtype=np.float32).reshape(-1, self.channels)
            with self.lock:
                idx = (self.written + np.arange(len(frames))) % len(self.buf)
                self.buf[idx] = frames
                self.written += len(frames)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def total(self) -> int:
        with self.lock:
            return self.written

    def latest(self, frames: int) -> np.ndarray | None:
        with self.lock:
            if self.written < frames or frames > len(self.buf):
                return None
            idx = (self.written - frames + np.arange(frames)) % len(self.buf)
            return self.buf[idx].copy()

    def span(self, start: int, end: int) -> np.ndarray | None:
        """Frames [start, end) gezählt ab Aufnahmebeginn; None, wenn noch nicht oder nicht mehr im Puffer."""
        with self.lock:
            if not 0 <= start <= end <= self.written or self.written - start > len(self.buf):
                return None
            return self.buf[np.arange(start, end) % len(self.buf)].copy()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def open_capture(target: tuple[str, int, tuple[str, ...]], seconds: float) -> Capture:
    """Aufnahme eines pw-record-Ziels (pwctl.raw_target/tracking_target), ohne Ausweichen aufs Standardmikrofon."""
    name, channels, positions = target
    return Capture(name, channels, seconds=seconds, positions=positions, no_fallback=True)
