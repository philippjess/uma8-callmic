"""Schätzfunktionen von tools/latency_probe.py an synthetischen Signalen mit bekanntem Versatz."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from uma8_callmic import constants as K

_spec = importlib.util.spec_from_file_location(
    "latency_probe", Path(__file__).resolve().parents[1] / "tools/latency_probe.py")
probe = sys.modules["latency_probe"] = importlib.util.module_from_spec(_spec)  # dataclasses brauchen den Eintrag
_spec.loader.exec_module(probe)

SR = probe.SR
LAG, WIN = SR, 6 * SR  # 1 s Vorlauf, 6 s Fenster


def speechlike(n: int, rng: np.random.Generator) -> np.ndarray:
    """Hüllkurve wie Sprache: 80–300 ms laute Abschnitte (−30 … −10 dBFS), dazwischen 50–400 ms Grundrauschen."""
    env = np.full(n, 10 ** (-70 / 20))
    i = 0
    while i < n:
        i += int(rng.integers(SR // 20, 2 * SR // 5))
        length = int(rng.integers(2 * SR // 25, 3 * SR // 10))
        env[i:i + length] = 10 ** (rng.uniform(-30, -10) / 20)
        i += length
    k = np.hanning(SR // 100)
    return np.convolve(env, k / k.sum(), "same")


def delayed(x: np.ndarray, d: int) -> np.ndarray:
    return np.concatenate([np.zeros(d), x[:len(x) - d]])


def test_sample_lag_is_sample_exact_for_gain_stage():
    rng = np.random.default_rng(1)
    ref = 0.01 * rng.standard_normal(LAG + WIN)
    out = 8.0 * delayed(ref, 1234)[LAG:] + 1e-3 * rng.standard_normal(WIN)  # +18 dB, etwas eigenes Rauschen
    lag = probe.sample_lag(ref, out, LAG)
    assert lag.samples == 1234
    assert lag.strength > 0.9


def test_sample_lag_zero_and_silence():
    rng = np.random.default_rng(2)
    ref = rng.standard_normal(LAG + WIN)
    assert probe.sample_lag(ref, ref[LAG:], LAG).samples == 0
    silent = probe.sample_lag(ref, np.zeros(WIN), LAG)
    assert silent.samples is None and silent.note == "Stille"


def nonlinear_output(rng: np.random.Generator, d: int) -> tuple[np.ndarray, np.ndarray]:
    """Roh-Mikrofon und eine Ausgabe wie hinter Beamforming + DeepFilterNet + Begrenzer: gleiche Hüllkurve um d
    verzögert, aber eigener Träger (nicht sampelweise korreliert), Pausen um 40 dB abgesenkt, gesättigt."""
    env = speechlike(LAG + WIN, rng)
    ref = env * rng.standard_normal(LAG + WIN)
    env_out = delayed(env, d)
    out = env_out * rng.standard_normal(LAG + WIN)
    out = np.where(env_out > 10 ** (-40 / 20), out, 0.01 * out)
    out = np.tanh(20 * out) / 20
    return ref, out[LAG:]


def test_envelope_lag_finds_delay_of_gated_nonlinear_output():
    rng = np.random.default_rng(3)
    d = K.BEAM_LATENCY + K.DFN_LATENCY + K.LIMIT_LATENCY + 4800  # Kette plus 100 ms Puffer
    ref, out = nonlinear_output(rng, d)
    lag = probe.envelope_lag(ref, out, LAG)
    assert lag.samples is not None, lag
    assert abs(lag.samples - d) <= probe.ENV_HOP, f"{lag.ms:.2f} ms statt {d * 1000 / SR:.2f} ms"
    assert lag.strength > 0.5
    # Der Samplevergleich findet hier nichts: eigener Träger
    assert probe.sample_lag(ref, out, LAG).samples is None


def test_envelope_lag_marks_stationary_noise_as_silence():
    rng = np.random.default_rng(4)
    ref = 0.001 * rng.standard_normal(LAG + WIN)
    lag = probe.envelope_lag(ref, 0.5 * delayed(ref, 2000)[LAG:], LAG)
    assert lag.samples is None and lag.note == "Stille"


def test_analyze_measures_each_stage_against_raw():
    rng = np.random.default_rng(5)
    ref, out = nonlinear_output(rng, 3000)
    pre = 8.0 * ref
    aec = delayed(pre, 480)
    seg = np.column_stack([ref, pre, aec, np.concatenate([np.zeros(LAG), out])])
    stages = [probe.Stage("Vorverstärkung", "p", False), probe.Stage("Echounterdrückung", "a", False),
              probe.Stage("Ausgang", "o", True)]
    lags = probe.analyze(seg, stages, LAG)
    assert [lags[0].samples, lags[1].samples] == [0, 480]
    assert abs(lags[2].samples - 3000) <= probe.ENV_HOP


RAW = K.RAW_DEVICE
ALL_PORTS = {f"{RAW}:capture_FL", f"{RAW}:capture_FR", f"{K.PRE_NODE}:capture_AUX0", f"{K.AEC_NODE}:capture_AUX0",
             f"{K.SOURCE_NODE}:capture_MONO", f"{K.CAPTURE_NODE}:monitor_AUX0"}


def test_plan_stages_with_and_without_echo_cancel():
    raw_port, stages = probe.plan_stages(ALL_PORTS, RAW)
    assert raw_port == f"{RAW}:capture_FL"
    assert [(s.port, s.envelope) for s in stages] == [
        (f"{K.PRE_NODE}:capture_AUX0", False), (f"{K.AEC_NODE}:capture_AUX0", False),
        (f"{K.SOURCE_NODE}:capture_MONO", True)]
    plain = {p for p in ALL_PORTS if not p.startswith((K.PRE_NODE + ":", K.AEC_NODE + ":"))}
    assert [s.label for s in probe.plan_stages(plain, RAW)[1]] == ["Ausgang"]
    # Raw-Quelle nach schneller Neuanmeldung mit Suffix (pwctl.find_raw_source)
    renamed = {p.replace(RAW, RAW + ".9") for p in ALL_PORTS}
    assert probe.plan_stages(renamed, RAW + ".9")[0] == f"{RAW}.9:capture_FL"


def test_plan_stages_requires_raw_and_output():
    with pytest.raises(probe.ProbeError, match="Raw-Quelle"):
        probe.plan_stages(ALL_PORTS, None)
    with pytest.raises(probe.ProbeError, match="Raw-Quelle"):
        probe.plan_stages({p for p in ALL_PORTS if not p.startswith(RAW)}, RAW)
    with pytest.raises(probe.ProbeError, match="UMA-8 Call Mic"):
        probe.plan_stages(ALL_PORTS - {f"{K.SOURCE_NODE}:capture_MONO"}, RAW)


def test_report_checks_median_against_expect_max(capsys):
    stages = [probe.Stage("Echounterdrückung", "a", False), probe.Stage("Ausgang", "o", True)]
    ms = lambda v: probe.Lag(v * SR / 1000, 0.9)  # noqa: E731
    rows = [[ms(10), ms(56)], [ms(10), ms(58)], [ms(10), probe.Lag(None, 0.0, "Stille")], [ms(10), ms(300)]]
    assert probe.report(stages, rows, None) == 0
    assert probe.report(stages, rows, 60) == 0  # Median 58 ms, der Ausreißer zählt nicht
    assert probe.report(stages, rows, 50) == 1
    silent = [[ms(10), probe.Lag(None, 0.0, "Stille")]]
    assert probe.report(stages, silent, 60) == 1
    text = capsys.readouterr().out
    expected = 10 + (K.BEAM_LATENCY + K.DFN_LATENCY + K.LIMIT_LATENCY) * 1000 / SR
    assert f"= {expected:.1f} ms" in text
    assert "nicht prüfbar" in text
