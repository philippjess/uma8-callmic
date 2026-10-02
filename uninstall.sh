#!/usr/bin/env bash
# Entfernt die Einrichtung für den aktuellen Benutzer (install.sh bzw. Einrichtung durch das Tray eines Pakets).
# Einstellungen bleiben, außer man bestätigt die Löschung. Pakete (RPM, Arch) bleiben installiert (Hinweis am Ende).
set -euo pipefail
systemctl --user disable --now uma8-callmic-chain.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/uma8-callmic-chain.service"
systemctl --user daemon-reload
# Tray und Helfer beenden, jede Installation: Entwickler (python3 -m uma8_callmic …), Fedora
# (/usr/bin/python3 -sP /usr/bin/uma8-callmic …), Arch (/usr/bin/python /usr/bin/uma8-callmic …), auch --ref-linker
pkill -u "$(id -u)" -f '^[^ ]*python[0-9.]*( -[^ ]+)* (-m uma8_callmic|[^ ]*/uma8-callmic)( |$)' 2>/dev/null || true
rm -f "$HOME/.config/pipewire/uma8-callmic.conf" \
      "$HOME/.local/lib/ladspa/libuma8_beam.so" \
      "$HOME/.local/bin/uma8-callmic" \
      "$HOME/.config/autostart/uma8-callmic.desktop" \
      "$HOME/.local/share/applications/uma8-callmic.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/uma8-callmic.svg"
read -r -p "Einstellungen und Log löschen? [j/N] " answer
if [ "${answer,,}" = "j" ]; then
    rm -rf "$HOME/.config/uma8-callmic" "$HOME/.local/state/uma8-callmic"
elif [ -f "$HOME/.config/uma8-callmic/config.toml" ]; then
    # Einstellungen bleiben, die Einrichtung nicht: Das Tray (z. B. aus einem Paket) richtet Dienst und Autostart
    # beim nächsten Start wieder ein
    sed -i '/^setup_done = /d' "$HOME/.config/uma8-callmic/config.toml"
fi
echo "uma8-callmic für diesen Benutzer entfernt."
if command -v rpm >/dev/null; then
    rpms=""
    for p in uma8-callmic deepfilternet-ladspa; do rpm -q --quiet "$p" 2>/dev/null && rpms="$rpms $p"; done
    [ -z "$rpms" ] || echo "Hinweis: RPM-Pakete noch installiert – entfernen mit: sudo dnf remove$rpms
         (oder behalten: UMA-8 Call Mic aus dem Startmenü starten, es richtet sich neu ein)"
fi
if command -v pacman >/dev/null; then
    pkgs=""
    for p in uma8-callmic deepfilternet-ladspa; do pacman -Q "$p" >/dev/null 2>&1 && pkgs="$pkgs $p"; done
    [ -z "$pkgs" ] || echo "Hinweis: Arch-Pakete noch installiert – entfernen mit: sudo pacman -R$pkgs
         (oder behalten: UMA-8 Call Mic aus dem Startmenü starten, es richtet sich neu ein)"
fi
