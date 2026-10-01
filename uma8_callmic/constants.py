"""Pfade, Knotennamen und gemessene Latenzen."""
import os
from pathlib import Path

SAMPLE_RATE = 48000
RAW_DEVICE = "alsa_input.usb-miniDSP_micArray_RAW_SPK-00.analog-surround-71"
CAPTURE_NODE = "uma8_callmic_capture"
SOURCE_NODE = "uma8_callmic"
SERVICE = "uma8-callmic-chain.service"

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
    Ort aus LADSPA_DIRS; ist das Plugin nirgends installiert, der RPM-Ort (das Tray meldet das Fehlen)."""
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    candidates = [d / so_name for d in LADSPA_DIRS]
    return next((p for p in candidates if p.exists()), candidates[1])


def beam_plugin() -> Path:
    return find_plugin(BEAM_SO, "UMA8_BEAM_PLUGIN")


def dfn_plugin() -> Path:
    return find_plugin(DFN_SO, "UMA8_DFN_PLUGIN")


#: Latenz von uma8_beam „Beam Out“: FFT-Länge der STFT (fest in allen Modi)
BEAM_LATENCY = 1024
#: Latenz von deep_filter_mono (20 ms), gemessen mit tests/test_dfn_latency.py
DFN_LATENCY = 960
