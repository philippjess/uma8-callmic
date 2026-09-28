"""Erzeugt die PipeWire-Konfiguration der Filterkette aus den Einstellungen."""
from pathlib import Path

from . import constants as K
from .config import Config
from .params import all_params

TEMPLATE = K.REPO_DIR / "pipewire" / "uma8-callmic.conf.in"


def _controls(params: dict[str, float], node: str) -> str:
    items = [(key.split(":", 1)[1], value) for key, value in params.items() if key.startswith(node + ":")]
    return " ".join(f'"{name}" = {float(value):.6g}' for name, value in items)


def render(cfg: Config, beam_plugin: Path = K.BEAM_PLUGIN, dfn_plugin: Path = K.DFN_PLUGIN) -> str:
    params = all_params(cfg)
    subs = {
        "@BEAM_PLUGIN@": str(beam_plugin), "@DFN_PLUGIN@": str(dfn_plugin),
        "@CAPTURE_NODE@": K.CAPTURE_NODE, "@SOURCE_NODE@": K.SOURCE_NODE, "@RAW_DEVICE@": K.RAW_DEVICE,
        "@BEAM_CONTROLS@": _controls(params, "beam"), "@DFN_CONTROLS@": _controls(params, "dfn"),
        "@MIX_CONTROLS@": _controls(params, "mix"), "@LIMIT_CONTROLS@": _controls(params, "limit"),
    }
    text = TEMPLATE.read_text()
    for placeholder, value in subs.items():
        text = text.replace(placeholder, value)
    return text


def write(cfg: Config, path: Path = K.CHAIN_CONF) -> bool:
    """Schreibt die Konfiguration; True, wenn sich der Inhalt geändert hat."""
    text = render(cfg)
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True
