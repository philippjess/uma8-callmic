"""PipeWire-, Dienst- und Autostart-Steuerung über pw-dump, pw-cli, systemctl und pactl."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import constants as K

log = logging.getLogger(__name__)
#: Antworten von „systemctl is-enabled“, bei denen der Dienst schon beim Login startet
ENABLED_STATES = {"enabled", "enabled-runtime", "linked", "linked-runtime", "alias", "static", "indirect",
                  "generated", "transient"}


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


def ensure_service_enabled() -> bool:
    """Erststart ohne install.sh (RPM): Dienst für diesen Benutzer aktivieren und starten.

    Nur im Zustand „disabled“; maskiert bleibt maskiert. True, wenn der Dienst jetzt aktiviert wurde.
    Fehler werden protokolliert, nie geworfen – das Tray zeigt einen nicht laufenden Dienst ohnehin an."""
    try:
        state = _run(["systemctl", "--user", "is-enabled", K.SERVICE]).stdout.strip()
        if state != "disabled":
            if state not in ENABLED_STATES:
                log.warning("Dienst %s wird nicht aktiviert (Zustand: %s)", K.SERVICE, state or "unbekannt")
            return False
        r = _run(["systemctl", "--user", "enable", "--now", K.SERVICE], timeout=30.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("systemctl fehlgeschlagen: %s", e)
        return False
    if r.returncode != 0:
        log.warning("Dienst %s nicht aktiviert: %s", K.SERVICE, (r.stderr or r.stdout).strip())
        return False
    log.info("Dienst %s aktiviert und gestartet", K.SERVICE)
    return True


def set_default_source() -> None:
    _run(["pactl", "set-default-source", K.SOURCE_NODE])


def autostart_entry() -> str:
    """Inhalt der XDG-Autostart-Datei mit dem Startbefehl dieser Installation."""
    template = (K.DATA_DIR / "uma8-callmic.desktop").read_text()
    return template.replace("@BIN@", shutil.which("uma8-callmic") or str(K.LAUNCHER))


def sync_autostart(enabled: bool, path: Path = K.AUTOSTART_FILE) -> bool:
    """Autostart-Datei an die Einstellung angleichen; True, wenn sich etwas geändert hat."""
    if not enabled:
        if path.exists():
            path.unlink()
            return True
        return False
    text = autostart_entry()
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


@dataclass
class Status:
    device: str
    service: bool
    chain: bool
    dfn: bool
    beam: bool = True

    @property
    def problem(self) -> str | None:
        if not self.dfn:
            return ("DeepFilterNet nicht installiert (Paket deepfilternet-ladspa, "
                    "Arch: AUR deepfilternet-plugin-pipewire-bin)")
        if not self.beam:
            return "Plugin libuma8_beam.so fehlt (Paket uma8-callmic oder ./install.sh)"
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
    return Status(device_state(objs), running, find_node(objs, K.CAPTURE_NODE) is not None,
                  K.dfn_plugin().exists(), K.beam_plugin().exists())
