"""Einstellungen in ~/.config/uma8-callmic/config.toml."""
from __future__ import annotations

import shutil
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .array import ArrayGeometry

DIRECTION_MODES = ("calibrated", "manual", "tracking", "omni")
DEFAULT_RING = [1, 6, 5, 4, 3, 2]


@dataclass
class Config:
    active: bool = True
    direction_mode: str = "calibrated"
    calibrated: bool = False
    calibrated_azimuth: float = 0.0
    calibrated_elevation: float = 20.0
    manual_azimuth: float = 0.0
    dereverb: bool = False
    dereverb_strength: float = 0.6
    dereverb_t60: float = 0.5
    noise_reduction_db: float = 30.0
    gain_db: float = 30.0
    ceiling_db: float = -1.0
    autostart: bool = True
    geometry_checked: bool = False
    center_channel: int = 0
    ring: list[int] = field(default_factory=lambda: list(DEFAULT_RING))
    ring_offset_deg: float = 90.0
    radius_mm: float = 43.0

    def geometry(self) -> ArrayGeometry:
        return ArrayGeometry(self.center_channel, tuple(self.ring), self.ring_offset_deg, self.radius_mm / 1000.0)


_RANGES = {
    "calibrated_azimuth": (0.0, 360.0), "calibrated_elevation": (0.0, 90.0), "manual_azimuth": (0.0, 360.0),
    "dereverb_strength": (0.0, 1.0), "dereverb_t60": (0.1, 1.5), "noise_reduction_db": (0.0, 100.0),
    "gain_db": (0.0, 60.0), "ceiling_db": (-12.0, 0.0), "ring_offset_deg": (0.0, 360.0), "radius_mm": (20.0, 60.0),
}


@dataclass
class LoadResult:
    config: Config
    warnings: list[str]
    broken: bool = False


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _check(name: str, value, default):
    """Geprüfter Wert oder None, wenn ungültig."""
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
        return value if value in DIRECTION_MODES else None
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
    return LoadResult(cfg, warnings)


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


def save(cfg: Config, path: Path, broken: bool = False) -> None:
    """Speichert atomar; eine zuvor unlesbare Datei wird als .toml.broken gesichert."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if broken and path.exists():
        shutil.copy2(path, path.with_suffix(".toml.broken"))
    text = "# uma8-callmic Einstellungen\n" + "".join(f"{k} = {_toml(v)}\n" for k, v in asdict(cfg).items())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)
