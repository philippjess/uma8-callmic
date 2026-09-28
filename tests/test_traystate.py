from uma8_callmic.config import Config
from uma8_callmic.pwctl import Status
from uma8_callmic.traystate import icon_state, tooltip

OK = Status("raw", True, True, True)


def test_icon_state():
    assert icon_state(OK, True) == "active"
    assert icon_state(OK, False) == "inactive"
    assert icon_state(Status("missing", True, True, True), True) == "error"


def test_tooltip_texts():
    cfg = Config(calibrated=True, calibrated_azimuth=215.0)
    assert "Aktiv" in tooltip(OK, cfg) and "215°" in tooltip(OK, cfg)
    cfg.direction_mode = "tracking"
    assert "automatisch (40°)" in tooltip(OK, cfg, tracked=40.0)
    assert "nicht angeschlossen" in tooltip(Status("missing", True, True, True), cfg)
    assert "Hinweis: kaputt" in tooltip(OK, cfg, warnings=["kaputt"])
    assert "noch nicht kalibriert" in tooltip(OK, Config())
