import time

import numpy as np
import pytest
from scipy.signal import welch

from ladspa_host import Plugin
from sim import band_noise, diffuse_noise, plane_wave
from uma8_callmic.array import UMA8
from uma8_callmic.constants import BEAM_LATENCY

PORTS = (
    [f"In {i}" for i in range(7)] + ["Beam Out", "Raw Out", "Azimuth (deg)", "Elevation (deg)", "Mode",
    "Center Channel"] + [f"Ring {k}" for k in range(6)] + ["Ring Offset (deg)", "Radius (mm)", "Gain (dB)",
    "Dereverb", "Dereverb Strength", "Dereverb T60 (s)", "Raw Extra Delay (samples)", "Late Reverb", "Min WNG (dB)"]
)
SD, OMNI, DS = 0.0, 1.0, 2.0


def make(plugin_so, az=0.0, el=0.0, mode=SD, dereverb=0.0, late=0.0, strength=0.6, raw_extra=0.0, block=512):
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
    p.set("Dereverb Strength", strength)
    p.set("Dereverb T60 (s)", 0.5)
    p.set("Late Reverb", late)
    p.set("Min WNG (dB)", -3.0)
    p.set("Raw Extra Delay (samples)", raw_extra)
    return p


def channels(x: np.ndarray) -> dict:
    return {f"In {i}": x[:, i].astype(np.float32) for i in range(7)}


def run(plugin_so, x: np.ndarray, **kw) -> dict:
    p = make(plugin_so, **kw)
    out = p.process(channels(x))
    p.close()
    return out


def test_ports(plugin_so):
    p = Plugin(plugin_so, "uma8_beam")
    assert p.names == PORTS
    p.close()


def test_gain_can_attenuate(plugin_so):
    """Mit Echounterdrückung liegen 24 dB schon vor dem Plugin, „Gain (dB)“ wird dann auch negativ."""
    p = Plugin(plugin_so, "uma8_beam")
    h = p.d.port_range_hints[PORTS.index("Gain (dB)")]
    assert (h.lower, h.upper) == (-30.0, 60.0)
    p.close()
    x = np.random.default_rng(3).standard_normal((48000, 7)).astype(np.float32) * 0.01
    level = {}
    for gain in (0.0, -24.0, -40.0):
        p = make(plugin_so, mode=OMNI)
        p.set("Gain (dB)", gain)
        y = p.process(channels(x))["Raw Out"][24000:]
        p.close()
        level[gain] = 10 * np.log10(np.mean(np.square(y, dtype=np.float64)))
    assert abs(level[0.0] - level[-24.0] - 24.0) < 0.05
    assert abs(level[0.0] - level[-40.0] - 30.0) < 0.05  # unter −30 dB wird geklemmt


def test_latency_constant_matches_plugin():
    assert BEAM_LATENCY == 1024  # FFT-Länge der STFT im Plugin


@pytest.mark.parametrize("mode", [SD, OMNI, DS])
@pytest.mark.parametrize("dereverb,late", [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])
def test_latency_is_constant(plugin_so, mode, dereverb, late):
    x = np.zeros((4000, 7))
    x[500, :] = 1.0  # Quelle senkrecht über dem Array
    out = run(plugin_so, x, el=90.0, mode=mode, dereverb=dereverb, late=late, raw_extra=100)
    assert int(np.argmax(np.abs(out["Beam Out"]))) == 500 + BEAM_LATENCY
    assert int(np.argmax(np.abs(out["Raw Out"]))) == 500 + BEAM_LATENCY + 100


@pytest.mark.parametrize("mode", [SD, DS])
def test_directivity_2_to_6_khz(plugin_so, mode):
    x = plane_wave(0.1 * band_noise(48000, 2000, 6000), UMA8.positions(), 90.0, 0.0)
    powers = {az: np.mean(run(plugin_so, x, az=az, mode=mode)["Beam Out"][4800:].astype(np.float64) ** 2)
              for az in (90.0, 270.0)}
    assert 10 * np.log10(powers[90.0] / powers[270.0]) >= 6.0


@pytest.mark.parametrize("mode", [SD, DS])
def test_target_direction_is_undistorted(plugin_so, mode):
    """Verzerrungsfrei zur Zielrichtung: Ausgang = Mittel-Mikrofon, um die Latenz verzögert."""
    x = plane_wave(0.1 * band_noise(24000, 100, 16000, seed=2), UMA8.positions(), 200.0, 30.0)
    y = run(plugin_so, x, az=200.0, el=30.0, mode=mode)["Beam Out"].astype(np.float64)
    ref = x[:-BEAM_LATENCY, UMA8.center]
    err = y[BEAM_LATENCY:][4800:] - ref[4800:]
    assert 10 * np.log10(np.sum(err ** 2) / np.sum(ref[4800:] ** 2)) < -40


def test_superdirective_beats_delay_and_sum_in_diffuse_field(plugin_so):
    """Diffuses Feld (Raumhall-Modell): superdirektiv deutlich leiser als Delay-and-Sum unter 2 kHz."""
    x = 0.1 * diffuse_noise(96000, UMA8.positions(), n_sources=200, seed=4)
    spectra = {}
    for mode in (SD, DS):
        y = run(plugin_so, x, az=0.0, el=25.0, mode=mode)["Beam Out"][BEAM_LATENCY + 4800:]
        f, spectra[mode] = welch(y.astype(np.float64), 48000, nperseg=4096)
    for lo, hi, need in [(400, 700, 3.0), (700, 1400, 3.0), (1400, 2000, 2.0)]:
        band = (f >= lo) & (f < hi)
        gain_db = 10 * np.log10(spectra[DS][band].sum() / spectra[SD][band].sum())
        assert gain_db >= need, f"{lo}–{hi} Hz: SD nur {gain_db:.1f} dB besser"


@pytest.mark.parametrize("late,need_tail", [(0.0, 8.0), (1.0, 14.0)])
def test_dereverb_reduces_diffuse_tail(plugin_so, late, need_tail):
    """Rauschstoß als ebene Welle plus exponentiell abklingendes diffuses Feld (T60 0,5 s), diffus
    3,5 dB stärker als direkt wie im kleinen Raum bei 0,6 m Abstand."""
    sr, n, burst = 48000, 72000, 24000
    pos = UMA8.positions()
    dry = 0.1 * band_noise(n, 200, 10000, seed=7)
    dry[burst:] = 0.0
    env = np.exp(-3 * np.log(10) * np.maximum(np.arange(n) - burst, 0) / (0.5 * sr))
    x = plane_wave(dry, pos, 30.0, 30.0) + 0.15 * diffuse_noise(n, pos, n_sources=80, seed=9) * env[:, None]
    out = {d: run(plugin_so, x, az=30.0, el=30.0, dereverb=d, late=d * late)["Beam Out"].astype(np.float64)
           for d in (0.0, 1.0)}
    seg = lambda y, a, b: np.sum(y[BEAM_LATENCY + a:BEAM_LATENCY + b] ** 2)  # noqa: E731
    tail_db = 10 * np.log10(seg(out[0.0], burst + 4800, burst + 19200) / seg(out[1.0], burst + 4800, burst + 19200))
    burst_db = 10 * np.log10(seg(out[0.0], 4800, burst) / seg(out[1.0], 4800, burst))
    assert tail_db >= need_tail, f"Nachhall nur um {tail_db:.1f} dB gesenkt"
    assert burst_db <= 4.0, f"Stoß um {burst_db:.1f} dB gedämpft"


@pytest.mark.parametrize("mode", [SD, DS])
def test_steering_change_is_click_free(plugin_so, mode):
    t = np.arange(48000) / 48000
    x = plane_wave(np.sin(2 * np.pi * 1000 * t), UMA8.positions(), 0.0, 0.0)
    p = make(plugin_so, az=0.0, mode=mode, dereverb=1.0)
    a = p.process(channels(x[:24000]))["Beam Out"]
    p.set("Azimuth (deg)", 180.0)
    b = p.process(channels(x[24000:30000]))["Beam Out"]
    p.set("Mode", OMNI if mode == SD else SD)
    p.set("Late Reverb", 1.0)
    c = p.process(channels(x[30000:]))["Beam Out"]
    p.close()
    y = np.concatenate([a, b, c])[BEAM_LATENCY + 100:]
    assert np.max(np.abs(np.diff(y))) <= 2 * np.pi * 1000 / 48000 * 1.1


def test_block_size_does_not_change_output(plugin_so):
    x = plane_wave(0.1 * band_noise(12000, 300, 8000, seed=5), UMA8.positions(), 45.0, 20.0)
    x += 0.01 * np.random.default_rng(6).standard_normal(x.shape)
    ref = make(plugin_so, az=45.0, dereverb=1.0, late=1.0, block=512)
    y_ref = ref.process(channels(x))
    ref.close()
    for block in (1, 7, 4096):
        p = make(plugin_so, az=45.0, dereverb=1.0, late=1.0, block=block)
        y = p.process(channels(x))
        p.close()
        np.testing.assert_allclose(y["Beam Out"], y_ref["Beam Out"], atol=1e-6)
        np.testing.assert_allclose(y["Raw Out"], y_ref["Raw Out"], atol=1e-6)


def test_silence_and_full_scale(plugin_so):
    p = make(plugin_so, dereverb=1.0, late=1.0)
    silent = p.process(channels(np.zeros((9600, 7))))
    loud = p.process(channels(np.ones((9600, 7)) * np.sign(np.sin(np.arange(9600) * 0.3))[:, None]))
    p.close()
    assert np.all(silent["Beam Out"] == 0) and np.all(silent["Raw Out"] == 0)
    assert np.all(np.isfinite(loud["Beam Out"])) and np.all(np.isfinite(loud["Raw Out"]))


def test_cpu_budget(plugin_so):
    """10 s Audio mit beiden Hallstufen. Entwicklungsrechner ≈ 0,11 s; die Grenze 0,25 s (2,5 % eines
    Kerns) lässt langsameren oder ausgelasteten Bauhosts (rpmbuild %check) Luft, fängt aber echte
    Fehler wie einen Neuentwurf je Frame (Sekunden) sicher. Bester von drei Läufen gegen Ausreißer."""
    x = np.random.default_rng(1).standard_normal((480000, 7)) * 0.01
    ins = channels(x)
    runs = []
    for _ in range(3):
        p = make(plugin_so, dereverb=1.0, late=1.0)
        start = time.perf_counter()
        p.process(ins)
        runs.append(time.perf_counter() - start)
        p.close()
    assert min(runs) < 0.25, f"10 s Audio brauchten {', '.join(f'{t:.3f}' for t in runs)} s"
