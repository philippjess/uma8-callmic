import sys
from pathlib import Path

import numpy as np
import pytest

from desksim import SR, Clock, SceneCapture, read_wav, run_sweep
from sim import plane_wave
from test_doa import speech_like
from uma8_callmic import workspace as ws
from uma8_callmic.array import UMA8
from uma8_callmic.doa import angle_diff

P = UMA8.positions()


def scene(dirs, level_db=-50.0, noise_db=-75.0, latency=0.12, seed=0):
    """Rohaufnahme wie bei der Messung: 1 s Grundrauschen, je Richtung ein Stoß (Start = Start von pw-play)."""
    rng = np.random.default_rng(seed)
    pre, b, gap = SR, int(ws.BURST_S * SR), int(0.8 * SR)
    x = rng.standard_normal((pre + len(dirs) * (b + gap), 8)) * 10 ** (noise_db / 20)
    bursts = []
    for i, d in enumerate(dirs):
        s = pre + i * (b + gap)
        if d is not None:
            sig = ws.pink_noise(b, seed=i) * 10 ** (level_db / 20)
            x[s + int(latency * SR):s + int(latency * SR) + b, :7] += plane_wave(sig, P, *d)
        bursts.append((s, s + b))
    return x, bursts, (0, pre)


def test_burst_is_pink_one_channel_and_faded(tmp_path):
    x = ws.burst(1, seed=1)
    assert x.shape == (int(ws.BURST_S * SR), 2) and np.all(x[:, 0] == 0)
    assert abs(20 * np.log10(np.sqrt(np.mean(x[:, 1].astype(float) ** 2))) - ws.BURST_RMS_DBFS) < 0.2
    assert abs(x[0, 1]) < 1e-3 and abs(x[-1, 1]) < 1e-3          # ein- und ausgeblendet, kein Klick
    f = np.fft.rfftfreq(len(x), 1 / SR)
    p = np.abs(np.fft.rfft(x[:, 1])) ** 2
    octave = [p[(f >= lo) & (f < 2 * lo)].sum() for lo in (500, 1000, 2000, 4000)]
    assert np.allclose(np.diff(10 * np.log10(octave)), 0.0, atol=0.5)  # rosa: gleiche Energie je Oktave
    left, right = ws.write_bursts(tmp_path)
    assert "links" in left.name and "rechts" in right.name
    data = read_wav(right)
    assert data.shape == x.shape and np.max(np.abs(data[:, 1] - x[:, 1])) < 1e-4 and not data[:, 0].any()


def test_play_command_uses_default_output():
    cmd = ws.play_command(Path("/tmp/a.wav"))
    assert cmd[0] == "pw-play" and cmd[-1] == "/tmp/a.wav" and "--target" not in cmd


def test_band_level():
    t = np.arange(SR) / SR
    assert abs(ws.band_level_dbfs(np.sin(2 * np.pi * 1000 * t)) + 3.01) < 0.1
    assert ws.band_level_dbfs(np.sin(2 * np.pi * 12000 * t)) < -60       # über dem Sprachband


@pytest.mark.parametrize("dirs", [[(100.0, 5.0), (260.0, 5.0)], [(10.0, 0.0), (350.0, 10.0)],
                                  [(217.0, 8.0), (41.0, 3.0)]])
def test_speakers_found_within_a_degree(dirs):
    x, bursts, noise = scene(dirs)
    m = ws.analyse_speakers(x, bursts, noise, P)
    assert m.ok and len(m.directions) == 2
    for r, (az, el) in zip(m.speakers, dirs):
        assert r.ok and angle_diff(r.azimuth, az) <= 1.0 and abs(r.elevation - el) <= 5.0, r
        assert r.confidence > ws.GOOD_CONFIDENCE
        assert abs(r.level_dbfs - (-50.0 - 3.0)) < 2.0                # Pegel im Sprachband
    assert abs(m.noise_dbfs - (-75.0 + 10 * np.log10(7900 / 24000))) < 1.0
    assert m.levels_dbfs == [r.level_dbfs for r in m.speakers]


def test_no_signal_gives_clear_message():
    x, bursts, noise = scene([None, (260.0, 5.0)])
    m = ws.analyse_speakers(x, bursts, noise, P)
    assert not m.speakers[0].ok and "links" in m.speakers[0].message and "Lautstärke" in m.speakers[0].message
    assert m.speakers[1].ok and m.directions == [[m.speakers[1].azimuth, m.speakers[1].elevation]]
    x, bursts, noise = scene([(100.0, 5.0), (260.0, 5.0)], level_db=-85.0)
    assert not ws.analyse_speakers(x, bursts, noise, P).ok


def test_mono_speaker_gives_one_direction():
    x, bursts, noise = scene([(120.0, 5.0), (124.0, 5.0)])
    m = ws.analyse_speakers(x, bursts, noise, P)
    assert len(m.directions) == 1 and angle_diff(m.directions[0][0], 122.0) <= 1.5
    assert any("ein Lautsprecher" in n for n in m.notes)


def test_loud_speaker_warns_about_clipping():
    x, bursts, noise = scene([(100.0, 5.0), (260.0, 5.0)], level_db=-28.0)
    assert any("abgeschnitten" in n for n in ws.analyse_speakers(x, bursts, noise, P).notes)


def test_speakers_in_reverberant_room():
    """Raumsimulation (Schreibtisch-Szenario aus tools/eval_nulls.py, T60 0,45 s): Azimut auf wenige Grad;
    die hallfeste Elevation liegt näher an der Wahrheit als das SRP-Maximum allein."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    from roomsim import rirs
    from scipy.signal import fftconvolve

    room, array = (4.0, 3.5, 2.6), np.array([1.8, 0.8, 0.75])
    speakers = [(180.0, 5.0), (340.0, 5.0)]
    rng = np.random.default_rng(3)
    pre, b, gap = SR, int(ws.BURST_S * SR), int(0.8 * SR)
    x = rng.standard_normal((pre + 2 * (b + gap), 7)) * 10 ** (-75 / 20)
    bursts = []
    for i, (az, el) in enumerate(speakers):
        a, e = np.radians(az), np.radians(el)
        src = array + 0.55 * np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])
        h = rirs(room, src, array + P, 0.45)
        sig = ws.pink_noise(b, seed=i) * 0.05
        y = np.stack([fftconvolve(sig, h[m]) for m in range(7)], 1)
        s = pre + i * (b + gap)
        y = y[:len(x) - s - 4800]
        x[s + 4800:s + 4800 + len(y)] += y
        bursts.append((s, s + b))
    m = ws.analyse_speakers(x, bursts, (0, pre), P)
    srp = ws.SrpPhat(P, SR, ws.NFFT, *ws.SPEAKER_BAND, elevations=ws.SPEAKER_ELEVATIONS)
    for r, (az, el) in zip(m.speakers, speakers):
        assert r.ok and angle_diff(r.azimuth, az) <= 3.0, r
        assert r.elevation <= 15.0, r
    # Vergleich: Elevation nur aus dem SRP-Maximum (bei gleichem Azimut) liegt höher
    cs = srp.cross_spectra(x[bursts[0][0] + 4800:bursts[0][1]])
    els = np.arange(0.0, 87.5, 2.5)
    plain = els[np.argmax(srp.srp_at(cs, np.full(els.shape, m.speakers[0].azimuth), els))]
    assert ws.elevation_fit(srp, cs, m.speakers[0].azimuth, els) < plain


def test_keyboard_direction_from_typing():
    clock = Clock(10.0)
    cap = SceneCapture(clock, P, seconds=10.0)
    cap.add_typing(213.0, 5.0, 1.0, 5.0)
    r = ws.analyse_keyboard(cap.span(0, int(7 * SR)), P)
    assert r.ok and angle_diff(r.azimuth, 213.0) <= 3.0, r
    quiet = SceneCapture(Clock(10.0), P, seconds=10.0)
    r = ws.analyse_keyboard(quiet.span(0, int(7 * SR)), P)
    assert not r.ok and "Kein Tippen" in r.message


def test_sweep_end_to_end_with_simulated_playback(tmp_path):
    clock = Clock()
    cap = SceneCapture(clock, P, sources=[(100.0, 5.0), (260.0, 5.0)])
    sweep = ws.SpeakerSweep(cap, ws.write_bursts(tmp_path), P, player=cap.play)
    texts = set()
    run_sweep(sweep, clock, tick=lambda t: (sweep.step(t), texts.add(sweep.status())))
    assert sweep.error is None and cap.played == [0, 1]
    assert [angle_diff(d[0], az) <= 1.0 for d, az in zip(sweep.result.directions, (100.0, 260.0))] == [True, True]
    assert any("Grundrauschen" in t for t in texts) and any("links" in t for t in texts)
    assert any("rechts" in t for t in texts) and sweep.progress() == 1.0
    s0, e0 = sweep.bursts[0]
    assert sweep.noise[1] <= s0 and e0 - s0 >= ws.BURST_S * SR    # Grundrauschen vor dem ersten Stoß


def test_sweep_errors(tmp_path):
    files = ws.write_bursts(tmp_path)
    clock = Clock()
    cap = SceneCapture(clock, P, play_code=1)
    sweep = ws.SpeakerSweep(cap, files, P, player=cap.play)
    run_sweep(sweep, clock)
    assert "Wiedergabe fehlgeschlagen" in sweep.error

    class Dead(SceneCapture):
        def total(self):
            return 0
    clock = Clock()
    sweep = ws.SpeakerSweep(Dead(clock, P), files, P, player=lambda p: pytest.fail("darf nicht spielen"))
    run_sweep(sweep, clock)
    assert "Keine Daten vom UMA-8" in sweep.error

    clock = Clock()
    cap = SceneCapture(clock, P, sources=[None, None])
    sweep = ws.SpeakerSweep(cap, files, P, player=cap.play)
    run_sweep(sweep, clock)
    assert sweep.result is not None and not sweep.result.ok and "nichts zu hören" in sweep.error

    class Hangs:
        def poll(self):
            return None

        def kill(self):
            self.killed = True
    clock, hang = Clock(), Hangs()
    cap = SceneCapture(clock, P)
    sweep = ws.SpeakerSweep(cap, files, P, player=lambda p: hang)
    run_sweep(sweep, clock, until=30.0)
    assert "hängt" in sweep.error and hang.killed


def test_placement_hints():
    assert "Sprich" in ws.placement_hints(None, -75.0, -50.0)[0].text
    good = ws.placement_hints(-40.0, -75.0, -55.0)
    assert all(h.good for h in good) and len(good) == 2
    near = ws.placement_hints(-50.0, -75.0, -55.0)
    assert not near[0].good and "Mikrofon näher zu dir" in near[0].text
    bad = ws.placement_hints(-50.0, -55.0, -48.0)
    assert "Gegensprechen" in bad[0].text and "Grundrauschen" in bad[1].text
    assert "nicht gemessen" in ws.placement_hints(-50.0, None, None)[0].text


def test_profile_notes():
    assert ws.profile_notes(None, [[100.0, 5.0]]) == []
    notes = ws.profile_notes(200.0, [[215.0, 5.0], [300.0, 5.0]], [205.0, 2.0])
    assert any("Sprechrichtung (215°)" in n for n in notes) and any("Tastatur 5°" in n for n in notes)


def test_live_analysis_speech_noise_and_map():
    rng = np.random.default_rng(0)
    live = ws.LiveAnalysis(P, accept=lambda az: angle_diff(az, 215.0) <= 45.0)
    noise = lambda: rng.standard_normal((9600, 7)) * 10 ** (-75 / 20)  # noqa: E731
    for _ in range(10):
        live.feed(noise())
    assert live.speech_dbfs() is None and abs(live.noise_dbfs() - (-75 + 10 * np.log10(7900 / 24000))) < 1.5
    for i in range(6):  # Lautsprecher-Sprache zählt nicht als eigene Sprache
        live.feed(noise() + plane_wave(0.003 * speech_like(9600), P, 100.0, 5.0))
    assert live.speech_dbfs() is None
    assert angle_diff(live.azimuths[np.argmax(live.map_norm())], 100.0) <= 10.0
    talk = plane_wave(0.003 * speech_like(9600), P, 215.0, 25.0)
    for i in range(6):
        live.feed(noise() + talk)
    assert abs(live.speech_dbfs() - ws.band_level_dbfs(talk[:, 0])) < 1.0
    assert live.activity() == 1.0 and live.map_norm().max() == 1.0
