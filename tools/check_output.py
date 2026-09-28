#!/usr/bin/env python3
"""Nimmt 10 s vom virtuellen Mikrofon auf und prüft Aussetzer, Pegel und Bandbreite."""
import subprocess
import sys

import numpy as np

SR, SECONDS = 48000, 10
raw = subprocess.run(
    ["timeout", str(SECONDS), "pw-record", "--target", "uma8_callmic", "--rate", str(SR),
     "--channels", "1", "--format", "f32", "--raw", "-"],
    capture_output=True,
).stdout
x = np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype=np.float32)[SR // 2:]
ok = True
print(f"Samples: {len(x)} (erwartet ≈ {(SECONDS - 0.5) * SR:.0f})")
if len(x) < (SECONDS - 1.5) * SR:
    print("FEHLER: zu wenige Samples – Kette läuft nicht oder stockt")
    sys.exit(1)
zero_runs = np.diff(np.flatnonzero(np.diff(np.concatenate([[0], (x == 0).astype(int), [0]]))))[::2]
longest = int(zero_runs.max()) if zero_runs.size else 0
print(f"Längste Folge exakter Nullen: {longest} Samples")
if longest > SR // 100:
    print("FEHLER: Aussetzer (> 10 ms Stille)")
    ok = False
rms_db = 20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)
print(f"Pegel: {rms_db:.1f} dBFS")
spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
f = np.fft.rfftfreq(len(x), 1 / SR)
ratio = 10 * np.log10(spec[(f > 8000) & (f < 16000)].mean() / spec[(f > 1000) & (f < 4000)].mean())
print(f"Energie 8–16 kHz relativ zu 1–4 kHz: {ratio:.1f} dB")
if ratio < -60:
    print("FEHLER: keine Energie oberhalb 8 kHz")
    ok = False
print("OK" if ok else "PROBLEME GEFUNDEN")
sys.exit(0 if ok else 1)
