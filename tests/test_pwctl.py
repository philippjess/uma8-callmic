import os
from pathlib import Path

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
    assert Status("raw", True, True, False, False).problem.startswith("DeepFilterNet")
    assert "libuma8_beam.so" in Status("raw", True, True, True, False).problem
    assert Status("dsp", True, True, True).problem.startswith("Raw-Firmware")
    assert Status("missing", True, True, True).problem.startswith("UMA-8 nicht")
    assert Status("raw", False, False, True).problem.startswith("Dienst")
    assert Status("raw", True, False, True).problem.startswith("Filterkette")
    assert Status("raw", True, False, True, aec=False).problem.startswith("Filterkette")
    assert Status("raw", True, True, True, aec=False).problem.startswith("Echounterdrückung nicht geladen")
    assert Status("raw", True, True, True).problem is None
    stale = "libuma8_beam.so veraltet: /x"
    assert Status("missing", False, False, True, beam_api=stale).problem == stale
    assert Status("raw", True, True, False, beam_api=stale).problem.startswith("DeepFilterNet")
    left = Status("missing", True, True, True, leftovers=(Path.home() / ".local/lib/ladspa/libuma8_beam.so",))
    assert "./uninstall.sh" in left.problem and "~/.local/lib/ladspa/libuma8_beam.so" in left.problem
    assert Status("raw", True, True, True, beam_api=stale, leftovers=left.leftovers).problem == stale


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


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _fake_systemctl(monkeypatch, state, enable_rc=0):
    calls = []

    def run(args, timeout=5.0):
        calls.append(args)
        if "is-enabled" in args:
            return _Result(state + "\n", 0 if state == "enabled" else 1)
        return _Result(returncode=enable_rc, stderr="Failed to enable unit" if enable_rc else "")

    monkeypatch.setattr(pwctl, "_run", run)
    return calls


def test_ensure_service_enabled_enables_disabled_unit(monkeypatch):
    calls = _fake_systemctl(monkeypatch, "disabled")
    assert pwctl.ensure_service_enabled() is True
    assert calls[-1] == ["systemctl", "--user", "enable", "--now", pwctl.K.SERVICE]


@pytest.mark.parametrize("state,result", [("enabled", False), ("static", False), ("masked", False),
                                          ("not-found", None), ("", None)])
def test_ensure_service_enabled_leaves_other_states_alone(monkeypatch, state, result):
    """False: nichts zu tun (auch maskiert bleibt maskiert); None: Dienst unbekannt, später erneut versuchen."""
    calls = _fake_systemctl(monkeypatch, state)
    assert pwctl.ensure_service_enabled() is result
    assert all("enable" not in c for c in calls)


def test_ensure_service_enabled_never_raises(monkeypatch):
    _fake_systemctl(monkeypatch, "disabled", enable_rc=1)
    assert pwctl.ensure_service_enabled() is None

    def broken(args, timeout=5.0):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(pwctl, "_run", broken)
    assert pwctl.ensure_service_enabled() is None


def test_sync_autostart(tmp_path, monkeypatch):
    path = tmp_path / "autostart" / "uma8-callmic.desktop"
    monkeypatch.setattr(pwctl.K.shutil, "which", lambda name: "/usr/bin/uma8-callmic")
    assert pwctl.sync_autostart(True, path) is True
    text = path.read_text()
    assert "Exec=/usr/bin/uma8-callmic\n" in text and "@" not in text
    assert pwctl.sync_autostart(True, path) is False
    monkeypatch.setattr(pwctl.K.shutil, "which", lambda name: None)  # Entwickler-Installation
    assert pwctl.sync_autostart(True, path) is True
    assert f"Exec={pwctl.K.LAUNCHER}\n" in path.read_text()
    assert pwctl.sync_autostart(False, path) is True
    assert not path.exists()
    assert pwctl.sync_autostart(False, path) is False


def test_beam_api_matches_built_plugin(plugin_so):
    """Das gebaute Plugin hat jedes Control, das Kette und Tray setzen (auch eine neue Einstellung ohne Port fiele
    hier auf)."""
    assert pwctl.ladspainfo.ports(plugin_so)["uma8_beam"][:2] == ["In 0", "In 1"]
    pwctl._api_checked.clear()
    assert pwctl.beam_api_problem(Path(plugin_so)) is None


def test_beam_api_reports_stale_or_broken_plugin(tmp_path, monkeypatch):
    """Alte Version (fehlende Controls) oder nicht ladbar: Meldung statt Absturz; je Änderungszeit einmal geprüft."""
    so = tmp_path / "libuma8_beam.so"
    so.write_bytes(b"kein ELF")
    pwctl._api_checked.clear()
    problem = pwctl.beam_api_problem(so)                        # echter Kindprozess, scheitert am Laden
    assert problem.startswith(f"libuma8_beam.so nicht ladbar: {so} (OSError: ")
    calls = []
    old = {"uma8_beam": ["In 0", "Mode", "Gain (dB)"], "uma8_limit": ["In", "Out", "Ceiling (dB)"]}
    monkeypatch.setattr(pwctl.ladspainfo, "ports", lambda path: calls.append(path) or old)
    os.utime(so, ns=(1, 1))
    problem = pwctl.beam_api_problem(so)
    assert problem.startswith(f"libuma8_beam.so veraltet: {so}") and "„Null Weight (dB)“" in problem
    assert "„Late Reverb“" in problem and "„Ceiling (dB)“" not in problem and "Paket" in problem
    assert pwctl.beam_api_problem(so) == problem and len(calls) == 1
    monkeypatch.setattr(pwctl.ladspainfo, "ports", lambda path: (_ for _ in ()).throw(
        pwctl.subprocess.TimeoutExpired("python3", 10)))
    os.utime(so, ns=(2, 2))
    assert "nicht ladbar" in pwctl.beam_api_problem(so)
    assert pwctl.beam_api_problem(tmp_path / "fehlt.so") is None   # Fehlen meldet Status.beam
    monkeypatch.setattr(pwctl.K, "USER_LADSPA_DIR", tmp_path)
    monkeypatch.setattr(pwctl.ladspainfo, "ports", lambda path: old)
    os.utime(so, ns=(3, 3))
    assert "./install.sh" in pwctl.beam_api_problem(so)


def test_dev_leftovers_only_when_running_from_package(tmp_path, monkeypatch):
    plugin, unit, launcher = tmp_path / "ladspa", tmp_path / "unit.service", tmp_path / "uma8-callmic"
    monkeypatch.setattr(pwctl.K, "USER_LADSPA_DIR", plugin)
    monkeypatch.setattr(pwctl.K, "USER_UNIT", unit)
    monkeypatch.setattr(pwctl.K, "LAUNCHER", launcher)
    monkeypatch.setattr(pwctl.K, "FROM_REPO", False)
    assert pwctl.dev_leftovers() == []
    plugin.mkdir()
    (plugin / pwctl.K.BEAM_SO).touch()
    unit.touch()
    assert pwctl.dev_leftovers() == [plugin / pwctl.K.BEAM_SO, unit]
    monkeypatch.setattr(pwctl.K, "FROM_REPO", True)            # install.sh selbst: das sind keine Reste
    assert pwctl.dev_leftovers() == []


def test_status_checks_aec_only_when_enabled(monkeypatch):
    aec = {"id": 79, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "uma8_callmic_aec"}}}
    monkeypatch.setattr(pwctl, "service_active", lambda: True)
    monkeypatch.setattr(pwctl, "dump", lambda: OBJS)
    assert pwctl.status(echo_cancel=False).aec is True
    st = pwctl.status(echo_cancel=True)
    assert st.chain and not st.aec
    monkeypatch.setattr(pwctl, "dump", lambda: OBJS + [aec])
    assert pwctl.status(echo_cancel=True).aec is True


def test_status_reports_plugin_api_and_leftovers(monkeypatch):
    monkeypatch.setattr(pwctl, "service_active", lambda: True)
    monkeypatch.setattr(pwctl, "beam_api_problem", lambda path: f"veraltet: {path.name}")
    monkeypatch.setattr(pwctl, "dev_leftovers", lambda: [Path("/rest")])
    st = pwctl.status(echo_cancel=False, objs=OBJS)
    assert st.beam_api == f"veraltet: {pwctl.K.BEAM_SO}" and st.leftovers == (Path("/rest"),)


def test_tracking_follows_echo_cancel(monkeypatch):
    """Mit Echounterdrückung hört die Nachführung auf deren Ausgang (7 Mikrofone), sonst auf das Gerät (8 Kanäle)."""
    monkeypatch.setattr(pwctl, "raw_source", lambda: "raw.9")
    assert pwctl.tracking_target(True) == ("uma8_callmic_aec", 7, tuple(f"AUX{i}" for i in range(7)))
    assert pwctl.tracking_target(False) == ("raw.9", 8, ("FL", "FR", "FC", "LFE", "RL", "RR", "FLC", "FRC"))
    assert pwctl.raw_target() == pwctl.tracking_target(False)


def test_restart_chain_only_restarts_running_service(monkeypatch):
    calls = []
    monkeypatch.setattr(pwctl, "_run", lambda args, timeout=5.0: calls.append(args))
    pwctl.restart_chain()
    assert calls == [["systemctl", "--user", "try-restart", "--no-block", pwctl.K.SERVICE]]


def test_status_uses_given_objects(monkeypatch):
    """Das Tray liest pw-dump einmal je Durchlauf und reicht die Objekte weiter."""
    monkeypatch.setattr(pwctl, "service_active", lambda: True)
    monkeypatch.setattr(pwctl, "dump", lambda: pytest.fail("kein zweites pw-dump"))
    st = pwctl.status(echo_cancel=False, objs=OBJS)
    assert st.device == "raw" and st.chain


def test_try_dump_returns_none_on_failure(monkeypatch):
    def broken():
        raise RuntimeError("weg")
    monkeypatch.setattr(pwctl, "dump", broken)
    assert pwctl.try_dump() is None


def test_default_sink_description():
    meta = {"id": 40, "type": "PipeWire:Interface:Metadata", "props": {"metadata.name": "default"},
            "metadata": [{"subject": 0, "key": "default.audio.sink",
                          "value": {"name": "alsa_output.pci-0000_0c_00.4.analog-stereo"}}]}
    sink = {"id": 41, "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": "alsa_output.pci-0000_0c_00.4.analog-stereo",
                               "node.description": "Lautsprecher (Starship/Matisse)"}}}
    assert pwctl.default_sink_description(OBJS + [meta, sink]) == "Lautsprecher (Starship/Matisse)"
    assert pwctl.default_sink_description(OBJS + [meta]) == "alsa_output.pci-0000_0c_00.4.analog-stereo"
    assert pwctl.default_sink_description(OBJS) is None
