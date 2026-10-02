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
| Kanalpositionen | in Gerätereihenfolge `FL FR FC LFE RL RR SL SR` (Knoten: `FL FR RL RR FC LFE SL SR`, ACP). Die USB-Belegung nennt Kanal 6/7 `FLC FRC`; diese Namen hat der Knoten nicht, PipeWire würde Kanal 6 stumm liefern und in Kanal 4 mischen |
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

uma8-callmic --ref-linker (im selben Dienst, nur mit Echounterdrückung): verbindet
   die Referenz nur, solange ein Programm „UMA-8 Call Mic“ aufnimmt

Tray-Programm (Python/PySide6)
   ├─ zeigt Zustand, schaltet um (pw-cli set-param, live)
   ├─ Optionen, Kalibrierung, Kanalzuordnung, Arbeitsplatz-Profil, Platzierung
   └─ Nachführung: liest die 7 Kanäle hinter der Echounterdrückung (ohne sie:
      die 8 Kanäle des UMA-8) parallel mit, setzt „Azimuth“ live – nur während
      ein Programm „UMA-8 Call Mic“ aufnimmt
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
| Null 1/2 Azimuth, Null 1/2 Elevation | 0–360°, 0–90° | feste Störrichtungen (Lautsprecher), Bezugssystem wie Azimuth; ein Lautsprecher: beide gleich |
| Null Weight | 0–40 dB | Gewicht der Nullstellen, 0 = aus (Standard), empfohlen 10; nur superdirektiv (Komponente 5) |

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
- Nullstellen (optional, nur superdirektiv; Ports 28–32: „Null 1 Azimuth (deg)“,
  „Null 1 Elevation (deg)“, „Null 2 Azimuth (deg)“, „Null 2 Elevation (deg)“,
  „Null Weight (dB)“ 0–40, 0 = aus, empfohlen 10): feste Störrichtungen für
  Lautsprecher. MVDR gegen R = Γ + β/5·Σ ddᴴ mit β = 10^(dB/10) − 1; jede
  Nullstelle ist aus 5 Richtungen (Mitte, ±8° in Azimut und Elevation, je β/5)
  aufgeweitet, damit Richtungsfehler der Messung (±5–10°) nicht ins Leere
  laufen. β wird zwischen 4 und 6 kHz mit Kosinus ausgeblendet (darüber kosten
  Nullstellen viel Richtwirkung, der Sprecher wird halliger, das Echo nicht
  leiser). R ist komplex, Zerlegung per Jacobi wie Γ. Gleiche Richtungen
  zählen einmal; ausgeschaltet bitgleich zur Ausgabe ohne Nullstellen.
  Simulation (`tools/eval_nulls.py`, Lautsprecher ±100°, 5° Richtungsfehler,
  ±1 dB Mikrofonstreuung, T60 0,45 s): Direktschall der Lautsprecher
  −5/−11/−9 dB bei 1/2/4 kHz, Sprecher praktisch unverändert (nach den
  Hallstufen ≤ 0,3 dB schlechter); das Gesamtecho sinkt aber nur um
  0,3–1 dB, weil dort Reflexionen überwiegen – daher standardmäßig aus,
  Nutzen an echten Aufnahmen prüfen.
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
  Geometrien werden ignoriert. Der Neuentwurf läuft allokationsfrei in `run()`
  und ist über mehrere Hops verteilt: Budget 512 Kosteneinheiten je Hop (Gewichte
  eines Bins = 1, Eigenzerlegung eines Bins = 15, mit Nullstellen komplexe
  Zerlegung = 30 je Bin bis 6 kHz); bis zum Abschluss gelten die alten Gewichte.
  Der erste Frame nach `instantiate()` entwirft einmal synchron. Die Überblendung
  beginnt bei Richtung, Modus, WNG oder Kanalzuordnung ≈ 2 Hops nach der
  Änderung, bei neuem Radius ≈ 17 Hops (≈ 90 ms), bei neuen Nullstellen
  ≈ 10 Hops plus Überblendung (≈ 0,11 s). Längster einzelner `run()` bei
  480 Samples ≈ 0,4 ms für Radius- und Nullstellenwechsel (vorher 2,3–2,9 ms
  auf einmal).

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
  Bin-Korrelation des Fensters berechnet) und je Mikrofonpaar die erwartete
  Schätzrauschleistung (Carter) (1−|Γ̂|²)(2−|Γ̂|²)/(2·N_eff) von der Abweichung
  Γ̂−Γn abziehen (Leistungssubtraktion, die Richtung bleibt), erst danach folgt
  der Schwarz-Kellermann-Schätzer. Die Korrektur verschwindet bei kohärenten
  Bins (|Γ̂| → 1); ein fester Abzug vom CDR (früher 1,2/√N_eff ≈ 0,22) drückte
  dagegen auch Bins mit überwiegendem Direktschall auf die Untergrenze
  (8–24 % in der Simulation). Überschätzung β = 1 bis Stärke 0,6, darüber
  linear bis β = 3 bei Stärke 1 (tiefere Untergrenze, einzelne Fehl-Bins
  fielen sonst als Musical Noise auf). Ergebnis in Simulation: rein diffus
  CDR ≈ 0; Standardkette 0,9–2,6 % Direktschall-Bins an der Untergrenze (fester
  Abzug: 3–6 %), Nachhallschwanz −37,5 statt −37,7 dB. Kürzere Glättung
  (8–20 ms) dämpfte den Nachhall im Raum-Test schlechter, längere (50–70 ms)
  brachte nichts.
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
| superdirektiv | 4,3 | −22,9 | 1,9 | −16,2 |
| superdirektiv + Kohärenz, 0,6 | 4,5 | −30,9 | 2,4 | −24,5 |
| **superdirektiv + Kohärenz + spät, 0,6 (Standard)** | **4,6** | **−37,5** | **2,8** | **−28,0** |
| superdirektiv + Kohärenz + spät, 1,0 | 4,2 | −43,4 | 2,3 | −34,7 |

Unkorreliertes Sensorrauschen am Ausgang relativ zu einem Mikrofon (dB, bei
−35 dB Eigenrauschen je Mikrofon; niedriger = besser), Standardkette: 125 Hz–1 kHz
−1,3…−1,5, 2 kHz −7,0, 4 kHz −17,2, 8 kHz −21,2 (nur superdirektiv: +2,9 unter
1 kHz, −8 dB ab 4 kHz). Stärke 1,0: 2 kHz −12,4, 4 kHz −27,3, 8 kHz −33,9 dB.

Mit 10° Azimut- und 10° Höhenfehler des Strahls verliert der Standard nur
0,3 dB SI-SDR. Der Direktschall wird im Standard um ≈ 2,5–3,0 dB leiser,
überwiegend unter 1 kHz (dort ist der Beam-Ausgang auch während der Sprache
hallig); „Gain“ gleicht den Pegel aus. Gewählte Standards und Gründe:

- WNG ≥ −3 dB: mit ideal gleichen Mikrofonen wären −6 dB um 0,6 dB SI-SDR
  besser, bei 0,5 dB Streuung (Standardabweichung) der Empfindlichkeit nur
  noch 0,2 dB, bei 1 dB Streuung ist −3 dB am besten (−6 und 0 dB je
  0,2–0,3 dB schlechter). MEMS-Toleranz ist typisch ±1 dB.
- Stärke 0,6: von 0,3 bis 1,0 kostet jede Stufe nur wenig SI-SDR (0,5–0,6 dB
  insgesamt) bei 11–12 dB weniger Schwanz; 0,6 ist die vorsichtige Mitte, weil
  Musical Noise in diesen Maßen nicht sichtbar ist. Mehr Wirkung: Regler
  „Stärke“.
- Hallunterdrückung und später Nachhall standardmäßig an: zusammen 9–9,5 dB
  weniger Schwanz als die bisherige Kette und +4,3–4,7 dB SI-SDR.

`tools/offline.py` verarbeitet eine echte 8-Kanal-Aufnahme
(`pw-record --target <Raw-Quelle> --channels 8 --channel-map FL,FR,FC,LFE,RL,RR,SL,SR --format f32 rec.wav`) mit den
Einstellungen aus `config.toml` und einzeln überschreibbaren Werten zu einem
Mono-WAV, für den Hörvergleich im eigenen Raum.

**Echtzeitregeln:** keine Speicherallokation, keine Locks, keine
Systemaufrufe in `run()`; alle Puffer werden in `instantiate()` angelegt,
Neuentwürfe arbeiten auf vorhandenen Puffern. Geglättete Spektren unter
10⁻³⁰ werden auf 0 gesetzt (keine Denormals), nicht endliche Schätzerzustände
ebenfalls (ein ∞ bliebe sonst dauerhaft). Der Eingang wird in `Stft::push` auf
±1000 begrenzt (Audio ist ±1, mit +24 dB Vorverstärkung ±16), nicht endliche
Werte werden 0; so kann kein Schätzer überlaufen (ein einzelner riesiger Wert
dämpfte vorher dauerhaft um 7,7 dB). Läuft ein Frame trotzdem über, wird er
verworfen und die Schätzzustände zurückgesetzt, die Ausgabe bleibt NaN-frei. FFT über das
Crate `realfft` mit vorab geplanten Instanzen. Rechenzeit (10 s Audio, ein
Kern): Standardkette (superdirektiv, beide Hallstufen) ≈ 107 ms (≈ 1,1 % CPU); der Test
`test_cpu_budget` verlangt den Bestwert aus drei Läufen ≤ 0,25 s.

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
- DeepFilterNet: `libdeep_filter_ladspa.so`, Label `deep_filter_mono`,
  bevorzugt aus dem Paket `deepfilternet-ladspa` dieses Repos (RPM bzw.
  PKGBUILD: 0.5.6 mit behobenem Thread-Leck). Das AUR-Paket
  `deepfilternet-plugin-pipewire-bin` geht auch, behält aber das Leck.
  Control „Attenuation Limit (dB)“ (0–100) = Optionswert
  „Rauschunterdrückung“. Die vom AUR-Paket mitgelieferte Beispielkette
  (`/etc/pipewire/filter-chain.conf.d/deepfilter-mono-source.conf`,
  Dienst `filter-chain.service`) bleibt deaktiviert.
- Plugin-Pfade (`constants.find_plugin`, bei jedem Schreiben der
  Konfiguration neu bestimmt): erster vorhandener Ort aus
  `~/.local/lib/ladspa` (Entwickler-Installation), `/usr/lib64/ladspa`
  (Fedora), `/usr/lib/ladspa` (Arch). Ein Ort, der nur ein anderer Name
  eines anderen, kanonischen Suchorts ist (Arch: `/usr/lib64` → `lib`),
  entfällt; ein per Symlink verlegtes `~/.local/lib` bleibt. Ist nichts
  installiert, gilt der erste Systemort (Fedora `/usr/lib64/ladspa`, Arch
  `/usr/lib/ladspa`), das Tray meldet das Fehlen. `UMA8_BEAM_PLUGIN` bzw.
  `UMA8_DFN_PLUGIN` erzwingen einen Pfad.
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
  Standardausgabe (Stereo), verbunden aber nur während einer Aufnahme (siehe
  „Referenz nur während einer Aufnahme“). Eingang
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

#### Referenz nur während einer Aufnahme (`reflink.py`)

Dauerhaft mit dem Monitor verbunden, weckte jede Wiedergabe auf der
Standardausgabe die ganze Kette samt UMA-8, AEC, Beam und DeepFilterNet, auch
ohne Anruf (DeepFilterNet allein ≈ 18 % eines 5950X-Kerns; auf einem Laptop
Akku und Lüfter). Ursache in PipeWire 1.6.9, `src/pipewire/context.c`
(`run_nodes` Z. 999, `collect_nodes` Z. 1080–1205): Ab einem lauffähigen
Knoten folgt `run_nodes` allen vorbereiteten Links, passive eingeschlossen,
und allen Knoten derselben `node.group`/`node.link-group`. Wiedergabe-Stream →
Senke → Monitor → Referenz-Stream → (Gruppe des Moduls) AEC-Eingang und
-Ausgang → Vorverstärkung → UMA-8 sowie → Hauptkette → `uma8_callmic`.

Ohne verbundene Referenz arbeitet das Modul weiter
(`src/modules/module-echo-cancel.c`, 1.6.9): `process()` läuft nur, wenn
Aufnahme- und Referenz-Stream im selben Zyklus geliefert haben
(`capture_cycle == sink_cycle`, Z. 577–583 und 987–993). Der Referenz-Stream
trägt aber die `node.group` des Moduls (Z. 1357–1360, 1422–1423), wird also mit
dem Aufnahme-Stream eingeplant (live: „running“ ohne Link), sein Adapter
liefert Stille, und `aec_run` bekommt eine Null-Referenz (Z. 431). WebRTC AEC3
reicht das Mikrofon dann durch, gemessen an `uma8_callmic_pre` gegen
`uma8_callmic_aec`: 150 Hz–4,8 kHz Kohärenz ≥ 0,999 bei −0,7…+0,1 dB, darunter
der Hochpass (60–150 Hz −4 dB), darüber Kohärenz 0,89–0,95 bei −0,8…−0,1 dB. Kein
Stocken, auch nicht beim Trennen mitten in der Aufnahme. Fehlt der Helfer,
bleibt das Mikrofon also nutzbar, nur ohne Echounterdrückung.

Umsetzung:

- Referenz-Stream mit `node.autoconnect = false` (`stream.c` setzt den
  Schlüssel nur, wenn er fehlt): WirePlumber verbindet ihn nie.
- `uma8-callmic --ref-linker` verbindet den Monitor der Standardausgabe
  (`default.audio.sink` der Metadaten „default“) passiv mit der Referenz
  (`pw-link -P`, Kanal für Kanal; Mono-Senke auf beide, ohne gemeinsame
  Kanalnamen der Reihe nach), sobald ein Knoten hinter `uma8_callmic` läuft,
  und trennt 2 s nach der letzten Aufnahme (Programme öffnen das Mikrofon beim
  Anrufstart oft mehrmals). Wechselt die Standardausgabe im Anruf: erst neu
  verbinden, dann trennen.
- Kriterium ist ein laufender Abnehmer, nicht der Zustand von `uma8_callmic`:
  Mit verbundener Referenz hält schon Musik die ganze Kette samt
  `uma8_callmic` auf „running“ (aufgezeichnet: `tests/data/reflink_graphs.json`,
  Zustand `ended`), die Referenz bliebe für immer verbunden. Gestoppte
  Abnehmer (corked) laufen nicht und zählen nicht.
- Ereignisgesteuert: liest `pw-dump --monitor` (geänderte Objekte vollständig,
  entfernte als `"info": null`, von Metadaten nur geänderte Einträge) und
  entscheidet nach jedem Änderungsblock und nach Ablauf der Wartezeit; kein
  Abfragen. Standardausgabe noch unbekannt (direkt nach dem Start): nichts
  ändern. Angeforderte Links gelten 2 s als unterwegs, danach neuer Versuch.
  Endet pw-dump, startet er ihn neu (2 s, bei wiederholtem Scheitern bis 60 s).
- Lebensdauer: Die Kettenkonfiguration startet ihn per `context.exec`
  (PipeWire 1.6.9: `src/pipewire/conf.c` Z. 942–1090, in
  `pw_context_new` nach Modulen und Objekten, `context.c` Z. 577), nur mit
  `echo_cancel = true`. Doppelter fork; der Prozess bleibt in der cgroup des
  Dienstes und endet mit ihm, kein eigener Dienst, nichts zu paketieren.
  Er gibt beim Start die geerbte Signalmaske frei: PipeWire blockiert
  SIGINT/SIGTERM (liest sie per signalfd), sonst überhörte er SIGTERM
  (gemessen: Stopp wartete 45 s, dann SIGABRT). Ohne systemd (Kette von Hand
  gestartet) endet er 5 s, nachdem der Referenz-Knoten verschwunden ist.
- Stirbt er allein, bleiben bestehende Links; der laufende Anruf ist
  ungestört. Bis zum nächsten Start des Dienstes fehlt dann bei neuen Anrufen
  die Echounterdrückung bzw. weckt eine verbliebene Verbindung wieder. Neu
  gestartet übernimmt er vorhandene Links und trennt verwaiste sofort.
- Tests: Entscheidungslogik auf aufgezeichneten pw-dump-Graphen der
  Testkette (`tests/test_reflink.py`). Für Live-Tests nennt `UMA8_REF_METADATA`
  ein eigenes Metadaten-Objekt statt „default“, die Standardausgabe des Nutzers
  bleibt unberührt.

Messung im echten Graphen (Testkette ohne UMA-8 wie oben, Referenz über eine
Testsenke mit eigenem Takt, Ryzen 9 5950X):

| Messung | Ergebnis |
|---|---|
| Musik auf der Senke, niemand nimmt auf | Kette bleibt „suspended“, keine Links zur Referenz; Kettenprozess 0,3–0,9 % (davon die Testsenke), dauerhaft verbundene Referenz: 5,9 % |
| Aufnahme beginnt → Referenz verbunden | 39–50 ms ab Start von pw-record, 6–15 ms ab „running“ des Abnehmers (5 Läufe) |
| Aufnahme endet → getrennt | 2,01 s, danach Kette „idle“, UMA-8 und AEC „suspended“ |
| Standardausgabe wechselt im Anruf | neue Links nach 15 ms, alte nach 24 ms entfernt |
| Echo −45 dBFS ohne diffusen Nachhall | Einschwingen 1,5–5 s 40,7 dB, eingeschwungen 41,0 dB leiser (dauerhaft verbunden, selber Lauf: 38,4/42,6 dB; früher: 42,3/43,3 dB); Gegensprechen −9,7 dB |
| CPU des Helfers samt pw-dump | Leerlauf und Musik ohne Anruf 0 ms in 30 s, im Anruf 1,2 ms in 30 s, je Anrufende ≈ 50 ms, Start ≈ 0,25 s; 25 + 8 MB |
| Helfer im Anruf per `kill -9` beendet | Links bleiben, Anruf ungestört; neu gestartet übernimmt er sie bzw. trennt sofort, wenn der Anruf inzwischen vorbei ist |

Bekannte Nachteile:

- Die ersten ≈ 50 ms einer Aufnahme laufen ohne Referenz (AEC reicht durch).
  Beim Verbinden kommen Lautsprecher und UMA-8 in einen gemeinsamen Takt (einer
  folgt mit Resampling); läuft dabei Musik, ist ein kurzer Aussetzer auf den
  Lautsprechern denkbar (nicht gemessen, mit dauerhaft verbundener Referenz
  passierte dasselbe beim Start jeder Wiedergabe).
- Nachführung: Las der Tracker `uma8_callmic_aec` dauerhaft, hielt er UMA-8,
  AEC und Hauptkette samt DeepFilterNet wach (live: alles „running“,
  Kettenprozess 6,8 %), und ohne Anruf zog Sprache aus Videos den Strahl zum
  Lautsprecher. Deshalb nimmt er nur noch auf, solange ein Programm
  `uma8_callmic` aufnimmt (Komponente 5, „Nachführung nur während einer
  Aufnahme“).
- Nur Ton auf der Standardausgabe wird entfernt. Gibt das Anruf-Programm auf
  einem anderen Gerät aus, bleibt dessen Echo.
- Die Referenz ist stereo (`FL FR`). Von einer Mehrkanal-Standardausgabe
  (z. B. 5.1) verbindet der Helfer nur die Monitor-Ports FL und FR (Zuordnung
  nach Kanalnamen); was nur über Mitte, LFE oder hinten kommt, fehlt in der
  Referenz und bleibt als Echo; auch Stereo-Ton eines Anruf-Programms, den
  PipeWire per Upmix (`channelmix.upmix`) auf Mitte oder hinten verteilt.
  Mit Stereo-Ausgaben (Laptop, Kopfhörer, USB-Lautsprecher) tritt das nicht
  auf. Eine Mehrkanal-Referenz hieße mehr Referenzkanäle für die AEC, nicht
  umgesetzt und nicht gemessen.
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
| `capture.py` | Mehrkanal-Aufnahme über einen `pw-record`-Unterprozess (float32, 48 kHz), Ringpuffer. Immer mit den Kanalpositionen der Quelle (`--channel-map`): pw-record nähme sonst ein Standardlayout, und PipeWire mischte um (gemessen: an der AEC-Quelle kamen nur AUX0/1 an). Ohne Ausweichen aufs Standardmikrofon (`node.dont-fallback`): fehlt die Quelle, endet die Aufnahme |
| `doa.py` | Richtungsschätzung: SRP-PHAT über 72 Azimuth- × 4 Elevationswerte, Sprachaktivitätserkennung (Energie + spektrale Flachheit) |
| `geometry.py` | Kanalzuordnung und Radius aus Raumrauschen: Kohärenzmatrix, Mittel-Mikrofon = höchste mittlere Kohärenz, Ringreihenfolge und Radius per Fit an sinc(k·d) |
| `reflink.py` | Echo-Referenz nur während einer Aufnahme verbinden (`uma8-callmic --ref-linker`, gestartet per `context.exec` der Kette): `pw-dump --monitor` lesen, Monitor der Standardausgabe per `pw-link` verbinden/trennen; Entscheidungslogik ohne PipeWire testbar |
| `tracker.py` | Nachführung: alle 0,2 s DOA bei Sprache, Median über 1 s, Hysterese 15°, setzt Azimuth. Mit Arbeitsplatz-Profil nur Schätzungen in der Sprechzone und nicht bei den Lautsprechern (`Zone`). Hört mit Echounterdrückung auf `uma8_callmic_aec` (7 Kanäle): Im Anruf ist Sprache aus den Lautsprechern dort entfernt, der Strahl folgt dann nicht dem Lautsprecher; ohne Anruf ist die Referenz getrennt (im Test erkannte die Sprachaktivität Lautsprecher-Sprache vor der AEC in 39 von 43 Blöcken, dahinter in 3). Kalibrierung und Kanalzuordnung lesen weiter das UMA-8 direkt. Endet die Aufnahme (Kette neu gestartet), verbindet das Tray neu |
| `chainconf.py` | erzeugt `uma8-callmic.conf` aus Vorlage und Einstellungen |
| `ladspainfo.py` | Labels und Portnamen eines LADSPA-Plugins per ctypes, in einem Kindprozess (dlopen lädt einen geladenen Pfad nie neu, ein kaputtes `.so` reißt das Tray nicht mit); erkennt ein veraltetes `libuma8_beam.so` |
| `tray.py` | Icon, Menü, Umschalten, Zustandsabfrage alle 2 s (ein `pw-dump` je Durchlauf); Nachführung nur während einer Aufnahme |
| `dialogs.py` | Optionen, Kalibrierung, Kanalzuordnung |
| `workspace.py` | Arbeitsplatz-Profil ohne GUI: Testsignal, Ablauf und Auswertung der Lautsprechermessung, Tastatur, Pegel, Live-Auswertung, Platzierungshinweise |
| `beampattern.py` | Richtcharakteristik des Plugins in numpy (Entwurf wie `beam.rs`) für die Platzierungsansicht |
| `workspace_ui.py` | Assistent „Arbeitsplatz einmessen…“, Platzierungsansicht, Polardiagramm (QPainter) |

Tray-Zustände:

| Icon | Zustand |
|---|---|
| farbig | aktiv (Beam-Weg) |
| grau | deaktiviert (Roh-Weg) |
| rot | Problem: DeepFilterNet oder `libuma8_beam.so` fehlt, `libuma8_beam.so` veraltet oder nicht ladbar, Reste von `install.sh` neben einem Paket, falsche Firmware, Gerät fehlt, Dienst läuft nicht, Kette nicht geladen, Echounterdrückung nicht geladen (in dieser Rangfolge); Details im Tooltip |

Linksklick = umschalten. Rechtsklick-Menü: ☑ Aktiv · Kalibrieren… ·
Arbeitsplatz einmessen… · Platzierung… · Optionen… · Beenden (nur Tray;
Dienst läuft weiter).

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
- Lautsprecher ausblenden (Nullstellen), Standard aus; nur mit eingemessenen
  Lautsprechern und superdirektivem Strahl wählbar, live (Komponente 5)
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

### 5. Arbeitsplatz-Profil (Assistent, Platzierung)

Für einen festen Schreibtisch: Lautsprecher und Tastatur haben feste
Richtungen, nur der Nutzer bewegt sich. Anlass ist Philipps Platz (49"-32:9-
Bildschirm, gebogen, ≈ 90 cm entfernt, Stereo-Lautsprecher unter den
Bildschirmrändern, geschätzt ±100° neben dem Sprecher und 0–10° hoch;
Mikrofon zwischen Tastatur und Bildschirm, die Tastatur also in Sprechrichtung,
nur tiefer). Alles ist optional: Ohne Profil sind Kettenkonfiguration,
Live-Werte und Nachführung genau wie vorher (Test
`test_no_profile_leaves_params_and_chain_unchanged` vergleicht mit den
Controls vor dem Profil).

Gespeichert in `config.toml`:

| Schlüssel | Bedeutung |
|---|---|
| `speakers` | 0–2 Lautsprecherrichtungen `[[Azimut, Elevation], …]`, Bezugssystem wie `calibrated_azimuth` |
| `speaker_levels_dbfs` | Rohpegel je Lautsprecher bei der Messung (Sprachband, Mittel-Mikrofon) |
| `keyboard` | Tastaturrichtung `[Azimut, Elevation]` oder leer, nur Anzeige |
| `null_weight_db` | Nullstellen auf die Lautsprecher, 0 = aus (Standard), eingeschaltet 10 |
| `talker_zone_deg` | Sprechzone ±Grad um `calibrated_azimuth`, 180 = unbeschränkt (Standard) |
| `noise_floor_dbfs`, `speech_level_dbfs` | Grundrauschen und Sprachpegel beim Einmessen; fehlt der Schlüssel, nicht gemessen |

Alle Pegel sind Rohpegel des Mittel-Mikrofons im Sprachband 100 Hz – 8 kHz,
für Stimme, Lautsprecher und Grundrauschen gleich gerechnet (Hann-Frames,
Leistung im Band), damit ihre Abstände vergleichbar sind.

**Lautsprecher einmessen** (`workspace.SpeakerSweep`, GUI-frei). Testsignal je
Kanal: rosa Rauschen 80 Hz – 16 kHz, 2,5 s, −23 dBFS RMS (etwa Sprache in einem
Anruf), 50 ms Rampen, als Stereo-WAV (16 bit) mit stummem zweitem Kanal.
Wiedergabe mit `pw-play` ohne Ziel, also auf der Standardausgabe (im eigenen
Test-Daemon ohne Geräte geprüft: Datei erkannt, `media.name` gesetzt).
Gleichzeitig nimmt `pw-record` das UMA-8 direkt auf (8 Kanäle, Kanalpositionen
wie die Kette, ohne Ausweichen): vor der Echounterdrückung, die genau dieses
Signal entfernen würde. Ablauf: 1 s Grundrauschen, Stoß links, 0,8 s Pause,
Stoß rechts, 0,8 s; Start und Ende jedes Stoßes in Samples der Aufnahme
(`Capture.span` liest absolut, ohne Wettlauf mit dem Lesethread).

Auswertung je Stoß (`analyse_speakers`): Frames (1024, Hop 512) vom Start von
`pw-play` bis 0,3 s nach seinem Ende, die im Band 1–6 kHz mindestens 10 dB über
dem Grundrauschen und höchstens 6 dB unter ihrem Median liegen (nimmt Anlauf und
Nachhall nach dem Stoß heraus); mindestens 40 Frames, sonst „am Mikrofon nichts
zu hören. Lautstärke an? Ist die Standardausgabe dieser Lautsprecher (nicht
Kopfhörer, HDMI)?“. Richtung per SRP-PHAT über diese Frames im Band 1–6 kHz
(darunter trennt das 86-mm-Array kaum), Gitter 5° × 5 Elevationen, verfeinert
auf 0,25° Azimut. Eindeutigkeit wie bei der Kalibrierung (Haupt- minus
Nebenmaximum außerhalb ±30°): unter 0,05 unbrauchbar, ab 0,15 „eindeutig“.
Beide Kanäle näher als 15° beieinander gelten als ein Lautsprecher (beide
Nullstellen gleich). Rohspitzen über −30 dBFS: Hinweis, dass die AEC-Vorstufe
(+24 dB, Ausgang auf ±1 begrenzt) bald abschneidet.

Elevation: Die SRP allein schätzt mit Hall zu steil, weil der diffuse Anteil
(reelle sinc-Kohärenz) am besten zu kleinen Laufzeitunterschieden passt, also zu
el → 90°. `elevation_fit` passt deshalb je Bin die Kreuzspektren der 21 Paare
als α·d(el) + β·Γ an (α, β reell) und nimmt die Elevation mit dem kleinsten
Rest. Raumsimulation (Raum aus `tools/eval_nulls.py`, vier Lautsprecher
0,55–0,8 m entfernt, wahre Elevation 3–8°, ohne Tisch):

| T60 | Azimutfehler | Elevation nur SRP | Elevation mit Fit |
|---|---|---|---|
| ohne Hall | 0° | wahr (2,5-°-Raster) | wahr |
| 0,3 s | ≤ 0,75° | 10–20° | 10–12,5° |
| 0,45 s | ≤ 0,75° | 12,5–22,5° | 10–17,5° |
| 0,7 s | ≤ 0,5° | 17,5–25° | 10–20° |

Der Rest kommt vermutlich großteils von der Bodenreflexion, die ein Array auf
dem Tisch nicht sieht. Die Elevation zählt für die Nullstellen: Mit 12,5° statt
5° dämpfen sie den Direktschall (1–4 kHz, Modell, ideale Mikrofone) um 19–26
statt 25–29 dB gegenüber ohne Nullstellen, mit 25° nur noch um 10–17 dB.

**Sprechrichtung:** die vorhandene Kalibrierung (`CalibrationDialog`), die
dabei zusätzlich den Pegel der Sprachblöcke festhält (Median →
`speech_level_dbfs`). Eine bestehende Kalibrierung kann bleiben.

**Tastatur** (optional): 5 s tippen; Frames 512 im Band 2–7 kHz, die mindestens
12 dB über dem 30. Perzentil liegen (Anschläge), SRP-PHAT wie oben. Nur Anzeige:
Die Tastatur liegt in Sprechrichtung und lässt sich nicht ausblenden, das
Tippen dämpft DeepFilterNet.

**Ergebnis und Speichern:** Polardiagramm, Richtungen relativ zum Sprecher,
Hinweise (Lautsprecher in Sprechrichtung), „Bewegungsbereich“ (±15–90° oder
unbeschränkt), „Lautsprecher ausblenden (Nullstellen)“ (aus; Tooltip nennt
Nutzen und Grenze: Direktschall 1–4 kHz −5…−11 dB, Echo insgesamt im simulierten
Raum nur −0,3…−1 dB, weil Reflexionen überwiegen; im Anruf vergleichen).
Speichern schreibt `config.toml` und die Kettenkonfiguration und setzt alle
Controls live, einschließlich „Null Weight“ 0 nach „Profil löschen“. Die
Null-Controls stehen nur mit eingemessenen Lautsprechern in der
Kettenkonfiguration; das Plugin rechnet mit Gewicht 0 bitgleich wie ohne.

**Nachführung mit Profil** (`tracker.Zone`): Schätzungen außerhalb ±Sprechzone
um die kalibrierte Richtung und innerhalb ±20° (Azimut) um einen Lautsprecher
zählen nicht. Ein Lautsprecher näher als 30° an der Sprechrichtung wird nicht
ausgeschlossen (sonst fände die Nachführung den Sprecher nie), der Assistent
warnt dann. Die Zone folgt einer neuen Kalibrierung.

**Nachführung nur während einer Aufnahme** (für alle, nicht nur mit Profil):
Das Tray liest alle 2 s ein `pw-dump` (vorher zwei) und prüft mit
`reflink.in_use` wie der Referenz-Helfer, ob ein Knoten hinter `uma8_callmic`
läuft. Nur dann nimmt die Nachführung auf; endet die Aufnahme, stoppt sie nach
`reflink.HOLD_S` (2 s, beim 2-s-Takt also nach 2–4 s). Der Strahl bleibt auf der
zuletzt gefundenen Richtung, auch über einen Neustart der Kette; erst ein
anderer Richtungsmodus vergisst sie. Antwortet `pw-dump` nicht, bleibt der
letzte Zustand. Tooltip: „Richtung: automatisch (…°), ruht ohne Aufnahme“. Das
Pegelmeter im Optionen-Dialog nimmt `uma8_callmic` auf und weckt die
Nachführung daher mit.

**Platzierung…** (`PlacementWindow`): eigene Rohaufnahme, nur solange das
Fenster offen ist; alle 0,2 s:

- Schallkarte: SRP-PHAT (300 Hz – 6 kHz, Maximum über die Elevationen,
  geglättet 0,5) über den Azimut, normiert; Deckkraft nach Abstand zum
  Grundrauschen.
- Strahl bei 1 und 3 kHz aus `beampattern.py`, aus genau den Controls, die das
  Plugin bekommt (`params.all_params`), für Schall unter der
  Lautsprecher-Elevation (ohne Lautsprecher: Blickrichtung). Abgleich: mit
  `tools/eval_nulls.py` auf 0,01 dB, mit dem Plugin (Sinus auf einem Bin bzw.
  3 kHz, ladspa_host) auf 0,15 dB über −30 dB und 1,5 dB in tiefen Nullstellen
  (f32).
- Marker: du (mit Sprechzone), Lautsprecher (mit ±20°-Ausschluss), Tastatur,
  aktueller Strahl (nachgeführt, kalibriert oder manuell).
- „Array-Sicht“ (0° rechts, gegen den Uhrzeigersinn) oder „aus deiner Sicht“
  (Standard, sobald kalibriert): gedreht, sodass du unten bist, und gespiegelt,
  falls der zuerst gemessene, linke Lautsprecher sonst rechts läge.
- Pegel: Stimme (Median der Sprachblöcke aus der Sprechzone, ohne Zone ±45° um
  die Kalibrierung, nie aus Lautsprecherrichtung), Grundrauschen
  (10. Perzentil der letzten 10 s), Lautsprecher (letzte Messung). Hinweise
  (Faustregeln): Stimme − Lautsprecher ≥ 10 dB gut, 3–10 dB „Mikrofon näher zu
  dir oder weiter weg von den Lautsprechern“, darunter zusätzlich „Lautsprecher
  leiser“ (Grund: die AEC dämpft bei Gegensprechen die Stimme umso mehr, je
  lauter das Echo, Komponente 3: Echo 5 dB unter der Stimme → Stimme −8 dB);
  Stimme − Grundrauschen ≥ 20 dB gut, 10–20 dB „Mikrofon näher zu dir“.
- „Lautsprecher neu messen…“ (erst nach Bestätigung der Lautstärke-Warnung)
  nutzt dieselbe Aufnahme; das Ergebnis erscheint sofort im Diagramm und wird
  mit „Messung übernehmen“ gespeichert.

Grenzen: Der Lautsprecherpegel gilt für die Lautstärke bei der Messung. Die
Hinweise sind Faustregeln. Mit DSP-Firmware ungetestet am echten Gerät; alles
ist mit simulierten ebenen Wellen, Raumimpulsantworten und einer simulierten
Wiedergabe getestet (`tests/desksim.py`), nie mit echter Wiedergabe.

### 6. Installation

Drei Wege, die sich gegenseitig ausschließen:

- **Fedora-RPMs** (`packaging/build-rpms.sh`, Podman mit Fedora 44, Quellen
  per tar über stdin, eingebunden wird nur `packaging/out/`): `uma8-callmic`
  (Tray, Plugin in `/usr/lib64/ladspa`, Benutzerdienst in
  `/usr/lib/systemd/user`, Startmenü-Eintrag) und `deepfilternet-ladspa`.
  `%check` führt Unit- und Rust-Tests aus, ohne die Rechenzeit-Tests (Marker
  `timing`).
- **Arch-Pakete** (`packaging/arch/*/PKGBUILD`, im Checkout `makepkg -si`
  oder `packaging/build-arch.sh` in Podman mit `archlinux:latest`): dieselben
  zwei Pakete, Plugins in `/usr/lib/ladspa`; `deepfilternet-ladspa` ersetzt
  das AUR-Paket. `check()` wie `%check`.
- **Entwickler-Installation** `install.sh` (ohne root, läuft aus dem Repo):
  1. verweigert sich, solange das Paket `uma8-callmic` installiert ist
     (`rpm -q` bzw. `pacman -Q`): Plugin und Dienst in `~` gingen dem Paket
     vor
  2. prüft PipeWire, `pactl`, Rust, PySide6, numpy und das
     DeepFilterNet-Plugin; fehlt es, Hinweis auf das Paket
     `deepfilternet-ladspa` dieses Repos (AUR-Paket nur als Alternative,
     wegen des Lecks) und Abbruch
  3. baut das Plugin und kopiert `libuma8_beam.so` nach `~/.local/lib/ladspa/`
  4. beendet ein laufendes Tray (jede Installationsform, nicht die
     Hilfsbefehle): ein altes Tray setzte sonst seine alten Werte in die neue
     Kette
  5. Startbefehl `~/.local/bin/uma8-callmic` (`PYTHONPATH` aufs Repo),
     Konfiguration, Dienst-Datei nach `~/.config/systemd/user/`, aktiviert
     und startet den Dienst neu, Startmenü-Eintrag, Autostart (außer
     `autostart = false`), Standard-Mikrofon
  6. startet das beendete Tray wieder (nur in einer grafischen Sitzung)

`uninstall.sh` entfernt die Einrichtung des Benutzers (Dienst, Dateien in
`~`, Autostart; beendet Tray und Helfer jeder Installationsform) und löscht
die Einstellungen nur auf Nachfrage; behaltene Einstellungen verlieren
`setup_done`, damit ein Paket-Tray sich danach neu einrichtet. Pakete bleiben
installiert (Hinweis).

Dienst: `ExecStartPre=<Startbefehl> --write-config` schreibt die
Kettenkonfiguration vor jedem Start aus den Einstellungen, auch wenn das
Tray nie lief; `ExecStart=/usr/bin/pipewire -c …/uma8-callmic.conf`.
Konfiguration, Einstellungen und Autostart-Datei werden atomar geschrieben
(temporäre Datei, `os.replace`): Tray und `ExecStartPre` schreiben beim Login
womöglich gleichzeitig.

Einrichtung durch das Tray (Pakete, die nichts selbst aktivieren): beim
ersten Start Autostart nach Einstellung anlegen und den Dienst aktivieren
(`systemctl --user enable --now`, nur im Zustand „disabled“), dann
`setup_done = true` speichern. Ältere Einstellungen ohne den Schlüssel
durchlaufen das einmal (für schon Eingerichtete ohne Wirkung); scheitert
systemctl, beim nächsten Start erneut. Danach ändert das Tray beides nie
mehr von selbst: Ein per `systemctl --user disable` abgeschalteter Dienst und
ein gelöschter Autostart-Eintrag bleiben so, die Option „Beim Login starten“
zeigt den echten Zustand und ist der einzige Schalter.

Prüfungen im Tray gegen gemischte Installationen:

- Plugin-API: Die Ports von `libuma8_beam.so` werden per `ladspainfo.py`
  gelesen (je Pfad und Änderungszeit einmal) und mit allen Controls
  verglichen, die Kette und Tray setzen. Fehlt eins (alte Version: die Kette
  lädt dann mit stillen Warnungen, Controls wirken nicht, Modus 2 wird zu
  „alle Richtungen“, negative Verstärkung zu 0), ist das Icon rot:
  „libuma8_beam.so veraltet: <Pfad> …“; nicht ladbar ebenso.
- Läuft das Programm aus einem Paket (nicht aus einem Checkout) und liegen
  noch `~/.local/lib/ladspa/libuma8_beam.so`,
  `~/.config/systemd/user/uma8-callmic-chain.service` oder
  `~/.local/bin/uma8-callmic` herum: rot mit Hinweis auf `./uninstall.sh`.

Mindestversion PipeWire 1.2.0 (RPM `pipewire >= 1.2.0`, Arch
`pipewire>=1:1.2.0`): `context.exec` nimmt erst ab 1.2.0 (`conf.c`,
`pw_strv_parse`, Entwicklungsstand 1.1.81) Argumente als Array; 1.0.x setzt
den Rohtext an den Pfad und zerlegt ihn an Leerzeichen, der Helfer bekäme
`[ "--ref-linker" ]` als drei Argumente. Alles andere (`monitor.mode` des
Echo-Cancel-Moduls, builtin `linear`/`mixer`) gibt es schon in 1.0.

## Fehlerbehandlung

| Fall | Verhalten |
|---|---|
| Gerät abgezogen | Kette wartet (target.object), Tray rot; beim Einstecken automatisch wieder verbunden |
| DSP-Firmware statt Raw | Tray rot: „Raw-Firmware nötig“ |
| DeepFilterNet fehlt | Dienst startet nicht, Tray rot mit Hinweis |
| Dienst abgestürzt | systemd startet neu; Tray zeigt rot, bis er wieder läuft |
| Nachführung ohne Audio | Azimuth bleibt stehen, Hinweis im Tooltip |
| Echounterdrückung an, aber nicht in der laufenden Kette | Tray rot: „Echounterdrückung nicht geladen (Dienst … neu starten)“ |
| Referenz-Helfer beendet | Mikrofon läuft weiter, bei neuen Anrufen ohne Echounterdrückung; startet mit dem Dienst neu (Meldungen im Journal des Dienstes) |
| Kaputte config.toml | Standardwerte, Hinweis im Tray; vor dem nächsten Speichern wird die kaputte Datei als `config.toml.broken` gesichert |
| `pw-cli` schlägt fehl | Meldung im Log (`~/.local/state/uma8-callmic/log`), Kette läuft mit den bisherigen Werten weiter; startet sie neu, setzt das Tray alle Werte erneut |
| `libuma8_beam.so` veraltet oder nicht ladbar | Tray rot mit Pfad und fehlenden Controls; Prüfung im Kindprozess, das Tray selbst stürzt nie |
| Paket installiert, Reste von `install.sh` in `~` | Tray rot mit den Pfaden: `./uninstall.sh` ausführen |
| `--ref-linker`: unerwartete Ausnahme | protokolliert (Journal des Dienstes), Neustart mit derselben Pause wie bei Fehlern von pw-dump (2 s, bis 60 s) |
| Lautsprechermessung: nichts am Mikrofon | Meldung je Kanal (Lautstärke, Standardausgabe prüfen); ein gehörter Kanal reicht für ein Profil |
| Lautsprechermessung: `pw-play` scheitert oder hängt | Meldung mit Rückgabewert; nach 8,5 s abgebrochen |

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
- CPU: Plugin verarbeitet 10 s Audio in ≤ 0,25 s (Bestwert aus drei Läufen), auch mit Nullstellen, die alle 0,5 s verschoben werden (Marker `timing`: für Entwickler an, in den Paket-Builds abgewählt)
- Robustheit der Verteilung: Neuentwurf je `run()` begrenzt, Eingangsbegrenzung ±1000 und nicht endliche Eingänge ohne dauerhafte Dämpfung

Python (pytest):

- `doa.py`: synthetische 7-Kanal-Signale aus 12 Richtungen, mittlerer Fehler
  < 15°, mit und ohne Rauschen
- `geometry.py`: synthetisches diffuses Rauschen mit permutierten Kanälen,
  Mittelkanal und Ringnachbarschaft korrekt erkannt
- `config.py`, `chainconf.py`: Round-Trip, Validierung, erzeugte Konfiguration
  startet in einem Probelauf (`pipewire -c <datei>`, 2 s) ohne Fehler; beide
  Kettenvarianten werden geparst (SPA-JSON) und geprüft: Knotennamen,
  Klassen, Kanalzahlen und -positionen, `aec.args`, Gesamtverstärkung jeder
  Vorverstärkungsstrecke, `null`-Eingang nur ohne Echounterdrückung,
  Referenz ohne Autoconnect, Helfer per `context.exec` nur mit ihr
- Arbeitsplatz-Profil: Konfiguration (Round-Trip, Validierung, ohne Profil
  dieselben Controls wie vorher), Nullstellen-Controls, Zone und Ausschluss der
  Nachführung, Tray mit Attrappen (Nachführung nur während einer Aufnahme,
  Richtung bleibt, Profil live), Lautsprechermessung an ebenen Wellen (≤ 1°),
  in der Raumsimulation (≤ 3°) und als ganzer Ablauf mit simulierter Wiedergabe
  (`tests/desksim.py`: Testuhr, Aufnahme-Attrappe, `pw-play`-Attrappe, die die
  WAV-Datei liest und als ebene Welle einspielt), Fehlerfälle, Tastatur,
  Hinweise, `beampattern.py` gegen Modell und Plugin, Assistent und
  Platzierungsansicht offscreen
- `reflink.py`: Entscheidungen auf aufgezeichneten pw-dump-Graphen der
  Testkette (Musik ohne Anruf, Anruf beginnt, verbunden, Anruf vorbei mit
  laufender Kette), Wartezeit, Wechsel der Standardausgabe, Neustart mitten im
  Anruf, Zerlegung der pw-dump-Ausgabe in beliebigen Stücken, Neustart-Pausen

Integration (manuell ausgelöst, braucht das Gerät):

- Dienst starten, 10 s vom virtuellen Mikrofon aufnehmen: kein Aussetzer
  (lückenlose Samples), Pegel plausibel, Energie oberhalb 8 kHz vorhanden
- Umschalten per Tray während der Aufnahme: kein Klick

Abnahme durch den Nutzer: Hörvergleich Roh gegen Bearbeitet per Tray-Umschalter
und ein echter Anruf.

## Projektstruktur

```
uma8-callmic/
├── plugin/              Rust-Crate (cdylib): src/{lib.rs, ladspa.rs (LADSPA-Hülle), plugins.rs (uma8_beam,
│                        uma8_limit), pipeline.rs, stft.rs, beam.rs, jacobi.rs, cdr.rs, dereverb.rs, limiter.rs},
│                        tests/no_alloc.rs (run() ohne Allokation), examples/run_cost.rs
├── uma8_callmic/        Python-Paket (Module siehe oben), data/{uma8-callmic.conf.in, uma8-callmic-aec.conf.in,
│                        uma8-callmic.desktop}, icons/{active,inactive,error}.svg
├── pipewire/            uma8-callmic-chain.service (@BIN@ setzen install.sh bzw. die Pakete)
├── tests/               test_*.py je Modul (array, beampattern, beam_plugin, calibration, capture, chainconf,
│                        config, dfn_latency, dialogs, doa, geometry, limit_plugin, params, pwctl, reflink, roomsim,
│                        tracker, tray, traystate, workspace, workspace_ui); Hilfen: conftest.py (baut plugin/),
│                        ladspa_host.py, sim.py, desksim.py, spajson.py; data/reflink_graphs.json
├── tools/               roomsim.py, eval_dereverb.py, eval_nulls.py (Raumsimulation, Auswertung),
│                        offline.py (Aufnahme verarbeiten), check_output.py (mit Gerät)
├── packaging/           uma8-callmic.spec, deepfilternet-ladspa.spec (+ .sources, Patches), build-rpms.sh,
│                        rpmlint.toml, arch/{uma8-callmic,deepfilternet-ladspa}/PKGBUILD, build-arch.sh
├── firmware/            docker-compose.usb.yml (Firmware-Dateien lokal, nicht im Repo)
├── docs/                specs/ (dieses Dokument), plans/
├── install.sh, uninstall.sh, pyproject.toml, README.md
```

Die miniDSP-Firmware-Dateien, der Windows-Treiber und das DFU-Tool sind
proprietär und werden per `.gitignore` aus dem Repository ausgeschlossen.

## Offene Punkte für die Umsetzung

- Die ODAS-Kanalzuordnung wird mit `geometry.py` am echten Gerät
  überprüft (Hardware-Aufgabe im Plan).
- Die Latenz von DeepFilterNet wird gemessen und als Konstante hinterlegt.
