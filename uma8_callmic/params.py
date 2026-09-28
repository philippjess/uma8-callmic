"""Einstellungen → Controls der Filterkette („<knoten>:<control>“)."""
from .config import Config
from .constants import DFN_LATENCY


def geometry_params(cfg: Config) -> dict[str, float]:
    p = {"beam:Center Channel": float(cfg.center_channel)}
    for k, ch in enumerate(cfg.ring):
        p[f"beam:Ring {k}"] = float(ch)
    p["beam:Ring Offset (deg)"] = float(cfg.ring_offset_deg)
    p["beam:Radius (mm)"] = float(cfg.radius_mm)
    p["beam:Raw Extra Delay (samples)"] = float(DFN_LATENCY)
    return p


def steering_params(cfg: Config, tracked_azimuth: float | None = None) -> dict[str, float]:
    if cfg.direction_mode == "omni":
        return {"beam:Mode": 1.0, "beam:Azimuth (deg)": 0.0, "beam:Elevation (deg)": 0.0}
    if cfg.direction_mode == "manual":
        az = cfg.manual_azimuth
    elif cfg.direction_mode == "tracking" and tracked_azimuth is not None:
        az = tracked_azimuth
    else:
        az = cfg.calibrated_azimuth
    return {"beam:Mode": 0.0, "beam:Azimuth (deg)": float(az) % 360.0,
            "beam:Elevation (deg)": float(cfg.calibrated_elevation)}


def processing_params(cfg: Config) -> dict[str, float]:
    return {
        "beam:Gain (dB)": float(cfg.gain_db),
        "beam:Dereverb": 1.0 if cfg.dereverb else 0.0,
        "beam:Dereverb Strength": float(cfg.dereverb_strength),
        "beam:Dereverb T60 (s)": float(cfg.dereverb_t60),
        "dfn:Attenuation Limit (dB)": float(cfg.noise_reduction_db),
        "limit:Ceiling (dB)": float(cfg.ceiling_db),
    }


def mix_params(active: bool) -> dict[str, float]:
    return {"mix:Gain 1": 1.0 if active else 0.0, "mix:Gain 2": 0.0 if active else 1.0}


def all_params(cfg: Config, tracked_azimuth: float | None = None) -> dict[str, float]:
    return {**geometry_params(cfg), **steering_params(cfg, tracked_azimuth),
            **processing_params(cfg), **mix_params(cfg.active)}
