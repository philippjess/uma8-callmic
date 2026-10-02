from pathlib import Path

import numpy as np
import pytest

from ladspa_host import Plugin
from uma8_callmic.constants import dfn_plugin

DFN = str(dfn_plugin())


def measure(limit_db: float) -> tuple[int, float]:
    """Verzögerung (Samples) und Korrelationsstärke von deep_filter_mono."""
    p = Plugin(DFN, "deep_filter_mono", block=480)
    p.set("Attenuation Limit (dB)", limit_db)
    x = (0.1 * np.random.default_rng(3).standard_normal(48000 * 4)).astype(np.float32)
    y = p.process({"Audio In": x})["Audio Out"]
    p.close()
    a, b = x[48000:].astype(np.float64), y[48000:].astype(np.float64)
    n = 2 * len(a)
    corr = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    lag = int(np.argmax(corr[:48000]))
    strength = corr[lag] / np.sqrt(np.sum(a * a) * np.sum(b * b))
    return lag, float(strength)


@pytest.mark.skipif(not Path(DFN).exists(), reason="DeepFilterNet nicht installiert")
def test_dfn_latency_constant_matches_measurement():
    from uma8_callmic.constants import DFN_LATENCY

    lag6, s6 = measure(6.0)
    lag12, s12 = measure(12.0)
    assert s6 > 0.3 and s12 > 0.3, f"Korrelation zu schwach ({s6:.2f}/{s12:.2f})"
    assert lag6 == lag12, "Latenz hängt von der Dämpfung ab"
    assert lag6 == DFN_LATENCY
