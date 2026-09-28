import numpy as np

from ladspa_host import Plugin


def test_limit_plugin_has_expected_ports(plugin_so):
    p = Plugin(plugin_so, "uma8_limit")
    assert p.names == ["In", "Out", "Ceiling (dB)"]
    p.close()


def test_limit_plugin_caps_loud_sine(plugin_so):
    p = Plugin(plugin_so, "uma8_limit")
    p.set("Ceiling (dB)", -1.0)
    t = np.arange(48000) / 48000
    out = p.process({"In": 2.0 * np.sin(2 * np.pi * 440 * t)})["Out"]
    p.close()
    assert np.max(np.abs(out)) <= 10 ** (-1 / 20) + 1e-5


def test_limit_plugin_quiet_signal_unchanged_after_latency(plugin_so):
    p = Plugin(plugin_so, "uma8_limit")
    p.set("Ceiling (dB)", -1.0)
    x = (0.1 * np.sin(2 * np.pi * 440 * np.arange(9600) / 48000)).astype(np.float32)
    out = p.process({"In": x})["Out"]
    p.close()
    np.testing.assert_allclose(out[240:], x[:-240], atol=1e-6)
