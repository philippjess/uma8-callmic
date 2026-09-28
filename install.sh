#!/usr/bin/env bash
# Installiert uma8-callmic für den aktuellen Benutzer (ohne root).
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
LADSPA_DIR="$HOME/.local/lib/ladspa"
BIN="$HOME/.local/bin/uma8-callmic"
UNIT_DIR="$HOME/.config/systemd/user"
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }

say "Voraussetzungen prüfen"
command -v pipewire >/dev/null || fail "PipeWire fehlt"
command -v cargo >/dev/null || fail "Rust fehlt: sudo pacman -S rust"
python3 -c 'import PySide6, numpy' 2>/dev/null || fail "Python-Module fehlen: sudo pacman -S pyside6 python-numpy"
[ -f /usr/lib/ladspa/libdeep_filter_ladspa.so ] || fail "DeepFilterNet fehlt: yay -S deepfilternet-plugin-pipewire-bin"
lsusb -d 2752:001d >/dev/null || echo "Hinweis: UMA-8 mit Raw-Firmware (2752:001d) nicht gefunden – Installation läuft weiter."

say "Plugin bauen"
cargo build --release --quiet --manifest-path "$REPO/plugin/Cargo.toml"
install -Dm644 "$REPO/plugin/target/release/libuma8_beam.so" "$LADSPA_DIR/libuma8_beam.so"

say "Startbefehl einrichten"
install -d "$(dirname "$BIN")"
cat > "$BIN" <<EOF
#!/bin/sh
PYTHONPATH="$REPO\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m uma8_callmic "\$@"
EOF
chmod 755 "$BIN"

say "Filterkette und Dienst einrichten"
"$BIN" --write-config >/dev/null
install -Dm644 "$REPO/pipewire/uma8-callmic-chain.service" "$UNIT_DIR/uma8-callmic-chain.service"
systemctl --user daemon-reload
systemctl --user enable uma8-callmic-chain.service
systemctl --user restart uma8-callmic-chain.service

say "Autostart einrichten"
install -Dm644 "$REPO/pipewire/uma8-callmic.desktop" "$HOME/.config/autostart/uma8-callmic.desktop"
sed -i "s|@BIN@|$BIN|" "$HOME/.config/autostart/uma8-callmic.desktop"

say "Als Standard-Mikrofon setzen"
sleep 2
pactl set-default-source uma8_callmic || echo "Hinweis: Standard-Mikrofon nicht gesetzt (Dienst prüfen: systemctl --user status uma8-callmic-chain)"

say "Fertig. Tray jetzt starten mit: uma8-callmic &"
