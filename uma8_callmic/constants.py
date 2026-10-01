"""Pfade, Knotennamen und gemessene Latenzen."""
from pathlib import Path

SAMPLE_RATE = 48000
RAW_DEVICE = "alsa_input.usb-miniDSP_micArray_RAW_SPK-00.analog-surround-71"
CAPTURE_NODE = "uma8_callmic_capture"
SOURCE_NODE = "uma8_callmic"
SERVICE = "uma8-callmic-chain.service"

DFN_PLUGIN = Path("/usr/lib/ladspa/libdeep_filter_ladspa.so")
BEAM_PLUGIN = Path.home() / ".local/lib/ladspa/libuma8_beam.so"
CONFIG_FILE = Path.home() / ".config/uma8-callmic/config.toml"
CHAIN_CONF = Path.home() / ".config/pipewire/uma8-callmic.conf"
STATE_DIR = Path.home() / ".local/state/uma8-callmic"
AUTOSTART_FILE = Path.home() / ".config/autostart/uma8-callmic.desktop"
LAUNCHER = Path.home() / ".local/bin/uma8-callmic"
REPO_DIR = Path(__file__).resolve().parent.parent

#: Latenz von uma8_beam „Beam Out“: FFT-Länge der STFT (fest in allen Modi)
BEAM_LATENCY = 1024
#: Latenz von deep_filter_mono (20 ms), gemessen mit tests/test_dfn_latency.py
DFN_LATENCY = 960
