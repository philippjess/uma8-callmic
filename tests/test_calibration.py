from uma8_callmic.calibration import evaluate
from uma8_callmic.doa import DoaResult


def r(az, conf=0.3, el=20.0):
    return DoaResult(az, el, conf, 0.5)


def test_consistent_results_are_accepted():
    out = evaluate([r(210), r(215), r(220), r(212), r(218)])
    assert out.ok and abs(out.azimuth - 215) < 2 and "215°" in out.message


def test_too_few_blocks():
    out = evaluate([r(210)] * 3)
    assert not out.ok and "Zu wenig Sprache" in out.message


def test_scattered_results_are_rejected():
    out = evaluate([r(0), r(90), r(180), r(270), r(45)])
    assert not out.ok and "nicht eindeutig" in out.message


def test_low_confidence_is_rejected():
    out = evaluate([r(100, conf=0.01)] * 6)
    assert not out.ok
