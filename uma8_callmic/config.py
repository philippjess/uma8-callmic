"""Einstellungen in ~/.config/uma8-callmic/config.toml."""
from __future__ import annotations

import os
import shutil
import threading
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Callable

from .array import ArrayGeometry

DIRECTION_MODES = ("calibrated", "manual", "tracking", "omni")
#: superdirektiv (Standard) oder Delay-and-Sum zum Vergleich; „alle Richtungen“ ist eine Richtungsart
BEAMFORMERS = ("superdirective", "delay_and_sum")
DEFAULT_RING = [1, 6, 5, 4, 3, 2]


@dataclass
class Config:
    active: bool = True
    direction_mode: str = "calibrated"
    calibrated: bool = False
    calibrated_azimuth: float = 0.0
    calibrated_elevation: float = 20.0
    manual_azimuth: float = 0.0
    beamformer: str = "superdirective"
    #: Kohärenzfilter als zusätzliche Hallunterdrückung. Aus: Im echten Raum brachte er nach dem Abklingmodell nur
    #: 1–3 dB weniger Schwanz, kostete ≈ 1 dB Sprachpegel und klang im Hörvergleich etwas schlechter.
    dereverb: bool = False
    dereverb_strength: float = 0.6
    #: Hallunterdrückung über das Abklingmodell des späten Nachhalls (nutzt dereverb_t60)
    late_reverb: bool = True
    dereverb_t60: float = 0.5
    noise_reduction_db: float = 30.0
    #: Gesamtverstärkung; mit Echounterdrückung liegt AEC_PRE_GAIN_DB davon vor der AEC, der Rest im Plugin
    gain_db: float = 30.0
    ceiling_db: float = -1.0
    #: Echounterdrückung (Referenz: Standardausgabe). Umschalten ändert den Aufbau der Kette → Dienst-Neustart
    echo_cancel: bool = True
    autostart: bool = True
    #: Einmalige Einrichtung des Benutzers erledigt (Dienst aktivieren, Autostart anlegen). Danach ändert das Tray
    #: beides nie mehr von selbst; uninstall.sh löscht den Schlüssel wieder.
    setup_done: bool = False
    geometry_checked: bool = False
    center_channel: int = 0
    ring: list[int] = field(default_factory=lambda: list(DEFAULT_RING))
    ring_offset_deg: float = 90.0
    radius_mm: float = 43.0
    # --- Arbeitsplatz-Profil („Arbeitsplatz einmessen…“). Ohne Profil (Standardwerte) ist alles wie bisher.
    #: Lautsprecherrichtungen [[Azimut, Elevation], …], 0–2, im Bezugssystem von calibrated_azimuth
    speakers: list[list[float]] = field(default_factory=list)
    #: Rohpegel je Lautsprecher bei der Messung (dBFS, Sprachband, Mittel-Mikrofon), leer oder wie speakers
    speaker_levels_dbfs: list[float] = field(default_factory=list)
    #: Tastaturrichtung [Azimut, Elevation] oder leer; nur Anzeige
    keyboard: list[float] = field(default_factory=list)
    #: Nullstellen des Beams auf die Lautsprecher: Gewicht in dB, 0 = aus
    null_weight_db: float = 0.0
    #: Nachführung nur ±talker_zone_deg um calibrated_azimuth; 180 = unbeschränkt
    talker_zone_deg: float = 180.0
    #: Grundrauschen und Sprachpegel beim Einmessen (dBFS, Sprachband, Mittel-Mikrofon); None = nicht gemessen
    noise_floor_dbfs: float | None = None
    speech_level_dbfs: float | None = None

    def geometry(self) -> ArrayGeometry:
        return ArrayGeometry(self.center_channel, tuple(self.ring), self.ring_offset_deg, self.radius_mm / 1000.0)

    def has_profile(self) -> bool:
        """Irgendein Wert des Arbeitsplatz-Profils weicht vom Standard ab."""
        return any(getattr(self, k) != v for k, v in profile_defaults().items())


#: Felder des Arbeitsplatz-Profils
PROFILE_FIELDS = ("speakers", "speaker_levels_dbfs", "keyboard", "null_weight_db", "talker_zone_deg",
                  "noise_floor_dbfs", "speech_level_dbfs")


def profile_defaults() -> dict:
    """Standardwerte des Profils („Profil löschen“)."""
    d = Config()
    return {k: getattr(d, k) for k in PROFILE_FIELDS}


_RANGES = {
    "calibrated_azimuth": (0.0, 360.0), "calibrated_elevation": (0.0, 90.0), "manual_azimuth": (0.0, 360.0),
    "dereverb_strength": (0.0, 1.0), "dereverb_t60": (0.1, 1.5), "noise_reduction_db": (0.0, 100.0),
    "gain_db": (0.0, 60.0), "ceiling_db": (-12.0, 0.0), "ring_offset_deg": (0.0, 360.0), "radius_mm": (20.0, 60.0),
    "null_weight_db": (0.0, 40.0), "talker_zone_deg": (5.0, 180.0),
}
#: Pegel in dBFS (Profil)
_LEVEL = (-200.0, 0.0)


_CHOICES = {"direction_mode": DIRECTION_MODES, "beamformer": BEAMFORMERS}


@dataclass
class LoadResult:
    config: Config
    warnings: list[str]
    broken: bool = False
    #: Alte Datei an neue Bedeutungen angepasst; das Tray speichert sie dann einmal und sagt Bescheid
    migrated: bool = False


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return _is_int(v) or isinstance(v, float)


def _direction(v) -> list[float] | None:
    """[Azimut 0–360, Elevation 0–90] als Floats oder None."""
    ok = isinstance(v, list) and len(v) == 2 and all(_is_num(x) for x in v) and 0 <= v[0] <= 360 and 0 <= v[1] <= 90
    return [float(v[0]), float(v[1])] if ok else None


def _level(v) -> float | None:
    return float(v) if _is_num(v) and _LEVEL[0] <= v <= _LEVEL[1] else None


def _speakers(v):
    dirs = [_direction(d) for d in v] if isinstance(v, list) and len(v) <= 2 else [None]
    return None if None in dirs else dirs


def _levels(v):
    levels = [_level(x) for x in v] if isinstance(v, list) and len(v) <= 2 else [None]
    return None if None in levels else levels


#: Felder mit eigener Prüfung (Profil: Listen von Richtungen, optionale Pegel)
_SPECIAL: dict[str, Callable] = {
    "speakers": _speakers, "speaker_levels_dbfs": _levels,
    "keyboard": lambda v: [] if v == [] else _direction(v),
    "noise_floor_dbfs": _level, "speech_level_dbfs": _level,
}


def _check(name: str, value, default):
    """Geprüfter Wert oder None, wenn ungültig."""
    if name in _SPECIAL:
        return _SPECIAL[name](value)
    if isinstance(default, bool):
        return value if isinstance(value, bool) else None
    if isinstance(default, float):
        if not (_is_int(value) or isinstance(value, float)):
            return None
        lo, hi = _RANGES[name]
        return float(value) if lo <= value <= hi else None
    if isinstance(default, int):
        return value if _is_int(value) and 0 <= value <= 6 else None
    if isinstance(default, str):
        return value if value in _CHOICES[name] else None
    if isinstance(default, list):
        ok = isinstance(value, list) and len(value) == 6 and all(_is_int(v) for v in value)
        return list(value) if ok else None
    return None


def load(path: Path) -> LoadResult:
    if not path.exists():
        return LoadResult(Config(), [])
    try:
        raw = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        return LoadResult(Config(), [f"Einstellungen unlesbar, Standardwerte aktiv: {e}"], broken=True)
    migrated = "dereverb" in raw and "late_reverb" not in raw
    if migrated:
        # Datei von vor der Kohärenz-Hallunterdrückung: „dereverb“ schaltete das T60-Modell, das jetzt
        # „late_reverb“ heißt. Der alte Wert war meist nur der Standard (aus); es gelten die neuen Standards.
        # Auch die Stärke: sie setzt jetzt zusätzlich die Untergrenze des Kohärenzfilters (−25·s dB), ein alter
        # Wert nahe 1 ergäbe zusammen bis zu −40 dB.
        raw = {k: v for k, v in raw.items() if k not in ("dereverb", "dereverb_strength")}
    cfg, warnings = Config(), []
    for f in fields(Config):
        if f.name not in raw:
            continue
        checked = _check(f.name, raw[f.name], getattr(cfg, f.name))
        if checked is None:
            warnings.append(f"Ungültiger Wert für {f.name}: {raw[f.name]!r}, Standard wird verwendet")
        else:
            setattr(cfg, f.name, checked)
    if not cfg.geometry().is_valid():
        warnings.append("Ungültige Kanalzuordnung, Standard wird verwendet")
        cfg.center_channel, cfg.ring = 0, list(DEFAULT_RING)
    if cfg.speaker_levels_dbfs and len(cfg.speaker_levels_dbfs) != len(cfg.speakers):
        warnings.append("Lautsprecherpegel passen nicht zu den Lautsprechern, werden verworfen")
        cfg.speaker_levels_dbfs = []
    return LoadResult(cfg, warnings, migrated=migrated)


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    raise TypeError(f"nicht serialisierbar: {v!r}")


def write_atomic(path: Path, text: str) -> None:
    """Über eine eigene temporäre Datei im selben Verzeichnis und os.replace: Leser sehen nie eine halbe Datei,
    und gleichzeitige Schreiber (Tray und ExecStartPre beim Login) ersetzen sie nur ganz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save(cfg: Config, path: Path, broken: bool = False) -> None:
    """Speichert atomar; eine zuvor unlesbare Datei wird als .toml.broken gesichert."""
    if broken and path.exists():
        shutil.copy2(path, path.with_suffix(".toml.broken"))
    # None = nicht gesetzt: TOML kennt kein null, der Schlüssel fehlt dann (Laden ergibt wieder None)
    write_atomic(path, "# uma8-callmic Einstellungen\n" + "".join(f"{k} = {_toml(v)}\n"
                                                                  for k, v in asdict(cfg).items() if v is not None))
