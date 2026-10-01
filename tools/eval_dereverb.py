#!/usr/bin/env python3
"""Bewertet Beamforming und Hallunterdrückung von uma8_beam in simulierten Räumen.

Kleiner leerer Raum 4 × 3,5 × 2,6 m (T60 0,45 s und 0,7 s), Array auf dem Schreibtisch, Sprecher
0,6 m entfernt und 0,35 m höher; Strahl auf die wahre Richtung und mit 10°/10° Fehler.
Ergebnisse: Tabelle auf stdout und in <out>/results.md, WAVs zum Anhören in <out>/.

    python3 tools/eval_dereverb.py [--so plugin/target/release/libuma8_beam.so] [--out /tmp/uma8-eval]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, fftconvolve, resample_poly, sosfiltfilt, welch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "tools")]

from ladspa_host import Plugin  # noqa: E402
from roomsim import C_SOUND, measured_t60, rirs  # noqa: E402
from uma8_callmic.array import UMA8  # noqa: E402
from uma8_callmic.constants import BEAM_LATENCY  # noqa: E402

SR = 48000
ROOM = (4.0, 3.5, 2.6)
ARRAY = np.array([2.0, 0.5, 0.75])          # Schreibtisch, 0,5 m vor der Wand
TALKER_AZ, TALKER_DIST, TALKER_RISE = 80.0, 0.6, 0.35
SENSOR_NOISE_DB = -35.0                     # Eigenrauschen je Mikrofon relativ zum Sprachpegel
SENTENCES = [
    ("de", "Guten Morgen, ich rufe wegen des Termins am Donnerstag an."),
    ("de+f3", "Kannst du mich gut verstehen, oder klingt es noch hallig?"),
    ("de", "Die Besprechung beginnt um halb drei im kleinen Raum."),
    ("de+f3", "Sechs Mikrofone im Kreis und eines in der Mitte."),
]
ALSA_WAVS = ["/usr/share/sounds/alsa/Front_Center.wav", "/usr/share/sounds/alsa/Rear_Left.wav"]
OCTAVES = [125, 250, 500, 1000, 2000, 4000, 8000]

# Name → Plugin-Controls (Mode 0 = superdirektiv, 1 = Mittel-Mikrofon, 2 = Delay-and-Sum;
# PF = Kohärenz-Postfilter „Dereverb“, Spät = Abklingmodell „Late Reverb“, Zahl = Stärke)
CONFIGS = {
    "Mitte": dict(mode=1),
    "DS": dict(mode=2),
    "SD": dict(mode=0),
    "DS+Spät 0.6 (bisher)": dict(mode=2, late=1, strength=0.6),
    "SD+Spät 0.6": dict(mode=0, late=1, strength=0.6),
    "SD+PF 0.3": dict(mode=0, dereverb=1, strength=0.3),
    "SD+PF 0.6": dict(mode=0, dereverb=1, strength=0.6),
    "SD+PF 1.0": dict(mode=0, dereverb=1, strength=1.0),
    "SD+PF+Spät 0.3": dict(mode=0, dereverb=1, late=1, strength=0.3),
    "SD+PF+Spät 0.6 (Standard)": dict(mode=0, dereverb=1, late=1, strength=0.6),
    "SD+PF+Spät 1.0": dict(mode=0, dereverb=1, late=1, strength=1.0),
}
LINEAR = ("Mitte", "DS", "SD")


def build() -> Path:
    subprocess.run(["cargo", "build", "--release", "--quiet", "--manifest-path", str(ROOT / "plugin/Cargo.toml")],
                   check=True)
    return ROOT / "plugin/target/release/libuma8_beam.so"


def speech(cache: Path) -> np.ndarray:
    """Sätze (espeak-ng, 22,05 kHz → 48 kHz) und ALSA-Testansagen, je 0,8 s Pause dahinter."""
    parts = []
    for i, (voice, text) in enumerate(SENTENCES):
        wav = cache / f"satz{i}.wav"
        if not wav.exists():
            subprocess.run(["espeak-ng", "-v", voice, "-s", "150", "-w", str(wav), text], check=True)
        x, sr = sf.read(wav)
        parts.append(resample_poly(x, 320, 147) if sr == 22050 else x)
    for path in ALSA_WAVS:
        if Path(path).exists():
            parts.append(sf.read(path)[0])
    pause = np.zeros(int(0.8 * SR))
    x = np.concatenate([np.concatenate([p / np.max(np.abs(p)), pause]) for p in parts])
    return 0.1 * x / np.sqrt(np.mean(x ** 2))


def geometry(steer_error: bool):
    pos = UMA8.positions()
    az = np.radians(TALKER_AZ)
    src = ARRAY + np.array([TALKER_DIST * np.cos(az), TALKER_DIST * np.sin(az), TALKER_RISE])
    el = np.degrees(np.arctan2(TALKER_RISE, TALKER_DIST))
    steer = (TALKER_AZ + 10.0, el + 10.0) if steer_error else (TALKER_AZ, el)
    return src, ARRAY + pos, steer


def room_rirs(cache: Path, t60: float, direct_only: bool = False) -> np.ndarray:
    src, mics, _ = geometry(False)
    key = hashlib.sha1(repr((ROOM, src.tolist(), mics.tolist(), t60, direct_only)).encode()).hexdigest()[:12]
    path = cache / f"rir_{key}.npy"
    if path.exists():
        return np.load(path)
    h = rirs(ROOM, src, mics, t60, SR, direct_only=direct_only)
    np.save(path, h)
    return h


def process(so: str, x: np.ndarray, steer, mode=0, dereverb=0, late=0, strength=0.6, t60=0.5,
            min_wng=-3.0) -> np.ndarray:
    """7-Kanal-Signal (n, 7) durch uma8_beam; Ausgang um die Latenz zurückgeschoben."""
    p = Plugin(so, "uma8_beam", block=4096)
    for name, value in [("Center Channel", UMA8.center), ("Ring Offset (deg)", UMA8.ring_offset_deg),
                        ("Radius (mm)", UMA8.radius_m * 1000), ("Azimuth (deg)", steer[0] % 360),
                        ("Elevation (deg)", steer[1]), ("Mode", mode), ("Gain (dB)", 0.0), ("Dereverb", dereverb),
                        ("Dereverb Strength", strength), ("Dereverb T60 (s)", t60), ("Late Reverb", late),
                        ("Min WNG (dB)", min_wng), ("Raw Extra Delay (samples)", 0.0)]:
        p.set(name, value)
    for k, ch in enumerate(UMA8.ring):
        p.set(f"Ring {k}", ch)
    pad = np.concatenate([x, np.zeros((BEAM_LATENCY, 7))])
    y = p.process({f"In {i}": pad[:, i].astype(np.float32) for i in range(7)})["Beam Out"]
    p.close()
    return y[BEAM_LATENCY:].astype(np.float64)


def ir_metrics(h: np.ndarray, t_direct: int) -> dict:
    """DRR (±2,5 ms um den Direktschall) und C50, breitbandig und je Oktave."""
    w = int(0.0025 * SR)
    a, b = max(t_direct - w, 0), t_direct + w
    e = h ** 2
    res = {"DRR": 10 * np.log10(e[a:b].sum() / (e[:a].sum() + e[b:].sum())),
           "C50": 10 * np.log10(e[a:t_direct + int(0.05 * SR)].sum() / e[t_direct + int(0.05 * SR):].sum())}
    for fc in (500, 1000, 2000, 4000):
        hb = sosfiltfilt(butter(3, [fc / np.sqrt(2), fc * np.sqrt(2)], "bandpass", fs=SR, output="sos"), h)
        eb = hb ** 2
        res[f"C50@{fc}"] = 10 * np.log10(eb[a:t_direct + int(0.05 * SR)].sum() / eb[t_direct + int(0.05 * SR):].sum())
    return res


def activity(dry: np.ndarray, delay: int):
    """10-ms-Frames: Sprache aktiv bzw. Nachhallschwanz (50–400 ms nach dem Ende eines Abschnitts)."""
    hop = SR // 100
    e = np.add.reduceat(np.concatenate([np.zeros(delay), dry]) ** 2, np.arange(0, len(dry) + delay, hop))
    active = e > np.percentile(e, 95) * 10 ** (-35 / 10)
    tail = np.zeros_like(active)
    since = 10 ** 6
    for i, a in enumerate(active):
        since = 0 if a else since + 1
        tail[i] = 5 <= since <= 40
    return hop, active, tail


def signal_metrics(y: np.ndarray, ref: np.ndarray, hop: int, active, tail) -> dict:
    n = min(len(y), len(ref))
    y, ref = y[:n], ref[:n]
    alpha = np.dot(y, ref) / np.dot(ref, ref)
    err = y - alpha * ref
    e = np.add.reduceat(y ** 2, np.arange(0, n, hop))[:len(active)]
    return {"SI-SDR": 10 * np.log10(np.sum((alpha * ref) ** 2) / np.sum(err ** 2)),
            "Direkt": 20 * np.log10(abs(alpha)),
            "Schwanz": 10 * np.log10(e[tail[:len(e)]].mean() / e[active[:len(e)]].mean())}


def noise_bands(so: str, steer, cfg: dict) -> list[float]:
    """Unkorreliertes Sensorrauschen: Ausgangsleistung je Oktave relativ zu einem Mikrofon (dB)."""
    x = np.random.default_rng(3).standard_normal((10 * SR, 7))
    y = process(so, x, steer, **cfg)[SR:]
    f, p = welch(y, SR, nperseg=8192)
    return [10 * np.log10(p[(f >= fc / np.sqrt(2)) & (f < fc * np.sqrt(2))].mean() * SR / 2) for fc in OCTAVES]


def table(title: str, cols: list[str], rows: dict[str, dict]) -> str:
    out = [f"### {title}", "", "| Konfiguration | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for name, vals in rows.items():
        out.append(f"| {name} | " + " | ".join(f"{vals[c]:.1f}" if c in vals else "–" for c in cols) + " |")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--so", help="fertiges Plugin statt cargo build")
    ap.add_argument("--out", default="/tmp/uma8-eval")
    ap.add_argument("--t60", type=float, nargs="+", default=[0.45, 0.7], help="simulierte Nachhallzeiten (s)")
    ap.add_argument("--t60-setting", type=float, default=0.5,
                    help="„Dereverb T60 (s)“ des Plugins; 0 = wahre Nachhallzeit des Raums (Standard 0,5 wie in der Konfiguration)")
    ap.add_argument("--configs", nargs="+", default=list(CONFIGS), help="Auswahl aus: " + ", ".join(CONFIGS))
    ap.add_argument("--no-wav", action="store_true")
    args = ap.parse_args()
    so = str(args.so or build())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    configs = {k: CONFIGS[k] for k in args.configs}

    dry = speech(out)
    src, mics, _ = geometry(False)
    t_direct = int(round(np.linalg.norm(src - mics[UMA8.center]) / C_SOUND * SR))
    report, results = ["# uma8_beam – Auswertung im simulierten Raum", ""], {}
    if not args.no_wav:
        sf.write(out / "trocken.wav", dry / np.max(np.abs(dry)) * 0.5, SR)

    for t60 in args.t60:
        h = room_rirs(out, t60)
        h_direct = room_rirs(out, t60, direct_only=True)
        wet = np.stack([fftconvolve(dry, h[m])[:len(dry) + h.shape[1]] for m in range(7)], axis=1)
        ref = fftconvolve(dry, h_direct[UMA8.center])[:len(wet)]
        speech_rms = np.sqrt(np.mean(wet[:, UMA8.center] ** 2))
        noise = np.random.default_rng(1).standard_normal(wet.shape) * speech_rms * 10 ** (SENSOR_NOISE_DB / 20)
        hop, active, tail = activity(dry, 0)
        meas = measured_t60(h[UMA8.center])
        report.append(f"## Raum T60 {t60:.2f} s (gemessen {meas:.2f} s)\n")
        print(report[-1], flush=True)
        if not args.no_wav:
            sf.write(out / f"t60_{t60:.2f}_mikrofon_mitte.wav", wet[:, UMA8.center] / np.max(np.abs(wet)) * 0.5, SR)
        for steer_error in (False, True):
            steer = geometry(steer_error)[2]
            label = f"T60 {t60:.2f} s, Strahl {'mit 10°/10° Fehler' if steer_error else 'auf Sprecher'}"
            ir_rows, sig_rows = {}, {}
            for name, cfg in configs.items():
                cfg = dict(cfg, t60=args.t60_setting or t60)
                if name in LINEAR:
                    ir_rows[name] = ir_metrics(process(so, h.T, steer, **cfg), t_direct)
                y = process(so, wet + noise, steer, **cfg)
                sig_rows[name] = signal_metrics(y, ref, hop, active, tail)
                if not args.no_wav:
                    tag = name.replace(" ", "_").replace("+", "_").replace("(", "").replace(")", "")
                    sf.write(out / f"t60_{t60:.2f}_{'fehler' if steer_error else 'genau'}_{tag}.wav",
                             y / np.max(np.abs(wet)) * 0.5, SR)
            results[label] = {"ir": ir_rows, "signal": sig_rows}
            part = [f"### {label}\n"]
            if ir_rows:
                part.append(table("Systemimpulsantwort (dB, höher = besser)",
                                  ["DRR", "C50", "C50@500", "C50@1000", "C50@2000", "C50@4000"], ir_rows))
            part.append(table("Sprache (SI-SDR gegen Direktschall: höher = besser; Direkt = Pegel des "
                              "Direktschalls; Schwanz = Pegel 50–400 ms nach Sprachende relativ zur Sprache)",
                              ["SI-SDR", "Direkt", "Schwanz"], sig_rows))
            report += part
            print("\n".join(part), flush=True)

    steer = geometry(False)[2]
    noise_rows = {name: dict(zip([f"{f} Hz" for f in OCTAVES], noise_bands(so, steer, cfg)))
                  for name, cfg in configs.items()}
    results["noise"] = noise_rows
    report.append(table("Unkorreliertes Sensorrauschen: Ausgang relativ zu einem Mikrofon (dB, niedriger = besser)",
                        [f"{f} Hz" for f in OCTAVES], noise_rows))
    print(report[-1])
    (out / "results.md").write_text("\n".join(report))
    (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(f"Ergebnisse und WAVs in {out}/")


if __name__ == "__main__":
    main()
