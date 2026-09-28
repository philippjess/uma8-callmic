import time

import numpy as np
import pytest

from ladspa_host import Plugin
from sim import band_noise, plane_wave
from uma8_callmic.array import UMA8

BEAM_LATENCY = 1049
PORTS = (
    [f"In {i}" for i in range(7)] + ["Beam Out", "Raw Out", "Azimuth (deg)", "Elevation (deg)", "Mode",
    "Center Channel"] + [f"Ring {k}" for k in range(6)] + ["Ring Offset (deg)", "Radius (mm)", "Gain (dB)",
    "Dereverb", "Dereverb Strength", "Dereverb T60 (s)", "Raw Extra Delay (samples)"]
)


def make(plugin_so, az=0.0, el=0.0, mode=0.0, dereverb=0.0, raw_extra=0.0, block=512):
    p = Plugin(plugin_so, "uma8_beam", block=block)
    p.set("Center Channel", UMA8.center)
    for k, ch in enumerate(UMA8.ring):
        p.set(f"Ring {k}", ch)
    p.set("Ring Offset (deg)", UMA8.ring_offset_deg)
    p.set("Radius (mm)", UMA8.radius_m * 1000)
    p.set("Azimuth (deg)", az)
    p.set("Elevation (deg)", el)
    p.set("Mode", mode)
    p.set("Gain (dB)", 0.0)
    p.set("Dereverb", dereverb)
    p.set("Dereverb Strength", 0.6)
    p.set("Dereverb T60 (s)", 0.5)
    p.set("Raw Extra Delay (samples)", raw_extra)
    return p


def channels(x: np.ndarray) -> dict:
    return {f"In {i}": x[:, i].astype(np.float32) for i in range(7)}


def test_ports(plugin_so):
    p = Plugin(plugin_so, "uma8_beam")
    assert p.names == PORTS
    p.close()


@pytest.mark.parametrize("dereverb", [0.0, 1.0])
def test_latency_is_constant(plugin_so, dereverb):
    x = np.zeros((4000, 7))
    x[500, :] = 1.0  # Quelle senkrecht über dem Array
    p = make(plugin_so, el=90.0, dereverb=dereverb, raw_extra=100)
    out = p.process(channels(x))
    p.close()
    assert int(np.argmax(np.abs(out["Beam Out"]))) == 500 + BEAM_LATENCY
    assert int(np.argmax(np.abs(out["Raw Out"]))) == 500 + BEAM_LATENCY + 100


def test_directivity_2_to_6_khz(plugin_so):
    x = plane_wave(0.1 * band_noise(48000, 2000, 6000), UMA8.positions(), 90.0, 0.0)
    powers = {}
    for az in (90.0, 270.0):
        p = make(plugin_so, az=az)
        y = p.process(channels(x))["Beam Out"][4800:]
        p.close()
        powers[az] = np.mean(y.astype(np.float64) ** 2)
    assert 10 * np.log10(powers[90.0] / powers[270.0]) >= 6.0


def test_steering_change_is_click_free(plugin_so):
    t = np.arange(48000) / 48000
    x = plane_wave(np.sin(2 * np.pi * 1000 * t), UMA8.positions(), 0.0, 0.0)
    p = make(plugin_so, az=0.0)
    a = p.process(channels(x[:24000]))["Beam Out"]
    p.set("Azimuth (deg)", 180.0)
    b = p.process(channels(x[24000:]))["Beam Out"]
    p.close()
    y = np.concatenate([a, b])[BEAM_LATENCY + 100:]
    assert np.max(np.abs(np.diff(y))) <= 2 * np.pi * 1000 / 48000 * 1.1


def test_block_size_does_not_change_output(plugin_so):
    x = plane_wave(0.1 * band_noise(12000, 300, 8000, seed=5), UMA8.positions(), 45.0, 20.0)
    ref = make(plugin_so, az=45.0, dereverb=1.0, block=512)
    y_ref = ref.process(channels(x))
    ref.close()
    for block in (1, 7, 4096):
        p = make(plugin_so, az=45.0, dereverb=1.0, block=block)
        y = p.process(channels(x))
        p.close()
        np.testing.assert_allclose(y["Beam Out"], y_ref["Beam Out"], atol=1e-6)
        np.testing.assert_allclose(y["Raw Out"], y_ref["Raw Out"], atol=1e-6)


def test_silence_and_full_scale(plugin_so):
    p = make(plugin_so, dereverb=1.0)
    silent = p.process(channels(np.zeros((9600, 7))))
    loud = p.process(channels(np.ones((9600, 7)) * np.sign(np.sin(np.arange(9600) * 0.3))[:, None]))
    p.close()
    assert np.all(silent["Beam Out"] == 0) and np.all(silent["Raw Out"] == 0)
    assert np.all(np.isfinite(loud["Beam Out"])) and np.all(np.isfinite(loud["Raw Out"]))


def test_cpu_budget(plugin_so):
    x = np.random.default_rng(1).standard_normal((480000, 7)) * 0.01
    ins = channels(x)
    p = make(plugin_so, dereverb=1.0)
    start = time.perf_counter()
    p.process(ins)
    elapsed = time.perf_counter() - start
    p.close()
    assert elapsed < 0.2, f"10 s Audio brauchten {elapsed:.3f} s"
