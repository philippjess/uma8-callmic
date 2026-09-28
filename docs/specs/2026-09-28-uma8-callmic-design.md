# uma8-callmic – Design

Stand: 2026-09-28 · Status: Entwurf zur Durchsicht

## Ziel

Das miniDSP-UMA-8-Mikrofonarray soll in Telefonaten unter Linux (Arch, KDE,
PipeWire) deutlich besser klingen als mit der DSP-Firmware des Geräts. Die
DSP-Firmware rechnet intern mit 16 kHz und schneidet alles über 8 kHz ab
(gemessen: −44 dB oberhalb 8 kHz). Mit der Raw-Firmware liefert das Gerät
7 unbearbeitete Mikrofonkanäle in voller Bandbreite; die Verarbeitung
übernimmt diese Software auf dem Laptop.

Erfolgskriterien:

- Virtuelles Mikrofon „UMA-8 Call Mic“, das in allen Anruf-Programmen
  auswählbar ist und automatisch Standard wird.
- Sprache in voller Bandbreite (kein Abschnitt bei 8 kHz), deutlich weniger
  Raumrauschen als das Rohsignal.
- Keine hörbaren Aussetzer oder Klicks im Betrieb, auch nicht beim
  Umschalten und beim Nachführen der Richtung.
- KDE-Tray-Icon zeigt den Zustand und schaltet per Klick um.

## Ausgangslage Hardware

| | Wert |
|---|---|
| Gerät | miniDSP UMA-8 v2 / UMA-8-SP (XMOS XVF3000) |
| Firmware | `micArray_vf_raw_v1.3` (seit 2026-09-28) |
| USB | `2752:001d`, „micArray RAW SPK“ |
| Aufnahme | 8 Kanäle, S32_LE (24 Bit), 11,025–48 kHz; Kanal 0–6 = MEMS, Kanal 7 = freier PDM-Eingang (stumm) |
| PipeWire-Quelle | `alsa_input.usb-miniDSP_micArray_RAW_SPK-00.analog-surround-71` |
| Pegel | sehr leise, ca. −75 dBFS Raumrauschen (keine Verstärkung im Chip) |
| Geometrie | 1 Mikrofon mittig, 6 im Kreis; Radius laut Platinengröße ca. 43 mm, wird beim Einrichten per Kohärenz-Fit überprüft |

Firmware-Wechsel ausschließlich mit dem offiziellen miniDSP-Tool in der
Windows-VM (`firmware/docker-compose.usb.yml`). Eigene DFU-Befehle von Linux
aus haben den Chip wiederholt hängen lassen und sind ausgeschlossen.

## Nicht im Umfang

- Echounterdrückung: machen die Anruf-Programme (WebRTC, Zoom, Signal …)
  selbst. Die Verarbeitung ist so ausgelegt, dass sie deren AEC nicht stört
  (keine schnell schwankende Verstärkung).
- Adaptive Beamformer (MVDR/GSC), andere Mikrofone, andere Betriebssysteme.
- Rückkehr zur DSP-Firmware per Software.

## Architektur

```
UMA-8 (Kanal 0–6, 48 kHz)
   │
   ▼  PipeWire filter-chain (systemd --user: uma8-callmic-chain.service)
   ├─ uma8_beam (7→2): Beamforming, Hallunterdrückung, Verstärkung
   │     ├─ „Beam Out“ ─► deep_filter_mono (DeepFilterNet) ──────┐
   │     └─ „Raw Out“ (Mittel-Mikrofon, latenzangeglichen) ──────┤
   │                                            builtin mixer ◄───┘  (Umschalter)
   │                                                 │
   │                                  uma8_limit (Begrenzer)
   ▼                                                 │
„UMA-8 Call Mic“ (Audio/Source, hohe Priorität) ◄────┘

Tray-Programm (Python/PySide6)
   ├─ zeigt Zustand, schaltet um (pw-cli set-param, live)
   ├─ Optionen, Kalibrierung, Kanalzuordnung
   └─ Nachführung: liest die 8 Kanäle parallel mit, setzt „Azimuth“ live
```

Grundsatz: Das Tray-Programm ist nie im Tonweg. Fällt es aus, läuft der Ton
mit den zuletzt gesetzten Parametern weiter.

## Komponenten

### 1. LADSPA-Plugin `uma8-beam` (Rust)

Ein Rust-Crate (`cdylib`), das `libuma8_beam.so` mit zwei LADSPA-Plugins
erzeugt. Die LADSPA-Schnittstelle (`ladspa_descriptor`, Descriptor-Structs)
wird direkt über `extern "C"` und `#[repr(C)]` umgesetzt, ohne
Fremd-Crate. Die DSP-Logik liegt in sicherem Rust in eigenen Modulen
(`beam`, `dereverb`, `limiter`) und ist unabhängig von der
LADSPA-Hülle testbar. `unsafe` bleibt auf die Hülle beschränkt; Panics
werden an der FFI-Grenze abgefangen (`catch_unwind`) und führen zu
Stille statt zum Absturz des PipeWire-Prozesses.

**`uma8_beam`** – 7 Audio-Eingänge („In 0“ … „In 6“), 2 Audio-Ausgänge:
„Beam Out“ (Beamforming + Hallunterdrückung) und „Raw Out“ (Mittel-Mikrofon,
um dieselbe Latenz plus „Raw Extra Delay“ verzögert, damit es am Umschalter
zeitgleich mit dem DeepFilterNet-Ausgang ankommt). Beide Ausgänge werden mit
„Gain (dB)“ verstärkt, damit DeepFilterNet einen normalen Pegel bekommt.

| Control | Bereich | Bedeutung |
|---|---|---|
| Azimuth | 0–359,9° | Strahlrichtung |
| Elevation | 0–60° | Höhenwinkel der Quelle über der Array-Ebene |
| Mode | 0/1 | 0 = Beamforming, 1 = alle Richtungen (nur Mittel-Mikrofon) |
| Center Channel | 0–6 | Index des Mittel-Mikrofons |
| Ring Order 0–5 | 0–6 | Kanalindex der Ringmikrofone in Kreisreihenfolge |
| Ring Offset | 0–359,9° | Winkel des ersten Ringmikrofons |
| Radius | 20–60 mm | Ringradius |
| Gain | 0–60 dB | Verstärkung beider Ausgänge (Rohsignal ist sehr leise) |
| Raw Extra Delay | 0–4800 Samples | Zusatzverzögerung für „Raw Out“ = gemessene Latenz von DeepFilterNet |
| Dereverb | 0/1 | Hallunterdrückung an/aus |
| Dereverb Strength | 0–1 | Stärke (untere Grenze der Dämpfung) |
| Dereverb T60 | 0,1–1,5 s | angenommene Nachhallzeit des Raums |

- Beamforming: Delay-and-Sum im Fernfeld. Verzögerungen aus Geometrie,
  Azimuth und Elevation; Bruchteil-Verzögerungen über gefensterte
  Sinc-FIR (32 Taps). Die gemeinsame Grundlatenz ist fest (halbe FIR-Länge
  plus maximale Laufzeit über das Array), damit sich die Gesamtlatenz beim
  Schwenken nicht ändert.
- Richtungswechsel: Bei jeder Änderung der Steuerwerte laufen alter und
  neuer Strahl parallel und werden über 50 ms mit Kosinus-Rampe
  übergeblendet. Kein Klick, keine Latenzänderung.
- Hallunterdrückung: Unterdrückung des späten Nachhalls nach Lebart/Habets.
  STFT 1024/256 bei 48 kHz, PSD des späten Nachhalls aus dem exponentiellen
  Abklingmodell (T60) geschätzt, Wiener-artige Verstärkung mit unterer Grenze
  aus „Strength“, zeitliche Glättung gegen Musical Noise. Bei
  Dereverb = 0 wird der STFT-Pfad umgangen, die Latenz bleibt über eine
  Ausgleichsverzögerung gleich, damit Umschalten keinen Sprung erzeugt.
- Echtzeitregeln: keine Speicherallokation, keine Locks, keine
  Systemaufrufe in `run()`; alle Puffer werden in `instantiate()`
  angelegt. Denormals werden abgefangen. FFT über das Crate `realfft`
  mit vorab geplanten Instanzen.

**`uma8_limit`** – 1 Eingang, 1 Ausgang.

| Control | Bereich | Bedeutung |
|---|---|---|
| Ceiling | −12–0 dBFS | Obergrenze |

Look-ahead-Begrenzer (5 ms), Release 100 ms. Keine automatische
Pegelregelung, damit die AEC der Anruf-Programme nicht gestört wird.

### 2. PipeWire-Filterkette

- Konfiguration `~/.config/pipewire/uma8-callmic.conf`, aus der Vorlage im
  Repo erzeugt (Installation und beim Speichern der Einstellungen).
- Eigener Prozess: `pipewire -c uma8-callmic.conf` als
  `systemd --user`-Dienst `uma8-callmic-chain.service` (Restart=on-failure).
- Capture-Seite: `target.object` = Raw-Quelle des UMA-8, 8 Kanäle,
  Kanal 7 wird nicht verwendet. `node.passive = true`, damit das Gerät nur
  läuft, wenn jemand das virtuelle Mikrofon nutzt oder das Tray mithört.
- Playback-Seite: `media.class = Audio/Source`, `node.name = uma8_callmic`,
  `node.description = "UMA-8 Call Mic"`, mono, 48 kHz,
  `priority.session` höher als alle anderen Quellen, damit WirePlumber sie
  zum Standard macht.
- Umschalter: builtin `mixer` mit zwei Eingängen (Beam-Weg, Roh-Weg).
  Aktiv: Gain 1 = 1, Gain 2 = 0; deaktiviert umgekehrt. Beim Umschalten
  werden die Gains in 10 Schritten gesetzt, um Klicks zu vermeiden. Die
  Latenz des Roh-Wegs gleicht `uma8_beam` über „Raw Extra Delay“ an.
- DeepFilterNet: `/usr/lib/ladspa/libdeep_filter_ladspa.so`, Label
  `deep_filter_mono` (AUR `deepfilternet-plugin-pipewire-bin`, installiert
  und geprüft). Control „Attenuation Limit (dB)“ (0–100) = Optionswert
  „Rauschunterdrückung“. Die vom Paket mitgelieferte Beispielkette
  (`/etc/pipewire/filter-chain.conf.d/deepfilter-mono-source.conf`,
  Dienst `filter-chain.service`) bleibt deaktiviert.
- Parameteränderungen zur Laufzeit: `pw-cli set-param <node> Props
  '{ params = [ "<plugin>:<control>" <wert> ] }'` auf dem Capture-Knoten
  der Kette. Alle Controls einschließlich Geometrie sind live änderbar;
  ein Neustart des Dienstes ist nur nach einer Neuinstallation nötig.

### 3. Tray-Programm (Python 3, PySide6, numpy)

Module:

| Modul | Aufgabe |
|---|---|
| `config.py` | Einstellungen lesen/schreiben (`~/.config/uma8-callmic/config.toml`), Standardwerte, Validierung |
| `array.py` | Array-Geometrie (Kanal → Position), identisch zur Rust-Seite |
| `params.py` | Abbildung Einstellungen → Plugin-Controls (für Konfiguration und Live-Änderungen) |
| `constants.py` | Pfade, Knotennamen, gemessene Latenzen |
| `pwctl.py` | PipeWire-Anbindung über `pw-dump`/`pw-cli`/`systemctl --user`: Zustand lesen, Parameter setzen, Dienst steuern |
| `capture.py` | 8-Kanal-Aufnahme über einen `pw-record`-Unterprozess (float32, 48 kHz), Ringpuffer |
| `doa.py` | Richtungsschätzung: SRP-PHAT über 72 Azimuth- × 4 Elevationswerte, Sprachaktivitätserkennung (Energie + spektrale Flachheit) |
| `geometry.py` | Kanalzuordnung und Radius aus Raumrauschen: Kohärenzmatrix, Mittel-Mikrofon = höchste mittlere Kohärenz, Ringreihenfolge und Radius per Fit an sinc(k·d) |
| `tracker.py` | Nachführung: alle 0,2 s DOA bei Sprache, Median über 1 s, Hysterese 15°, setzt Azimuth |
| `chainconf.py` | erzeugt `uma8-callmic.conf` aus Vorlage und Einstellungen |
| `tray.py` | Icon, Menü, Umschalten, Zustandsabfrage alle 2 s |
| `dialogs.py` | Optionen, Kalibrierung, Kanalzuordnung |

Tray-Zustände:

| Icon | Zustand |
|---|---|
| farbig | aktiv (Beam-Weg) |
| grau | deaktiviert (Roh-Weg) |
| rot | Problem: Gerät fehlt, falsche Firmware, Dienst läuft nicht, DeepFilterNet fehlt; Details im Tooltip |

Linksklick = umschalten. Rechtsklick-Menü: ☑ Aktiv · Kalibrieren… ·
Optionen… · Beenden (nur Tray; Dienst läuft weiter).

Optionen:

- Richtung: Kalibriert (Standard) · Manuell (Azimuth-Regler) ·
  Automatisch nachführen · Alle Richtungen
- Hallunterdrückung: an/aus, Stärke
- Rauschunterdrückung: 0–100 dB, Standard 30 dB
- Verstärkung: dB-Regler mit Pegelanzeige
- Beim Login starten

Kalibrierung: Countdown, 5 s normal sprechen mit Pegelanzeige, SRP-PHAT über
die Sprachanteile, Ergebnis mit Eindeutigkeit (Verhältnis Haupt- zu
Nebenmaximum). Unter Schwelle oder ohne Sprache: „Bitte wiederholen“.
„Übernehmen“ setzt Azimuth und Elevation live.

Kanalzuordnung: Standard ist die bekannte UMA-8-Geometrie aus dem
ODAS-Projekt (`config/odaslive/minidsp.cfg`: Kanal 0 Mitte, Kanäle 1–6 im
Uhrzeigersinn ab 90°, Radius 43 mm). `geometry.py` prüft sie beim ersten
Start mit 10 s Raumrauschen und schlägt bei Abweichung eine erkannte
Zuordnung vor; über die Optionen jederzeit wiederholbar. Drehung und Spiegelung des Rings
sind unerheblich, weil Kalibrierung und Nachführung im selben Bezugssystem
arbeiten.

### 4. Installation

`install.sh` (ohne root):

1. prüft PipeWire, Python-Module (PySide6, numpy), Raw-Firmware
2. prüft Rust-Toolchain und DeepFilterNet-Plugin; fehlt es, Hinweis auf
   `yay -S deepfilternet-plugin-pipewire-bin` und Abbruch
3. baut das Plugin (`cargo build --release`) und kopiert
   `libuma8_beam.so` nach `~/.local/lib/ladspa/`
4. installiert das Python-Paket als Link auf das Repo, Dienst-Datei nach
   `~/.config/systemd/user/`, Autostart-Eintrag nach `~/.config/autostart/`
5. aktiviert und startet den Dienst

`uninstall.sh` entfernt alles davon wieder; Einstellungen nur auf Nachfrage.

## Fehlerbehandlung

| Fall | Verhalten |
|---|---|
| Gerät abgezogen | Kette wartet (target.object), Tray rot; beim Einstecken automatisch wieder verbunden |
| DSP-Firmware statt Raw | Tray rot: „Raw-Firmware nötig“ |
| DeepFilterNet fehlt | Dienst startet nicht, Tray rot mit Hinweis |
| Dienst abgestürzt | systemd startet neu; Tray zeigt rot, bis er wieder läuft |
| Nachführung ohne Audio | Azimuth bleibt stehen, Hinweis im Tooltip |
| Kaputte config.toml | Standardwerte, Hinweis im Tray; vor dem nächsten Speichern wird die kaputte Datei als `config.toml.broken` gesichert |
| `pw-cli` schlägt fehl | Fehler im Tooltip und im Log (`~/.local/state/uma8-callmic/log`) |

## Tests

Plugin, zwei Ebenen:

- Rust-Unit-Tests (`cargo test`) für die DSP-Module direkt
  (Verzögerungsgenauigkeit, Überblendung, Begrenzer).
- pytest: Das fertige `.so` wird per ctypes als LADSPA geladen und von
  außen geprüft wie ein Host:

- Richtwirkung: simulierte ebene Welle (Rauschen, 300–8000 Hz) aus 90°;
  Strahl auf 90° mindestens 6 dB lauter als Strahl auf 270° oberhalb 2 kHz
- Bruchteil-Verzögerung: Phasenfehler < 2° bis 8 kHz
- Schwenken: Sinus 1 kHz, Azimuth-Sprung 0° → 180° während der Wiedergabe;
  keine Diskontinuität größer als der normale Sinus-Schritt
- Latenz konstant über alle Azimuth-, Mode- und Dereverb-Umschaltungen
- Hallunterdrückung: Sprachähnliches Signal gefaltet mit synthetischem
  exponentiellem Nachhall (T60 = 0,6 s); Energie im Nachhallschwanz um
  mindestens 6 dB reduziert
- Robustheit: Stille, Vollaussteuerung, NaN-freie Ausgabe, Blockgrößen 1–4096
- CPU: Plugin verarbeitet 10 s Audio in < 0,2 s

Python (pytest):

- `doa.py`: synthetische 7-Kanal-Signale aus 12 Richtungen, mittlerer Fehler
  < 15°, mit und ohne Rauschen
- `geometry.py`: synthetisches diffuses Rauschen mit permutierten Kanälen,
  Mittelkanal und Ringnachbarschaft korrekt erkannt
- `config.py`, `chainconf.py`: Round-Trip, Validierung, erzeugte Konfiguration
  startet in einem Probelauf (`pipewire -c <datei>`, 2 s) ohne Fehler

Integration (manuell ausgelöst, braucht das Gerät):

- Dienst starten, 10 s vom virtuellen Mikrofon aufnehmen: kein Aussetzer
  (lückenlose Samples), Pegel plausibel, Energie oberhalb 8 kHz vorhanden
- Umschalten per Tray während der Aufnahme: kein Klick

Abnahme durch den Nutzer: Hörvergleich Roh gegen Bearbeitet per Tray-Umschalter
und ein echter Anruf.

## Projektstruktur

```
uma8-callmic/
├── plugin/            Rust-Crate: Cargo.toml, src/{lib.rs (LADSPA-Hülle), beam.rs, dereverb.rs, limiter.rs}
├── uma8_callmic/      Python-Paket (Module siehe oben), icons/
├── pipewire/          chain.conf.in, uma8-callmic-chain.service, uma8-callmic.desktop
├── tests/             test_plugin.py, test_doa.py, test_geometry.py, test_config.py
├── firmware/          docker-compose.usb.yml (Firmware-Dateien lokal, nicht im Repo)
├── docs/specs/        dieses Dokument
├── install.sh, uninstall.sh, README.md
```

Die miniDSP-Firmware-Dateien, der Windows-Treiber und das DFU-Tool sind
proprietär und werden per `.gitignore` aus dem Repository ausgeschlossen.

## Offene Punkte für die Umsetzung

- Die ODAS-Kanalzuordnung wird mit `geometry.py` am echten Gerät
  überprüft (Hardware-Aufgabe im Plan).
- Die Latenz von DeepFilterNet wird gemessen und als Konstante hinterlegt.
