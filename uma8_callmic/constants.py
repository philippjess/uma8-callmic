"""Pfade, Knotennamen und gemessene Latenzen."""
import os
import shutil
from pathlib import Path

SAMPLE_RATE = 48000
RAW_DEVICE = "alsa_input.usb-miniDSP_micArray_RAW_SPK-00.analog-surround-71"
#: Kanalpositionen der Raw-Quelle in Gerätereihenfolge; Kanal 0–6 = Mikrofone, Kanal 7 = freier PDM-Eingang.
#: Namen wie im PipeWire-Knoten (ACP-Profil analog-surround-71: SL/SR), nicht wie in der USB-Kanalbelegung
#: (FLC/FRC): Positionen, die der Knoten nicht hat, mischt PipeWire um (am Gerät gemessen: Kanal 6 kam stumm an
#: und landete in Kanal 4). FC/LFE/RL/RR stimmen, weil ALSAs surround71 die USB-Reihenfolge umsortiert.
RAW_POSITIONS = ("FL", "FR", "FC", "LFE", "RL", "RR", "SL", "SR")
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
#: Vorverstärkung vor der Echounterdrückung (deren Ausgang ist auf ±1 begrenzt). Offline mit der PipeWire-AEC3-
#: Konfiguration gemessen (synthetischer Raum, Rohpegel ≈ −75 dBFS Grundrauschen): ohne Verstärkung ERLE 19 dB und
#: Sprache des Nutzers bei Gegensprechen um 14 dB gedämpft, mit +18 dB 23 dB/5,5 dB, mit +24 dB 25 dB/4,4 dB.
#: Am Gerät erreichte lautes Lachen am Platz −28,4 dBFS Rohspitze: mit +24 dB blieben 4,4 dB bis zum Abschneiden,
#: mit +18 dB sind es 10 dB, für gut 1 dB weniger AEC-Leistung.
AEC_PRE_GAIN_DB = 18.0

CONFIG_FILE = Path.home() / ".config/uma8-callmic/config.toml"
CHAIN_CONF = Path.home() / ".config/pipewire/uma8-callmic.conf"
STATE_DIR = Path.home() / ".local/state/uma8-callmic"
AUTOSTART_FILE = Path.home() / ".config/autostart/uma8-callmic.desktop"
#: Startbefehl der Entwickler-Installation (install.sh); die Pakete installieren /usr/bin/uma8-callmic
LAUNCHER = Path.home() / ".local/bin/uma8-callmic"
#: Dienst der Entwickler-Installation; überdeckt den des Pakets (/usr/lib/systemd/user)
USER_UNIT = Path.home() / ".config/systemd/user" / SERVICE

#: Vorlagen und Icons liegen im Python-Paket – gleich, ob aus dem Repo gestartet oder installiert
PKG_DIR = Path(__file__).resolve().parent
DATA_DIR = PKG_DIR / "data"
ICON_DIR = PKG_DIR / "icons"
#: Läuft aus einem Checkout (install.sh, Entwicklung) statt aus einem Paket
FROM_REPO = (PKG_DIR.parent / "pyproject.toml").is_file()

BEAM_SO = "libuma8_beam.so"
DFN_SO = "libdeep_filter_ladspa.so"
#: Plugin-Ort der Entwickler-Installation (install.sh); geht allen Paketen vor
USER_LADSPA_DIR = Path.home() / ".local/lib/ladspa"
#: Suchorte der LADSPA-Plugins: Entwickler-Installation, Fedora (RPM), Arch (Pakete, AUR)
LADSPA_DIRS = (USER_LADSPA_DIR, Path("/usr/lib64/ladspa"), Path("/usr/lib/ladspa"))


def _resolve(d: Path) -> Path:
    try:
        return d.resolve()
    except (OSError, RuntimeError):  # Symlink-Schleife
        return d


def search_dirs() -> list[Path]:
    """LADSPA_DIRS ohne Aliasse: Ein Ort, der über Symlinks auf einen anderen, selbst kanonischen Suchort zeigt,
    entfällt (Arch: /usr/lib64 → lib, sonst stünde /usr/lib64/ladspa in der Konfiguration). Ein per Symlink
    verlegtes ~/.local/lib zeigt auf keinen anderen Suchort und bleibt."""
    real = {d: _resolve(d) for d in LADSPA_DIRS}
    canonical = {d for d, r in real.items() if r == d}
    return [d for d in LADSPA_DIRS if d in canonical or real[d] not in canonical]


def find_plugin(so_name: str, env_var: str) -> Path:
    """Pfad eines LADSPA-Plugins, bei jedem Aufruf neu bestimmt.

    Eine gesetzte Umgebungsvariable gilt immer, auch wenn die Datei fehlt – ein Tippfehler zeigt sich
    dann als fehlendes Plugin statt still auf ein anderes auszuweichen. Sonst gilt der erste vorhandene
    Ort aus search_dirs(); ist das Plugin nirgends installiert, der erste Systemort (Fedora /usr/lib64/ladspa,
    Arch /usr/lib/ladspa; das Tray meldet das Fehlen)."""
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    dirs = search_dirs()
    found = next((d / so_name for d in dirs if (d / so_name).exists()), None)
    if found is not None:
        return found
    system = [d for d in dirs if d != LADSPA_DIRS[0]] or dirs  # LADSPA_DIRS[0]: Entwickler-Installation
    return system[0] / so_name


def launcher() -> str:
    """Startbefehl dieser Installation: Paket /usr/bin/uma8-callmic, sonst der von install.sh."""
    return shutil.which("uma8-callmic") or str(LAUNCHER)


def beam_plugin() -> Path:
    return find_plugin(BEAM_SO, "UMA8_BEAM_PLUGIN")


def dfn_plugin() -> Path:
    return find_plugin(DFN_SO, "UMA8_DFN_PLUGIN")


#: Latenz von uma8_beam „Beam Out“: FFT-Länge der STFT (fest in allen Modi)
BEAM_LATENCY = 1024
#: Frame von DeepFilterNet3 (10 ms): Einheit von Verarbeitung und Ausgabepuffer des Plugins
DFN_FRAME = 480
#: „Min Processing Buffer (frames)“ von deep_filter_mono: Puffer von 1 + 1 Frames statt einem. Der Worker-Thread
#: des Plugins läuft mit normaler Priorität und liefert auf einem ausgelasteten Desktop manchmal mehr als 10 ms zu
#: spät; mit einem Frame pendelte die Latenz 10↔20 ms, und jeder Rückbau endete nach Sekundenbruchteilen in einem
#: Underrun (10 ms Stille im Anrufmikrofon, PipeWires RT-Thread blockiert). Ab deepfilternet-ladspa 0.5.6-4 gilt
#: der Wert von Anfang an, nicht erst nach dem ersten Underrun.
DFN_MIN_BUFFER_FRAMES = 1
#: Latenz von deep_filter_mono (30 ms): 20 ms mit einem Frame Puffer, gemessen mit tests/test_dfn_latency.py,
#: plus der Mindestpuffer
DFN_LATENCY = 960 + DFN_MIN_BUFFER_FRAMES * DFN_FRAME
#: Latenz von uma8_limit: 5 ms Vorschau (plugin/src/limiter.rs), geprüft in tests/test_limit_plugin.py
LIMIT_LATENCY = 240
