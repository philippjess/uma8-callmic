"""PipeWire-, Dienst- und Autostart-Steuerung über pw-dump, pw-cli, systemctl und pactl."""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import constants as K
from . import ladspainfo
from .config import Config, write_atomic
from .params import NULL_KEYS, all_params

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


#: pw-record-Ziel: Knotenname, Kanalzahl, Kanalpositionen der Quelle
Target = tuple[str, int, tuple[str, ...]]


def raw_target() -> Target:
    """Raw-Quelle mit denselben Kanalpositionen wie in der Kette (Kalibrierung, Kanalzuordnung)."""
    return raw_source(), len(K.RAW_POSITIONS), K.RAW_POSITIONS


def tracking_target(echo_cancel: bool) -> Target:
    """Quelle der Nachführung. Mit Echounterdrückung deren Ausgang: Sprache aus den Lautsprechern ist dort
    entfernt, der Strahl folgt also nie dem Lautsprecher. Ohne sie die Raw-Quelle."""
    return (K.AEC_NODE, K.MICS, K.MIC_POSITIONS) if echo_cancel else raw_target()


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


def restart_chain() -> None:
    """Kette mit der neu geschriebenen Konfiguration neu starten, nur wenn sie läuft; wartet nicht darauf."""
    _run(["systemctl", "--user", "try-restart", "--no-block", K.SERVICE])


def service_active() -> bool:
    return _run(["systemctl", "--user", "is-active", K.SERVICE]).stdout.strip() == "active"


def service(action: str) -> None:
    if action not in ("start", "stop", "restart"):
        raise ValueError(action)
    _run(["systemctl", "--user", action, K.SERVICE], timeout=15.0)


def ensure_service_enabled() -> bool | None:
    """Einmalige Einrichtung ohne install.sh (Paket): Dienst für diesen Benutzer aktivieren und starten.

    Nur im Zustand „disabled“; maskiert bleibt maskiert. True: jetzt aktiviert; False: nichts zu tun (schon aktiv,
    maskiert, statisch …); None: nicht gelungen (systemctl scheitert, Dienst unbekannt) – später erneut versuchen.
    Fehler werden protokolliert, nie geworfen – das Tray zeigt einen nicht laufenden Dienst ohnehin an."""
    try:
        state = _run(["systemctl", "--user", "is-enabled", K.SERVICE]).stdout.strip()
        if state != "disabled":
            if state in ("", "not-found"):
                log.warning("Dienst %s nicht gefunden", K.SERVICE)
                return None
            if state not in ENABLED_STATES:
                log.warning("Dienst %s wird nicht aktiviert (Zustand: %s)", K.SERVICE, state)
            return False
        r = _run(["systemctl", "--user", "enable", "--now", K.SERVICE], timeout=30.0)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("systemctl fehlgeschlagen: %s", e)
        return None
    if r.returncode != 0:
        log.warning("Dienst %s nicht aktiviert: %s", K.SERVICE, (r.stderr or r.stdout).strip())
        return None
    log.info("Dienst %s aktiviert und gestartet", K.SERVICE)
    return True


def set_default_source() -> None:
    _run(["pactl", "set-default-source", K.SOURCE_NODE])


def autostart_entry() -> str:
    """Inhalt der XDG-Autostart-Datei mit dem Startbefehl dieser Installation."""
    template = (K.DATA_DIR / "uma8-callmic.desktop").read_text()
    return template.replace("@BIN@", K.launcher())


def sync_autostart(enabled: bool, path: Path | None = None) -> bool:
    """Autostart-Datei an die Einstellung angleichen; True, wenn sich etwas geändert hat."""
    path = path or K.AUTOSTART_FILE
    if not enabled:
        if path.exists():
            path.unlink(missing_ok=True)
            return True
        return False
    text = autostart_entry()
    if path.exists() and path.read_text() == text:
        return False
    write_atomic(path, text)
    return True


def expected_controls() -> dict[str, set[str]]:
    """Label → Controls, die Kette und Tray setzen; fehlt eins im installierten Plugin, ist es veraltet."""
    labels = {"beam": "uma8_beam", "limit": "uma8_limit"}
    wanted: dict[str, set[str]] = {label: set() for label in labels.values()}
    for key in (*all_params(Config()), *NULL_KEYS):
        node, control = key.split(":", 1)
        if node in labels:
            wanted[labels[node]].add(control)
    return wanted


_api_checked: dict[tuple, str | None] = {}


def beam_api_problem(path: Path) -> str | None:
    """Meldung, wenn libuma8_beam.so unter `path` nicht ladbar ist oder Controls fehlen (alte Version, z. B. eine
    vergessene Entwickler-Installation vor dem Paket: die Kette lädt dann mit stillen Warnungen, Controls wirken
    nicht). Je Datei und Änderungszeit einmal geprüft; fehlt die Datei, None (das meldet Status.beam)."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _api_checked:
        _api_checked.clear()
        _api_checked[key] = _check_beam_api(path)
    return _api_checked[key]


def _check_beam_api(path: Path) -> str | None:
    try:
        found = ladspainfo.ports(path)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        log.warning("%s nicht ladbar: %s", path, e)
        return f"{K.BEAM_SO} nicht ladbar: {path} ({str(e).replace(f'{path}: ', '')})"
    missing = sorted(f"„{c}“" for label, controls in expected_controls().items()
                     for c in controls - set(found.get(label, ())))
    if not missing:
        return None
    log.warning("%s veraltet, es fehlen: %s", path, ", ".join(missing))
    fix = ("./install.sh im Repo erneut ausführen, beim Paket ./uninstall.sh" if path.parent == K.USER_LADSPA_DIR
           else "Paket uma8-callmic aktualisieren")
    return f"{K.BEAM_SO} veraltet: {path} (es fehlen {', '.join(missing)}). {fix}"


def dev_leftovers() -> list[Path]:
    """Reste von install.sh, wenn dieses Programm aus einem Paket läuft: Plugin und Dienst überdecken die des
    Pakets, der Startbefehl in ~/.local/bin steht im PATH meist vor /usr/bin."""
    if K.FROM_REPO:
        return []
    return [p for p in (K.USER_LADSPA_DIR / K.BEAM_SO, K.USER_UNIT, K.LAUNCHER) if p.exists()]


@dataclass
class Status:
    device: str
    service: bool
    chain: bool
    dfn: bool
    beam: bool = True
    #: Echounterdrückung geladen oder nicht eingeschaltet
    aec: bool = True
    #: Meldung von beam_api_problem: Plugin veraltet oder nicht ladbar
    beam_api: str | None = None
    #: Reste der Entwickler-Installation neben einem Paket (dev_leftovers)
    leftovers: tuple[Path, ...] = ()

    @property
    def problem(self) -> str | None:
        if not self.dfn:
            return ("DeepFilterNet nicht installiert: Paket deepfilternet-ladspa aus diesem Repo (packaging/, siehe "
                    "README); Arch alternativ AUR deepfilternet-plugin-pipewire-bin (mit Thread-Leck)")
        if not self.beam:
            return "Plugin libuma8_beam.so fehlt (Paket uma8-callmic oder ./install.sh)"
        if self.beam_api:
            return self.beam_api
        if self.leftovers:
            home = str(Path.home())
            paths = ", ".join(str(p).replace(home, "~", 1) for p in self.leftovers)
            return (f"Reste von install.sh überdecken das Paket ({paths}): im Repo ./uninstall.sh ausführen, "
                    "dann UMA-8 Call Mic neu starten")
        if self.device == "dsp":
            return "Raw-Firmware nötig (Mikrofon läuft mit DSP-Firmware)"
        if self.device == "missing":
            return "UMA-8 nicht angeschlossen"
        if not self.service:
            return "Dienst uma8-callmic-chain läuft nicht"
        if not self.chain:
            return "Filterkette nicht geladen"
        if not self.aec:
            return "Echounterdrückung nicht geladen (Dienst uma8-callmic-chain neu starten)"
        return None


def try_dump() -> list[dict] | None:
    """pw-dump oder None, wenn PipeWire nicht antwortet."""
    try:
        return dump()
    except (RuntimeError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def default_sink_description(objs: list[dict]) -> str | None:
    """Beschreibung der Standardausgabe (Metadaten „default“) aus pw-dump-Objekten."""
    from .reflink import Graph

    name = Graph(objs).default_sink()
    for o in objs:
        p = _props(o)
        if o.get("type") == "PipeWire:Interface:Node" and name is not None and p.get("node.name") == name:
            return p.get("node.description") or name
    return name


def status(echo_cancel: bool = False, objs: list[dict] | None = None) -> Status:
    """Zustand aus `objs` (pw-dump) oder einem eigenen pw-dump."""
    if objs is None:
        objs = try_dump() or []
    try:
        running = service_active()
    except (OSError, subprocess.TimeoutExpired):
        running = False
    beam = K.beam_plugin()
    return Status(device_state(objs), running, find_node(objs, K.CAPTURE_NODE) is not None,
                  K.dfn_plugin().exists(), beam.exists(),
                  not echo_cancel or find_node(objs, K.AEC_NODE) is not None,
                  beam_api_problem(beam), tuple(dev_leftovers()))
