//! Kohärenzbasierte Hallunterdrückung am Beam-Ausgang.
//!
//! Das Verhältnis von Direktschall zu diffusem Schall (CDR) wird je Bin aus der räumlichen
//! Kohärenz von Mikrofonpaaren geschätzt (Schwarz & Kellermann, IEEE/ACM TASLP 2015,
//! richtungsunabhängiger Schätzer). Der Beam hat den diffusen Anteil bereits um den
//! Richtwirkungsfaktor DI gesenkt, am Ausgang gilt also CDR·DI. Verstärkung als spektrale
//! Subtraktion: G = max(1 − μ/(1 + CDR·DI), G_min).
//!
//! Für kleine Kohärenz ist der Schätzer linear in |Γx|. Die geschätzte Kohärenz rein diffusen
//! Schalls ist aber um ≈ 1/√N_eff zu groß (N_eff = Zahl unabhängiger Mittelungen), und am
//! Ausgang wird das noch mit DI (bis ≈ 7) multipliziert – ohne Gegenmaßnahme bliebe diffuser
//! Hall fast ungedämpft. Deshalb wird über Nachbar-Bins geglättet und der erwartete Fehler
//! `BIAS_SCALE/√N_eff` vom CDR abgezogen (in Simulation: rein diffus CDR ≈ 0,03 statt 0,6,
//! Mischungen mit CDR ≥ 1 bleiben nahezu erwartungstreu).
//!
//! Die Konstanten sind mit `tools/eval_dereverb.py` (Spiegelquellen-Raum, T60 0,45/0,7 s)
//! abgestimmt; Begründung und Zahlen im Design-Dokument.

use crate::beam::{diffuse_coherence, wavenumber, Geometry, CHANNELS};
use crate::stft::{BINS, C32, FFT_LEN, HOP};

/// Paare mit großem Abstand: 3 Durchmesser (2r = 86 mm) und 6 Sehnen über ein Ringmikrofon
/// hinweg (√3·r = 74,5 mm). Sie trennen schon ab ≈ 430–500 Hz; Paare mit dem Mittel-Mikrofon
/// oder Nachbarn (43 mm) erst ab ≈ 850 Hz und kosten nur Rechenzeit.
pub const PAIRS: usize = 9;
/// Zeitkonstante der Kohärenzschätzung.
const COHERENCE_TAU_S: f32 = 0.035;
/// Glättung über ±SMOOTH_BINS Nachbar-Bins (7 Bins ≈ 330 Hz).
const SMOOTH_BINS: usize = 3;
/// Abzug vom CDR in Einheiten von 1/√N_eff (per Simulation bestimmt).
const BIAS_SCALE: f32 = 1.2;
/// |ρ|² zwischen Frames im Abstand 1–3 Hops (Wurzel-Hann, 75 % Überlappung) und zwischen Nachbar-Bins.
const FRAME_RHO2: [f32; 3] = [0.57, 0.10, 0.0023];
const BIN_RHO2: f32 = 0.25;
/// Ein Paar trennt erst, wenn seine Diffuskohärenz darunter liegt (tiefe Frequenzen: sinc ≈ 1).
/// 0,93 → Durchmesser ab ≈ 430 Hz; darunter gilt die mittlere Verstärkung der Oktave darüber.
const MAX_DIFFUSE_COHERENCE: f64 = 0.93;
/// Bis hierher wird der CDR geschätzt; darüber gilt die mittlere Verstärkung der Oktave darunter
/// (Schätzung bis 12 oder 24 kHz brachte im Raum-Test nichts, kostet aber Rechenzeit).
const MAX_FREQ_HZ: f64 = 8000.0;
const MAX_COHERENCE: f32 = 0.999;
/// Verstärkung steigt sofort (Sprachanfänge bleiben erhalten) und fällt mit 0,8 je Hop (≈ 24 ms).
const GAIN_RELEASE: f32 = 0.8;
const EPS: f32 = 1e-30;

/// Richtungsunabhängiger CDR-Schätzer aus gemessener Kohärenz `gx` und Diffuskohärenz `gn`.
pub fn cdr_nodoa(gx: C32, gn: f32) -> f32 {
    let mag = gx.norm();
    let gx = if mag > MAX_COHERENCE { gx * (MAX_COHERENCE / mag) } else { gx };
    let (re, mag2, gn2) = (gx.re, gx.norm_sqr(), gn * gn);
    let root = (gn2 * re * re - gn2 * mag2 + gn2 - 2.0 * gn * re + mag2).max(0.0).sqrt();
    ((gn * re - mag2 - root) / (mag2 - 1.0)).max(0.0)
}

/// „Dereverb Strength“ s ∈ [0, 1] → (Untergrenze G_min, Überschätzung μ):
/// G_min = −25·s dB, μ = 1 + 0,5·s. Bei s = 0 bleibt das Signal unverändert.
pub fn strength_params(s: f32) -> (f32, f32) {
    let s = s.clamp(0.0, 1.0);
    (10f32.powf(-25.0 * s / 20.0), 1.0 + 0.5 * s)
}

/// Zahl unabhängiger Mittelungen der Kohärenzschätzung (rekursiv über Frames, über Bins).
fn effective_averages(alpha: f32) -> f32 {
    let frames: f32 = FRAME_RHO2.iter().enumerate().map(|(m, r)| alpha.powi(m as i32 + 1) * r).sum();
    let v_time = (1.0 - alpha) / (1.0 + alpha) * (1.0 + 2.0 * frames);
    let n = (2 * SMOOTH_BINS + 1) as f32;
    let v_freq = (1.0 + 2.0 * (n - 1.0) * BIN_RHO2 / n) / n;
    1.0 / (v_time * v_freq)
}

#[inline]
fn ftz(x: f32) -> f32 {
    if x.abs() < EPS {
        0.0
    } else {
        x
    }
}

pub struct Postfilter {
    sample_rate: f64,
    alpha: f32,
    bias: f32,
    geom: Option<Geometry>,
    pairs: [(usize, usize); PAIRS],
    first_bin: [usize; PAIRS],
    lo_bin: usize,
    hi_bin: usize,
    gamma_n: Vec<[f32; PAIRS]>,
    psd: Vec<[f32; CHANNELS]>,
    cpsd: Vec<[C32; PAIRS]>,
    target: Vec<f32>,
    gain: Vec<f32>,
    enabled: bool,
    floor: f32,
    mu: f32,
}

impl Postfilter {
    pub fn new(sample_rate: f32, geom: &Geometry) -> Self {
        let sr = sample_rate as f64;
        let alpha = (-(HOP as f32) / (COHERENCE_TAU_S * sample_rate)).exp();
        let mut p = Postfilter {
            sample_rate: sr,
            alpha,
            bias: BIAS_SCALE / effective_averages(alpha).sqrt(),
            geom: None,
            pairs: [(0, 0); PAIRS],
            first_bin: [0; PAIRS],
            lo_bin: SMOOTH_BINS + 1,
            hi_bin: ((MAX_FREQ_HZ * FFT_LEN as f64 / sr).round() as usize).clamp(2 * SMOOTH_BINS + 2, BINS - 1 - SMOOTH_BINS),
            gamma_n: vec![[0.0; PAIRS]; BINS],
            psd: vec![[0.0; CHANNELS]; BINS],
            cpsd: vec![[C32::new(0.0, 0.0); PAIRS]; BINS],
            target: vec![1.0; BINS],
            gain: vec![1.0; BINS],
            enabled: false,
            floor: 1.0,
            mu: 1.0,
        };
        p.set_geometry(geom);
        p
    }

    pub fn set_params(&mut self, enabled: bool, strength: f32) {
        self.enabled = enabled;
        (self.floor, self.mu) = strength_params(strength);
    }

    /// Paare und Diffuskohärenzen für eine (gültige) Geometrie; nur bei Änderung neu berechnet.
    pub fn set_geometry(&mut self, geom: &Geometry) {
        if self.geom == Some(*geom) {
            return;
        }
        self.geom = Some(*geom);
        let r = geom.ring;
        for s in 0..3 {
            self.pairs[s] = (r[s], r[s + 3]);
        }
        for s in 0..6 {
            self.pairs[3 + s] = (r[s], r[(s + 2) % 6]);
        }
        const B: usize = SMOOTH_BINS;
        for (p, &(i, j)) in self.pairs.iter().enumerate() {
            let d = geom.distance(i, j);
            self.first_bin[p] = BINS;
            // Diffuskohärenz gemittelt über dieselben Bins wie die Spektren
            for k in B..BINS - B {
                let gn = (k - B..=k + B).map(|kk| diffuse_coherence(wavenumber(kk, self.sample_rate) * d)).sum::<f64>()
                    / (2 * B + 1) as f64;
                self.gamma_n[k][p] = gn as f32;
                if gn <= MAX_DIFFUSE_COHERENCE && self.first_bin[p] == BINS {
                    self.first_bin[p] = k;
                }
            }
        }
        self.lo_bin = self.first_bin.iter().copied().min().unwrap_or(B + 1).clamp(B + 1, self.hi_bin);
    }

    pub fn reset(&mut self) {
        self.psd.fill([0.0; CHANNELS]);
        self.cpsd.fill([C32::new(0.0, 0.0); PAIRS]);
        self.gain.fill(1.0);
    }

    /// Aktualisiert die Kohärenzschätzung mit den Mikrofonspektren `x` und liefert die
    /// Verstärkung je Bin für den Beam-Ausgang mit Richtwirkungsfaktor `di`.
    pub fn gains(&mut self, x: &[Vec<C32>], di: &[f32]) -> &[f32] {
        const B: usize = SMOOTH_BINS;
        let (a, b) = (self.alpha, 1.0 - self.alpha);
        for k in self.lo_bin - B..=self.hi_bin + B {
            for (c, psd) in self.psd[k].iter_mut().enumerate() {
                *psd = ftz(a * *psd + b * x[c][k].norm_sqr());
            }
            for (cpsd, &(i, j)) in self.cpsd[k].iter_mut().zip(&self.pairs) {
                let v = *cpsd * a + x[i][k] * x[j][k].conj() * b;
                *cpsd = C32::new(ftz(v.re), ftz(v.im));
            }
        }
        if !self.enabled {
            for g in self.gain.iter_mut() {
                *g = (GAIN_RELEASE * *g + (1.0 - GAIN_RELEASE)).min(1.0);
            }
            return &self.gain;
        }
        for k in self.lo_bin..=self.hi_bin {
            let mut psd = [0.0f32; CHANNELS];
            let mut cpsd = [C32::new(0.0, 0.0); PAIRS];
            for kk in k - B..=k + B {
                for c in 0..CHANNELS {
                    psd[c] += self.psd[kk][c];
                }
                for p in 0..PAIRS {
                    cpsd[p] += self.cpsd[kk][p];
                }
            }
            let (mut sum, mut n) = (0.0, 0);
            for p in 0..PAIRS {
                if k >= self.first_bin[p] {
                    let (i, j) = self.pairs[p];
                    let gx = cpsd[p] / (psd[i] * psd[j] + EPS).sqrt();
                    sum += cdr_nodoa(gx, self.gamma_n[k][p]);
                    n += 1;
                }
            }
            let cdr = (sum / n.max(1) as f32 - self.bias).max(0.0) * di[k];
            self.target[k] = (1.0 - self.mu / (1.0 + cdr)).max(self.floor);
        }
        // Ränder aus dem nächsten verlässlichen Band (je eine Oktave) auffüllen.
        let mean = |t: &[f32]| t.iter().sum::<f32>() / t.len() as f32;
        let (lo, hi) = (self.lo_bin, self.hi_bin);
        let lo_fill = mean(&self.target[lo..=(2 * lo).min(hi)]);
        let hi_fill = mean(&self.target[(hi / 2).max(lo)..=hi]);
        self.target[..lo].fill(lo_fill);
        self.target[hi + 1..].fill(hi_fill);
        for (g, &t) in self.gain.iter_mut().zip(&self.target) {
            *g = if t > *g { t } else { GAIN_RELEASE * *g + (1.0 - GAIN_RELEASE) * t };
        }
        &self.gain
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::jacobi;

    struct Rng(u32);
    impl Rng {
        fn uniform(&mut self) -> f32 {
            self.0 = self.0.wrapping_mul(1664525).wrapping_add(1013904223);
            ((self.0 >> 8) as f32 + 0.5) / (1u32 << 24) as f32
        }
        fn gauss(&mut self) -> C32 {
            let (u, v) = (self.uniform(), self.uniform());
            C32::from_polar((-u.ln()).sqrt(), 2.0 * std::f32::consts::PI * v)
        }
    }

    #[test]
    fn estimator_recovers_known_cdr() {
        let mut rng = Rng(5);
        for &cdr in &[0.0f32, 0.1, 0.5, 1.0, 3.0, 10.0, 50.0] {
            for &gn in &[0.9f32, 0.6, 0.2, -0.2, 0.05] {
                for _ in 0..5 {
                    let phase = 2.0 * std::f32::consts::PI * rng.uniform();
                    let gx = (C32::from_polar(cdr, phase) + gn) / (cdr + 1.0);
                    let est = cdr_nodoa(gx, gn);
                    assert!((est - cdr).abs() <= 1e-3 * (1.0 + cdr), "CDR {cdr}, Γn {gn}: {est}");
                }
            }
        }
        assert_eq!(cdr_nodoa(C32::new(0.6, 0.0), 0.6), 0.0, "rein diffus");
    }

    /// Spektren mit gegebener Kohärenzmatrix je Bin: x = V·√Λ·z mit unabhängigem z.
    fn frames(geom: &Geometry, coherent: bool, rng: &mut Rng) -> Vec<Vec<C32>> {
        let mut x = vec![vec![C32::new(0.0, 0.0); BINS]; CHANNELS];
        for k in 0..BINS {
            if coherent {
                let s = rng.gauss();
                let p = geom.positions();
                for c in 0..CHANNELS {
                    let tau = wavenumber(k, 48000.0) * p[c][0];
                    x[c][k] = s * C32::from_polar(1.0, tau as f32);
                }
                continue;
            }
            let wn = wavenumber(k, 48000.0);
            let g: [[f64; CHANNELS]; CHANNELS] =
                std::array::from_fn(|i| std::array::from_fn(|j| diffuse_coherence(wn * geom.distance(i, j))));
            let (l, v) = jacobi::eigh(g);
            let z: [C32; CHANNELS] = std::array::from_fn(|i| rng.gauss() * l[i].max(0.0).sqrt() as f32);
            for c in 0..CHANNELS {
                x[c][k] = (0..CHANNELS).map(|i| z[i] * v[c][i] as f32).sum();
            }
        }
        x
    }

    fn mean_gain(coherent: bool) -> f32 {
        let geom = Geometry::UMA8;
        let mut pf = Postfilter::new(48000.0, &geom);
        pf.set_params(true, 1.0);
        let di = vec![1.0; BINS];
        let mut rng = Rng(11);
        let mut g = vec![];
        for _ in 0..60 {
            g = pf.gains(&frames(&geom, coherent, &mut rng), &di).to_vec();
        }
        g[20..BINS].iter().sum::<f32>() / (BINS - 20) as f32
    }

    #[test]
    fn diffuse_field_is_suppressed_and_plane_wave_passes() {
        let (floor, _) = strength_params(1.0);
        let diffuse = mean_gain(false);
        let direct = mean_gain(true);
        assert!(diffuse < 2.0 * floor, "diffus: {diffuse}");
        assert!(direct > 0.95, "Direktschall: {direct}");
    }
}
