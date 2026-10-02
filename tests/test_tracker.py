import numpy as np

from uma8_callmic.doa import DoaResult
import time

from uma8_callmic.tracker import Tracker, TrackerThread, Zone, zone_for


class FakeEstimator:
    def __init__(self, results):
        self.results = list(results)

    def estimate(self, block):
        return self.results.pop(0)


class AlwaysSpeech:
    def is_speech(self, mono):
        return True


def run(results, initial=0.0):
    applied = []
    t = Tracker(FakeEstimator(results), AlwaysSpeech(), applied.append, initial_azimuth=initial)
    block = np.zeros((9600, 8))
    outs = [t.feed(block) for _ in results]
    return applied, outs, t


def test_moves_after_consistent_estimates():
    applied, outs, t = run([DoaResult(90, 20, 0.3, 0.5)] * 3)
    assert outs[:2] == [None, None]
    assert abs(applied[0] - 90) < 1e-6 and abs(t.current - 90) < 1e-6


def test_small_changes_are_ignored():
    applied, _, _ = run([DoaResult(10, 20, 0.3, 0.5)] * 4)
    assert applied == []


def test_low_confidence_is_ignored():
    applied, _, _ = run([DoaResult(180, 20, 0.01, 0.5)] * 5)
    assert applied == []


def test_wraparound_mean():
    applied, _, _ = run([DoaResult(a, 20, 0.3, 0.5) for a in (170, 190, 180)], initial=0.0)
    assert abs(applied[0] - 180) < 1e-6


def test_thread_ignores_dead_capture():
    """Endet die Aufnahme (Quelle weg), wird der letzte Puffer nicht immer wieder ausgewertet."""
    class DeadCapture:
        alive = False

        def latest(self, frames):
            raise AssertionError("darf nicht gelesen werden")

    fed = []

    class Recorder:
        def feed(self, block):
            fed.append(block)

    th = TrackerThread(DeadCapture(), Recorder(), interval=0.01)
    th.start()
    time.sleep(0.1)
    th.stop()
    th.join(1)
    assert fed == []


def test_tracker_works_with_seven_channels():
    """Die AEC-Quelle liefert nur die 7 Mikrofone (ohne Kanal 7)."""
    class Echo:
        def estimate(self, block):
            assert block.shape[1] == 7
            return DoaResult(90, 20, 0.3, 0.5)

    applied = []
    t = Tracker(Echo(), AlwaysSpeech(), applied.append, center=6)
    for _ in range(3):
        t.feed(np.zeros((9600, 7)))
    assert applied == [90.0]


def test_zone_accepts_inside_and_excludes_speakers():
    z = Zone(center=200.0, half_width=40.0, avoid=(110.0, 230.0))
    assert z.accepts(200.0) and z.accepts(170.0) and not z.accepts(155.0) and not z.accepts(245.0)
    assert not z.accepts(215.0) and z.accepts(209.0)      # ±20° um den Lautsprecher bei 230°
    free = Zone(avoid=(100.0,))
    assert free.accepts(0.0) and free.accepts(300.0) and not free.accepts(115.0)


def test_zone_for_config():
    from uma8_callmic.config import Config

    assert zone_for(Config()) is None                                   # kein Profil: wie bisher
    assert zone_for(Config(talker_zone_deg=40.0)) is None               # ohne Kalibrierung keine Mitte
    z = zone_for(Config(calibrated=True, calibrated_azimuth=200.0, talker_zone_deg=40.0))
    assert z == Zone(200.0, 40.0, ())
    z = zone_for(Config(calibrated=True, calibrated_azimuth=200.0, speakers=[[100.0, 5.0], [215.0, 5.0]]))
    assert z.half_width == 180.0 and z.avoid == (100.0,)               # Lautsprecher in Sprechrichtung zählt nicht
    assert zone_for(Config(speakers=[[100.0, 5.0]])).avoid == (100.0,)


def test_tracker_ignores_estimates_outside_zone():
    applied = []
    est = FakeEstimator([DoaResult(a, 20, 0.3, 0.5) for a in (110, 112, 108, 300, 300, 300, 205, 210, 207)])
    t = Tracker(est, AlwaysSpeech(), applied.append, initial_azimuth=180.0,
                zone=Zone(200.0, 40.0, avoid=(110.0,)))
    for _ in range(9):
        t.feed(np.zeros((9600, 8)))
    # Lautsprecher und Richtung außerhalb verworfen, erst die Sprecher-Schätzungen bewegen den Strahl
    assert len(t.history) == 3 and len(applied) == 1 and abs(applied[0] - 207.3) < 0.1
    t.zone = None                                          # Profil gelöscht: wieder alles
    t.history.clear()
    t.estimator = FakeEstimator([DoaResult(300, 20, 0.3, 0.5)] * 3)
    for _ in range(3):
        t.feed(np.zeros((9600, 8)))
    assert applied[-1] == 300.0
