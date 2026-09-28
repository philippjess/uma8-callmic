import numpy as np
import pytest

from sim import band_noise, plane_wave
from uma8_callmic.array import UMA8
from uma8_callmic.doa import SrpPhat, VoiceDetector, angle_diff, circular_mean


@pytest.fixture(scope="module")
def srp():
    return SrpPhat(UMA8.positions())


@pytest.mark.parametrize("az", range(0, 360, 30))
def test_srp_finds_azimuth(srp, az):
    rng = np.random.default_rng(az)
    x = plane_wave(band_noise(9600, 300, 6000, seed=az), UMA8.positions(), az, 20.0)
    x += 0.1 * rng.standard_normal(x.shape)
    r = srp.estimate(x)
    assert angle_diff(r.azimuth, az) < 15.0, r
    assert r.confidence > 0.05


def test_angle_helpers():
    assert angle_diff(350, 10) == 20
    assert angle_diff(circular_mean([350, 10, 0]), 0) < 1e-6
    assert abs(circular_mean([80, 100]) - 90) < 1e-6


def speech_like(n):
    t = np.arange(n) / 48000
    f0 = 140 + 20 * np.sin(2 * np.pi * 3 * t)
    phase = 2 * np.pi * np.cumsum(f0) / 48000
    voiced = sum(np.sin(k * phase) / k for k in range(1, 20))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t) ** 2
    return voiced * envelope


def test_voice_detector():
    rng = np.random.default_rng(0)
    vad = VoiceDetector()
    for _ in range(5):
        assert not vad.is_speech(0.001 * rng.standard_normal(9600))
    assert vad.is_speech(0.02 * speech_like(9600))
    assert not vad.is_speech(0.001 * rng.standard_normal(9600))
