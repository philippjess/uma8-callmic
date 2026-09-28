"""Kanalzuordnung aus diffusem Raumrauschen: Kohärenz sinkt mit dem Mikrofonabstand."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .array import ArrayGeometry


def coherence_matrix(block: np.ndarray, sample_rate: int = 48000, nfft: int = 1024,
                     fmin: float = 200.0, fmax: float = 2000.0) -> np.ndarray:
    hop = nfft // 2
    frames = np.stack([block[s:s + nfft] for s in range(0, len(block) - nfft + 1, hop)])
    spec = np.fft.rfft(frames * np.hanning(nfft)[None, :, None], axis=1)
    f = np.fft.rfftfreq(nfft, 1 / sample_rate)
    spec = spec[:, (f >= fmin) & (f <= fmax), :]
    cross = np.einsum("fki,fkj->kij", spec, np.conj(spec)) / len(frames)
    auto = np.real(np.einsum("kii->ki", cross))
    coh = np.abs(cross) ** 2 / (auto[:, :, None] * auto[:, None, :] + 1e-30)
    return coh.mean(axis=0)


def detect(coh: np.ndarray, radius_m: float, ring_offset_deg: float) -> tuple[ArrayGeometry, float]:
    """Mitte = höchste mittlere Kohärenz; Ring = Kette der jeweils kohärentesten Nachbarn."""
    off = coh * (1 - np.eye(coh.shape[0]))
    center = int(np.argmax(off.sum(axis=1)))
    ring_channels = [c for c in range(coh.shape[0]) if c != center]
    order = [min(ring_channels)]
    while len(order) < 6:
        rest = [c for c in ring_channels if c not in order]
        order.append(max(rest, key=lambda c: off[order[-1], c]))
    neighbours = np.mean([off[order[k], order[(k + 1) % 6]] for k in range(6)])
    opposite = np.mean([off[order[k], order[k + 3]] for k in range(3)])
    return ArrayGeometry(center, tuple(order), ring_offset_deg, radius_m), float(neighbours - opposite)


def _neighbour_pairs(ring) -> set[frozenset]:
    return {frozenset((ring[k], ring[(k + 1) % 6])) for k in range(6)}


@dataclass
class GeometryCheck:
    detected: ArrayGeometry | None
    matches_default: bool
    contrast: float
    message: str


def check(block: np.ndarray, default: ArrayGeometry, min_contrast: float = 0.05) -> GeometryCheck:
    coh = coherence_matrix(np.asarray(block, dtype=float)[:, :7])
    geom, contrast = detect(coh, default.radius_m, default.ring_offset_deg)
    if contrast < min_contrast:
        return GeometryCheck(None, False, contrast,
                             "Raumgeräusch zu leise oder zu gerichtet – Standardzuordnung bleibt aktiv.")
    same = geom.center == default.center and _neighbour_pairs(geom.ring) == _neighbour_pairs(default.ring)
    return GeometryCheck(geom, same, contrast,
                         "Standardzuordnung bestätigt." if same else "Abweichende Kanalzuordnung erkannt.")
