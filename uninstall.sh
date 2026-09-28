#!/usr/bin/env bash
# Entfernt uma8-callmic. Einstellungen bleiben, außer man bestätigt die Löschung.
set -euo pipefail
systemctl --user disable --now uma8-callmic-chain.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/uma8-callmic-chain.service"
systemctl --user daemon-reload
pkill -f "python3 -m uma8_callmic" 2>/dev/null || true
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
echo "uma8-callmic entfernt."
