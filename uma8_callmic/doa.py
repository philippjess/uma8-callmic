"""Richtungsschätzung (SRP-PHAT), Sprachaktivität und Winkelhilfen."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

C_SOUND = 343.0


def angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def circular_mean(angles) -> float:
    r = np.radians(np.asarray(angles, dtype=float))
    return float(np.degrees(np.arctan2(np.sin(r).mean(), np.cos(r).mean())) % 360.0)


@dataclass
class DoaResult:
    azimuth: float
    elevation: float
    confidence: float  # Haupt- minus Nebenmaximum (außerhalb ±30°)
    peak: float        # normierte SRP am Maximum (−1 … 1)


class SrpPhat:
    def __init__(self, positions: np.ndarray, sample_rate: int = 48000, nfft: int = 1024,
                 fmin: float = 300.0, fmax: float = 6000.0, az_step: float = 5.0,
                 elevations=(0.0, 15.0, 30.0, 45.0)):
        self.nfft = nfft
        freqs = np.fft.rfftfreq(nfft, 1 / sample_rate)
        self.bins = np.where((freqs >= fmin) & (freqs <= fmax))[0]
        self.w = 2 * np.pi * freqs[self.bins]
        self.positions = np.asarray(positions, float)
        m = positions.shape[0]
        self.pairs = [(i, j) for i in range(m) for j in range(i + 1, m)]
        az_grid, el_grid = np.meshgrid(np.arange(0.0, 360.0, az_step), np.asarray(elevations, float), indexing="ij")
        self.grid_az, self.grid_el = az_grid.ravel(), el_grid.ravel()
        self.steer = self._steering(self.grid_az, self.grid_el)            # (G, P, K)
        self.window = np.hanning(nfft)

    def _steering(self, az_deg: np.ndarray, el_deg: np.ndarray) -> np.ndarray:
        a, e = np.radians(az_deg), np.radians(el_deg)
        u = np.stack([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)], axis=1)
        lead = u @ self.positions.T / C_SOUND                               # (G, M)
        dl = np.stack([lead[:, i] - lead[:, j] for i, j in self.pairs], 1)  # (G, P)
        # X_i X_j* = |S|² e^{+jω(lead_i − lead_j)} → Ausrichten mit e^{−jω·dl}
        return np.exp(-1j * dl[:, :, None] * self.w[None, None, :])

    def spectra(self, block: np.ndarray) -> np.ndarray:
        """(F, K, M): gefensterte Spektren je Frame (Hop nfft/2) in den Bins des Bands."""
        hop = self.nfft // 2
        frames = np.stack([block[s:s + self.nfft] for s in range(0, len(block) - self.nfft + 1, hop)])
        return np.fft.rfft(frames * self.window[None, :, None], axis=1)[:, self.bins, :]

    def cross_spectra(self, block: np.ndarray, select: np.ndarray | None = None) -> np.ndarray:
        """PHAT-normierte Kreuzspektren (P, K), gemittelt über alle Frames oder nur die in `select` (bool je Frame)."""
        spec = self.spectra(block)
        if select is not None:
            spec = spec[np.asarray(select, bool)]
        out = np.empty((len(self.pairs), len(self.bins)), dtype=complex)
        for p, (i, j) in enumerate(self.pairs):
            c = spec[:, :, i] * np.conj(spec[:, :, j])
            out[p] = np.mean(c / (np.abs(c) + 1e-12), axis=0)
        return out

    def srp(self, cs: np.ndarray) -> np.ndarray:
        """Normierte SRP (−1 … 1) je Gitterrichtung."""
        return np.real(np.einsum("gpk,pk->g", self.steer, cs)) / cs.size

    def srp_at(self, cs: np.ndarray, az_deg, el_deg) -> np.ndarray:
        """Normierte SRP für beliebige Richtungen (Verfeinerung um ein Gittermaximum)."""
        steer = self._steering(np.atleast_1d(np.asarray(az_deg, float)), np.atleast_1d(np.asarray(el_deg, float)))
        return np.real(np.einsum("gpk,pk->g", steer, cs)) / cs.size

    def estimate_cs(self, cs: np.ndarray) -> DoaResult:
        srp = self.srp(cs)
        best = int(np.argmax(srp))
        far = angle_diff_array(self.grid_az, self.grid_az[best]) > 30.0
        second = float(np.max(srp[far])) if far.any() else -1.0
        return DoaResult(float(self.grid_az[best]), float(self.grid_el[best]),
                         float(srp[best] - second), float(srp[best]))

    def estimate(self, block: np.ndarray) -> DoaResult:
        return self.estimate_cs(self.cross_spectra(np.asarray(block, dtype=float)))


def angle_diff_array(a: np.ndarray, b: float) -> np.ndarray:
    return np.abs((a - b + 180.0) % 360.0 - 180.0)


class VoiceDetector:
    """Sprache = Energie über adaptivem Grundrauschen und geringe spektrale Flachheit."""

    def __init__(self, threshold_db: float = 6.0, flatness_max: float = 0.6, sample_rate: int = 48000):
        self.threshold = 10 ** (threshold_db / 10)
        self.flatness_max = flatness_max
        self.sr = sample_rate
        self.noise: float | None = None

    def _flatness(self, x: np.ndarray) -> float:
        nfft = 1024
        frames = np.stack([x[s:s + nfft] for s in range(0, len(x) - nfft + 1, nfft // 2)])
        p = np.mean(np.abs(np.fft.rfft(frames * np.hanning(nfft), axis=1)) ** 2, axis=0)
        f = np.fft.rfftfreq(nfft, 1 / self.sr)
        band = p[(f >= 300) & (f <= 4000)] + 1e-20
        return float(np.exp(np.mean(np.log(band))) / np.mean(band))

    def is_speech(self, mono: np.ndarray) -> bool:
        x = np.asarray(mono, dtype=float)
        e = float(np.mean(x * x)) + 1e-20
        if self.noise is None:
            self.noise = e
            return False
        speech = e > self.noise * self.threshold and self._flatness(x) < self.flatness_max
        if e < self.noise:
            self.noise = 0.7 * self.noise + 0.3 * e
        elif not speech:
            self.noise *= 1.05
        return speech
