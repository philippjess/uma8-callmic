#!/usr/bin/env bash
# Entfernt die Einrichtung für den aktuellen Benutzer (install.sh bzw. Erststart nach dem RPM).
# Einstellungen bleiben, außer man bestätigt die Löschung. RPM-Pakete bleiben installiert (Hinweis am Ende).
set -euo pipefail
systemctl --user disable --now uma8-callmic-chain.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/uma8-callmic-chain.service"
systemctl --user daemon-reload
# Tray beenden: Entwickler-Start (python3 -m uma8_callmic) oder RPM-Start (/usr/bin/uma8-callmic)
pkill -f 'python3.* (-m uma8_callmic|/usr/bin/uma8-callmic)' 2>/dev/null || true
rm -f "$HOME/.config/pipewire/uma8-callmic.conf" \
      "$HOME/.local/lib/ladspa/libuma8_beam.so" \
      "$HOME/.local/bin/uma8-callmic" \
      "$HOME/.config/autostart/uma8-callmic.desktop" \
      "$HOME/.local/share/applications/uma8-callmic.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/uma8-callmic.svg"
read -r -p "Einstellungen und Log löschen? [j/N] " answer
if [ "${answer,,}" = "j" ]; then
    rm -rf "$HOME/.config/uma8-callmic" "$HOME/.local/state/uma8-callmic"
fi
echo "uma8-callmic für diesen Benutzer entfernt."
if command -v rpm >/dev/null; then
    rpms=""
    for p in uma8-callmic deepfilternet-ladspa; do rpm -q --quiet "$p" 2>/dev/null && rpms="$rpms $p"; done
    [ -z "$rpms" ] || echo "Hinweis: RPM-Pakete noch installiert – entfernen mit: sudo dnf remove$rpms"
fi
