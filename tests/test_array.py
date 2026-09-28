import numpy as np

from uma8_callmic.array import UMA8, ArrayGeometry


def test_uma8_positions_match_odas():
    p = UMA8.positions()
    np.testing.assert_allclose(p[0], [0, 0, 0], atol=1e-9)
    np.testing.assert_allclose(p[1], [0.0, 0.043, 0], atol=1e-3)
    np.testing.assert_allclose(p[2], [0.037, 0.021, 0], atol=1e-3)
    np.testing.assert_allclose(p[6], [-0.037, 0.021, 0], atol=1e-3)


def test_validity():
    assert UMA8.is_valid()
    assert not ArrayGeometry(center=1).is_valid()
    assert not ArrayGeometry(ring=(1, 2, 3)).is_valid()
