"""Testsignale für das Array: ebene Wellen und diffuses Rauschen."""
import numpy as np

C_SOUND = 343.0


def plane_wave(signal: np.ndarray, positions: np.ndarray, az_deg: float, el_deg: float, sr: int = 48000) -> np.ndarray:
    """Kanal i erhält `signal` um (p_i·u)/c vorgezogen (Bruchteil-Verschiebung per FFT)."""
    az, el = np.radians(az_deg), np.radians(el_deg)
    u = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
    lead = positions @ u / C_SOUND
    n = len(signal)
    spec = np.fft.rfft(signal)
    f = np.fft.rfftfreq(n, 1 / sr)
    return np.stack([np.fft.irfft(spec * np.exp(2j * np.pi * f * l), n) for l in lead], axis=1)


def band_noise(n: int, lo: float, hi: float, sr: int = 48000, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / sr)
    spec[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(spec, n)
    return x / np.std(x)


def diffuse_noise(n: int, positions: np.ndarray, sr: int = 48000, n_sources: int = 150, seed: int = 0) -> np.ndarray:
    """Summe unabhängiger ebener Wellen aus gleichverteilten Richtungen der Kugel."""
    rng = np.random.default_rng(seed)
    out = np.zeros((n, positions.shape[0]))
    for _ in range(n_sources):
        v = rng.standard_normal(3)
        v /= np.linalg.norm(v)
        az = np.degrees(np.arctan2(v[1], v[0]))
        el = np.degrees(np.arcsin(v[2]))
        out += plane_wave(rng.standard_normal(n), positions, az, el, sr)
    return out / np.sqrt(n_sources)
