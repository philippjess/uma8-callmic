"""Quaderraum-Simulation nach der Spiegelquellenmethode (Allen & Berkley) für das UMA-8-Array.

Frequenzunabhängige Wandreflexion aus Sabine für eine Ziel-Nachhallzeit, alle Spiegelquellen bis
zur Laufzeit T60, Bruchteil-Verzögerungen per gefensterter Sinc-Interpolation. Ein Hochpass
(80 Hz) entfernt den Gleichanteil, den frequenzunabhängige Reflexionen sonst erzeugen.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

C_SOUND = 343.0


def sabine_beta(dims, t60: float) -> float:
    """Reflexionsfaktor (Amplitude) aller Wände für die Nachhallzeit `t60` nach Sabine."""
    lx, ly, lz = dims
    volume, surface = lx * ly * lz, 2 * (lx * ly + lx * lz + ly * lz)
    alpha = 0.161 * volume / (surface * t60)
    if not 0 < alpha < 1:
        raise ValueError(f"T60 {t60} s ist für diesen Raum nicht erreichbar (α = {alpha:.2f})")
    return float(np.sqrt(1 - alpha))


def _axis_images(src: float, length: float, nmax: int) -> tuple[np.ndarray, np.ndarray]:
    """Koordinaten und Reflexionszahl der Spiegelquellen entlang einer Achse."""
    n = np.arange(-nmax, nmax + 1)
    coords = np.concatenate([src + 2 * n * length, -src + 2 * n * length])
    refl = np.concatenate([2 * np.abs(n), np.abs(n - 1) + np.abs(n)])
    return coords, refl


def _sinc_kernel(frac: np.ndarray, taps: int) -> np.ndarray:
    """Hann-gefensterte Sinc-Koeffizienten für Verzögerung `floor + frac`, Stützstellen −taps/2+1 … taps/2."""
    k = np.arange(-taps // 2 + 1, taps // 2 + 1)
    t = k[None, :] - frac[:, None]
    return np.sinc(t) * (0.5 + 0.5 * np.cos(np.pi * t / (taps // 2)))


def rirs(dims, src, mics, t60: float, sr: int = 48000, length_s: float | None = None,
         direct_only: bool = False, hp_hz: float = 80.0) -> np.ndarray:
    """Raumimpulsantworten (len(mics), n): Quelle `src` → jedes Mikrofon, Positionen in Metern.

    Frühe Spiegelquellen (< 50 ms) mit 64 Stützstellen, späte mit 16 – genau genug für den Hall,
    bei vertretbarer Rechenzeit (T60 0,7 s ≈ 1,6 Mio. Spiegelquellen je Mikrofon)."""
    dims, src, mics = np.asarray(dims, float), np.asarray(src, float), np.atleast_2d(np.asarray(mics, float))
    length_s = t60 if length_s is None else length_s
    beta = sabine_beta(dims, t60)
    max_dist = C_SOUND * length_s
    n_out = int(np.ceil(length_s * sr)) + 64
    out = np.zeros((len(mics), n_out))
    axes = [_axis_images(src[a], dims[a], 0 if direct_only else int(np.ceil(max_dist / (2 * dims[a]))) + 1)
            for a in range(3)]
    if direct_only:
        axes = [(c[:1], r[:1]) for c, r in axes]
    (cx, rx), (cy, ry), (cz, rz) = axes
    for m, mic in enumerate(mics):
        dy2 = (cy - mic[1])[:, None] ** 2 + (cz - mic[2])[None, :] ** 2
        ryz = ry[:, None] + rz[None, :]
        for x, r in zip(cx, rx):
            dist = np.sqrt((x - mic[0]) ** 2 + dy2)
            keep = dist <= max_dist
            if not keep.any():
                continue
            d = dist[keep]
            amp = beta ** (r + ryz[keep]) / (4 * np.pi * d)
            delay = d / C_SOUND * sr
            early = delay < 0.05 * sr
            for sel, taps in ((early, 64), (~early, 16)):
                if not sel.any():
                    continue
                base = np.floor(delay[sel]).astype(int)
                kernel = _sinc_kernel(delay[sel] - base, taps) * amp[sel, None]
                idx = base[:, None] + np.arange(-taps // 2 + 1, taps // 2 + 1)[None, :]
                ok = (idx >= 0) & (idx < n_out)
                out[m] += np.bincount(idx[ok], weights=kernel[ok], minlength=n_out)
    if hp_hz:
        out = sosfilt(butter(2, hp_hz, "highpass", fs=sr, output="sos"), out, axis=1)
    return out


def measured_t60(rir: np.ndarray, sr: int = 48000) -> float:
    """T60 aus der Schroeder-Rückwärtsintegration (Steigung zwischen −5 und −25 dB, T20 ×3)."""
    edc = np.cumsum(rir[::-1] ** 2)[::-1]
    edc_db = 10 * np.log10(edc / edc[0] + 1e-30)
    i5, i25 = np.argmax(edc_db <= -5), np.argmax(edc_db <= -25)
    t = np.arange(i5, i25) / sr
    slope = np.polyfit(t, edc_db[i5:i25], 1)[0]
    return float(-60.0 / slope)
