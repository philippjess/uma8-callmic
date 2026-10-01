"""Pfade, Knotennamen und gemessene Latenzen."""
import os
import shutil
from pathlib import Path

SAMPLE_RATE = 48000
RAW_DEVICE = "alsa_input.usb-miniDSP_micArray_RAW_SPK-00.analog-surround-71"
#: Kanalpositionen der Raw-Quelle; Kanal 0–6 = Mikrofone, Kanal 7 = freier PDM-Eingang
RAW_POSITIONS = ("FL", "FR", "FC", "LFE", "RL", "RR", "FLC", "FRC")
MICS = 7
#: Kanalpositionen der 7 Mikrofonkanäle hinter der Vorverstärkung (Echounterdrückung, Hauptkette)
MIC_POSITIONS = tuple(f"AUX{i}" for i in range(MICS))
CAPTURE_NODE = "uma8_callmic_capture"
SOURCE_NODE = "uma8_callmic"
#: Interne Quellen der Echounterdrückung (Audio/Source/Internal: für Programme unsichtbar, nie Standard)
PRE_NODE = "uma8_callmic_pre"
AEC_NODE = "uma8_callmic_aec"
#: Referenz-Stream der Echounterdrückung; verbunden nur während einer Aufnahme (reflink.py)
AEC_REF_NODE = "uma8_callmic_aec_ref"
SERVICE = "uma8-callmic-chain.service"
#: Vorverstärkung vor der Echounterdrückung. Offline mit der PipeWire-AEC3-Konfiguration gemessen (synthetischer
#: Raum, Rohpegel ≈ −75 dBFS Grundrauschen): ohne Verstärkung ERLE 19 statt 25 dB und Sprache des Nutzers bei
#: Gegensprechen um 14 statt 4,4 dB gedämpft; +30 dB war nicht besser und kostet Aussteuerungsreserve.
AEC_PRE_GAIN_DB = 24.0

CONFIG_FILE = Path.home() / ".config/uma8-callmic/config.toml"
CHAIN_CONF = Path.home() / ".config/pipewire/uma8-callmic.conf"
STATE_DIR = Path.home() / ".local/state/uma8-callmic"
AUTOSTART_FILE = Path.home() / ".config/autostart/uma8-callmic.desktop"
#: Startbefehl der Entwickler-Installation (install.sh); das RPM installiert /usr/bin/uma8-callmic
LAUNCHER = Path.home() / ".local/bin/uma8-callmic"

#: Vorlagen und Icons liegen im Python-Paket – gleich, ob aus dem Repo gestartet oder installiert
PKG_DIR = Path(__file__).resolve().parent
DATA_DIR = PKG_DIR / "data"
ICON_DIR = PKG_DIR / "icons"

BEAM_SO = "libuma8_beam.so"
DFN_SO = "libdeep_filter_ladspa.so"
#: Suchorte der LADSPA-Plugins: Entwickler-Installation (install.sh), RPM (Fedora), /usr/lib (Arch/AUR)
LADSPA_DIRS = (Path.home() / ".local/lib/ladspa", Path("/usr/lib64/ladspa"), Path("/usr/lib/ladspa"))


def find_plugin(so_name: str, env_var: str) -> Path:
    """Pfad eines LADSPA-Plugins, bei jedem Aufruf neu bestimmt.

    Eine gesetzte Umgebungsvariable gilt immer, auch wenn die Datei fehlt – ein Tippfehler zeigt sich
    dann als fehlendes Plugin statt still auf ein anderes auszuweichen. Sonst gilt der erste vorhandene
    Ort aus LADSPA_DIRS; ist das Plugin nirgends installiert, der Systemort der Distribution (das Tray meldet
    das Fehlen). Orte unter einem Symlink-Verzeichnis zählen nicht (Arch: /usr/lib64 → lib), sonst stünde dort
    /usr/lib64/ladspa statt /usr/lib/ladspa in der Konfiguration."""
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    candidates = [d / so_name for d in LADSPA_DIRS if not d.parent.is_symlink()]
    return next((p for p in candidates if p.exists()), candidates[1])


def launcher() -> str:
    """Startbefehl dieser Installation: RPM /usr/bin/uma8-callmic, sonst der von install.sh."""
    return shutil.which("uma8-callmic") or str(LAUNCHER)


def beam_plugin() -> Path:
    return find_plugin(BEAM_SO, "UMA8_BEAM_PLUGIN")


def dfn_plugin() -> Path:
    return find_plugin(DFN_SO, "UMA8_DFN_PLUGIN")


#: Latenz von uma8_beam „Beam Out“: FFT-Länge der STFT (fest in allen Modi)
BEAM_LATENCY = 1024
#: Latenz von deep_filter_mono (20 ms), gemessen mit tests/test_dfn_latency.py
DFN_LATENCY = 960
