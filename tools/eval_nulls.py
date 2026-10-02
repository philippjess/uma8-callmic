#!/usr/bin/env python3
"""Bewertet die Nullstellen (feste Lautsprecherrichtungen) des superdirektiven Beams von uma8_beam.

Szenario Schreibtisch: Raum 4 × 3,5 × 2,6 m (T60 0,45 s und 0,7 s), UMA-8 flach auf dem Tisch,
Sprecher 0,65 m entfernt unter El 30°, Stereo-Lautsprecher bei ±100° Azimut relativ zum Sprecher,
El 5°, 0,55 m. Strahl auf den Sprecher, Nullstellen auf die Lautsprecher mit 0/5/10° Fehler,
dazu ±1 dB zufällige Mikrofon-Empfindlichkeit. Alle Werte je Oktave 250 Hz – 8 kHz aus den
Systemimpulsantworten (Raumimpulsantwort → Plugin), Hall- und Nachhallstufen aus:

- Lautsprecher direkt / Echo gesamt: Energie des Lautsprecher-Direktschalls bzw. der ganzen
  Raumimpulsantwort beider Lautsprecher am Beam-Ausgang, relativ zum Beam ohne Nullstellen (dB,
  niedriger = mehr Dämpfung).
- Sprecher SI-SDR: Direktschall des Sprechers durch den Beam gegen den Direktschall am
  Mittel-Mikrofon (Verzerrung; Fernfeld-Annahme bei 0,65 m und Mikrofon-Streuung begrenzen es).
- Sprecher Hall: SI-SDR der ganzen Sprecher-Raumimpulsantwort durch den Beam gegen den Direktschall
  am Mittel-Mikrofon, Änderung gegenüber ohne Nullstellen (dB, negativ = halliger).
- Echo / Sprecher nach Hallstufen: ganze Kette mit Kohärenz-Postfilter und Nachhall (Standard 0,6),
  Sprache über beide Lautsprecher (Monosignal wie im Anruf) bzw. vom Sprecher, je allein:
  Änderung der Echo-Energie bzw. des Sprecher-SI-SDR durch die Nullstellen (dB).
- DI-Verlust: Richtwirkungsindex gegen 3D-diffusen Schall mit minus ohne Nullstellen (dB, aus dem
  Modell des Entwurfs; `--check` vergleicht Modell und Plugin, Abweichung < 0,05 dB).

Ergebnis (Gewicht 10 dB, 5° Fehler, ±1 dB Streuung, T60 0,45 s): Lautsprecher-Direktschall 1/2/4 kHz
−4,7/−10,9/−8,9 dB, Echo gesamt aber nur −0,3/−0,8/−1,0 dB (nach dem Beam überwiegen die Reflexionen;
die Lautsprecher stehen etwa im Hallradius), Sprecher nach den Hallstufen ≤ 0,3 dB schlechter.
`--sweep` begründet Muster ±8° und den Abfall 4–6 kHz.

    python3 tools/eval_nulls.py [--so plugin/target/release/libuma8_beam.so] [--out /tmp/uma8-nulls-eval]
    python3 tools/eval_nulls.py --sweep    # Modell: Breite des Robustheitsmusters × Gewicht
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "tools")]

from ladspa_host import Plugin  # noqa: E402
from eval_dereverb import speech  # noqa: E402
from roomsim import C_SOUND, rirs  # noqa: E402
from uma8_callmic.array import UMA8  # noqa: E402
from uma8_callmic.constants import BEAM_LATENCY  # noqa: E402

SR = 48000
ROOM = (4.0, 3.5, 2.6)
ARRAY = np.array([1.8, 0.8, 0.75])          # Schreibtisch, Bildschirm/Wand dahinter (−y)
TALKER = (80.0, 30.0, 0.65)                  # Azimut, Elevation (°), Abstand (m)
SPEAKERS = [(180.0, 5.0, 0.55), (340.0, 5.0, 0.55)]   # ±100° um den Sprecher
OCTAVES = [250, 500, 1000, 2000, 4000, 8000]
MISMATCH_DB = 1.0
MISMATCH_DRAWS = 8
SIGNAL_DRAWS = 3
SENSOR_NOISE_DB = -35.0
WNG_DB = -3.0
# Muster im Plugin (beam.rs NULL_SPREAD_DEG): Mitte und ±δ in Azimut und Elevation
PATTERN_DEG = 8.0
# beam.rs NULL_TAPER_HZ: β voll bis zur ersten Frequenz, darüber Kosinus-Abfall bis 0
TAPER_HZ = (4000.0, 6000.0)
WEIGHTS_DB = [6.0, 10.0, 15.0]


def unit(az: float, el: float) -> np.ndarray:
    a, e = np.radians(az), np.radians(el)
    return np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])


def source(az: float, el: float, dist: float) -> np.ndarray:
    return ARRAY + dist * unit(az, el)


def room_rirs(cache: Path, src: np.ndarray, t60: float, direct_only: bool) -> np.ndarray:
    mics = ARRAY + UMA8.positions()
    key = hashlib.sha1(repr((ROOM, src.tolist(), mics.tolist(), t60, direct_only)).encode()).hexdigest()[:12]
    path = cache / f"rir_{key}.npy"
    if path.exists():
        return np.load(path)
    h = rirs(ROOM, src, mics, t60, SR, direct_only=direct_only)
    np.save(path, h)
    return h


# --- Modell des Entwurfs (wie beam.rs, in numpy) ---

def steering(f: np.ndarray, az: float, el: float) -> np.ndarray:
    """(F, 7) Fernfeld-Steuervektoren in Kanal-Reihenfolge."""
    k = 2 * np.pi * f / C_SOUND
    return np.exp(1j * k[:, None] * (UMA8.positions() @ unit(az, el))[None, :])


def gamma(f: np.ndarray) -> np.ndarray:
    p = UMA8.positions()
    dist = np.linalg.norm(p[:, None] - p[None], axis=2)
    return np.sinc(2 * f[:, None, None] * dist[None] / C_SOUND)


def null_dirs(nulls, delta: float) -> list[tuple[float, float]]:
    pattern = [(0.0, 0.0), (delta, 0.0), (-delta, 0.0), (0.0, delta), (0.0, -delta)] if delta else [(0.0, 0.0)]
    return [(az + da, el + de) for az, el in nulls for da, de in pattern]


def null_taper(f: np.ndarray, band=TAPER_HZ) -> np.ndarray:
    """Anteil von β je Frequenz: 1 bis band[0], Kosinus-Abfall bis 0 bei band[1] (wie beam.rs)."""
    if band is None:
        return np.ones_like(f)
    lo, hi = band
    x = np.clip((f - lo) / (hi - lo), 0.0, 1.0)
    return 0.5 + 0.5 * np.cos(np.pi * x)


def model_weights(f: np.ndarray, look, nulls=(), weight_db: float = 0.0, delta: float = PATTERN_DEG,
                  wng_db: float = WNG_DB, taper=TAPER_HZ) -> np.ndarray:
    """(F, 7) Gewichte w (Ausgang Σ conj(w_m)·x_m): MVDR gegen Γ + β(f)/5·Σ d dᴴ mit WNG-Untergrenze."""
    R = gamma(f).astype(complex)
    beta = (10 ** (weight_db / 10) - 1) * null_taper(f, taper)
    if nulls and weight_db > 0:
        dirs = null_dirs(nulls, delta)
        for az, el in dirs:
            v = steering(f, az, el)
            R += (beta * len(nulls) / len(dirs))[:, None, None] * v[:, :, None] * v[:, None, :].conj()
    d = steering(f, *look)
    lam, U = np.linalg.eigh(R)
    lam = np.maximum(lam, 0)
    pw = np.abs(np.einsum("fmi,fm->fi", U.conj(), d)) ** 2

    def wng(mu):
        r = pw / (lam + mu[:, None])
        return r.sum(1) ** 2 / (r / (lam + mu[:, None])).sum(1)

    lo, hi = np.full(len(f), np.log(1e-6)), np.full(len(f), np.log(1e4))
    wmin = 10 ** (wng_db / 10)
    for _ in range(30):
        mid = 0.5 * (lo + hi)
        good = wng(np.exp(mid)) >= wmin
        hi, lo = np.where(good, mid, hi), np.where(good, lo, mid)
    mu = np.where(wng(np.full(len(f), 1e-6)) >= wmin, 1e-6, np.exp(hi))
    inv = np.einsum("fmi,fi,fni->fmn", U, 1 / (lam + mu[:, None]), U.conj())
    x = np.einsum("fmn,fn->fm", inv, d)
    return x / np.einsum("fm,fm->f", d.conj(), x)[:, None]


def model_di_db(f: np.ndarray, w: np.ndarray) -> np.ndarray:
    return -10 * np.log10(np.real(np.einsum("fi,fij,fj->f", w.conj(), gamma(f), w)))


# --- Systemantworten (Spektren auf einem gemeinsamen Raster) ---

N_FFT = 1 << 16
FREQS = np.fft.rfftfreq(N_FFT, 1 / SR)
BAND = [(FREQS >= fc / np.sqrt(2)) & (FREQS < fc * np.sqrt(2)) for fc in OCTAVES]
WARMUP = SR // 2   # Nullstellen werden nach dem Start verteilt entworfen (≈ 0,25 s bis zur Wirkung)


def plugin(so: str, x: np.ndarray, look, nulls=(), weight_db: float = 0.0, dereverb: bool = False) -> np.ndarray:
    """7-Kanal-Signal (7, n) durch uma8_beam (superdirektiv; Hallstufen aus oder Standard 0,6),
    Ausgang ohne Latenz und Anlaufzeit, n + 2048 Samples."""
    p = Plugin(so, "uma8_beam", block=4096)
    for name, value in [("Center Channel", UMA8.center), ("Ring Offset (deg)", UMA8.ring_offset_deg),
                        ("Radius (mm)", UMA8.radius_m * 1000), ("Azimuth (deg)", look[0] % 360),
                        ("Elevation (deg)", look[1]), ("Mode", 0.0), ("Gain (dB)", 0.0),
                        ("Dereverb", float(dereverb)), ("Late Reverb", float(dereverb)), ("Dereverb Strength", 0.6),
                        ("Dereverb T60 (s)", 0.5), ("Min WNG (dB)", WNG_DB), ("Null Weight (dB)", weight_db)]:
        p.set(name, value)
    for k, ch in enumerate(UMA8.ring):
        p.set(f"Ring {k}", ch)
    for i, (az, el) in enumerate(nulls or [(0.0, 0.0), (0.0, 0.0)]):
        p.set(f"Null {i + 1} Azimuth (deg)", az % 360)
        p.set(f"Null {i + 1} Elevation (deg)", abs(el))
    pad = np.concatenate([np.zeros((WARMUP, 7)), x.T, np.zeros((BEAM_LATENCY + 2048, 7))])
    y = p.process({f"In {i}": pad[:, i].astype(np.float32) for i in range(7)})["Beam Out"]
    p.close()
    return y[WARMUP + BEAM_LATENCY:].astype(np.float64)


def plugin_response(so: str, h: np.ndarray, look, nulls=(), weight_db: float = 0.0) -> np.ndarray:
    """Spektrum der Raumimpulsantworten (7, n) durch uma8_beam (superdirektiv, Hallstufen aus)."""
    return np.fft.rfft(plugin(so, h, look, nulls, weight_db), N_FFT)


@lru_cache(maxsize=256)
def _weights(look, nulls, weight_db: float, delta: float, taper) -> np.ndarray:
    coarse = np.fft.rfftfreq(1024, 1 / SR)
    wc = model_weights(coarse, look, list(nulls), weight_db, delta, taper=taper)
    return np.stack([np.interp(FREQS, coarse, wc[:, m].real) + 1j * np.interp(FREQS, coarse, wc[:, m].imag)
                     for m in range(7)], 1)


def model_response(h: np.ndarray, look, nulls=(), weight_db: float = 0.0, delta: float = PATTERN_DEG,
                   taper=TAPER_HZ) -> np.ndarray:
    """Wie `plugin_response` mit den Modellgewichten (je STFT-Bin entworfen, dazwischen interpoliert)."""
    w = _weights(tuple(look), tuple(map(tuple, nulls)), weight_db if nulls else 0.0, delta, taper)
    return np.einsum("fm,mf->f", w.conj(), np.fft.rfft(h, N_FFT, axis=1))


def energy_db(Y: np.ndarray) -> np.ndarray:
    return np.array([10 * np.log10(np.sum(np.abs(Y[b]) ** 2)) for b in BAND])


def si_sdr(Y: np.ndarray, R: np.ndarray) -> np.ndarray:
    """SI-SDR je Oktave (Projektion im Frequenzbereich, Bandgrenzen hart)."""
    out = []
    for b in BAND:
        a = np.real(np.vdot(R[b], Y[b])) / np.vdot(R[b], R[b]).real
        out.append(10 * np.log10(np.sum(np.abs(a * R[b]) ** 2) / np.sum(np.abs(Y[b] - a * R[b]) ** 2)))
    return np.array(out)


# --- Auswertung ---

def scenario(cache: Path, t60: float) -> dict:
    """Raumimpulsantworten (7, n) für Sprecher und beide Lautsprecher, ganz und nur Direktschall."""
    srcs = {"talker": source(*TALKER), **{f"spk{i}": source(*s) for i, s in enumerate(SPEAKERS)}}
    return {name: {kind: room_rirs(cache, src, t60, kind == "direct") for kind in ("full", "direct")}
            for name, src in srcs.items()}


def null_errors(err: float) -> list[tuple[float, float]]:
    """Fehler der Nullrichtungen (ΔAz, ΔEl), beide Lautsprecher gleich verschoben; Mittel über die Fälle."""
    return [(0.0, 0.0)] if err == 0 else [(err, 0.0), (-err, 0.0), (0.0, err)]


def evaluate(irs: dict, respond, look, weight_db: float, errs, mismatch: bool, seed: int = 0) -> dict:
    """Je Fehler der Nullrichtung: Werte je Oktave (dB), gemittelt über Fehlerfälle und Streuungen."""
    rng = np.random.default_rng(seed)
    draws = [np.ones(7)] if not mismatch else [10 ** (rng.uniform(-MISMATCH_DB, MISMATCH_DB, 7) / 20)
                                              for _ in range(MISMATCH_DRAWS)]
    R = np.fft.rfft(irs["talker"]["direct"][UMA8.center], N_FFT)
    keys = [(s, k) for s in irs for k in ("full", "direct")]
    acc = {e: {k: [] for k in ("spk_direct", "echo", "talker_sisdr", "talker_reverb")} for e in errs}
    for g in draws:
        scaled = {sk: irs[sk[0]][sk[1]] * g[:, None] for sk in keys}
        base = {sk: respond(scaled[sk], look, (), 0.0) for sk in keys}
        for err in errs:
            for ea, ee in null_errors(err):
                nulls = [(az + ea, el + ee) for az, el, _ in SPEAKERS]
                out = {sk: respond(scaled[sk], look, nulls, weight_db) for sk in keys}
                a = acc[err]
                for key, kind in (("spk_direct", "direct"), ("echo", "full")):
                    e_on = sum(10 ** (energy_db(out[(s, kind)]) / 10) for s in ("spk0", "spk1"))
                    e_off = sum(10 ** (energy_db(base[(s, kind)]) / 10) for s in ("spk0", "spk1"))
                    a[key].append(10 * np.log10(e_on / e_off))
                a["talker_sisdr"].append(si_sdr(out[("talker", "direct")], R))
                a["talker_reverb"].append(si_sdr(out[("talker", "full")], R) - si_sdr(base[("talker", "full")], R))
    return {e: {k: np.mean(v, axis=0) for k, v in acc[e].items()} for e in errs}


def energy_bands(y: np.ndarray) -> np.ndarray:
    """Energie je Oktave eines beliebig langen Signals (dB)."""
    Y = np.fft.rfft(y)
    f = np.fft.rfftfreq(len(y), 1 / SR)
    return np.array([10 * np.log10(np.sum(np.abs(Y[(f >= fc / np.sqrt(2)) & (f < fc * np.sqrt(2))]) ** 2))
                     for fc in OCTAVES])


def si_sdr_bands(y: np.ndarray, ref: np.ndarray) -> np.ndarray:
    n = min(len(y), len(ref))
    Y, R = np.fft.rfft(y[:n]), np.fft.rfft(ref[:n])
    f = np.fft.rfftfreq(n, 1 / SR)
    out = []
    for fc in OCTAVES:
        b = (f >= fc / np.sqrt(2)) & (f < fc * np.sqrt(2))
        a = np.real(np.vdot(R[b], Y[b])) / np.vdot(R[b], R[b]).real
        out.append(10 * np.log10(np.sum(np.abs(a * R[b]) ** 2) / np.sum(np.abs(Y[b] - a * R[b]) ** 2)))
    return np.array(out)


def evaluate_signals(so: str, irs: dict, dry: np.ndarray, look, weight_db: float, errs, mismatch: bool,
                     seed: int = 0) -> dict:
    """Ganze Kette mit Hallstufen (Standard): Sprecher allein bzw. Fernsprecher über beide Lautsprecher
    (gleiches Monosignal) allein, je mit Eigenrauschen −35 dB. Änderung durch die Nullstellen (dB):
    Echo-Energie je Oktave und SI-SDR des Sprechers gegen seinen Direktschall."""
    rng = np.random.default_rng(seed)
    draws = [np.ones(7)] if not mismatch else [10 ** (rng.uniform(-MISMATCH_DB, MISMATCH_DB, 7) / 20)
                                              for _ in range(SIGNAL_DRAWS)]
    far = dry[::-1].copy()
    talk = np.stack([fftconvolve(dry, irs["talker"]["full"][m]) for m in range(7)])
    echo = np.stack([fftconvolve(far, irs["spk0"]["full"][m]) + fftconvolve(far, irs["spk1"]["full"][m])
                     for m in range(7)])
    ref = fftconvolve(dry, irs["talker"]["direct"][UMA8.center])
    noise = rng.standard_normal(talk.shape) * 10 ** (SENSOR_NOISE_DB / 20)
    talk += noise * np.sqrt(np.mean(talk[UMA8.center] ** 2))
    echo += noise * np.sqrt(np.mean(echo[UMA8.center] ** 2))
    acc = {e: {"echo_post": [], "talker_post": []} for e in errs}
    for g in draws:
        t_off = si_sdr_bands(plugin(so, talk * g[:, None], look, dereverb=True), ref)
        e_off = energy_bands(plugin(so, echo * g[:, None], look, dereverb=True))
        for err in errs:
            for ea, ee in null_errors(err):
                nulls = [(az + ea, el + ee) for az, el, _ in SPEAKERS]
                t_on = si_sdr_bands(plugin(so, talk * g[:, None], look, nulls, weight_db, True), ref)
                e_on = energy_bands(plugin(so, echo * g[:, None], look, nulls, weight_db, True))
                acc[err]["talker_post"].append(t_on - t_off)
                acc[err]["echo_post"].append(e_on - e_off)
    return {e: {k: np.mean(v, axis=0) for k, v in acc[e].items()} for e in errs}


def di_loss(look, weight_db: float, err: float, delta: float = PATTERN_DEG, taper=TAPER_HZ) -> np.ndarray:
    f = np.concatenate([fc * 2 ** np.linspace(-0.5, 0.5, 15) for fc in OCTAVES])
    base = model_di_db(f, model_weights(f, look))
    loss = []
    for ea, ee in null_errors(err):
        nulls = [(az + ea, el + ee) for az, el, _ in SPEAKERS]
        loss.append(model_di_db(f, model_weights(f, look, nulls, weight_db, delta, taper=taper)) - base)
    return np.mean(loss, axis=0).reshape(len(OCTAVES), -1).mean(1)


def fmt_row(label: str, vals) -> str:
    return f"| {label} | " + " | ".join(f"{v:.1f}" for v in vals) + " |"


def table(title: str, rows: list[tuple[str, np.ndarray]]) -> str:
    head = "| | " + " | ".join(f"{f} Hz" if f < 1000 else f"{f // 1000} kHz" for f in OCTAVES) + " |"
    return "\n".join([f"#### {title}", "", head, "|---" * (len(OCTAVES) + 1) + "|", *[fmt_row(*r) for r in rows], ""])


def build() -> Path:
    subprocess.run(["cargo", "build", "--release", "--quiet", "--manifest-path", str(ROOT / "plugin/Cargo.toml")],
                   check=True)
    return ROOT / "plugin/target/release/libuma8_beam.so"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--so", help="fertiges Plugin statt cargo build")
    ap.add_argument("--out", default="/tmp/uma8-nulls-eval")
    ap.add_argument("--t60", type=float, nargs="+", default=[0.45, 0.7])
    ap.add_argument("--weights", type=float, nargs="+", default=WEIGHTS_DB, help="„Null Weight (dB)“")
    ap.add_argument("--sweep", action="store_true", help="Modell: Musterbreite × Gewicht statt Plugin")
    ap.add_argument("--deltas", type=float, nargs="+", default=[0.0, 5.0, 8.0, 10.0], help="Musterbreiten (--sweep)")
    ap.add_argument("--check", action="store_true", help="Modell gegen Plugin (eine Einstellung)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    look = TALKER[:2]
    errs = (0.0, 5.0, 10.0)
    report, results = ["# uma8_beam – Nullstellen auf die Lautsprecher", ""], {}

    if args.sweep:
        irs = scenario(out, args.t60[0])
        tapers = {"ohne": None, "3–5 kHz": (3000.0, 5000.0), "4–6 kHz (Plugin)": TAPER_HZ}
        for (tname, taper), delta, weight in itertools.product(tapers.items(), args.deltas, args.weights):
            respond = lambda h, lk, nl, w, d=delta, t=taper: model_response(h, lk, nl, w, d, t)  # noqa: E731
            res = evaluate(irs, respond, look, weight, errs, mismatch=True)
            rows = []
            for err in errs:
                rows += [(f"{err:.0f}°: LS direkt", res[err]["spk_direct"]), (f"{err:.0f}°: Echo", res[err]["echo"]),
                         (f"{err:.0f}°: Sprecher Hall", res[err]["talker_reverb"])]
            rows.append(("5°: DI-Verlust", di_loss(look, weight, 5.0, delta, taper)))
            part = table(f"Modell T60 {args.t60[0]} s, Abfall {tname}, Muster ±{delta:.0f}°, Gewicht {weight:.0f} dB, "
                         f"±{MISMATCH_DB:.0f} dB Streuung", rows)
            report.append(part)
            print(part, flush=True)
        (out / "sweep.md").write_text("\n".join(report))
        return

    so = str(args.so or build())
    if args.check:
        irs = scenario(out, args.t60[0])
        nulls = [(az, el) for az, el, _ in SPEAKERS]
        rows = []
        for s in ("spk0", "spk1", "talker"):
            for w in (0.0, args.weights[0]):
                diff = energy_db(plugin_response(so, irs[s]["full"], look, nulls, w)) - \
                    energy_db(model_response(irs[s]["full"], look, nulls, w))
                rows.append((f"{s}, Gewicht {w:.0f} dB", diff))
        print(table("Plugin − Modell, Energie (dB)", rows))
        return

    dry = speech(out)
    for t60 in args.t60:
        irs = scenario(out, t60)
        respond = lambda h, lk, nl, w: plugin_response(so, h, lk, nl, w)  # noqa: E731
        for weight in args.weights:
            rows, res = [], {}
            for mismatch in (False, True):
                r = evaluate(irs, respond, look, weight, errs, mismatch)
                rs = evaluate_signals(so, irs, dry, look, weight, errs, mismatch)
                for err in errs:
                    tag = f"{err:.0f}°" + (f", ±{MISMATCH_DB:.0f} dB" if mismatch else "")
                    r[err].update(rs[err])
                    res[tag] = {k: v.round(2).tolist() for k, v in r[err].items()}
                    rows += [(f"{tag}: LS direkt", r[err]["spk_direct"]), (f"{tag}: Echo gesamt", r[err]["echo"]),
                             (f"{tag}: Echo nach Hallstufen", r[err]["echo_post"]),
                             (f"{tag}: Sprecher SI-SDR", r[err]["talker_sisdr"]),
                             (f"{tag}: Sprecher Hall", r[err]["talker_reverb"]),
                             (f"{tag}: Sprecher nach Hallstufen", r[err]["talker_post"])]
            for err in errs:
                loss = di_loss(look, weight, err)
                res[f"{err:.0f}°"]["di_loss"] = loss.round(2).tolist()
                rows.append((f"{err:.0f}°: DI-Verlust", loss))
            results[f"T60 {t60} s, Gewicht {weight} dB"] = res
            part = table(f"T60 {t60:.2f} s, „Null Weight (dB)“ {weight:.0f}: Fehler der Nullrichtung, Streuung", rows)
            report.append(part)
            print(part, flush=True)
        R = np.fft.rfft(irs["talker"]["direct"][UMA8.center], N_FFT)
        center = {s: np.fft.rfft(irs[s]["full"][UMA8.center], N_FFT) for s in irs}
        sd = {s: plugin_response(so, irs[s]["full"], look) for s in irs}
        part = table(f"T60 {t60:.2f} s, Bezug: Beam ohne Nullstellen (dB)", [
            ("Echo LS 1 rel. Mittel-Mikrofon", energy_db(sd["spk0"]) - energy_db(center["spk0"])),
            ("Echo LS 2 rel. Mittel-Mikrofon", energy_db(sd["spk1"]) - energy_db(center["spk1"])),
            ("Sprecher SI-SDR (direkt), Beam", si_sdr(plugin_response(so, irs["talker"]["direct"], look), R)),
            ("Sprecher SI-SDR (Hall), Beam", si_sdr(sd["talker"], R)),
            ("Sprecher SI-SDR (Hall), Mittel-Mikrofon", si_sdr(center["talker"], R))])
        report.append(part)
        print(part, flush=True)
    (out / "results.md").write_text("\n".join(report))
    (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(f"Ergebnisse in {out}/")


if __name__ == "__main__":
    main()
