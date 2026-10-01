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

- Adaptive Beamformer (MVDR/GSC mit laufend geschätzter Störkovarianz), andere
  Mikrofone, andere Betriebssysteme. Der superdirektive Beamformer ist ein
  festes MVDR gegen das ideale diffuse Feld, nicht adaptiv.
- Rückkehr zur DSP-Firmware per Software.

## Architektur

```
UMA-8 (Kanal 0–6, 48 kHz)          ein PipeWire-Prozess (systemd --user: uma8-callmic-chain.service)
   │
   ▼  filter-chain „Vorverstärkung“ (nur mit Echounterdrückung)
   │     7 × builtin linear, +24 dB ─► uma8_callmic_pre (Audio/Source/Internal, 7 Kanäle AUX0–6)
   ▼  echo-cancel (WebRTC AEC3, monitor.mode)           Referenz: Monitor der Standardausgabe
   │     7 Kanäle, je ein lineares Filter ─► uma8_callmic_aec (Audio/Source/Internal, 7 Kanäle)
   │
   ▼  filter-chain (Hauptkette; ohne Echounterdrückung direkt am UMA-8)
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
   └─ Nachführung: liest die 7 Kanäle hinter der Echounterdrückung (ohne sie:
      die 8 Kanäle des UMA-8) parallel mit, setzt „Azimuth“ live
```

Grundsatz: Das Tray-Programm ist nie im Tonweg. Fällt es aus, läuft der Ton
mit den zuletzt gesetzten Parametern weiter.

## Komponenten

### 1. LADSPA-Plugin `uma8-beam` (Rust)

Ein Rust-Crate (`cdylib`), das `libuma8_beam.so` mit zwei LADSPA-Plugins
erzeugt. Die LADSPA-Schnittstelle (`ladspa_descriptor`, Descriptor-Structs)
wird direkt über `extern "C"` und `#[repr(C)]` umgesetzt, ohne
Fremd-Crate. Die DSP-Logik liegt in sicherem Rust in eigenen Modulen
(`stft`, `beam`, `jacobi`, `cdr`, `dereverb`, `pipeline`, `limiter`) und ist
unabhängig von der LADSPA-Hülle testbar. `unsafe` bleibt auf die Hülle beschränkt; Panics
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
| Mode | 0/1/2 | 0 = superdirektiv, 1 = alle Richtungen (nur Mittel-Mikrofon), 2 = Delay-and-Sum (Vergleich) |
| Center Channel | 0–6 | Index des Mittel-Mikrofons |
| Ring Order 0–5 | 0–6 | Kanalindex der Ringmikrofone in Kreisreihenfolge |
| Ring Offset | 0–359,9° | Winkel des ersten Ringmikrofons |
| Radius | 20–60 mm | Ringradius |
| Gain | −30–60 dB | Verstärkung beider Ausgänge (Rohsignal ist sehr leise); mit Echounterdrückung „Verstärkung“ − 24 dB, weil die Vorverstärkung davor liegt |
| Raw Extra Delay | 0–4800 Samples | Zusatzverzögerung für „Raw Out“ = gemessene Latenz von DeepFilterNet |
| Dereverb | 0/1 | kohärenzbasierte Hallunterdrückung an/aus |
| Dereverb Strength | 0–1 | Stärke beider Hallstufen (Untergrenze der Dämpfung, Überschätzung) |
| Dereverb T60 | 0,1–1,5 s | angenommene Nachhallzeit des Raums (nur „Late Reverb“) |
| Late Reverb | 0/1 | späten Nachhall zusätzlich über das Abklingmodell dämpfen |
| Min WNG | −12–+6 dB | Untergrenze des White-Noise-Gains des superdirektiven Beams (Standard −3 dB) |

Namen und Reihenfolge der Ports sind Schnittstelle (die PipeWire-Konfiguration
nutzt die Namen); neue Controls kommen nur hinten dazu.

**Signalweg.** Alle Stufen teilen eine STFT: FFT 1024, Hop 256, periodisches
Wurzel-Hann als Analyse- und Synthesefenster (zusammen Hann, summiert sich bei
75 % Überlappung zu 2). Je Hop: 7 Kanäle fenstern und transformieren →
Beamformer → Verstärkung je Bin aus Kohärenz-Postfilter und Abklingmodell →
iFFT, Synthesefenster, Overlap-Add → „Gain (dB)“. „Beam Out“ hat in jedem
Modus und jeder Schalterstellung genau 1024 Samples (21,3 ms) Latenz; „Raw
Out“ ist das Mittel-Mikrofon, um 1024 + „Raw Extra Delay“ verzögert.

**Beamformer** (`beam.rs`).

- Steuervektor d_m = exp(+j·2πf·τ_m) mit τ_m = p_m·u/c und
  u = (cos el·cos az, cos el·sin az, sin el): Mikrofon m hört die ebene Welle
  um τ_m früher (gleiche Konvention wie `tests/sim.plane_wave` und die
  Vorwärts-FFT, per Test geprüft). Ausgang Y = Σ conj(w_m)·X_m, verzerrungsfrei
  zur Zielrichtung (wᴴd = 1), Bezugspunkt ist das Mittel-Mikrofon.
- Superdirektiv (Standard): w = (Γ+μI)⁻¹d / dᴴ(Γ+μI)⁻¹d mit
  Γ_ij = sinc(2πf·d_ij/c) (3D-diffuses Feld). μ je Bin ist der kleinste Wert in
  [10⁻⁶, 10⁴] (logarithmische Bisektion), bei dem WNG = 1/(wᴴw) ≥ „Min WNG“.
  Γ ist reell, symmetrisch und richtungsunabhängig; in Platz-Reihenfolge
  (Mitte, Ring 0–5) hängt es nur vom Radius ab und wird je Radius einmal mit
  einem eigenen zyklischen Jacobi-Verfahren (f64) zerlegt. Danach sind WNG,
  dᴴ(Γ+μI)⁻¹d und der Richtwirkungsfaktor DI = 1/(wᴴΓw) O(7)-Summen über die
  Eigenwerte. Entwurf in f64, Gewichte in f32.
- Delay-and-Sum (w = d/7) bleibt zum A/B-Vergleich; „alle Richtungen“ ist das
  Mittel-Mikrofon allein.
- Richtwirkungsindex gegen 3D-diffusen Schall (Az 0°, El 25°), in dB:

  | Frequenz | 300 Hz | 500 Hz | 1 kHz | 1,5 kHz | 2 kHz | 4 kHz |
  |---|---|---|---|---|---|---|
  | Delay-and-Sum | 0,2 | 0,4 | 1,6 | 3,4 | 5,0 | 8,0 |
  | superdirektiv, WNG ≥ −3 dB | 4,7 | 5,8 | 7,5 | 8,1 | 8,1 | 8,0 |

  Preis: unkorreliertes Mikrofonrauschen steigt unter ≈ 1,5 kHz um bis zu
  3 dB gegenüber einem einzelnen Mikrofon (Delay-and-Sum: −8,5 dB).
- Wechsel von Richtung, Geometrie, Modus oder WNG: alte und neue Gewichte
  laufen parallel, ihre Ausgänge werden über 10 Frames (≈ 53 ms) mit
  Kosinus-Rampe übergeblendet. Kein Klick, keine Latenzänderung. Kommt während
  einer Überblendung ein neues Ziel, folgt danach die nächste. Ungültige
  Geometrien werden ignoriert. Der Neuentwurf läuft allokationsfrei in `run()`:
  Richtung, Modus oder WNG ≈ 0,13 ms, Kanalzuordnung oder Ringdrehung ≈ 0,5 ms,
  neuer Radius ≈ 2,7 ms (Eigenzerlegung, nur nach der Geometrie-Prüfung).

**Hallunterdrückung, „Dereverb“** (`cdr.rs`): kohärenzbasierter Postfilter.

- Das Verhältnis von Direktschall zu diffusem Schall (CDR) wird je Bin mit dem
  richtungsunabhängigen Schätzer von Schwarz & Kellermann (IEEE/ACM TASLP 2015)
  bestimmt:
  CDR = (Γn·Re Γx − |Γx|² − √(Γn²·(Re Γx)² − Γn²·|Γx|² + Γn² − 2Γn·Re Γx + |Γx|²)) / (|Γx|² − 1),
  vorher |Γx| ≤ 0,999, danach CDR ≥ 0. Γx = Φ_ij/√(Φ_ii·Φ_jj) aus rekursiv
  geglätteten Spektren (35 ms), Γn = sinc-Kohärenz des Paars.
- Paare: die 3 Durchmesser (86 mm) und die 6 Sehnen über ein Ringmikrofon
  hinweg (74,5 mm). Sie trennen am frühesten (tiefe Frequenzen sind das
  Problem); Paare mit 43 mm trennen erst ab ≈ 1 kHz und kosten nur Rechenzeit.
  Ein Paar zählt ab Γn ≤ 0,93 (Durchmesser ab ≈ 430 Hz, Sehnen ab ≈ 500 Hz);
  der CDR wird über die nutzbaren Paare gemittelt und bis 8 kHz geschätzt.
  Darunter bzw. darüber gilt die mittlere Verstärkung der angrenzenden Oktave
  (Schätzung bis 12 oder 24 kHz änderte im Raum-Test nichts).
- Schätzfehler: Für kleine Kohärenz ist der Schätzer linear in |Γx|, und die
  geschätzte Kohärenz rein diffusen Schalls ist um ≈ 1/√N_eff zu groß
  (N_eff = Zahl unabhängiger Mittelungen). Mit 35 ms allein (N_eff ≈ 6) kam
  für rein diffusen Schall CDR ≈ 0,6 heraus; mal DI (bis ≈ 7) blieb diffuser
  Hall fast ungedämpft (−3,5 dB statt −20 dB). Abhilfe: Spektren und Γn
  zusätzlich über ±3 Bins (≈ 330 Hz) mitteln (N_eff ≈ 30, aus Frame- und
  Bin-Korrelation des Fensters berechnet) und 1,2/√N_eff ≈ 0,22 vom CDR
  abziehen. Ergebnis in Simulation: rein diffus CDR ≈ 0,03, Mischungen mit
  CDR ≥ 1 nahezu erwartungstreu; stationärer diffuser Schall wird um 14 dB
  (Stärke 0,6) bzw. 21 dB (Stärke 1) gedämpft. Kürzere Glättung (8–20 ms) dämpfte den Nachhall im
  Raum-Test schlechter, längere (50–70 ms) brachte nichts.
- Der Beam hat den diffusen Anteil bereits um DI gesenkt, am Ausgang gilt
  CDR·DI. Verstärkung G = max(1 − μ/(1 + CDR·DI), G_min) mit
  G_min = −25·s dB und μ = 1 + 0,5·s (s = „Dereverb Strength“; s = 0 lässt das
  Signal unverändert). G steigt sofort (Sprachanfänge bleiben erhalten) und
  fällt mit Faktor 0,8 je Hop (≈ 24 ms) gegen Musical Noise.

**Später Nachhall, „Late Reverb“** (`dereverb.rs`): Lebart/Habets auf dem
Beam-Spektrum. PSD des späten Nachhalls = PSD von vor 50 ms mal
exp(−2δ·50 ms), δ = 3·ln 10/T60; Wiener-artige Verstärkung mit Untergrenze
−15·s dB, Glättung 0,5 je Hop. Beide Verstärkungen werden multipliziert: Die
Kohärenz erkennt diffusen Schall auch während der Sprache, das Abklingmodell
den Nachhall in Pausen; im Raum-Test dämpfte das Produkt den Schwanz um 6 dB
mehr als das Minimum der beiden, bei gleichem SI-SDR. Eine falsch eingestellte
Nachhallzeit schadet wenig (T60 0,5 s im 0,7-s-Raum fast wie die wahre, 1,2 s
kostet ≈ 0,5 dB Direktschall). Abschalten beider Stufen führt die Verstärkung
in ≈ 25 ms auf 1 zurück, ohne Klick.

Hinweis zur Echounterdrückung der Anruf-Programme: Beide Stufen ändern die
Verstärkung je Frequenz schnell, wie DeepFilterNet dahinter auch. Eine AEC
dahinter (im Anruf-Programm) kann diesen zeitvarianten Echoweg kaum lernen;
deshalb sitzt die eigene Echounterdrückung (Komponente 3) davor, auf den
Rohkanälen.

**Auswertung** (`tools/eval_dereverb.py`, Raumsimulation `tools/roomsim.py`):
Spiegelquellenmethode, Quaderraum 4 × 3,5 × 2,6 m, frequenzunabhängige
Reflexion aus Sabine (gemessen T60 0,44 s bzw. 0,70 s), alle Spiegelquellen bis
T60, Bruchteil-Verzögerung per gefensterter Sinc. Array auf dem Schreibtisch
(z = 0,75 m, 0,5 m vor der Wand), Sprecher 0,6 m entfernt und 0,35 m höher
(El 30°), Sprache aus espeak-ng und ALSA-Testansagen, Mikrofon-Eigenrauschen
−35 dB unter Sprache. Plugin-Einstellung T60 = 0,5 s (Standard). SI-SDR gegen
den Direktschall am Mittel-Mikrofon (höher = besser); Schwanz = Pegel
50–400 ms nach Sprachende relativ zur Sprache (niedriger = besser):

| Konfiguration | SI-SDR 0,45 s | Schwanz 0,45 s | SI-SDR 0,7 s | Schwanz 0,7 s |
|---|---|---|---|---|
| Mittel-Mikrofon | −1,1 | −22,4 | −3,4 | −16,0 |
| Delay-and-Sum | −0,1 | −22,5 | −2,5 | −15,9 |
| bisher: Delay-and-Sum + spät, 0,6 | 0,3 | −28,0 | −1,9 | −19,0 |
| superdirektiv | 4,3 | −22,8 | 1,9 | −16,2 |
| superdirektiv + Kohärenz, 0,6 | 4,4 | −31,1 | 2,4 | −24,8 |
| **superdirektiv + Kohärenz + spät, 0,6 (Standard)** | **4,5** | **−37,7** | **2,7** | **−28,3** |
| superdirektiv + Kohärenz + spät, 1,0 | 4,2 | −41,6 | 2,5 | −31,5 |

Mit 10° Azimut- und 10° Höhenfehler des Strahls verliert der Standard nur
0,3–0,4 dB SI-SDR. Der Direktschall wird im Standard um ≈ 2,6–3,2 dB leiser,
überwiegend unter 1 kHz (dort ist der Beam-Ausgang auch während der Sprache
hallig); „Gain“ gleicht den Pegel aus. Gewählte Standards und Gründe:

- WNG ≥ −3 dB: mit ideal gleichen Mikrofonen wären −6 dB um 0,6 dB SI-SDR
  besser, bei 0,5 dB Streuung (Standardabweichung) der Empfindlichkeit nur
  noch 0,2 dB, bei 1 dB Streuung ist −3 dB am besten (−6 und 0 dB je
  0,2–0,3 dB schlechter). MEMS-Toleranz ist typisch ±1 dB.
- Stärke 0,6: von 0,3 bis 1,0 kostet jede Stufe nur wenig SI-SDR (0,4 dB
  insgesamt) bei 9 dB weniger Schwanz; 0,6 ist die vorsichtige Mitte, weil
  Musical Noise in diesen Maßen nicht sichtbar ist. Mehr Wirkung: Regler
  „Stärke“.
- Hallunterdrückung und später Nachhall standardmäßig an: zusammen 9–10 dB
  weniger Schwanz als die bisherige Kette und +4,2–4,6 dB SI-SDR.

`tools/offline.py` verarbeitet eine echte 8-Kanal-Aufnahme
(`pw-record --target <Raw-Quelle> --channels 8 --format f32 rec.wav`) mit den
Einstellungen aus `config.toml` und einzeln überschreibbaren Werten zu einem
Mono-WAV, für den Hörvergleich im eigenen Raum.

**Echtzeitregeln:** keine Speicherallokation, keine Locks, keine
Systemaufrufe in `run()`; alle Puffer werden in `instantiate()` angelegt,
Neuentwürfe arbeiten auf vorhandenen Puffern. Geglättete Spektren unter
10⁻³⁰ werden auf 0 gesetzt (keine Denormals). Nicht endliche Eingangswerte
werden 0; läuft ein Frame bei absurd großen Werten über, wird er verworfen und
die Schätzzustände zurückgesetzt, die Ausgabe bleibt NaN-frei. FFT über das
Crate `realfft` mit vorab geplanten Instanzen. Rechenzeit (10 s Audio, ein
Kern): superdirektiv 42 ms, mit beiden Hallstufen 90 ms (≈ 0,9 % CPU).

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
  Kanal 7 wird nicht verwendet; mit Echounterdrückung stattdessen
  `uma8_callmic_aec`, 7 Kanäle `AUX0–6` (Komponente 3). `node.passive = true`,
  damit das Gerät nur läuft, wenn jemand das virtuelle Mikrofon nutzt oder das
  Tray mithört.
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
  ein Neustart des Dienstes ist nur nach einer Neuinstallation oder beim
  Umschalten der Echounterdrückung nötig.

### 3. Echounterdrückung (PipeWire `echo-cancel`)

Ziel: Was aus den Lautsprechern ins Mikrofon gelangt, soll das Gegenüber nicht
hören. Ohne eigene AEC hörte sich das Gegenüber selbst, und dessen
Anruf-Programm schaltete bei Gegensprechen das eigene Mikrofon stumm: Der
Echoweg durch Beam, Hallunterdrückung und DeepFilterNet ist nichtlinear und
zeitvariant, die AEC des Programms lernt ihn nicht und unterdrückt hart.

Aufbau, alles im selben PipeWire-Prozess, nur mit `echo_cancel = true`
(Standard), sonst ist die Kette genau die bisherige:

- **Vorverstärkung** (filter-chain): liest das UMA-8 wie sonst die Hauptkette
  (8 Kanäle, Gerätepositionen, `stream.dont-remix`, passiv), verwirft Kanal 7
  und verstärkt die 7 Mikrofone um 24 dB. builtin `linear` klemmt „Mult“ still
  auf ±10 (+20 dB), deshalb zwei gleiche Stufen (×3,98) je Kanal. Ausgang
  `uma8_callmic_pre`, `Audio/Source/Internal`, `AUX0–6`.
- **echo-cancel** mit `monitor.mode = true`: Referenz ist der Monitor der
  Standardausgabe (Stereo), WirePlumber folgt deren Wechsel. Eingang
  `uma8_callmic_pre` (passiv), Ausgang `uma8_callmic_aec`
  (`Audio/Source/Internal`, 7 Kanäle). WebRTC AEC3 rechnet je Kanal ein
  eigenes lineares Filter; die nichtlineare Nachunterdrückung ist für alle
  Kanäle gleich, die Phasenbeziehungen für Beam und Nachführung bleiben also
  erhalten. `aec.args`: `webrtc.noise_suppression = false` (Standard wäre an;
  DeepFilterNet folgt), `webrtc.gain_control = false`,
  `webrtc.high_pass_filter = true`. Mit WebRTC 2.x gibt es sonst nur noch
  `webrtc.mobile_mode` (aus). Das Modul fordert 10-ms-Blöcke an
  (`node.latency` 480/48000) und puffert bei anderer Quantengröße selbst;
  ≈ 9 ms zusätzliche Latenz.
- **Hauptkette** liest `uma8_callmic_aec` mit 7 Kanälen; „Gain (dB)“ von
  `uma8_beam` = „Verstärkung“ − 24 dB. Die Einstellung „Verstärkung“ bleibt
  die Gesamtverstärkung (0–60 dB).

`Audio/Source/Internal`: pipewire-pulse zeigt solche Knoten nicht (also auch
nicht die KDE-Lautstärkeregelung), WirePlumber macht sie nie zum Standard,
per `target.object` sind sie verbindbar und schlafen normal.
`Audio/Source/Virtual` ließ PipeWire 1.6.9 bei Stream-Knoten abstürzen; der
Referenz-Stream darf keine eigene `node.group` bekommen (dann arbeitet die AEC
nicht).

Warum +24 dB: Offline mit der PipeWire-Konfiguration der AEC3 nachgerechnet
(synthetischer Raum, Rohpegel mit −75 dBFS Grundrauschen): ohne Verstärkung
ERLE 19 statt 25 dB und Sprache des Nutzers bei Gegensprechen um 14 statt
4,4 dB gedämpft; +30 dB war nicht besser und kostet Aussteuerungsreserve.
7 Kanäle waren nie schlechter als einer.

Messung im echten PipeWire-Graphen (ohne Hardware: 8-kanalige Ersatzquelle,
Referenz über eine eigene Senke, Echo = Referenz über einen Raumpfad mit
Reflexionen und diffusem Nachhall T60 0,3 s, Rohpegel Echo −45 dBFS, Nutzer
−50 dBFS, Rauschen −75 dBFS):

| Messung | Ergebnis |
|---|---|
| Vorverstärkung | +24,00 dB |
| nur Gegenseite, erste 4 s | Echo 29 dB leiser |
| nur Gegenseite, eingeschwungen | −20 dBFS → −67 dBFS (47 dB, unter dem Grundrauschen) |
| nur Nutzer | −0,3 dB |
| Gegensprechen, Nutzeranteil | −19 dB (ohne diffusen Nachhall −9 dB; Echo 5 dB leiser als Nutzer: −8 dB) |
| Gesamtverstärkung Roh-Weg | +29,7 dB (ohne AEC +30,0 dB) |
| Rechenzeit AEC-Knoten | ≈ 0,31 ms je 5,3-ms-Zyklus (≈ 6 % eines Kerns) |

Bei Gegensprechen dämpft die AEC3-Nachunterdrückung den Nutzer also deutlich,
je lauter das Lautsprecher-Echo am Array und je länger der Nachhall. Die
Abhilfe dafür liegt außerhalb der Kette: Lautsprecher leiser oder weiter weg.

Aussteuerung: Die AEC begrenzt ihren Ausgang hart auf ±1 (0 dBFS, live mit
einem Sinus geprüft: Vorstufe 1,98, AEC-Ausgang 1,00). Rohspitzen über
−24 dBFS werden also abgeschnitten, und abgeschnittenes Echo kann die AEC
nicht mehr linear entfernen. Mit der Standardverstärkung von 30 dB setzt der
Begrenzer am Ende schon ab −31 dBFS Rohspitze ein; betroffen sind nur sehr
laute Quellen nah am Array (Lautsprecher daneben, Klopfen auf den Tisch).

Bekannte Nachteile:

- Jede Wiedergabe auf der Standardausgabe weckt die ganze Kette samt UMA-8,
  auch ohne Anruf: Der Referenz-Stream ist passiv mit dem Monitor verbunden,
  und PipeWire plant über die `node.group` des Moduls die ganze AEC mit
  (live gesehen: alle Knoten „running“, ohne Aufnahme). Abhilfe wäre eine
  dynamisch verbundene Referenz (WirePlumber-Skript); nicht umgesetzt.
- Nur Ton auf der Standardausgabe wird entfernt. Gibt das Anruf-Programm auf
  einem anderen Gerät aus, bleibt dessen Echo.
- Die eigene AEC der Anruf-Programme darf an bleiben; sie findet kaum noch
  Echo.

Umschalten (Optionen → „Echounterdrückung (Lautsprecher)“) ändert den Aufbau:
Das Tray schreibt die Konfiguration und startet den Dienst neu
(`systemctl --user try-restart --no-block`, kurze Tonpause, Hinweis im Tray).
Fehlt `uma8_callmic_aec`, obwohl die Kette läuft (etwa alte Konfiguration),
wird das Tray rot: „Echounterdrückung nicht geladen“.

### 4. Tray-Programm (Python 3, PySide6, numpy)

Module:

| Modul | Aufgabe |
|---|---|
| `config.py` | Einstellungen lesen/schreiben (`~/.config/uma8-callmic/config.toml`), Standardwerte, Validierung |
| `array.py` | Array-Geometrie (Kanal → Position), identisch zur Rust-Seite |
| `params.py` | Abbildung Einstellungen → Plugin-Controls (für Konfiguration und Live-Änderungen) |
| `constants.py` | Pfade, Knotennamen, gemessene Latenzen |
| `pwctl.py` | PipeWire-Anbindung über `pw-dump`/`pw-cli`/`systemctl --user`: Zustand lesen, Parameter setzen, Dienst steuern |
| `capture.py` | Mehrkanal-Aufnahme über einen `pw-record`-Unterprozess (float32, 48 kHz), Ringpuffer. Immer mit den Kanalpositionen der Quelle (`--channel-map`): pw-record nähme sonst 7.1 bzw. 7.0, und PipeWire mischte um (gemessen: FLC/FRC landeten in FL/FR, Kanal 6/7 blieben stumm; an der AEC-Quelle kamen nur AUX0/1 an). Ohne Ausweichen aufs Standardmikrofon (`node.dont-fallback`): fehlt die Quelle, endet die Aufnahme |
| `doa.py` | Richtungsschätzung: SRP-PHAT über 72 Azimuth- × 4 Elevationswerte, Sprachaktivitätserkennung (Energie + spektrale Flachheit) |
| `geometry.py` | Kanalzuordnung und Radius aus Raumrauschen: Kohärenzmatrix, Mittel-Mikrofon = höchste mittlere Kohärenz, Ringreihenfolge und Radius per Fit an sinc(k·d) |
| `tracker.py` | Nachführung: alle 0,2 s DOA bei Sprache, Median über 1 s, Hysterese 15°, setzt Azimuth. Hört mit Echounterdrückung auf `uma8_callmic_aec` (7 Kanäle): Sprache aus den Lautsprechern ist dort entfernt, der Strahl folgt nie dem Lautsprecher (im Test erkannte die Sprachaktivität Lautsprecher-Sprache vor der AEC in 39 von 43 Blöcken, dahinter in 3). Kalibrierung und Kanalzuordnung lesen weiter das UMA-8 direkt. Endet die Aufnahme (Kette neu gestartet), verbindet das Tray neu |
| `chainconf.py` | erzeugt `uma8-callmic.conf` aus Vorlage und Einstellungen |
| `tray.py` | Icon, Menü, Umschalten, Zustandsabfrage alle 2 s |
| `dialogs.py` | Optionen, Kalibrierung, Kanalzuordnung |

Tray-Zustände:

| Icon | Zustand |
|---|---|
| farbig | aktiv (Beam-Weg) |
| grau | deaktiviert (Roh-Weg) |
| rot | Problem: Gerät fehlt, falsche Firmware, Dienst läuft nicht, DeepFilterNet fehlt, Echounterdrückung nicht geladen; Details im Tooltip |

Linksklick = umschalten. Rechtsklick-Menü: ☑ Aktiv · Kalibrieren… ·
Optionen… · Beenden (nur Tray; Dienst läuft weiter).

Optionen:

- Richtung: Kalibriert (Standard) · Manuell (Azimuth-Regler) ·
  Automatisch nachführen · Alle Richtungen
- Beamformer: superdirektiv (Standard) oder Delay-and-Sum zum Vergleich
  (bei „Alle Richtungen“ ohne Wirkung)
- Hallunterdrückung (Standard an), späten Nachhall zusätzlich dämpfen
  (Standard an, mit Nachhallzeit des Raums), Stärke für beide
- Rauschunterdrückung: 0–100 dB, Standard 30 dB
- Verstärkung: dB-Regler mit Pegelanzeige
- Echounterdrückung (Lautsprecher), Standard an; Umschalten startet die Kette
  neu
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

### 5. Installation

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
| Echounterdrückung an, aber nicht in der laufenden Kette | Tray rot: „Echounterdrückung nicht geladen (Dienst … neu starten)“ |
| Kaputte config.toml | Standardwerte, Hinweis im Tray; vor dem nächsten Speichern wird die kaputte Datei als `config.toml.broken` gesichert |
| `pw-cli` schlägt fehl | Fehler im Tooltip und im Log (`~/.local/state/uma8-callmic/log`) |

## Tests

Plugin, zwei Ebenen:

- Rust-Unit-Tests (`cargo test`) für die DSP-Module direkt: STFT
  rekonstruiert exakt, Jacobi-Zerlegung, Beamformer verzerrungsfrei mit
  WNG-Untergrenze und erwartetem DI, Kanalzuordnung über Plätze, Vorzeichen
  des Steuervektors gegen eine analytische ebene Welle, CDR-Schätzer liefert
  einen vorgegebenen CDR exakt und für diffusen Schall 0, Postfilter dämpft
  synthetisch diffuses Feld und lässt ebene Wellen durch, Überblendung,
  Latenz, NaN-Eingänge, später Nachhall, Begrenzer.
- pytest: Das fertige `.so` wird per ctypes als LADSPA geladen und von
  außen geprüft wie ein Host:

- Richtwirkung: simulierte ebene Welle (Rauschen, 2–6 kHz) aus 90°;
  Strahl auf 90° mindestens 6 dB lauter als Strahl auf 270° (superdirektiv
  und Delay-and-Sum)
- Diffuses Feld: superdirektiv mindestens 3 dB (400–1400 Hz) bzw. 2 dB
  (1,4–2 kHz) leiser als Delay-and-Sum
- Zielrichtung verzerrungsfrei: Ausgang = Mittel-Mikrofon, Fehler < −40 dB
- Schwenken und Moduswechsel: Sinus 1 kHz, Azimuth-Sprung 0° → 180°, danach
  Moduswechsel; keine Diskontinuität größer als der normale Sinus-Schritt
- Latenz genau 1024 Samples in allen Modi und Hall-Schalterstellungen
- Hallunterdrückung: Rauschstoß plus abklingendes diffuses Feld (T60 0,5 s,
  diffus 3,5 dB stärker als direkt); Schwanz mindestens 8 dB (Kohärenz) bzw.
  14 dB (mit spätem Nachhall) leiser, Stoß höchstens 4 dB
- Robustheit: Stille, Vollaussteuerung, NaN-freie Ausgabe, Blockgrößen 1–4096
- CPU: Plugin verarbeitet 10 s Audio in < 0,2 s

Python (pytest):

- `doa.py`: synthetische 7-Kanal-Signale aus 12 Richtungen, mittlerer Fehler
  < 15°, mit und ohne Rauschen
- `geometry.py`: synthetisches diffuses Rauschen mit permutierten Kanälen,
  Mittelkanal und Ringnachbarschaft korrekt erkannt
- `config.py`, `chainconf.py`: Round-Trip, Validierung, erzeugte Konfiguration
  startet in einem Probelauf (`pipewire -c <datei>`, 2 s) ohne Fehler; beide
  Kettenvarianten werden geparst (SPA-JSON) und geprüft: Knotennamen,
  Klassen, Kanalzahlen und -positionen, `aec.args`, Gesamtverstärkung jeder
  Vorverstärkungsstrecke, `null`-Eingang nur ohne Echounterdrückung

Integration (manuell ausgelöst, braucht das Gerät):

- Dienst starten, 10 s vom virtuellen Mikrofon aufnehmen: kein Aussetzer
  (lückenlose Samples), Pegel plausibel, Energie oberhalb 8 kHz vorhanden
- Umschalten per Tray während der Aufnahme: kein Klick

Abnahme durch den Nutzer: Hörvergleich Roh gegen Bearbeitet per Tray-Umschalter
und ein echter Anruf.

## Projektstruktur

```
uma8-callmic/
├── plugin/            Rust-Crate: Cargo.toml, src/{lib.rs, ladspa.rs (LADSPA-Hülle), plugins.rs, pipeline.rs,
│                      stft.rs, beam.rs, jacobi.rs, cdr.rs, dereverb.rs, limiter.rs}
├── tools/             eval_dereverb.py + roomsim.py (Raumsimulation), offline.py (Aufnahme verarbeiten),
│                      check_output.py
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
