"""uma8_callmic/beampattern.py gegen das Modell in tools/eval_nulls.py und gegen das Plugin selbst."""
import sys
from pathlib import Path

import numpy as np
import pytest

from ladspa_host import Plugin
from sim import plane_wave
from uma8_callmic import beampattern as bp
from uma8_callmic.array import ArrayGeometry, UMA8
from uma8_callmic.config import Config
from uma8_callmic.constants import BEAM_LATENCY
from uma8_callmic.params import all_params

P = UMA8.positions()
LOOK, SPEAKERS = (80.0, 30.0), ((180.0, 5.0), (340.0, 5.0))
DIRS = [(180.0, 5.0), (340.0, 5.0), (80.0, 30.0), (0.0, 10.0), (260.0, 20.0)]


@pytest.mark.parametrize("weight", [0.0, 10.0])
def test_matches_eval_nulls_model(weight):
    pytest.importorskip("soundfile")  # tools/eval_nulls.py → eval_dereverb; nicht in den Paket-Builds
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import eval_nulls

    f = np.array([500.0, 1000.0, 2000.0, 3000.0, 4500.0, 7000.0])
    ref = eval_nulls.model_weights(f, LOOK, list(SPEAKERS) if weight else (), weight)
    w = bp.weights(f, P, bp.BeamSpec(*LOOK, nulls=SPEAKERS, null_weight_db=weight))
    for az, el in DIRS:
        g_ref = 20 * np.log10(np.abs(np.einsum("fm,fm->f", ref.conj(), eval_nulls.steering(f, az, el))))
        assert np.allclose(bp.response_db(w, f, P, [az], el)[:, 0], g_ref, atol=0.01), (az, el)


def test_other_modes_and_distortionless_look():
    f = np.array([1000.0, 3000.0])
    for mode in ("superdirective", "delay_and_sum"):
        w = bp.weights(f, P, bp.BeamSpec(*LOOK, mode=mode, nulls=SPEAKERS, null_weight_db=10.0))
        assert np.allclose(bp.response_db(w, f, P, [LOOK[0]], LOOK[1])[:, 0], 0.0, atol=1e-6)
    omni = bp.weights(f, ArrayGeometry(center=3, ring=(0, 1, 2, 4, 5, 6)).positions(),
                      bp.BeamSpec(*LOOK, mode="omni"), center=3)
    assert np.all(omni[:, 3] == 1) and np.count_nonzero(omni) == 2


def test_spec_from_params():
    cfg = Config(calibrated=True, calibrated_azimuth=80.0, calibrated_elevation=30.0,
                 speakers=[[180.0, 5.0]], null_weight_db=10.0)
    spec = bp.BeamSpec.from_params(all_params(cfg))
    assert spec == bp.BeamSpec(80.0, 30.0, "superdirective", ((180.0, 5.0), (180.0, 5.0)), 10.0, -3.0)
    assert bp.BeamSpec.from_params(all_params(Config(direction_mode="omni"))).mode == "omni"
    assert bp.BeamSpec.from_params(all_params(Config())).null_weight_db == 0.0
    az, pats = bp.pattern(all_params(cfg), P, el_deg=5.0)
    assert len(az) == 180 and set(pats) == {1000.0, 3000.0}
    i = int(np.argmin(np.abs(az - 180.0)))
    assert pats[1000.0][i] < -25.0 and pats[3000.0][i] < -25.0


def plugin_gain_db(so: str, spec: bp.BeamSpec, src, f: float) -> float:
    """Pegel einer ebenen Sinuswelle aus `src` am Beam-Ausgang relativ zum Mittel-Mikrofon."""
    n = 1024 * 40
    x = plane_wave(np.sin(2 * np.pi * f * np.arange(n) / 48000), P, *src)
    p = Plugin(so, "uma8_beam", block=1024)
    for k, ch in enumerate(UMA8.ring):
        p.set(f"Ring {k}", ch)
    controls = [("Center Channel", UMA8.center), ("Ring Offset (deg)", 90.0), ("Radius (mm)", 43.0),
                ("Azimuth (deg)", spec.azimuth), ("Elevation (deg)", spec.elevation), ("Mode", 0.0),
                ("Gain (dB)", 0.0), ("Dereverb", 0.0), ("Late Reverb", 0.0), ("Min WNG (dB)", spec.min_wng_db),
                ("Null Weight (dB)", spec.null_weight_db)]
    for i, (az, el) in enumerate(spec.nulls):
        controls += [(f"Null {i + 1} Azimuth (deg)", az), (f"Null {i + 1} Elevation (deg)", el)]
    for name, value in controls:
        p.set(name, value)
    y = p.process({f"In {i}": x[:, i].astype(np.float32) for i in range(7)})["Beam Out"].astype(float)
    p.close()
    ref = x[n // 2 - BEAM_LATENCY:n - BEAM_LATENCY, UMA8.center]
    return float(10 * np.log10(np.mean(y[n // 2:] ** 2) / np.mean(ref ** 2)))


@pytest.mark.parametrize("nulls", [SPEAKERS, ((200.0, 5.0), (200.0, 5.0))], ids=["zwei", "einer"])
def test_matches_plugin(plugin_so, nulls):
    """Sinus genau auf einem Bin (1031,25 Hz) und 3 kHz: Modell und Plugin auf 0,15 dB, in tiefen Nullstellen
    (unter −30 dB, Rechengenauigkeit f32) auf 1,5 dB. Eine doppelt angegebene Nullstelle zählt einfach."""
    spec = bp.BeamSpec(*LOOK, nulls=nulls, null_weight_db=10.0)
    for f in (22 * 48000 / 1024, 3000.0):
        w = bp.weights([f], P, spec)
        for src in [(180.0, 5.0), (340.0, 5.0), (200.0, 5.0), (130.0, 0.0)]:
            model = float(bp.response_db(w, [f], P, [src[0]], src[1])[0, 0])
            tol = 0.15 if model > -30.0 else 1.5
            assert plugin_gain_db(plugin_so, spec, src, f) == pytest.approx(model, abs=tol), (f, src)
