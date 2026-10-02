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
    "Dereverb", "Dereverb Strength", "Dereverb T60 (s)", "Raw Extra Delay (samples)", "Late Reverb", "Min WNG (dB)",
    "Null 1 Azimuth (deg)", "Null 1 Elevation (deg)", "Null 2 Azimuth (deg)", "Null 2 Elevation (deg)",
    "Null Weight (dB)"]
)
SD, OMNI, DS = 0.0, 1.0, 2.0
# Szenario Schreibtisch: Sprecher Az 80°/El 30°, Lautsprecher ±100° daneben unter El 5°
TALKER, SPEAKERS = (80.0, 30.0), [(180.0, 5.0), (340.0, 5.0)]
NULL_WEIGHT = 10.0      # empfohlenes „Null Weight (dB)“ (tools/eval_nulls.py)
NULL_SETTLE = 9600      # Nullstellen wirken nach Entwurf (≈ 10 Hops) und Überblendung (10 Hops)


def set_nulls(p, nulls, weight):
    for i, (az, el) in enumerate(nulls):
        p.set(f"Null {i + 1} Azimuth (deg)", az)
        p.set(f"Null {i + 1} Elevation (deg)", el)
    p.set("Null Weight (dB)", weight)


def make(plugin_so, az=0.0, el=0.0, mode=SD, dereverb=0.0, late=0.0, strength=0.6, raw_extra=0.0, block=512,
         nulls=None, null_weight=0.0):
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
    if nulls is not None:
        set_nulls(p, nulls, null_weight)
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


def test_null_ports_default_to_off(plugin_so):
    p = Plugin(plugin_so, "uma8_beam")
    hints = {n: p.d.port_range_hints[PORTS.index(n)] for n in PORTS[-5:]}
    assert [(h.lower, h.upper) for h in hints.values()] == [(0.0, 360.0), (0.0, 90.0), (0.0, 360.0), (0.0, 90.0),
                                                             (0.0, 40.0)]
    assert all(p.bufs[n][0] == 0.0 for n in hints)  # Standard 0, „Null Weight“ 0 = aus
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


@pytest.mark.timing
def test_cpu_budget(plugin_so):
    """10 s Audio mit beiden Hallstufen. Entwicklungsrechner ≈ 0,11 s; die Grenze 0,25 s (2,5 % eines
    Kerns) fängt echte Fehler wie einen Neuentwurf je Frame (Sekunden) sicher. Bester von drei Läufen gegen
    Ausreißer. In den Paket-Builds abgewählt (Marker „timing“): Bauhosts können beliebig langsam sein."""
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


@pytest.mark.parametrize("mode", [SD, OMNI, DS])
def test_nulls_off_is_bit_identical(plugin_so, mode):
    """„Null Weight“ 0 (Standard) mit beliebigen Nullrichtungen sowie Nullstellen in Delay-and-Sum und
    Mittel-Mikrofon ändern kein Bit, auch über Richtungs- und Moduswechsel hinweg."""
    x = plane_wave(0.1 * band_noise(36000, 100, 16000, seed=3), UMA8.positions(), 180.0, 5.0)
    x += 0.02 * diffuse_noise(36000, UMA8.positions(), n_sources=40, seed=4)

    def run_with(nulls, weight):
        p = make(plugin_so, az=TALKER[0], el=TALKER[1], mode=mode, dereverb=1.0, late=1.0, block=480,
                 nulls=nulls, null_weight=weight)
        a = p.process(channels(x[:12000]))
        p.set("Azimuth (deg)", 120.0)
        b = p.process(channels(x[12000:24000]))
        p.set("Mode", DS if mode == SD else SD)
        c = p.process(channels(x[24000:]))
        p.close()
        return np.concatenate([np.concatenate([o["Beam Out"], o["Raw Out"]]) for o in (a, b, c)])

    ref = run_with(None, 0.0)
    for nulls, weight in [(SPEAKERS, 0.0), ([(33.0, 12.0), (290.0, 80.0)], 0.0)]:
        assert np.array_equal(run_with(nulls, weight).view(np.uint32), ref.view(np.uint32))
    if mode != SD:
        p = make(plugin_so, az=TALKER[0], el=TALKER[1], mode=mode, nulls=SPEAKERS, null_weight=NULL_WEIGHT)
        y = p.process(channels(x[:12000]))["Beam Out"]
        p.close()
        p = make(plugin_so, az=TALKER[0], el=TALKER[1], mode=mode)
        assert np.array_equal(y, p.process(channels(x[:12000]))["Beam Out"])
        p.close()


def band_level_db(y: np.ndarray, lo: float, hi: float) -> float:
    f, p = welch(y.astype(np.float64), 48000, nperseg=2048)
    return 10 * np.log10(p[(f >= lo) & (f < hi)].sum())


@pytest.mark.parametrize("speaker", SPEAKERS)
def test_null_attenuates_speaker_plane_wave(plugin_so, speaker):
    """Ebene Welle aus einer Lautsprecherrichtung, genau und 5° daneben: 1–4 kHz deutlich leiser als
    ohne Nullstellen; darunter wenig (WNG-begrenzt), über 6 kHz unverändert."""
    for d_az in (0.0, 5.0):
        x = plane_wave(0.1 * band_noise(48000, 100, 16000, seed=8), UMA8.positions(), speaker[0] + d_az, speaker[1])
        out = {}
        for weight in (0.0, NULL_WEIGHT):
            p = make(plugin_so, az=TALKER[0], el=TALKER[1], nulls=SPEAKERS, null_weight=weight)
            out[weight] = p.process(channels(x))["Beam Out"][NULL_SETTLE + BEAM_LATENCY:]
            p.close()
        gain = {band: band_level_db(out[NULL_WEIGHT], *band) - band_level_db(out[0.0], *band)
                for band in [(1000, 2000), (2000, 4000), (7000, 12000)]}
        need = 20.0 if d_az == 0 else 10.0
        assert gain[(1000, 2000)] <= -need and gain[(2000, 4000)] <= -need, f"{d_az}°: {gain}"
        assert abs(gain[(7000, 12000)]) < 0.01, gain


def test_talker_undistorted_with_nulls(plugin_so):
    """Verzerrungsfrei zur Zielrichtung auch mit Nullstellen (wie ohne: Ausgang = Mittel-Mikrofon)."""
    x = plane_wave(0.1 * band_noise(36000, 100, 16000, seed=2), UMA8.positions(), *TALKER)
    p = make(plugin_so, az=TALKER[0], el=TALKER[1], nulls=SPEAKERS, null_weight=NULL_WEIGHT)
    y = p.process(channels(x))["Beam Out"].astype(np.float64)
    p.close()
    ref = x[:-BEAM_LATENCY, UMA8.center]
    err = y[BEAM_LATENCY:][4800:] - ref[4800:]
    assert 10 * np.log10(np.sum(err ** 2) / np.sum(ref[4800:] ** 2)) < -40


def test_null_change_is_click_free(plugin_so):
    """Nullstellen an, verschoben, aus: über ≈ 50 ms übergeblendet, kein Sprung im Ausgang."""
    t = np.arange(72000) / 48000
    x = plane_wave(np.sin(2 * np.pi * 1000 * t), UMA8.positions(), 190.0, 5.0)
    p = make(plugin_so, az=TALKER[0], el=TALKER[1], dereverb=1.0, nulls=SPEAKERS, null_weight=0.0)
    parts = [p.process(channels(x[:12000]))["Beam Out"]]
    p.set("Null Weight (dB)", NULL_WEIGHT)
    parts.append(p.process(channels(x[12000:36000]))["Beam Out"])
    p.set("Null 1 Azimuth (deg)", 200.0)
    parts.append(p.process(channels(x[36000:54000]))["Beam Out"])
    p.set("Null Weight (dB)", 0.0)
    parts.append(p.process(channels(x[54000:]))["Beam Out"])
    p.close()
    y = np.concatenate(parts)
    rms = lambda a, b: np.sqrt(np.mean(y[a:b] ** 2))  # noqa: E731
    assert rms(30000, 36000) < 0.5 * rms(6000, 12000), "Nullstelle wirkt nicht"
    y = y[BEAM_LATENCY + 100:]
    assert np.max(np.abs(np.diff(y))) <= 2 * np.pi * 1000 / 48000 * 1.1 * np.max(np.abs(y))


@pytest.mark.timing
def test_cpu_budget_with_null_changes(plugin_so):
    """10 s Audio, Nullstellen an und alle 0,5 s verschoben (je ≈ 1,5 ms Entwurf, verteilt): nicht
    wesentlich teurer als ohne (Grenze wie test_cpu_budget)."""
    x = np.random.default_rng(1).standard_normal((480000, 7)) * 0.01
    runs = []
    for _ in range(3):
        p = make(plugin_so, dereverb=1.0, late=1.0, block=480, nulls=SPEAKERS, null_weight=NULL_WEIGHT)
        elapsed = 0.0
        for i, start in enumerate(range(0, 480000, 24000)):
            p.set("Null 1 Azimuth (deg)", 180.0 + 3 * (i % 5))
            ins = channels(x[start:start + 24000])
            t0 = time.perf_counter()
            p.process(ins)
            elapsed += time.perf_counter() - t0
        runs.append(elapsed)
        p.close()
    assert min(runs) < 0.25, f"10 s Audio brauchten {', '.join(f'{t:.3f}' for t in runs)} s"
