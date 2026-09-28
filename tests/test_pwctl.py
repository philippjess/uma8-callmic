import pytest

from uma8_callmic import pwctl
from uma8_callmic.pwctl import Status

OBJS = [
    {"id": 31, "type": "PipeWire:Interface:Device",
     "info": {"props": {"device.vendor.id": "0x2752", "device.product.id": "0x001d"}}},
    {"id": 77, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "uma8_callmic_capture"}}},
    {"id": 78, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "uma8_callmic"}}},
    {"id": 90, "type": "PipeWire:Interface:Link", "info": {}},
]


def test_find_node():
    assert pwctl.find_node(OBJS, "uma8_callmic_capture") == 77
    assert pwctl.find_node(OBJS, "fehlt") is None


def test_device_state():
    assert pwctl.device_state(OBJS) == "raw"
    dsp = [{"id": 1, "type": "PipeWire:Interface:Device",
            "info": {"props": {"device.vendor.id": "0x2752", "device.product.id": "0x001c"}}}]
    assert pwctl.device_state(dsp) == "dsp"
    assert pwctl.device_state([]) == "missing"


def test_format_params():
    s = pwctl.format_params({"beam:Azimuth (deg)": 90.0, "mix:Gain 1": 1})
    assert s == '{ params = [ "beam:Azimuth (deg)" 90 "mix:Gain 1" 1 ] }'


def test_set_params_calls_pw_cli(monkeypatch):
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(pwctl, "_run", lambda args, timeout=5.0: calls.append(args) or Done())
    pwctl.set_params(77, {"beam:Mode": 1.0})
    assert calls == [["pw-cli", "set-param", "77", "Props", '{ params = [ "beam:Mode" 1 ] }']]


def test_set_params_raises_on_error(monkeypatch):
    class Fail:
        returncode, stdout, stderr = 1, "", "Error: unknown object"

    monkeypatch.setattr(pwctl, "_run", lambda args, timeout=5.0: Fail())
    with pytest.raises(RuntimeError, match="unknown object"):
        pwctl.set_params(77, {"beam:Mode": 1.0})


def test_fade_mix_ends_at_target(monkeypatch):
    seen = []
    monkeypatch.setattr(pwctl, "set_params", lambda node, p: seen.append(p))
    pwctl.fade_mix(77, active=False, steps=4)
    assert len(seen) == 4
    assert seen[-1] == {"mix:Gain 1": 0.0, "mix:Gain 2": 1.0}


def test_status_problem_priority():
    assert Status("raw", True, True, False).problem.startswith("DeepFilterNet")
    assert Status("dsp", True, True, True).problem.startswith("Raw-Firmware")
    assert Status("missing", True, True, True).problem.startswith("UMA-8 nicht")
    assert Status("raw", False, False, True).problem.startswith("Dienst")
    assert Status("raw", True, False, True).problem.startswith("Filterkette")
    assert Status("raw", True, True, True).problem is None


def _node(i, name, media_class="Audio/Source"):
    return {"id": i, "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name, "media.class": media_class}}}


def test_find_raw_source_handles_suffix_after_reconnect():
    base = "alsa_input.usb-miniDSP_micArray_RAW_SPK-00.analog-surround-71"
    assert pwctl.find_raw_source([_node(5, base)]) == base
    objs = [_node(5, base + ".8"), _node(9, base + ".9"), _node(3, base + "x"),
            _node(4, base + ".monitor", "Audio/Sink")]
    assert pwctl.find_raw_source(objs) == base + ".9"
    assert pwctl.find_raw_source([_node(1, "uma8_callmic")]) is None


def test_raw_source_falls_back_to_default_name(monkeypatch):
    monkeypatch.setattr(pwctl, "dump", lambda: (_ for _ in ()).throw(RuntimeError("weg")))
    assert pwctl.raw_source() == pwctl.K.RAW_DEVICE
