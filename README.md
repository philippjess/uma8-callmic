# uma8-callmic

Macht aus dem miniDSP UMA-8 (Raw-Firmware) ein gutes Telefonie-Mikrofon unter Linux:
Echounterdrückung, Beamforming über 7 Mikrofone, optionale Hallunterdrückung,
DeepFilterNet-Rauschunterdrückung, volle Bandbreite. Bedienung über ein KDE-Tray-Icon.

## Voraussetzungen

- Linux mit PipeWire und WirePlumber (getestet: Fedora 44 KDE; Arch über `install.sh`)
- UMA-8 mit **Raw-Firmware** (`micArray_vf_raw_v1.3_up.bin`, USB-ID `2752:001d`). Firmware nur mit dem
  offiziellen miniDSP-Tool wechseln (Windows-VM: siehe `firmware/docker-compose.usb.yml`).

## Installation (Fedora, RPM)

Die RPMs entstehen in einem Podman-Container aus Fedora 44 – auf dem Host braucht es nur `podman`, `git` und
`python3`, gebaut wird nichts ins System:

```sh
./packaging/build-rpms.sh                      # beide Pakete; nur eins: ./packaging/build-rpms.sh uma8-callmic
sudo dnf install packaging/out/*.x86_64.rpm
```

Das ergibt zwei Pakete:

- `uma8-callmic`: Tray-Programm (`/usr/bin/uma8-callmic`), Plugin `libuma8_beam.so` in `/usr/lib64/ladspa`,
  Benutzerdienst `uma8-callmic-chain.service`, Startmenü-Eintrag
- `deepfilternet-ladspa`: DeepFilterNet 0.5.6 als LADSPA-Plugin, mit behobenem Thread-Leck

Danach „UMA-8 Call Mic“ aus dem Startmenü starten. Beim ersten Start aktiviert das Tray den Dienst und den
Autostart für den eigenen Benutzer. Das Mikrofon „UMA-8 Call Mic“ einmal in den Audio-Einstellungen als
Standard wählen (oder `pactl set-default-source uma8_callmic`).

Der Bau lädt den DeepFilterNet-Quelltext (Prüfsumme in `packaging/deepfilternet-ladspa.sources`) und die
Rust-Crates herunter, `rpmbuild` selbst läuft danach offline. Ergebnisse, Logs und rpmlint-Ausgabe:
`packaging/out/`.

## Entwickler-Installation (ohne RPM)

Läuft direkt aus dem Repo, nur für den eigenen Benutzer, und verweigert sich, solange das RPM installiert ist.
Braucht Rust, PySide6, numpy und ein DeepFilterNet-Plugin:

- Fedora: `sudo dnf install cargo python3-pyside6 python3-numpy pipewire-utils pulseaudio-utils`, dazu
  `./packaging/build-rpms.sh deepfilternet-ladspa` und `sudo dnf install packaging/out/deepfilternet-ladspa-*.x86_64.rpm`
- Arch: `sudo pacman -S rust pyside6 python-numpy` und `yay -S deepfilternet-plugin-pipewire-bin`

```sh
./install.sh
uma8-callmic &
```

Die Plugins werden in dieser Reihenfolge gesucht: `~/.local/lib/ladspa`, `/usr/lib64/ladspa`, `/usr/lib/ladspa`;
`UMA8_BEAM_PLUGIN` bzw. `UMA8_DFN_PLUGIN` erzwingen einen bestimmten Pfad.

Beim ersten Start prüft das Programm die Kanalzuordnung (10 s still sein) und bittet danach um eine
Kalibrierung (Rechtsklick → Kalibrieren…).

## Bedienung

- Linksklick aufs Icon: aktiv ↔ deaktiviert (Rohsignal)
- Rechtsklick: Aktiv, Kalibrieren…, Optionen…, Beenden
- Icon grün = aktiv, grau = deaktiviert, rot = Problem (Tooltip zeigt die Ursache)

## Echounterdrückung

Entfernt aus allen 7 Mikrofonen, was die Lautsprecher abspielen, bevor Beamforming und Rauschunterdrückung
laufen. Das Gegenüber hört sich nicht mehr selbst, und sein Anruf-Programm schaltet bei Gegensprechen nicht
mehr das eigene Mikrofon stumm. Als Referenz dient die jeweilige Standardausgabe (PipeWire-Modul `echo-cancel`,
WebRTC AEC3); ein Wechsel der Standardausgabe wird übernommen.

- Die Echounterdrückung der Anruf-Programme kann an bleiben.
- Nur Ton auf der Standardausgabe wird entfernt. Gibt das Anruf-Programm auf einem anderen Gerät aus, bleibt
  dessen Echo.
- Spricht man gleichzeitig mit dem Gegenüber, wird die eigene Stimme leiser, umso mehr, je lauter die
  Lautsprecher am Mikrofon ankommen. Lautsprecher leiser oder weiter weg hilft.
- Nachteil: Jede Wiedergabe auf der Standardausgabe (Musik, Video) weckt die ganze Kette samt UMA-8, auch
  ohne Anruf, und kostet dann Rechenzeit.
- „Verstärkung“ bleibt die Gesamtverstärkung; 24 dB davon liegen vor der Echounterdrückung.
- Ausschalten: Optionen → „Echounterdrückung (Lautsprecher)“. Das startet die Filterkette neu (kurze
  Tonpause). Ohne Tray: `echo_cancel = false` in `~/.config/uma8-callmic/config.toml`, dann
  `systemctl --user restart uma8-callmic-chain`.

## Aufbau

```
UMA-8 (7 Mikros) ─► +24 dB ─► Echounterdrückung (Referenz: Standardausgabe)
   ─► uma8_beam (Beamforming, Hall) ─► DeepFilterNet ─┐
      └─ Roh-Weg (Mittel-Mikrofon, latenzangeglichen) ─┴► Umschalter ─► Begrenzer ─► „UMA-8 Call Mic“
```

Ohne Echounterdrückung liest `uma8_beam` das UMA-8 direkt und verstärkt allein. Die Zwischenstufen
(`uma8_callmic_pre`, `uma8_callmic_aec`) sind interne Quellen und tauchen in keiner Geräteliste auf.

Die Tonverarbeitung läuft als PipeWire-Filterkette im Dienst `uma8-callmic-chain` (Rust-LADSPA-Plugin in
`plugin/`). Vor jedem Start schreibt der Dienst die Kettenkonfiguration aus den Einstellungen
(`uma8-callmic --write-config`). Das Tray-Programm (`uma8_callmic/`) steuert nur und ist nie im Tonweg.

## Tests

```sh
python -m pytest                                  # Unit-Tests
cargo test --manifest-path plugin/Cargo.toml      # Rust-Tests
python -m pytest -m integration                   # nach der Installation, braucht PipeWire
python3 tools/check_output.py                     # mit angeschlossenem UMA-8
```

Der RPM-Bau führt die Unit- und Rust-Tests beider Pakete ebenfalls aus (`%check`).

## Deinstallation

```sh
./uninstall.sh                                    # Einrichtung des eigenen Benutzers (Dienst, Autostart, …)
sudo dnf remove uma8-callmic deepfilternet-ladspa # bei RPM-Installation zusätzlich
```
