import numpy as np

from uma8_callmic.doa import DoaResult
import time

from uma8_callmic.tracker import Tracker, TrackerThread


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
