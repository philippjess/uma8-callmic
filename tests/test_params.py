from uma8_callmic.config import Config
from uma8_callmic.constants import DFN_LATENCY
from uma8_callmic.params import MIN_WNG_DB, all_params, mix_params, processing_params, steering_params


def test_all_params_cover_every_node():
    p = all_params(Config())
    assert p["beam:Center Channel"] == 0.0
    assert [p[f"beam:Ring {k}"] for k in range(6)] == [1.0, 6.0, 5.0, 4.0, 3.0, 2.0]
    assert p["beam:Radius (mm)"] == 43.0
    assert p["beam:Raw Extra Delay (samples)"] == float(DFN_LATENCY)
    assert p["dfn:Attenuation Limit (dB)"] == 30.0
    assert p["limit:Ceiling (dB)"] == -1.0
    assert p["beam:Gain (dB)"] == 30.0
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
    p = processing_params(Config())
    assert p["beam:Dereverb"] == 1.0 and p["beam:Late Reverb"] == 1.0
    assert p["beam:Min WNG (dB)"] == MIN_WNG_DB == -3.0
    p = processing_params(Config(dereverb=False, late_reverb=False, dereverb_t60=0.8))
    assert p["beam:Dereverb"] == 0.0 and p["beam:Late Reverb"] == 0.0 and p["beam:Dereverb T60 (s)"] == 0.8


def test_mix_params():
    assert mix_params(True) == {"mix:Gain 1": 1.0, "mix:Gain 2": 0.0}
    assert mix_params(False) == {"mix:Gain 1": 0.0, "mix:Gain 2": 1.0}
