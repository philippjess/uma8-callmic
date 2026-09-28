import numpy as np
import pytest

from sim import diffuse_noise
from uma8_callmic.array import UMA8
from uma8_callmic.geometry import check


@pytest.fixture(scope="module")
def diffuse():
    x = diffuse_noise(48000 * 4, UMA8.positions(), seed=1)
    return x + 0.05 * np.random.default_rng(2).standard_normal(x.shape)


def test_default_mapping_is_confirmed(diffuse):
    res = check(diffuse, UMA8)
    assert res.matches_default, res.message
    assert res.contrast > 0.05


def test_permuted_channels_are_detected(diffuse):
    perm = [3, 0, 5, 1, 6, 2, 4]  # physischer Kanal i landet auf Aufnahme-Kanal perm[i]
    shuffled = np.empty_like(diffuse)
    for i, p in enumerate(perm):
        shuffled[:, p] = diffuse[:, i]
    res = check(shuffled, UMA8)
    assert not res.matches_default
    assert res.detected.center == perm[UMA8.center]
    expected = {frozenset((perm[UMA8.ring[k]], perm[UMA8.ring[(k + 1) % 6]])) for k in range(6)}
    got = {frozenset((res.detected.ring[k], res.detected.ring[(k + 1) % 6])) for k in range(6)}
    assert got == expected


def test_incoherent_noise_is_rejected():
    x = np.random.default_rng(0).standard_normal((48000 * 2, 8))
    res = check(x, UMA8)
    assert res.detected is None and "Standardzuordnung bleibt" in res.message
