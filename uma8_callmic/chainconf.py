"""Erzeugt die PipeWire-Konfiguration der Filterkette aus den Einstellungen."""
import math
from pathlib import Path

from . import constants as K
from .config import Config, write_atomic
from .params import all_params

TEMPLATE = K.DATA_DIR / "uma8-callmic.conf.in"
#: Vorverstärkung und Echounterdrückung, wird bei „echo_cancel“ vor der Hauptkette eingefügt
AEC_TEMPLATE = K.DATA_DIR / "uma8-callmic-aec.conf.in"
#: Höchster „Mult“ des builtin-Plugins „linear“ (PipeWire klemmt darüber still ab)
LINEAR_MAX_MULT = 10.0
_NODE_INDENT = " " * 20


def _controls(params: dict[str, float], node: str) -> str:
    items = [(key.split(":", 1)[1], value) for key, value in params.items() if key.startswith(node + ":")]
    return " ".join(f'"{name}" = {float(value):.6g}' for name, value in items)


def _words(items) -> str:
    return " ".join(items)


def gain_stages(db: float) -> list[float]:
    """Faktoren gleich großer linear-Stufen, deren Produkt db ergibt (je Stufe höchstens LINEAR_MAX_MULT)."""
    n = max(1, math.ceil(db / (20.0 * math.log10(LINEAR_MAX_MULT)) - 1e-9))
    return [10.0 ** (db / 20.0 / n)] * n


def _pre_graph(db: float) -> dict[str, str]:
    """Graph der Vorverstärkung: je Mikrofon eine Kette aus builtin-„linear“-Knoten g<kanal>_<stufe>."""
    mults = gain_stages(db)
    last = len(mults)
    nodes = [f'{_NODE_INDENT}{{ type = builtin name = g{c}_{s} label = linear control = {{ "Mult" = {m:.6g} }} }}'
             for c in range(K.MICS) for s, m in enumerate(mults, 1)]
    links = [f'{_NODE_INDENT}{{ output = "g{c}_{s}:Out" input = "g{c}_{s + 1}:In" }}'
             for c in range(K.MICS) for s in range(1, last)]
    return {
        "@PRE_NODES@": "\n".join(nodes), "@PRE_LINKS@": "\n".join(links),
        "@PRE_INPUTS@": _words(f'"g{c}_1:In"' for c in range(K.MICS)),
        "@PRE_OUTPUTS@": _words(f'"g{c}_{last}:Out"' for c in range(K.MICS)),
    }


def _fill(text: str, subs: dict[str, str]) -> str:
    for placeholder, value in subs.items():
        text = text.replace(placeholder, value)
    return text


def _aec_modules() -> str:
    subs = {
        "@PRE_GAIN_DB@": f"{K.AEC_PRE_GAIN_DB:g}", **_pre_graph(K.AEC_PRE_GAIN_DB),
        "@PRE_CAPTURE_NODE@": K.PRE_NODE + "_capture", "@PRE_NODE@": K.PRE_NODE,
        "@AEC_CAPTURE_NODE@": K.AEC_NODE + "_capture", "@AEC_REF_NODE@": K.AEC_REF_NODE,
        "@AEC_NODE@": K.AEC_NODE, "@RAW_DEVICE@": K.RAW_DEVICE, "@RAW_POSITIONS@": _words(K.RAW_POSITIONS),
        "@MICS@": str(K.MICS), "@MIC_POSITIONS@": _words(K.MIC_POSITIONS),
    }
    return _fill(AEC_TEMPLATE.read_text(), subs)


def _aec_exec() -> str:
    """Startet den Helfer für die Echo-Referenz mit der Kette; er liegt in deren cgroup und endet mit dem Dienst."""
    return ("\n# Echo-Referenz nur während einer Aufnahme verbinden (uma8_callmic/reflink.py)\n"
            f'context.exec = [\n    {{ path = "{K.launcher()}" args = [ "--ref-linker" ] }}\n]\n')


def render(cfg: Config, beam_plugin: Path | None = None, dfn_plugin: Path | None = None) -> str:
    """Plugin-Pfade ohne Angabe werden jetzt bestimmt, nicht beim Import (RPM, install.sh, Umgebung).

    Ohne Echounterdrückung liest die Hauptkette direkt die 8 Kanäle der Raw-Quelle (Kanal 7 → null), mit ihr
    die 7 Mikrofonkanäle der AEC-Quelle."""
    params = all_params(cfg)
    beam_inputs = [f'"beam:In {i}"' for i in range(K.MICS)]
    if cfg.echo_cancel:
        capture = {"@CAPTURE_TARGET@": K.AEC_NODE, "@CAPTURE_CHANNELS@": str(K.MICS),
                   "@CAPTURE_POSITIONS@": _words(K.MIC_POSITIONS), "@BEAM_INPUTS@": _words(beam_inputs)}
    else:
        capture = {"@CAPTURE_TARGET@": K.RAW_DEVICE, "@CAPTURE_CHANNELS@": str(len(K.RAW_POSITIONS)),
                   "@CAPTURE_POSITIONS@": _words(K.RAW_POSITIONS), "@BEAM_INPUTS@": _words(beam_inputs + ["null"])}
    subs = {
        "@AEC_MODULES@\n": _aec_modules() if cfg.echo_cancel else "",
        "@AEC_EXEC@\n": _aec_exec() if cfg.echo_cancel else "",
        "@BEAM_PLUGIN@": str(beam_plugin or K.beam_plugin()), "@DFN_PLUGIN@": str(dfn_plugin or K.dfn_plugin()),
        "@CAPTURE_NODE@": K.CAPTURE_NODE, "@SOURCE_NODE@": K.SOURCE_NODE, **capture,
        "@BEAM_CONTROLS@": _controls(params, "beam"), "@DFN_CONTROLS@": _controls(params, "dfn"),
        "@MIX_CONTROLS@": _controls(params, "mix"), "@LIMIT_CONTROLS@": _controls(params, "limit"),
    }
    return _fill(TEMPLATE.read_text(), subs)


def write(cfg: Config, path: Path = K.CHAIN_CONF) -> bool:
    """Schreibt die Konfiguration; True, wenn sich der Inhalt geändert hat."""
    text = render(cfg)
    if path.exists() and path.read_text() == text:
        return False
    write_atomic(path, text)  # Tray und ExecStartPre schreiben beim Login womöglich gleichzeitig
    return True
