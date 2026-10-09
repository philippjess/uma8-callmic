#!/usr/bin/env bash
# Entwickler-Installation für den aktuellen Benutzer (ohne root, läuft direkt aus dem Repo).
# Für den normalen Gebrauch die Pakete nehmen: ./packaging/build-rpms.sh bzw. ./packaging/build-arch.sh (siehe README).
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
LADSPA_DIR="$HOME/.local/lib/ladspa"
BIN="$HOME/.local/bin/uma8-callmic"
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG="$HOME/.config/uma8-callmic/config.toml"
# Tray jeder Installation (Entwickler: python3 -m uma8_callmic; Fedora: /usr/bin/python3 -sP /usr/bin/uma8-callmic;
# Arch: /usr/bin/python /usr/bin/uma8-callmic), ohne Argumente – nicht die Hilfsbefehle (--ref-linker, …)
TRAY_RE='^[^ ]*python[0-9.]*( -[^ ]+)* (-m uma8_callmic|[^ ]*/uma8-callmic)$'
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf 'Fehler: %s\n' "$*" >&2; exit 1; }
# need BEFEHL FEDORA-PAKET ARCH-PAKET
need() { command -v "$1" >/dev/null || fail "$1 fehlt – Fedora: sudo dnf install $2 · Arch: sudo pacman -S $3"; }

say "Voraussetzungen prüfen"
if command -v rpm >/dev/null && rpm -q --quiet uma8-callmic 2>/dev/null; then
    fail "Das RPM uma8-callmic ist installiert. Zwei Installationen kämen sich in die Quere (Dienst, Plugin, Autostart).
       Entweder beim RPM bleiben oder es erst entfernen: sudo dnf remove uma8-callmic"
fi
if command -v pacman >/dev/null && pacman -Q uma8-callmic >/dev/null 2>&1; then
    fail "Das Arch-Paket uma8-callmic ist installiert. Zwei Installationen kämen sich in die Quere (Dienst, Plugin, Autostart).
       Entweder beim Paket bleiben oder es erst entfernen: sudo pacman -R uma8-callmic"
fi
need pipewire pipewire pipewire
need pw-cli pipewire-utils pipewire
need pactl pulseaudio-utils libpulse
need cargo cargo rust
python3 -c 'import PySide6, numpy' 2>/dev/null \
    || fail "Python-Module fehlen – Fedora: sudo dnf install python3-pyside6 python3-numpy · Arch: sudo pacman -S pyside6 python-numpy"
DFN="$(PYTHONPATH="$REPO" python3 -c 'from uma8_callmic import constants as K; print(K.dfn_plugin())')"
[ -f "$DFN" ] || fail "DeepFilterNet fehlt ($DFN). Paket deepfilternet-ladspa aus diesem Repo (mit behobenem Thread-Leck und Latenzabbau):
       Fedora: ./packaging/build-rpms.sh deepfilternet-ladspa, dann sudo dnf install packaging/out/deepfilternet-ladspa-*.x86_64.rpm
       Arch:   ./packaging/build-arch.sh deepfilternet-ladspa, dann sudo pacman -U packaging/out/arch/deepfilternet-ladspa-*.pkg.tar.zst
               (oder: cd packaging/arch/deepfilternet-ladspa && makepkg -si)
       Alternative auf Arch: yay -S deepfilternet-plugin-pipewire-bin (behält Thread-Leck und wachsende Latenz)"
lsusb -d 2752:001d >/dev/null 2>&1 || echo "Hinweis: UMA-8 mit Raw-Firmware (2752:001d) nicht gefunden – Installation läuft weiter."

say "Plugin bauen"
cargo build --release --quiet --manifest-path "$REPO/plugin/Cargo.toml"
install -Dm644 "$REPO/plugin/target/release/libuma8_beam.so" "$LADSPA_DIR/libuma8_beam.so"

# Ein laufendes Tray (auch eine ältere Version) jetzt beenden: Sonst setzte es seine alten Werte in die neue Kette
# (z. B. die volle Verstärkung hinter die Vorverstärkung der Echounterdrückung) und speicherte mit altem Stand.
tray_was_running=false
if pgrep -u "$(id -u)" -f "$TRAY_RE" >/dev/null; then
    say "Laufendes Tray beenden"
    tray_was_running=true
    pkill -u "$(id -u)" -f "$TRAY_RE" || true
    for _ in $(seq 50); do pgrep -u "$(id -u)" -f "$TRAY_RE" >/dev/null || break; sleep 0.1; done
    pkill -KILL -u "$(id -u)" -f "$TRAY_RE" || true
fi

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
sed -i "s|@BIN@|$BIN|" "$UNIT_DIR/uma8-callmic-chain.service"
systemctl --user daemon-reload
systemctl --user enable uma8-callmic-chain.service
systemctl --user restart uma8-callmic-chain.service

say "Startmenü-Eintrag und Icon einrichten"
DESKTOP="$REPO/uma8_callmic/data/uma8-callmic.desktop"
install -Dm644 "$REPO/uma8_callmic/icons/active.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/uma8-callmic.svg"
install -Dm644 "$DESKTOP" "$HOME/.local/share/applications/uma8-callmic.desktop"
sed -i "s|@BIN@|$BIN|" "$HOME/.local/share/applications/uma8-callmic.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$HOME/.local/share/applications" || true
command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true

if grep -qx 'autostart = false' "$CONFIG" 2>/dev/null; then
    say "Autostart bleibt aus (Optionen → Beim Login starten)"
else
    say "Autostart einrichten"
    install -Dm644 "$DESKTOP" "$HOME/.config/autostart/uma8-callmic.desktop"
    sed -i "s|@BIN@|$BIN|" "$HOME/.config/autostart/uma8-callmic.desktop"
fi

say "Als Standard-Mikrofon setzen"
sleep 2
pactl set-default-source uma8_callmic || echo "Hinweis: Standard-Mikrofon nicht gesetzt (Dienst prüfen: systemctl --user status uma8-callmic-chain)"

if $tray_was_running && [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
    setsid -f "$BIN" >/dev/null 2>&1 </dev/null
    say "Fertig. Tray neu gestartet."
else
    say "Fertig. Tray jetzt starten mit: uma8-callmic &"
fi
