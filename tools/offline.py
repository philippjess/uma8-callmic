#!/usr/bin/env python3
"""Verarbeitet eine echte 8-Kanal-Aufnahme des UMA-8 offline mit uma8_beam → Mono-WAV (A/B-Vergleich).

Aufnahme (UMA-8 in Raw-Firmware; Kanal 0–6 = Mikrofone, Kanal 7 bleibt unbenutzt):
    pw-record --target alsa_input.usb-miniDSP_micArray_RAW_SPK-00.analog-surround-71 \\
              --channels 8 --channel-map FL,FR,FC,LFE,RL,RR,SL,SR --format f32 rec.wav
(Positionen wie im PipeWire-Knoten; FLC/FRC aus der USB-Belegung gibt es dort nicht, Kanal 6 käme stumm an)
Verarbeitung mit den Einstellungen aus ~/.config/uma8-callmic/config.toml, einzelne Werte überschreibbar:
    python3 tools/offline.py rec.wav neu.wav
    python3 tools/offline.py rec.wav ds.wav --beamformer delay_and_sum --dereverb off --late-reverb off
    python3 tools/offline.py rec.wav roh.wav --direction omni --dereverb off --late-reverb off
Dieselbe Abbildung Einstellungen → Controls wie die laufende Kette (params.all_params); die Latenz wird
entfernt, damit sich Ausgaben sampelgenau vergleichen lassen. --dfn schickt das Ergebnis zusätzlich durch
DeepFilterNet wie im Betrieb.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from ladspa_host import Plugin  # noqa: E402
from uma8_callmic import constants as K  # noqa: E402
from uma8_callmic.config import BEAMFORMERS, DIRECTION_MODES, Config, load  # noqa: E402
from uma8_callmic.params import all_params  # noqa: E402


def on_off(v: str) -> bool:
    if v not in ("on", "off"):
        raise argparse.ArgumentTypeError("on oder off")
    return v == "on"


def run_plugin(so: Path, label: str, inputs: dict[str, np.ndarray], controls: dict[str, float], sr: int,
               output: str) -> np.ndarray:
    p = Plugin(str(so), label, sample_rate=sr, block=4096)
    for name, value in controls.items():
        p.set(name, value)
    y = p.process(inputs)[output]
    p.close()
    return y


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", type=Path, help="8-Kanal-WAV (oder ≥ 7 Kanäle) vom UMA-8")
    ap.add_argument("output", type=Path, help="Mono-WAV (32-Bit-Float)")
    ap.add_argument("--config", type=Path, default=K.CONFIG_FILE, help="Einstellungsdatei (Standard: %(default)s)")
    ap.add_argument("--so", type=Path, help="libuma8_beam.so (Standard: frisch gebaut aus plugin/)")
    ap.add_argument("--direction", choices=DIRECTION_MODES, help="Richtungsart (omni = nur Mittel-Mikrofon)")
    ap.add_argument("--azimuth", type=float, help="fester Azimut in Grad (setzt Richtungsart manuell)")
    ap.add_argument("--elevation", type=float, help="Höhenwinkel in Grad")
    ap.add_argument("--beamformer", choices=BEAMFORMERS)
    ap.add_argument("--dereverb", type=on_off, metavar="on|off", help="kohärenzbasierte Hallunterdrückung")
    ap.add_argument("--late-reverb", type=on_off, metavar="on|off", help="späten Nachhall zusätzlich dämpfen")
    ap.add_argument("--strength", type=float, help="Stärke der Hallunterdrückung 0–1")
    ap.add_argument("--t60", type=float, help="angenommene Nachhallzeit in s")
    ap.add_argument("--gain-db", type=float, help="Verstärkung in dB")
    ap.add_argument("--dfn", action="store_true", help="zusätzlich DeepFilterNet (Dämpfungsgrenze aus den Einstellungen)")
    args = ap.parse_args()

    res = load(args.config)
    cfg: Config = res.config
    cfg.echo_cancel = False  # keine AEC-Stufe offline: die ganze Verstärkung liegt im Plugin
    for w in res.warnings:
        print(f"Hinweis: {w}", file=sys.stderr)
    if args.azimuth is not None:
        cfg.direction_mode, cfg.manual_azimuth = "manual", args.azimuth % 360.0
    for name, value in [("direction_mode", args.direction), ("calibrated_elevation", args.elevation),
                        ("beamformer", args.beamformer), ("dereverb", args.dereverb),
                        ("late_reverb", args.late_reverb), ("dereverb_strength", args.strength),
                        ("dereverb_t60", args.t60), ("gain_db", args.gain_db)]:
        if value is not None:
            setattr(cfg, name, value)

    x, sr = sf.read(args.recording, dtype="float32", always_2d=True)
    if x.shape[1] < 7:
        sys.exit(f"{args.recording}: {x.shape[1]} Kanäle, mindestens 7 nötig")
    so = args.so
    if so is None:
        subprocess.run(["cargo", "build", "--release", "--quiet", "--manifest-path", str(ROOT / "plugin/Cargo.toml")],
                       check=True)
        so = ROOT / "plugin/target/release/libuma8_beam.so"

    params = all_params(cfg)
    beam = {k.split(":", 1)[1]: v for k, v in params.items() if k.startswith("beam:")}
    pad = np.zeros((K.BEAM_LATENCY, 7), dtype=np.float32)
    xp = np.concatenate([x[:, :7], pad])
    y = run_plugin(so, "uma8_beam", {f"In {i}": np.ascontiguousarray(xp[:, i]) for i in range(7)}, beam, sr,
                   "Beam Out")[K.BEAM_LATENCY:]
    if args.dfn:
        dfn = K.dfn_plugin()
        if not dfn.exists():
            sys.exit(f"DeepFilterNet nicht gefunden: {dfn}")
        controls = {k.split(":", 1)[1]: v for k, v in params.items() if k.startswith("dfn:")}
        yp = np.concatenate([y, np.zeros(K.DFN_LATENCY, dtype=np.float32)])
        y = run_plugin(dfn, "deep_filter_mono", {"Audio In": yp}, controls, sr, "Audio Out")[K.DFN_LATENCY:]
    sf.write(args.output, y, sr, subtype="FLOAT")
    peak = 20 * np.log10(np.max(np.abs(y)) + 1e-12)
    print(f"{args.output}: {len(y) / sr:.1f} s, Spitze {peak:.1f} dBFS, Richtung {cfg.direction_mode}, "
          f"Beamformer {cfg.beamformer}, Hall {'an' if cfg.dereverb else 'aus'}/"
          f"spät {'an' if cfg.late_reverb else 'aus'}, Stärke {cfg.dereverb_strength:.2f}")


if __name__ == "__main__":
    main()
