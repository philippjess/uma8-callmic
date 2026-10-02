"""Richtcharakteristik von uma8_beam in numpy, Entwurf wie plugin/src/beam.rs (für die Platzierungsansicht).

Superdirektiv: MVDR gegen R = Γ + β/5·Σ d·dᴴ (Γ: 3D-diffuses Feld, Summe über das Muster jeder Nullstelle,
β = 10^(dB/10) − 1, oberhalb 4–6 kHz ausgeblendet), Diagonalladung μ je Frequenz gerade so groß, dass der
White-Noise-Gain die Untergrenze einhält. Delay-and-Sum w = d/7, „alle Richtungen“ = Mittel-Mikrofon.
Ausgang y = Σ conj(w_m)·x_m. Abgleich mit dem Plugin: tests/test_beampattern.py."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .doa import C_SOUND
from .params import BEAM_MODES, NULL_KEYS, OMNI_MODE

#: wie beam.rs: Muster je Nullstelle (Mitte, ±NULL_SPREAD_DEG in Azimut und Elevation), Abfall von β
NULL_SPREAD_DEG = 8.0
NULL_PATTERN = ((0.0, 0.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0))
NULL_TAPER_HZ = (4000.0, 6000.0)
MU_MIN, MU_MAX, BISECT_STEPS = 1e-6, 1e4, 30
_MODES = {v: k for k, v in BEAM_MODES.items()} | {OMNI_MODE: "omni"}


@dataclass(frozen=True)
class BeamSpec:
    """Was das Plugin aus seinen Controls entwirft."""
    azimuth: float
    elevation: float
    mode: str = "superdirective"
    nulls: tuple[tuple[float, float], ...] = ()
    null_weight_db: float = 0.0
    min_wng_db: float = -3.0

    @classmethod
    def from_params(cls, p: dict[str, float]) -> BeamSpec:
        """Aus den Controls von params.all_params (also genau dem, was das Plugin bekommt)."""
        n1az, n1el, n2az, n2el, weight = (p.get(k, 0.0) for k in NULL_KEYS)
        return cls(p["beam:Azimuth (deg)"], p["beam:Elevation (deg)"], _MODES.get(p["beam:Mode"], "superdirective"),
                   ((n1az, n1el), (n2az, n2el)), weight, p["beam:Min WNG (dB)"])


def unit(az_deg, el_deg) -> np.ndarray:
    a, e = np.radians(az_deg), np.radians(el_deg)
    return np.stack([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)], axis=-1)


def steering(f: np.ndarray, positions: np.ndarray, az_deg, el_deg) -> np.ndarray:
    """(F, M) bzw. (F, A, M): Mikrofon m hört die ebene Welle um p_m·u/c früher, d_m = exp(+j·2πf·τ_m)."""
    tau = unit(az_deg, el_deg) @ positions.T / C_SOUND
    return np.exp(2j * np.pi * np.multiply.outer(f, tau))


def gamma(f: np.ndarray, positions: np.ndarray) -> np.ndarray:
    dist = np.linalg.norm(positions[:, None] - positions[None], axis=2)
    return np.sinc(2 * f[:, None, None] * dist[None] / C_SOUND)


def null_taper(f: np.ndarray) -> np.ndarray:
    lo, hi = NULL_TAPER_HZ
    return 0.5 + 0.5 * np.cos(np.pi * np.clip((f - lo) / (hi - lo), 0.0, 1.0))


def null_directions(nulls) -> list[tuple[float, float]]:
    """Musterrichtungen aller Nullstellen; gleiche Nullstellen zählen einmal (wie im Plugin)."""
    distinct = list(dict.fromkeys((float(az), float(el)) for az, el in nulls))
    return [(az + da * NULL_SPREAD_DEG, el + de * NULL_SPREAD_DEG) for az, el in distinct for da, de in NULL_PATTERN]


def _regularization(lam: np.ndarray, pw: np.ndarray, wng_min: float) -> np.ndarray:
    def wng(mu):
        r = pw / (lam + mu[:, None])
        return r.sum(1) ** 2 / (r / (lam + mu[:, None])).sum(1)

    n = len(lam)
    lo, hi = np.full(n, np.log(MU_MIN)), np.full(n, np.log(MU_MAX))
    for _ in range(BISECT_STEPS):
        mid = 0.5 * (lo + hi)
        good = wng(np.exp(mid)) >= wng_min
        hi, lo = np.where(good, mid, hi), np.where(good, lo, mid)
    mu = np.exp(hi)
    mu = np.where(wng(np.full(n, MU_MAX)) < wng_min, MU_MAX, mu)
    return np.where(wng(np.full(n, MU_MIN)) >= wng_min, MU_MIN, mu)


def weights(f, positions: np.ndarray, spec: BeamSpec, center: int = 0) -> np.ndarray:
    """(F, M) Gewichte des Plugins für die Frequenzen `f` (Hz)."""
    f = np.atleast_1d(np.asarray(f, float))
    d = steering(f, positions, spec.azimuth, min(max(spec.elevation, 0.0), 90.0))
    m = positions.shape[0]
    if spec.mode == "omni":
        w = np.zeros_like(d)
        w[:, center] = 1.0
        return w
    if spec.mode == "delay_and_sum":
        return d / m
    R = gamma(f, positions).astype(complex)
    if spec.nulls and spec.null_weight_db > 0:
        beta = (10 ** (min(spec.null_weight_db, 40.0) / 10) - 1) / len(NULL_PATTERN) * null_taper(f)
        for az, el in null_directions((az % 360.0, min(max(el, 0.0), 90.0)) for az, el in spec.nulls):
            v = steering(f, positions, az, el)
            R += beta[:, None, None] * v[:, :, None] * v[:, None, :].conj()
    lam, U = np.linalg.eigh(R)
    lam = np.maximum(lam, 0.0)
    b = np.einsum("fmi,fm->fi", U.conj(), d)
    pw = np.abs(b) ** 2
    mu = _regularization(lam, pw, 10 ** (spec.min_wng_db / 10))
    c = b / (lam + mu[:, None])
    c /= np.sum(pw / (lam + mu[:, None]), axis=1)[:, None]
    return np.einsum("fmi,fi->fm", U, c)


def response_db(w: np.ndarray, f, positions: np.ndarray, az_deg, el_deg: float, floor_db: float = -60.0) -> np.ndarray:
    """(F, A) Pegel |wᴴd| in dB für ebene Wellen aus `az_deg` unter der Elevation `el_deg`."""
    f = np.atleast_1d(np.asarray(f, float))
    az = np.atleast_1d(np.asarray(az_deg, float))
    d = steering(f, positions, az, np.full(az.shape, float(el_deg)))      # (F, A, M)
    g = np.abs(np.einsum("fm,fam->fa", w.conj(), d))
    return np.maximum(20 * np.log10(g + 1e-12), floor_db)


def pattern(params: dict[str, float], positions: np.ndarray, freqs=(1000.0, 3000.0), el_deg: float | None = None,
            az_step: float = 2.0, center: int = 0) -> tuple[np.ndarray, dict[float, np.ndarray]]:
    """Azimut-Raster und Pegel (dB) je Frequenz für die Controls `params` (params.all_params) unter der
    Elevation `el_deg` (Standard: Blickrichtung)."""
    spec = BeamSpec.from_params(params)
    az = np.arange(0.0, 360.0, az_step)
    w = weights(freqs, positions, spec, center)
    el = spec.elevation if el_deg is None else el_deg
    db = response_db(w, freqs, positions, az, el)
    return az, {float(f): db[i] for i, f in enumerate(freqs)}
