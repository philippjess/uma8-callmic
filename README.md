# uma8-callmic

Macht aus dem miniDSP UMA-8 (Raw-Firmware) ein gutes Telefonie-Mikrofon unter Linux:
Beamforming über 7 Mikrofone, optionale Hallunterdrückung, DeepFilterNet-Rauschunterdrückung,
volle Bandbreite. Bedienung über ein KDE-Tray-Icon.

## Voraussetzungen

- Arch Linux mit PipeWire und WirePlumber
- `sudo pacman -S rust pyside6 python-numpy`
- `yay -S deepfilternet-plugin-pipewire-bin`
- UMA-8 mit **Raw-Firmware** (`micArray_vf_raw_v1.3_up.bin`, USB-ID `2752:001d`). Firmware nur mit dem
  offiziellen miniDSP-Tool wechseln (Windows-VM: siehe `firmware/docker-compose.usb.yml`).

## Installation

```sh
./install.sh
uma8-callmic &
```

Beim ersten Start prüft das Programm die Kanalzuordnung (10 s still sein) und bittet danach um eine
Kalibrierung (Rechtsklick → Kalibrieren…).

## Bedienung

- Linksklick aufs Icon: aktiv ↔ deaktiviert (Rohsignal)
- Rechtsklick: Aktiv, Kalibrieren…, Optionen…, Beenden
- Icon grün = aktiv, grau = deaktiviert, rot = Problem (Tooltip zeigt die Ursache)

## Aufbau

```
UMA-8 (7 Mikros) ─► uma8_beam (Beamforming, Hall) ─► DeepFilterNet ─┐
                    └─ Roh-Weg (Mittel-Mikrofon, latenzangeglichen) ─┴► Umschalter ─► Begrenzer ─► „UMA-8 Call Mic“
```

Die Tonverarbeitung läuft als PipeWire-Filterkette im Dienst `uma8-callmic-chain` (Rust-LADSPA-Plugin in
`plugin/`). Das Tray-Programm (`uma8_callmic/`) steuert nur und ist nie im Tonweg.

## Tests

```sh
python -m pytest                                  # Unit-Tests
cargo test --manifest-path plugin/Cargo.toml      # Rust-Tests
python -m pytest -m integration                   # nach install.sh, braucht PipeWire
python3 tools/check_output.py                     # mit angeschlossenem UMA-8
```

## Deinstallation

```sh
./uninstall.sh
```
