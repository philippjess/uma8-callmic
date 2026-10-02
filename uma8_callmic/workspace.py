"""Arbeitsplatz-Profil ohne GUI: Lautsprecher und Tastatur einmessen, Pegel, Platzierungshinweise.

Lautsprecher: je Kanal ein rosa Rauschstoß über die Standardausgabe (pw-play, Stereo-Datei mit einem stummen
Kanal), dabei Aufnahme des UMA-8 direkt (8 Kanäle, vor der Echounterdrückung – die entfernte genau dieses
Signal). Richtung per SRP-PHAT über die Frames des Stoßes im Band 1–6 kHz (darunter trennt das 86-mm-Array
kaum), Gitter 5°, verfeinert auf 0,25° Azimut und 2,5° Elevation. Elevation ist bei einem flachen Array
ungenau (nur über cos(el)) und oben/unten nicht zu unterscheiden.

Pegel sind Rohpegel des Mittel-Mikrofons im Sprachband 100 Hz – 8 kHz (dBFS), für Sprache, Lautsprecher und
Grundrauschen gleich gerechnet, damit ihre Abstände vergleichbar sind."""
from __future__ import annotations

import subprocess
import wave
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .constants import AEC_PRE_GAIN_DB
from .doa import C_SOUND, SrpPhat, VoiceDetector, angle_diff, circular_mean

SR = 48000
NFFT = 1024
#: Rauschstoß je Lautsprecher: Dauer, Pegel (RMS, etwa wie Sprache in einem Anruf), Ein-/Ausblenden
BURST_S = 2.5
BURST_RMS_DBFS = -23.0
FADE_S = 0.05
PINK_BAND = (80.0, 16000.0)
#: Bänder: Lautsprecherrichtung, Tastenanschläge, Pegel
SPEAKER_BAND = (1000.0, 6000.0)
KEYBOARD_BAND = (2000.0, 7000.0)
LEVEL_BAND = (100.0, 8000.0)
SPEAKER_ELEVATIONS = (0.0, 15.0, 30.0, 45.0, 60.0)
#: Frames des Stoßes: mindestens so weit über dem Grundrauschen (Band 1–6 kHz) und höchstens so weit unter
#: ihrem Median (nimmt Anlauf und Nachhall nach dem Stoß heraus)
MIN_SNR_DB = 10.0
BURST_SPREAD_DB = 6.0
#: so lange nach dem Ende von pw-play noch zum Stoß zählen (Ausgabelatenz)
TAIL_S = 0.3
MIN_FRAMES = 40
#: Haupt- minus Nebenmaximum (doa.DoaResult.confidence): darunter unbrauchbar bzw. unsicher
MIN_CONFIDENCE = 0.05
GOOD_CONFIDENCE = 0.15
#: Beide Kanäle näher beieinander → ein Lautsprecher (Mono), eine Nullstelle
MERGE_DEG = 15.0
#: Tastatur: Dauer, Schwelle der Anschläge über dem ruhigen Anteil, Mindestzahl Frames
TYPING_S = 5.0
TRANSIENT_DB = 12.0
MIN_TRANSIENT_FRAMES = 8
#: Rohspitze, ab der die Echounterdrückung (Vorverstärkung, Ausgang auf ±1 begrenzt) bald abschneidet: 6 dB Reserve
ECHO_PEAK_MAX_DBFS = -AEC_PRE_GAIN_DB - 6.0
#: Platzierungshinweise (Faustregeln): Abstand Sprache−Echo bzw. Sprache−Grundrauschen gut/mindestens
SPEECH_ECHO_DB = (10.0, 3.0)
SPEECH_NOISE_DB = (20.0, 10.0)
CHANNEL_NAMES = ("links", "rechts")


def db(power: float) -> float:
    return float(10 * np.log10(power + 1e-30))


# --- Testsignal ---------------------------------------------------------------

def pink_noise(n: int, sr: int = SR, band=PINK_BAND, seed: int = 0) -> np.ndarray:
    """Rosa Rauschen (−3 dB/Oktave) im Band, RMS 1."""
    rng = np.random.default_rng(seed)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / sr)
    shape = np.zeros_like(f)
    inside = (f >= band[0]) & (f <= band[1])
    shape[inside] = 1 / np.sqrt(f[inside])
    x = np.fft.irfft(spec * shape, n)
    return x / np.sqrt(np.mean(x * x))


def burst(channel: int, seconds: float = BURST_S, rms_dbfs: float = BURST_RMS_DBFS, sr: int = SR,
          seed: int = 0) -> np.ndarray:
    """(n, 2) Stereo-Rauschstoß, nur auf `channel` (0 = links, 1 = rechts), weich ein- und ausgeblendet."""
    n = int(seconds * sr)
    x = pink_noise(n, sr, seed=seed) * 10 ** (rms_dbfs / 20)
    k = int(FADE_S * sr)
    ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(k) / k)
    x[:k] *= ramp
    x[-k:] *= ramp[::-1]
    out = np.zeros((n, 2), dtype=np.float32)
    out[:, channel] = np.clip(x, -0.99, 0.99)
    return out


def write_wav(path: Path, data: np.ndarray, sr: int = SR) -> None:
    pcm = np.clip(np.round(np.asarray(data) * 32767), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def write_bursts(directory: Path) -> list[Path]:
    """Eine WAV-Datei je Lautsprecher (links, rechts)."""
    paths = []
    for ch, name in enumerate(CHANNEL_NAMES):
        path = Path(directory) / f"uma8-lautsprecher-{name}.wav"
        write_wav(path, burst(ch, seed=ch))
        paths.append(path)
    return paths


def play_command(path: Path) -> list[str]:
    """Wiedergabe auf der Standardausgabe (kein --target)."""
    return ["pw-play", "-P", '{ media.name = "UMA-8 Call Mic: Lautsprecher einmessen" }', str(path)]


def start_player(path: Path) -> subprocess.Popen:
    return subprocess.Popen(play_command(path), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


# --- Pegel --------------------------------------------------------------------

def frame_power(x: np.ndarray, nfft: int = NFFT, band=LEVEL_BAND, sr: int = SR) -> np.ndarray:
    """Mittlere Leistung von `x` im Band je Frame (Hann, Hop nfft/2, Rahmen wie doa.SrpPhat)."""
    hop = nfft // 2
    x = np.asarray(x, float)
    if len(x) < nfft:
        return np.zeros(0)
    frames = np.stack([x[s:s + nfft] for s in range(0, len(x) - nfft + 1, hop)])
    win = np.hanning(nfft)
    spec = np.abs(np.fft.rfft(frames * win, axis=1)) ** 2
    f = np.fft.rfftfreq(nfft, 1 / sr)
    inside = (f >= band[0]) & (f <= band[1])
    return 2 * spec[:, inside].sum(axis=1) / (nfft * np.sum(win * win))


def band_level_dbfs(x: np.ndarray, band=LEVEL_BAND, sr: int = SR) -> float:
    """Pegel (dBFS, Sinus mit Spitze 1 ≈ −3 dBFS) von `x` im Band."""
    p = frame_power(x, NFFT, band, sr)
    return db(float(np.mean(p))) if len(p) else db(0.0)


# --- Richtungen -----------------------------------------------------------------

@dataclass
class Direction:
    """Ergebnis einer Richtungsmessung; Winkel im Bezugssystem des Arrays (wie „Azimuth (deg)“)."""
    ok: bool
    message: str
    azimuth: float = 0.0
    elevation: float = 0.0
    confidence: float = 0.0
    #: Pegel am Mittel-Mikrofon (Sprachband, dBFS) und Spitze über alle Mikrofone (dBFS)
    level_dbfs: float | None = None
    peak_dbfs: float | None = None


def elevation_fit(srp: SrpPhat, cs: np.ndarray, azimuth: float, els: np.ndarray) -> float:
    """Elevation bei festem Azimut, unempfindlich gegen Hall: je Bin werden die Kreuzspektren der Paare als
    α·d(el) + β·Γ angenähert (α, β reell; Γ = sinc-Kohärenz des diffusen Felds), gewählt wird die Elevation
    mit dem kleinsten Rest. Die SRP allein bevorzugt mit Hall steilere Richtungen (der reelle, diffuse Anteil
    passt am besten zu kleinen Laufzeitunterschieden, also el → 90°); in der Raumsimulation (T60 0,3–0,7 s)
    halbiert das den Fehler etwa."""
    pos = srp.positions
    dist = np.array([np.linalg.norm(pos[i] - pos[j]) for i, j in srp.pairs])
    g = np.sinc(np.outer(dist, srp.w) / (np.pi * C_SOUND))                    # (P, K)
    d = np.conj(srp._steering(np.full(len(els), azimuth), els))             # (G, P, K) = e^{+jωΔτ}
    n = len(srp.pairs)
    s12 = np.sum(d.real * g, axis=1)                                         # (G, K)
    s22 = np.sum(g * g, axis=0)                                              # (K,)
    b1 = np.real(np.sum(np.conj(d) * cs, axis=1))                            # (G, K)
    b2 = np.sum(g * cs.real, axis=0)                                         # (K,)
    explained = (s22 * b1 ** 2 - 2 * s12 * b1 * b2 + n * b2 ** 2) / (n * s22 - s12 ** 2 + 1e-12)
    return float(els[np.argmax(explained.sum(axis=1))])


def locate(srp: SrpPhat, cs: np.ndarray, step: float = 5.0) -> tuple[float, float, float]:
    """Gittermaximum, dann Azimut (±step, 0,25°, SRP) und Elevation (0–85°, 2,5°, elevation_fit) verfeinert."""
    coarse = srp.estimate_cs(cs)
    az, el = coarse.azimuth, coarse.elevation
    els = np.arange(0.0, 87.5, 2.5)
    for _ in range(2):
        azs = az + np.arange(-step, step + 1e-9, 0.25)
        az = float(azs[np.argmax(srp.srp_at(cs, azs, np.full(azs.shape, el)))] % 360.0)
        el = elevation_fit(srp, cs, az, els)
    return az, el, coarse.confidence


def quality(confidence: float) -> str:
    return "eindeutig" if confidence >= GOOD_CONFIDENCE else "unsicher"


def _frame_starts(n_frames: int, nfft: int = NFFT) -> np.ndarray:
    return np.arange(n_frames) * (nfft // 2)


def _peak_dbfs(x: np.ndarray, sel: np.ndarray, nfft: int = NFFT) -> float:
    starts = _frame_starts(len(sel), nfft)[sel]
    peak = max(float(np.max(np.abs(x[s:s + nfft]))) for s in starts)
    return float(20 * np.log10(peak + 1e-12))


@dataclass
class SpeakerMeasurement:
    speakers: list[Direction]
    noise_dbfs: float
    #: Richtungen für das Profil (zu nahe beieinander → eine), Pegel je Eintrag
    directions: list[list[float]] = field(default_factory=list)
    levels_dbfs: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.directions)


def analyse_speakers(block: np.ndarray, bursts: list[tuple[int, int]], noise: tuple[int, int],
                     positions: np.ndarray, center: int = 0, sr: int = SR) -> SpeakerMeasurement:
    """`block`: Rohaufnahme (n, ≥7); `bursts`: Sample-Bereiche [Start von pw-play, Ende von pw-play] je Kanal
    (links, rechts); `noise`: Bereich nur mit Grundrauschen vor dem ersten Stoß."""
    x = np.asarray(block, float)[:, :7]
    srp = SrpPhat(positions, sr, NFFT, SPEAKER_BAND[0], SPEAKER_BAND[1], elevations=SPEAKER_ELEVATIONS)
    p_band = frame_power(x[:, center], NFFT, SPEAKER_BAND, sr)
    p_level = frame_power(x[:, center], NFFT, LEVEL_BAND, sr)
    starts = _frame_starts(len(p_band))
    quiet = (starts >= noise[0]) & (starts + NFFT <= noise[1])
    noise_band = float(np.median(p_band[quiet])) if quiet.any() else float(np.min(p_band))
    noise_level = db(float(np.mean(p_level[quiet]))) if quiet.any() else db(float(np.min(p_level)))
    results = []
    for (s, e), name in zip(bursts, CHANNEL_NAMES):
        window = (starts >= s) & (starts + NFFT <= e + int(TAIL_S * sr))
        sel = window & (p_band > noise_band * 10 ** (MIN_SNR_DB / 10))
        if sel.any():
            sel &= p_band >= np.median(p_band[sel]) * 10 ** (-BURST_SPREAD_DB / 10)
        if sel.sum() < MIN_FRAMES:
            results.append(Direction(False, f"Lautsprecher {name}: am Mikrofon nichts zu hören. Lautstärke an? "
                                            "Ist die Standardausgabe dieser Lautsprecher (nicht Kopfhörer, HDMI)?"))
            continue
        az, el, conf = locate(srp, srp.cross_spectra(x, sel))
        level, peak = db(float(np.mean(p_level[sel]))), _peak_dbfs(x, sel)
        if conf < MIN_CONFIDENCE:
            results.append(Direction(False, f"Lautsprecher {name}: Richtung nicht eindeutig (Eindeutigkeit "
                                            f"{conf:.2f}) – zu viel Hall oder Störgeräusch? Bitte wiederholen.",
                                     az, el, conf, level, peak))
            continue
        results.append(Direction(True, f"Lautsprecher {name}: {az:.0f}°, Höhe {el:.0f}° – {quality(conf)} "
                                       f"({conf:.2f}), Pegel {level:.0f} dBFS", az, el, conf, level, peak))
    m = SpeakerMeasurement(results, noise_level)
    good = [r for r in results if r.ok]
    if len(good) == 2 and angle_diff(good[0].azimuth, good[1].azimuth) < MERGE_DEG:
        m.directions = [[circular_mean([r.azimuth for r in good]), float(np.mean([r.elevation for r in good]))]]
        m.levels_dbfs = [db(float(np.mean([10 ** (r.level_dbfs / 10) for r in good])))]
        m.notes.append("Beide Kanäle kommen aus derselben Richtung (ein Lautsprecher?) – eine Nullstelle.")
    else:
        m.directions = [[r.azimuth, r.elevation] for r in good]
        m.levels_dbfs = [r.level_dbfs for r in good]
    peaks = [r.peak_dbfs for r in good if r.peak_dbfs is not None]
    if peaks and max(peaks) > ECHO_PEAK_MAX_DBFS:
        m.notes.append("Lautsprecher sehr laut am Mikrofon: Spitzen werden vor der Echounterdrückung "
                       "abgeschnitten – Lautsprecher leiser oder Mikrofon weiter weg.")
    return m


def analyse_keyboard(block: np.ndarray, positions: np.ndarray, center: int = 0, sr: int = SR) -> Direction:
    """Richtung der Tastenanschläge: kurze Frames (512) mit deutlich mehr Energie im hohen Band."""
    x = np.asarray(block, float)[:, :7]
    nfft = 512
    srp = SrpPhat(positions, sr, nfft, KEYBOARD_BAND[0], KEYBOARD_BAND[1], elevations=SPEAKER_ELEVATIONS)
    p = frame_power(x[:, center], nfft, KEYBOARD_BAND, sr)
    sel = p > np.percentile(p, 30) * 10 ** (TRANSIENT_DB / 10) if len(p) else np.zeros(0, bool)
    if sel.sum() < MIN_TRANSIENT_FRAMES:
        return Direction(False, "Kein Tippen erkannt – bitte kräftiger oder länger tippen und wiederholen.")
    az, el, conf = locate(srp, srp.cross_spectra(x, sel))
    if conf < MIN_CONFIDENCE:
        return Direction(False, f"Tastatur: Richtung nicht eindeutig ({conf:.2f}) – bitte wiederholen.", az, el, conf)
    return Direction(True, f"Tastatur: {az:.0f}°, Höhe {el:.0f}° – {quality(conf)} ({conf:.2f})", az, el, conf)


def profile_notes(talker_azimuth: float | None, directions: list[list[float]], keyboard: list[float] | None = None,
                  min_deg: float = 30.0) -> list[str]:
    """Hinweise zur gemessenen Anordnung (Lautsprecher in Sprechrichtung: weder ausblendbar noch ausschließbar)."""
    notes = []
    if talker_azimuth is None:
        return notes
    for az, _ in directions:
        if angle_diff(az, talker_azimuth) < min_deg:
            notes.append(f"Ein Lautsprecher liegt in deiner Sprechrichtung ({az:.0f}°): Nullstellen und Ausschluss "
                         "aus der Nachführung wirken dort nicht – Mikrofon oder Lautsprecher versetzen.")
    if keyboard:
        notes.append(f"Tastatur {angle_diff(keyboard[0], talker_azimuth):.0f}° neben deiner Sprechrichtung: "
                     "liegt sie im Strahl, dämpft nur die Rauschunterdrückung das Tippen.")
    return notes


# --- Ablauf der Lautsprechermessung -------------------------------------------

def keep_known_speakers(m: SpeakerMeasurement, old_dirs: list[list[float]],
                        old_levels: list[float]) -> SpeakerMeasurement:
    """Neue Messung, in der ein Kanal scheiterte: Dessen bisherige Richtung bleibt im Profil (sonst fiele der
    Lautsprecher still aus Sprechzone und Nullstellen). Nur wenn das Profil beide Kanäle getrennt kennt
    (Reihenfolge links, rechts); der neu gemessene Pegel des Kanals gilt, falls es einen gibt."""
    if not m.ok or all(r.ok for r in m.speakers) or len(old_dirs) != 2 or len(m.speakers) != 2:
        return m
    dirs, levels, notes = [], [], list(m.notes)
    for i, (r, name) in enumerate(zip(m.speakers, CHANNEL_NAMES)):
        if r.ok:
            dirs.append([r.azimuth, r.elevation])
            levels.append(r.level_dbfs)
            continue
        dirs.append([float(old_dirs[i][0]), float(old_dirs[i][1])])
        old = old_levels[i] if len(old_levels) == 2 else None
        level = r.level_dbfs if r.level_dbfs is not None else old
        if level is None:  # weder neu noch bisher ein Pegel: Kanal kann nicht bleiben
            return m
        levels.append(level)
        notes.append(f"Lautsprecher {name}: bisherige Richtung {old_dirs[i][0]:.0f}° bleibt.")
    return SpeakerMeasurement(m.speakers, m.noise_dbfs, dirs, levels, notes)


class SpeakerSweep:
    """Vorlauf (Grundrauschen) → je Datei Rauschstoß über `player` → Pause → Auswertung. Ohne GUI und ohne
    eigene Uhr: `step(now)` regelmäßig aufrufen (alle ≈ 0,1 s), bis `done`.

    `capture`: total(), span(start, end), alive (capture.Capture); `player(path)`: Popen-artig (poll, kill)."""

    PREROLL_S, GAP_S, NO_DATA_S = 1.0, 0.8, 2.0
    PLAY_TIMEOUT_S = BURST_S + 6.0

    def __init__(self, capture, files: list[Path], positions: np.ndarray, center: int = 0,
                 player: Callable[[Path], object] = start_player, sr: int = SR, dump: Path | None = None):
        self.capture, self.files, self.positions, self.center = capture, list(files), positions, center
        #: Rohaufnahme samt Stoß- und Rauschbereichen hierhin (.npz), um eine Messung nachträglich auszuwerten
        self.player, self.sr, self.dump = player, sr, dump
        self.phase, self.index = "preroll", 0
        self.t0: float | None = None
        self.t_phase = 0.0
        self.start_total = 0
        self.noise: tuple[int, int] | None = None
        self.bursts: list[tuple[int, int]] = []
        self.proc = None
        self.result: SpeakerMeasurement | None = None
        self.error: str | None = None

    @property
    def done(self) -> bool:
        return self.result is not None or self.error is not None

    def status(self) -> str:
        if self.phase == "preroll":
            return "Messe Grundrauschen – bitte still sein …"
        if self.phase in ("play", "gap") and self.index < len(self.files):
            return f"Lautsprecher {CHANNEL_NAMES[self.index]} spielt Rauschen …"
        return "Auswertung …"

    def progress(self) -> float:
        """0 … 1"""
        per = 1.0 / (len(self.files) + 1)
        if self.done:
            return 1.0
        return per * (self.index + (1 if self.phase != "preroll" else 0)) if self.files else 0.0

    def _fail(self, message: str) -> None:
        self.cancel()
        self.error = message

    def cancel(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()

    def _play(self, now: float, total: int) -> None:
        try:
            self.proc = self.player(self.files[self.index])
        except OSError as e:
            self._fail(f"Wiedergabe nicht möglich (pw-play): {e}")
            return
        self.bursts.append((total, total))
        self.phase, self.t_phase = "play", now

    def step(self, now: float) -> None:
        if self.done:
            return
        total = self.capture.total()
        if self.t0 is None:
            self.t0, self.start_total = now, total
        if self.phase == "preroll":
            if total - self.start_total >= int(self.PREROLL_S * self.sr):
                self.noise = (total - int(self.PREROLL_S * self.sr), total)
                self._play(now, total)
            elif now - self.t0 > self.NO_DATA_S and (total == self.start_total or not self.capture.alive):
                self._fail("Keine Daten vom UMA-8 – angeschlossen und mit Raw-Firmware?")
        elif self.phase == "play":
            code = self.proc.poll()
            if code is None:
                if now - self.t_phase > self.PLAY_TIMEOUT_S:
                    self._fail("Wiedergabe hängt (pw-play) – Standardausgabe prüfen.")
                return
            if code != 0:
                self._fail(f"Wiedergabe fehlgeschlagen (pw-play, Code {code}) – gibt es eine Standardausgabe?")
                return
            self.bursts[-1] = (self.bursts[-1][0], total)
            self.phase, self.t_phase = "gap", now
        elif self.phase == "gap" and now - self.t_phase >= self.GAP_S:
            self.index += 1
            if self.index < len(self.files):
                self._play(now, total)
            else:
                self._analyse(total)

    def _analyse(self, total: int) -> None:
        start = self.noise[0]
        block = self.capture.span(start, total)
        if block is None:
            self._fail("Aufnahme unvollständig – bitte wiederholen.")
            return
        rel = [(s - start, e - start) for s, e in self.bursts]
        if self.dump is not None:
            try:
                self.dump.parent.mkdir(parents=True, exist_ok=True)
                np.savez(self.dump, block=np.asarray(block[:, :7], np.float32), bursts=np.array(rel),
                         noise=np.array([0, self.noise[1] - start]), sr=self.sr)
            except OSError:
                pass  # nur zur Fehlersuche
        self.result = analyse_speakers(block, rel, (0, self.noise[1] - start), self.positions, self.center, self.sr)
        if not self.result.ok:
            self.error = "\n".join(r.message for r in self.result.speakers)


# --- Live-Auswertung (Platzierungsansicht) ------------------------------------

class LiveAnalysis:
    """Je Block (0,2 s Rohaufnahme): SRP über den Azimut, Pegel; Sprachpegel nur aus Blöcken mit Sprache aus
    einer erlaubten Richtung (`accept`, z. B. Sprechzone ohne Lautsprecher)."""

    def __init__(self, positions: np.ndarray, center: int = 0, accept: Callable[[float], bool] | None = None,
                 sr: int = SR):
        self.srp = SrpPhat(positions, sr)
        self.n_el = len(set(self.srp.grid_el.tolist()))
        self.azimuths = self.srp.grid_az[::self.n_el]
        self.center, self.accept = center, accept
        self.vad = VoiceDetector(sample_rate=sr)
        self.levels: deque[float] = deque(maxlen=50)   # 10 s
        self.speech: deque[float] = deque(maxlen=25)
        self.map: np.ndarray | None = None
        self.last_level: float | None = None
        self.peak: float | None = None

    def feed(self, block: np.ndarray) -> None:
        x = np.asarray(block, float)[:, :7]
        c = x[:, self.center]
        level = band_level_dbfs(c)
        self.levels.append(level)
        self.last_level = level
        cs = self.srp.cross_spectra(x)
        srp = self.srp.srp(cs).reshape(-1, self.n_el).max(axis=1)
        self.map = srp if self.map is None else 0.5 * self.map + 0.5 * srp
        r = self.srp.estimate_cs(cs)
        self.peak = r.azimuth
        if self.vad.is_speech(c) and r.confidence >= MIN_CONFIDENCE and (self.accept is None or self.accept(r.azimuth)):
            self.speech.append(level)

    def speech_dbfs(self) -> float | None:
        return float(np.median(self.speech)) if len(self.speech) >= 3 else None

    def noise_dbfs(self) -> float | None:
        return float(np.percentile(self.levels, 10)) if len(self.levels) >= 5 else None

    def activity(self) -> float:
        """0 … 1: wie deutlich der letzte Block über dem Grundrauschen liegt (10 dB = 1)."""
        noise = self.noise_dbfs()
        if noise is None or self.last_level is None:
            return 0.0
        return float(np.clip((self.last_level - noise) / 10.0, 0.0, 1.0))

    def map_norm(self) -> np.ndarray | None:
        if self.map is None:
            return None
        lo, hi = float(self.map.min()), float(self.map.max())
        return (self.map - lo) / (hi - lo) if hi > lo else np.zeros_like(self.map)


# --- Hinweise -------------------------------------------------------------------

@dataclass
class Hint:
    good: bool
    text: str


def placement_hints(speech: float | None, noise: float | None, echo: float | None) -> list[Hint]:
    """Platzierungshinweise aus Sprach-, Grundrausch- und Lautsprecherpegel (Rohpegel, dBFS). Faustregeln:
    Je lauter das Echo gegen die eigene Stimme, desto stärker dämpft die Echounterdrückung bei Gegensprechen
    die Stimme (gemessen: Echo 5 dB unter der Stimme → Stimme −8 dB)."""
    hints = []
    if speech is None:
        hints.append(Hint(False, "Sprich ein paar Sätze, dann erscheint dein Sprachpegel."))
        return hints
    if echo is None:
        hints.append(Hint(False, "Lautsprecher noch nicht gemessen."))
    else:
        d = speech - echo
        good, low = SPEECH_ECHO_DB
        if d >= good:
            hints.append(Hint(True, f"Stimme {d:.0f} dB lauter als die Lautsprecher – gut."))
        elif d >= low:
            hints.append(Hint(False, f"Stimme nur {d:.0f} dB lauter als die Lautsprecher: Mikrofon näher zu dir "
                                     "oder weiter weg von den Lautsprechern."))
        else:
            hints.append(Hint(False, f"Lautsprecher etwa so laut wie deine Stimme ({d:+.0f} dB): Mikrofon näher zu "
                                     "dir, Lautsprecher leiser oder weiter weg – sonst wird deine Stimme bei "
                                     "Gegensprechen stark gedämpft."))
    if noise is not None:
        d = speech - noise
        good, low = SPEECH_NOISE_DB
        if d >= good:
            hints.append(Hint(True, f"Stimme {d:.0f} dB über dem Grundrauschen – gut."))
        elif d >= low:
            hints.append(Hint(False, f"Stimme nur {d:.0f} dB über dem Grundrauschen: Mikrofon näher zu dir."))
        else:
            hints.append(Hint(False, f"Grundrauschen fast so laut wie deine Stimme ({d:.0f} dB): Mikrofon näher zu "
                                     "dir, Lüfter oder Rechner weiter weg."))
    return hints
