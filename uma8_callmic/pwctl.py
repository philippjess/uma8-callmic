"""PipeWire- und Dienststeuerung über pw-dump, pw-cli, systemctl und pactl."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from . import constants as K


def _run(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def dump() -> list[dict]:
    r = _run(["pw-dump"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "pw-dump fehlgeschlagen")
    return json.loads(r.stdout)


def _props(obj: dict) -> dict:
    return (obj.get("info") or {}).get("props") or {}


def find_node(objs: list[dict], name: str) -> int | None:
    for o in objs:
        if o.get("type") == "PipeWire:Interface:Node" and _props(o).get("node.name") == name:
            return o["id"]
    return None


def find_raw_source(objs: list[dict]) -> str | None:
    """Tatsächlicher Name der Raw-Quelle. Nach schnellen Neuanmeldungen hängt PipeWire
    ein Suffix an („….analog-surround-71.9“); dann gilt der neueste Knoten."""
    found = [(o["id"], _props(o)["node.name"]) for o in objs
             if o.get("type") == "PipeWire:Interface:Node"
             and _props(o).get("media.class") == "Audio/Source"
             and (_props(o).get("node.name") == K.RAW_DEVICE
                  or _props(o).get("node.name", "").startswith(K.RAW_DEVICE + "."))]
    return max(found)[1] if found else None


def raw_source() -> str:
    """Name der Raw-Quelle für pw-record; ohne PipeWire-Auskunft der Standardname."""
    try:
        return find_raw_source(dump()) or K.RAW_DEVICE
    except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return K.RAW_DEVICE


def device_state(objs: list[dict]) -> str:
    """'raw', 'dsp' oder 'missing' anhand der USB-Produkt-ID des miniDSP-Geräts."""
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Device":
            continue
        p = _props(o)
        if p.get("device.vendor.id") == "0x2752":
            return {"0x001d": "raw", "0x001c": "dsp"}.get(p.get("device.product.id"), "missing")
    return "missing"


def format_params(params: dict[str, float]) -> str:
    body = " ".join(f'"{key}" {float(value):.6g}' for key, value in params.items())
    return "{ params = [ " + body + " ] }"


def set_params(node_id: int, params: dict[str, float]) -> None:
    r = _run(["pw-cli", "set-param", str(node_id), "Props", format_params(params)])
    text = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "error" in text.lower():
        raise RuntimeError(f"pw-cli set-param fehlgeschlagen: {text}")


def fade_mix(node_id: int, active: bool, steps: int = 10) -> None:
    """Blendet zwischen Beam-Weg (Gain 1) und Roh-Weg (Gain 2) in `steps` Schritten um."""
    for i in range(1, steps + 1):
        a = i / steps if active else 1.0 - i / steps
        set_params(node_id, {"mix:Gain 1": a, "mix:Gain 2": 1.0 - a})


def service_active() -> bool:
    return _run(["systemctl", "--user", "is-active", K.SERVICE]).stdout.strip() == "active"


def service(action: str) -> None:
    if action not in ("start", "stop", "restart"):
        raise ValueError(action)
    _run(["systemctl", "--user", action, K.SERVICE], timeout=15.0)


def set_default_source() -> None:
    _run(["pactl", "set-default-source", K.SOURCE_NODE])


@dataclass
class Status:
    device: str
    service: bool
    chain: bool
    dfn: bool

    @property
    def problem(self) -> str | None:
        if not self.dfn:
            return "DeepFilterNet nicht installiert (yay -S deepfilternet-plugin-pipewire-bin)"
        if self.device == "dsp":
            return "Raw-Firmware nötig (Mikrofon läuft mit DSP-Firmware)"
        if self.device == "missing":
            return "UMA-8 nicht angeschlossen"
        if not self.service:
            return "Dienst uma8-callmic-chain läuft nicht"
        if not self.chain:
            return "Filterkette nicht geladen"
        return None


def status() -> Status:
    try:
        objs = dump()
    except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        objs = []
    try:
        running = service_active()
    except (OSError, subprocess.TimeoutExpired):
        running = False
    return Status(device_state(objs), running, find_node(objs, K.CAPTURE_NODE) is not None, K.DFN_PLUGIN.exists())
