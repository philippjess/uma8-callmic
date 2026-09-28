"""Mehrkanal-Aufnahme über pw-record (Rohdaten auf stdout) in einen Ringpuffer."""
from __future__ import annotations

import subprocess
import threading

import numpy as np


class Capture:
    def __init__(self, target: str, channels: int, seconds: float = 12.0, sample_rate: int = 48000,
                 command: list[str] | None = None):
        self.channels = channels
        self.buf = np.zeros((int(seconds * sample_rate), channels), dtype=np.float32)
        self.written = 0
        self.lock = threading.Lock()
        cmd = command or ["pw-record", "--target", target, "--rate", str(sample_rate),
                          "--channels", str(channels), "--format", "f32", "--raw", "-"]
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

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
