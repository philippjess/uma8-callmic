import re
import subprocess
import time

import pytest

from uma8_callmic import constants as K
from uma8_callmic.chainconf import render, write
from uma8_callmic.config import Config


def test_render_fills_all_placeholders():
    text = render(Config())
    assert "@" not in text
    assert f'plugin = "{K.BEAM_PLUGIN}"' in text
    assert '"Azimuth (deg)" = 0' in text
    assert '"Attenuation Limit (dB)" = 30' in text
    assert text.count("{") == text.count("}")
    assert text.count("[") == text.count("]")


def test_render_reflects_active_state():
    on = re.search(r'label = mixer\s+control = \{ (.*?) \}', render(Config(active=True))).group(1)
    off = re.search(r'label = mixer\s+control = \{ (.*?) \}', render(Config(active=False))).group(1)
    assert on == '"Gain 1" = 1 "Gain 2" = 0'
    assert off == '"Gain 1" = 0 "Gain 2" = 1'


def test_source_never_suspends():
    """Jedes Aufwachen leckt einen DeepFilterNet-Thread, also darf die Quelle nicht schlafen."""
    playback = re.search(r'playback\.props = \{(.*?)\}', render(Config()), re.S).group(1)
    assert re.search(r'^\s*session\.suspend-timeout-seconds = 0$', playback, re.M)


def test_write_reports_changes(tmp_path):
    path = tmp_path / "uma8-callmic.conf"
    assert write(Config(), path) is True
    assert write(Config(), path) is False
    assert write(Config(gain_db=40.0), path) is True


@pytest.mark.integration
def test_config_starts_in_pipewire(tmp_path):
    """Probelauf: braucht installiertes Plugin (install.sh) und laufendes PipeWire."""
    path = tmp_path / "probe.conf"
    path.write_text(render(Config()).replace('"uma8_callmic', '"uma8_callmic_probe'))
    proc = subprocess.Popen(["pipewire", "-c", str(path)], stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    proc.terminate()
    _, err = proc.communicate(timeout=5)
    assert "error" not in err.lower(), err
