import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from roomsim import C_SOUND, measured_t60, rirs, sabine_beta  # noqa: E402

ROOM, SRC, MIC = (4.0, 3.5, 2.6), (2.1, 1.1, 1.1), (2.0, 0.5, 0.75)


def test_direct_path_delay_and_amplitude():
    h = rirs(ROOM, SRC, [MIC], 0.3, direct_only=True, hp_hz=0)[0]
    d = np.linalg.norm(np.subtract(SRC, MIC))
    delay = d / C_SOUND * 48000
    assert abs(np.argmax(h) - delay) <= 0.5
    assert np.sum(h) == pytest.approx(1 / (4 * np.pi * d), rel=1e-3)  # Sinc-Kern summiert sich zu 1


@pytest.mark.parametrize("t60", [0.3, 0.5])
def test_reverberation_time_matches_target(t60):
    h = rirs(ROOM, SRC, [MIC], t60)[0]
    assert measured_t60(h) == pytest.approx(t60, rel=0.15)


def test_unreachable_t60_is_rejected():
    with pytest.raises(ValueError):
        sabine_beta(ROOM, 0.05)
