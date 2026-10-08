from uma8_callmic.config import Config
from uma8_callmic.constants import AEC_PRE_GAIN_DB, DFN_LATENCY, DFN_MIN_BUFFER_FRAMES
from uma8_callmic.params import (MIN_WNG_DB, NULL_KEYS, NULL_WEIGHT_DB, all_params, beam_gain_db, mix_params,
                                 null_params, processing_params, steering_params)


def test_all_params_cover_every_node():
    p = all_params(Config())
    assert p["beam:Center Channel"] == 0.0
    assert [p[f"beam:Ring {k}"] for k in range(6)] == [1.0, 6.0, 5.0, 4.0, 3.0, 2.0]
    assert p["beam:Radius (mm)"] == 43.0
    assert p["beam:Raw Extra Delay (samples)"] == float(DFN_LATENCY)
    assert p["dfn:Attenuation Limit (dB)"] == 30.0
    assert p["dfn:Min Processing Buffer (frames)"] == float(DFN_MIN_BUFFER_FRAMES) == 1.0
    assert DFN_LATENCY == 1440  # 30 ms: 20 ms mit einem Frame Puffer plus ein Frame Mindestpuffer
    assert p["limit:Ceiling (dB)"] == -1.0
    assert p["beam:Gain (dB)"] == 30.0 - AEC_PRE_GAIN_DB == 12.0
    assert {k.split(":")[0] for k in p} == {"beam", "dfn", "mix", "limit"}


def test_steering_modes():
    cfg = Config(calibrated_azimuth=200.0, calibrated_elevation=25.0, manual_azimuth=10.0)
    assert steering_params(cfg)["beam:Azimuth (deg)"] == 200.0
    cfg.direction_mode = "manual"
    assert steering_params(cfg)["beam:Azimuth (deg)"] == 10.0
    cfg.direction_mode = "tracking"
    assert steering_params(cfg)["beam:Azimuth (deg)"] == 200.0
    assert steering_params(cfg, tracked_azimuth=370.0)["beam:Azimuth (deg)"] == 10.0
    cfg.direction_mode = "omni"
    assert steering_params(cfg)["beam:Mode"] == 1.0


def test_beamformer_choice_sets_mode():
    cfg = Config()
    assert steering_params(cfg)["beam:Mode"] == 0.0
    cfg.beamformer = "delay_and_sum"
    assert steering_params(cfg)["beam:Mode"] == 2.0
    cfg.direction_mode = "omni"
    assert steering_params(cfg)["beam:Mode"] == 1.0


def test_dereverb_params():
    p = processing_params(Config())  # Standard: Abklingmodell an, Kohärenzfilter aus (Hörvergleich im Raum)
    assert p["beam:Dereverb"] == 0.0 and p["beam:Late Reverb"] == 1.0
    assert p["beam:Min WNG (dB)"] == MIN_WNG_DB == -3.0
    p = processing_params(Config(dereverb=True, late_reverb=False, dereverb_t60=0.8))
    assert p["beam:Dereverb"] == 1.0 and p["beam:Late Reverb"] == 0.0 and p["beam:Dereverb T60 (s)"] == 0.8


def test_mix_params():
    assert mix_params(True) == {"mix:Gain 1": 1.0, "mix:Gain 2": 0.0}
    assert mix_params(False) == {"mix:Gain 1": 0.0, "mix:Gain 2": 1.0}


def test_gain_is_split_around_echo_cancel():
    """„Verstärkung“ bleibt die Gesamtverstärkung; mit Echounterdrückung liegen 18 dB vor der AEC."""
    assert AEC_PRE_GAIN_DB == 18.0
    for gain in (0.0, 30.0, 60.0):
        assert beam_gain_db(Config(gain_db=gain, echo_cancel=True)) == gain - 18.0
        assert beam_gain_db(Config(gain_db=gain, echo_cancel=False)) == gain
    assert processing_params(Config(gain_db=42.0, echo_cancel=False))["beam:Gain (dB)"] == 42.0
    assert -30.0 <= beam_gain_db(Config(gain_db=0.0)) and beam_gain_db(Config(gain_db=60.0)) <= 60.0  # Port-Bereich


#: all_params vor dem Arbeitsplatz-Profil (Stand b2f0217), Standard-Einstellungen; seit dem DFN-Mindestpuffer
#: (deepfilternet-ladspa 0.5.6-4) mit „Min Processing Buffer“ und 10 ms mehr „Raw Extra Delay“
BEFORE_PROFILE = {
    'beam:Center Channel': 0.0, 'beam:Ring 0': 1.0, 'beam:Ring 1': 6.0, 'beam:Ring 2': 5.0, 'beam:Ring 3': 4.0,
    'beam:Ring 4': 3.0, 'beam:Ring 5': 2.0, 'beam:Ring Offset (deg)': 90.0, 'beam:Radius (mm)': 43.0,
    'beam:Raw Extra Delay (samples)': 1440.0, 'beam:Mode': 0.0, 'beam:Azimuth (deg)': 0.0, 'beam:Elevation (deg)': 20.0,
    'beam:Gain (dB)': 12.0, 'beam:Dereverb': 0.0, 'beam:Dereverb Strength': 0.6, 'beam:Dereverb T60 (s)': 0.5,
    'beam:Late Reverb': 1.0, 'beam:Min WNG (dB)': -3.0, 'dfn:Attenuation Limit (dB)': 30.0,
    'dfn:Min Processing Buffer (frames)': 1.0, 'limit:Ceiling (dB)': -1.0,
    'mix:Gain 1': 1.0, 'mix:Gain 2': 0.0,
}


def test_no_profile_leaves_params_and_chain_unchanged():
    """Ohne eingemessene Lautsprecher: dieselben Controls wie vorher (Reihenfolge eingeschlossen), keine
    Nullstellen in der Kettenkonfiguration – auch mit Sprechzone, Tastatur oder Pegeln im Profil."""
    from pathlib import Path

    from uma8_callmic.chainconf import render

    assert list(all_params(Config()).items()) == list(BEFORE_PROFILE.items())
    partial = Config(talker_zone_deg=40.0, keyboard=[200.0, 5.0], noise_floor_dbfs=-77.0, speech_level_dbfs=-45.0,
                     null_weight_db=10.0)
    assert list(all_params(partial).items()) == list(BEFORE_PROFILE.items())
    tracked = Config(direction_mode="tracking", beamformer="delay_and_sum", echo_cancel=False, active=False)
    expected = {**BEFORE_PROFILE, "beam:Mode": 2.0, "beam:Azimuth (deg)": 123.0, "beam:Gain (dB)": 30.0,
                "mix:Gain 1": 0.0, "mix:Gain 2": 1.0}
    assert list(all_params(tracked, 123.0).items()) == list(expected.items())
    for ec in (True, False):
        assert "Null" not in render(Config(echo_cancel=ec), Path("/x/b.so"), Path("/x/d.so"))
    assert null_params(Config()) == {}


def test_null_params_follow_speakers():
    two = Config(speakers=[[100.0, 5.0], [370.0 - 10.0, 8.0]], null_weight_db=10.0)
    assert null_params(two) == dict(zip(NULL_KEYS, (100.0, 5.0, 0.0, 8.0, 10.0)))
    one = Config(speakers=[[250.0, 3.0]], null_weight_db=10.0)   # ein Lautsprecher: beide Nullstellen gleich
    assert null_params(one) == dict(zip(NULL_KEYS, (250.0, 3.0, 250.0, 3.0, 10.0)))
    off = Config(speakers=[[250.0, 3.0]])                          # eingemessen, Nullstellen aus
    assert null_params(off)["beam:Null Weight (dB)"] == 0.0
    assert null_params(Config(), force=True) == dict.fromkeys(NULL_KEYS, 0.0)
    p = all_params(two)
    assert all(k in p for k in NULL_KEYS) and NULL_WEIGHT_DB == 10.0


def test_null_controls_rendered_with_profile():
    from pathlib import Path

    from uma8_callmic.chainconf import render

    text = render(Config(speakers=[[100.0, 5.0], [260.0, 7.5]], null_weight_db=10.0), Path("/x/b.so"),
                  Path("/x/d.so"))
    assert '"Null 1 Azimuth (deg)" = 100 "Null 1 Elevation (deg)" = 5 "Null 2 Azimuth (deg)" = 260 ' \
           '"Null 2 Elevation (deg)" = 7.5 "Null Weight (dB)" = 10' in text
