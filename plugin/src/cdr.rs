//! Kohärenzbasierte Hallunterdrückung am Beam-Ausgang.
//!
//! Das Verhältnis von Direktschall zu diffusem Schall (CDR) wird je Bin aus der räumlichen
//! Kohärenz von Mikrofonpaaren geschätzt (Schwarz & Kellermann, IEEE/ACM TASLP 2015,
//! richtungsunabhängiger Schätzer). Der Beam hat den diffusen Anteil bereits um den
//! Richtwirkungsfaktor DI gesenkt, am Ausgang gilt also CDR·DI. Verstärkung als spektrale
//! Subtraktion: G = max(1 − μ/(1 + CDR·DI), G_min).
//!
//! Schätzfehler: Die geschätzte Kohärenz Γ̂ streut um die wahre Γ mit
//! E|Γ̂ − Γ|² ≈ (1 − |Γ|²)(2 − |Γ|²)/(2·N_eff) – Betrag (1 − |Γ|²)²/2N plus Phase (1 − |Γ|²)/2N
//! (Carter 1973), N_eff = Zahl unabhängiger Mittelungen. Nahe der Diffuskohärenz Γn ist der
//! Schätzer linear im Betrag der Abweichung Γ̂ − Γn; rein diffuser Schall ergäbe so CDR ≈ 0,2 und
//! mal DI (bis ≈ 10) kaum Dämpfung. Deshalb wird je Paar die erwartete Fehlerleistung von der
//! Abweichung abgezogen (Richtung bleibt, wie bei spektraler Subtraktion), erst dann folgt der
//! CDR-Schätzer. Die Korrektur verschwindet für kohärenten Schall (|Γ̂| → 1) und relativ für
//! Abweichungen weit über dem Fehler. Ein fester Abzug vom CDR (≈ 0,22) hätte dagegen auch Bins
//! mit CDR·DI ≈ 1–2, also schon überwiegendem Direktschall, auf die Untergrenze gedrückt.
//!
//! Die Konstanten sind mit `tools/eval_dereverb.py` (Spiegelquellen-Raum, T60 0,45/0,7 s)
//! abgestimmt; Begründung und Zahlen im Design-Dokument.

use crate::beam::{diffuse_coherence, wavenumber, Geometry, CHANNELS};
use crate::stft::{BINS, C32, FFT_LEN, HOP};

/// Paare mit großem Abstand: 3 Durchmesser (2r = 86 mm) und 6 Sehnen über ein Ringmikrofon
/// hinweg (√3·r = 74,5 mm). Sie trennen schon ab ≈ 430–500 Hz; Paare mit dem Mittel-Mikrofon
/// oder Nachbarn (43 mm) erst ab ≈ 850 Hz, kosten Rechenzeit und machen das Mittel unruhiger.
pub const PAIRS: usize = 9;
/// Zeitkonstante der Kohärenzschätzung.
const COHERENCE_TAU_S: f32 = 0.035;
/// Glättung über ±SMOOTH_BINS Nachbar-Bins (7 Bins ≈ 330 Hz).
const SMOOTH_BINS: usize = 3;
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

/// Zieht die erwartete Fehlerleistung `var_scale·(1 − |Γ̂|²)(2 − |Γ̂|²)` der Kohärenzschätzung
/// von der Abweichung `gx − gn` ab (Leistungssubtraktion, Richtung bleibt). `var_scale` =
/// β/(2·N_eff), β = Überschätzung. `None`: Abweichung im Rahmen des Fehlers, also CDR 0.
pub fn denoise_coherence(gx: C32, gn: f32, var_scale: f32) -> Option<C32> {
    let r2 = gx.norm_sqr().min(1.0);
    let noise = var_scale * (1.0 - r2) * (2.0 - r2);
    let d = gx - gn;
    let d2 = d.norm_sqr();
    (d2 > noise).then(|| d * (1.0 - noise / d2).sqrt() + gn)
}

/// „Dereverb Strength“ s ∈ [0, 1] → (Untergrenze G_min, Überschätzung μ):
/// G_min = −25·s dB, μ = 1 + 0,5·s. Bei s = 0 bleibt das Signal unverändert.
pub fn strength_params(s: f32) -> (f32, f32) {
    let s = s.clamp(0.0, 1.0);
    (10f32.powf(-25.0 * s / 20.0), 1.0 + 0.5 * s)
}

/// Überschätzung β des Kohärenz-Schätzfehlers: bis Stärke 0,6 (G_min −15 dB) erwartungstreu,
/// darüber wächst sie mit der Tiefe der Untergrenze bis β = 3 bei Stärke 1 (−25 dB). Je tiefer
/// die Untergrenze, desto stärker fallen einzelne Bins auf, die ein Schätzfehler darüber hebt
/// (Musical Noise). Im Raum-Test (1–8 kHz, Nachhall nach Sprachende) stehen so bei Stärke 0,6 im
/// Mittel ≈ 10 % der Bins ≥ 10 dB über der Untergrenze, bei Stärke 1 ≈ 12 % wie mit dem früheren
/// festen Abzug; mit β = 1 wären es dort 50 %.
pub fn noise_oversubtraction(s: f32) -> f32 {
    1.0 + 5.0 * (s.clamp(0.0, 1.0) - 0.6).max(0.0)
}

/// Zahl unabhängiger Mittelungen der Kohärenzschätzung (rekursiv über Frames, über Bins).
fn effective_averages(alpha: f32) -> f32 {
    let frames: f32 = FRAME_RHO2.iter().enumerate().map(|(m, r)| alpha.powi(m as i32 + 1) * r).sum();
    let v_time = (1.0 - alpha) / (1.0 + alpha) * (1.0 + 2.0 * frames);
    let n = (2 * SMOOTH_BINS + 1) as f32;
    let v_freq = (1.0 + 2.0 * (n - 1.0) * BIN_RHO2 / n) / n;
    1.0 / (v_time * v_freq)
}

/// Schätzzustand: Denormals und nicht endliche Werte werden 0 (ein ∞ bliebe sonst für immer).
#[inline]
fn clean(x: f32) -> f32 {
    if x.abs() >= EPS && x.is_finite() {
        x
    } else {
        0.0
    }
}

pub struct Postfilter {
    sample_rate: f64,
    alpha: f32,
    /// 1/(2·N_eff)
    noise_var: f32,
    /// β/(2·N_eff)
    var_scale: f32,
    geom: Option<Geometry>,
    pairs: [(usize, usize); PAIRS],
    pair_dist: [f64; PAIRS],
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
        let noise_var = 0.5 / effective_averages(alpha);
        let mut p = Postfilter {
            sample_rate: sr,
            alpha,
            noise_var,
            var_scale: noise_var,
            geom: None,
            pairs: [(0, 0); PAIRS],
            pair_dist: [0.0; PAIRS],
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
        self.var_scale = noise_oversubtraction(strength) * self.noise_var;
    }

    /// Paare und Diffuskohärenzen für eine (gültige) Geometrie; nur bei Änderung neu berechnet.
    /// Gleiche Abstände (alle Durchmesser, alle Sehnen) werden nur einmal gerechnet, die Mittelung
    /// über Nachbar-Bins läuft als gleitende Summe: ≈ 2·BINS sinc-Werte, keine Spitze in `run()`.
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
        const W: usize = 2 * B + 1;
        for p in 0..PAIRS {
            let (i, j) = self.pairs[p];
            let d = geom.distance(i, j);
            self.pair_dist[p] = d;
            if let Some(q) = (0..p).find(|&q| (self.pair_dist[q] - d).abs() <= 1e-9 * d) {
                for g in self.gamma_n.iter_mut() {
                    g[p] = g[q];
                }
                self.first_bin[p] = self.first_bin[q];
                continue;
            }
            // Diffuskohärenz gemittelt über dieselben Bins wie die Spektren
            let sinc = |kk: usize| diffuse_coherence(wavenumber(kk, self.sample_rate) * d);
            let mut window = [0.0f64; W];
            for (kk, v) in window.iter_mut().enumerate() {
                *v = sinc(kk);
            }
            let mut sum: f64 = window.iter().sum();
            self.first_bin[p] = BINS;
            for k in B..BINS - B {
                if k > B {
                    let new = sinc(k + B);
                    let slot = (k + B) % W;
                    sum += new - window[slot];
                    window[slot] = new;
                }
                let gn = sum / W as f64;
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
                *psd = clean(a * *psd + b * x[c][k].norm_sqr());
            }
            for (cpsd, &(i, j)) in self.cpsd[k].iter_mut().zip(&self.pairs) {
                let v = *cpsd * a + x[i][k] * x[j][k].conj() * b;
                *cpsd = C32::new(clean(v.re), clean(v.im));
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
                    let gn = self.gamma_n[k][p];
                    let gx = cpsd[p] / (psd[i] * psd[j] + EPS).sqrt();
                    if let Some(g) = denoise_coherence(gx, gn, self.var_scale) {
                        sum += cdr_nodoa(g, gn);
                    }
                    n += 1;
                }
            }
            let cdr = sum / n.max(1) as f32 * di[k];
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

    /// Schallfeld je Bin: ebene Welle aus +x (Leistung `direct`) plus 3D-diffuser Schall
    /// (Leistung 1 je Mikrofon, x = V·√Λ·z mit unabhängigem z), unabhängig über Bins und Frames.
    struct Field {
        geom: Geometry,
        lambda: Vec<[f64; CHANNELS]>,
        v: Vec<[[f64; CHANNELS]; CHANNELS]>,
    }

    impl Field {
        fn new() -> Self {
            let geom = Geometry::UMA8;
            let (mut lambda, mut v) = (vec![], vec![]);
            for k in 0..BINS {
                let wn = wavenumber(k, 48000.0);
                let g: [[f64; CHANNELS]; CHANNELS] =
                    std::array::from_fn(|i| std::array::from_fn(|j| diffuse_coherence(wn * geom.distance(i, j))));
                let (l, vv) = jacobi::eigh(g);
                lambda.push(l);
                v.push(vv);
            }
            Field { geom, lambda, v }
        }

        fn frame(&self, direct: f32, diffuse: f32, rng: &mut Rng) -> Vec<Vec<C32>> {
            let p = self.geom.positions();
            let mut x = vec![vec![C32::new(0.0, 0.0); BINS]; CHANNELS];
            for k in 0..BINS {
                let s = rng.gauss() * direct.sqrt();
                let z: [C32; CHANNELS] =
                    std::array::from_fn(|i| rng.gauss() * (self.lambda[k][i].max(0.0) as f32 * diffuse).sqrt());
                for c in 0..CHANNELS {
                    let tau = wavenumber(k, 48000.0) * p[c][0];
                    x[c][k] = s * C32::from_polar(1.0, tau as f32)
                        + (0..CHANNELS).map(|i| z[i] * self.v[k][c][i] as f32).sum::<C32>();
                }
            }
            x
        }
    }

    /// Postfilter nach 60 Frames des Felds.
    fn settled(field: &Field, strength: f32, di: f32, direct: f32, diffuse: f32) -> Postfilter {
        let mut pf = Postfilter::new(48000.0, &field.geom);
        pf.set_params(true, strength);
        let di = vec![di; BINS];
        let mut rng = Rng(11);
        for _ in 0..60 {
            pf.gains(&field.frame(direct, diffuse, &mut rng), &di);
        }
        pf
    }

    /// Mittlere Verstärkung im geschätzten Band nach 60 Frames.
    fn steady_gain(field: &Field, strength: f32, di: f32, direct: f32, diffuse: f32) -> f32 {
        let pf = settled(field, strength, di, direct, diffuse);
        let band = pf.lo_bin..=pf.hi_bin;
        let n = band.clone().count() as f32;
        pf.gain[band].iter().sum::<f32>() / n
    }

    #[test]
    fn diffuse_field_is_suppressed_and_plane_wave_passes() {
        let f = Field::new();
        for s in [0.6f32, 1.0] {
            let (floor, _) = strength_params(s);
            // Der Beam (DI bis ≈ 10) verstärkt jeden Schätzfehler; ohne Korrektur läge diffuser
            // Schall hier bei 0,25–0,4.
            for di in [1.0, 10.0] {
                let diffuse = steady_gain(&f, s, di, 0.0, 1.0);
                assert!(diffuse < 1.2 * floor, "Stärke {s}, DI {di}: diffus {diffuse}");
            }
            // auch unter und über dem geschätzten Band (dort gilt die Nachbar-Oktave)
            let direct = settled(&f, s, 1.0, 1.0, 0.0);
            let worst = direct.gain.iter().copied().fold(1.0, f32::min);
            assert!(worst > 0.95, "Stärke {s}: Direktschall {worst}");
        }
    }

    #[test]
    fn gain_follows_cdr_at_beam_output() {
        // Mikrofon-CDR 0,3 (Hall überwiegt), am Beam-Ausgang mit DI 6 aber 1,8 (Direktschall
        // überwiegt): G = 1 − 1,3/2,8 = 0,54. Weder der Richtwirkungsfaktor darf fehlen noch die
        // Schätzfehler-Korrektur solche Bins auf die Untergrenze drücken (der frühere feste Abzug
        // 0,22 vom CDR tat das).
        let f = Field::new();
        let (floor, _) = strength_params(0.6);
        let with_beam = steady_gain(&f, 0.6, 6.0, 0.3, 1.0);
        assert!((0.38..=0.65).contains(&with_beam), "DI 6: {with_beam}");
        let omni = steady_gain(&f, 0.6, 1.0, 0.3, 1.0);
        assert!(omni < 1.1 * floor, "DI 1: {omni}");
        // überwiegend direkt auch am Mikrofon: nahe am Idealwert 1 − 1,3/(1 + 6) = 0,81
        let strong = steady_gain(&f, 0.6, 6.0, 1.0, 1.0);
        assert!((strong - 0.814).abs() < 0.05, "CDR 1, DI 6: {strong}");
    }

    #[test]
    fn gain_rises_at_once_and_falls_slowly() {
        let f = Field::new();
        let (floor, _) = strength_params(0.6);
        let mut pf = Postfilter::new(48000.0, &f.geom);
        pf.set_params(true, 0.6);
        let di = vec![6.0; BINS];
        let mut rng = Rng(3);
        let band = pf.lo_bin..=pf.hi_bin;
        let mean = |g: &[f32]| g[band.clone()].iter().sum::<f32>() / band.clone().count() as f32;
        for _ in 0..40 {
            pf.gains(&f.frame(0.0, 1.0, &mut rng), &di);
        }
        assert!(mean(&pf.gain) < 1.1 * floor);
        // Sprachanfang 20 dB über dem Hall: schon im ersten Frame offen
        let onset = mean(pf.gains(&f.frame(100.0, 1.0, &mut rng), &di));
        assert!(onset > 0.9, "erster Frame nach dem Anfang: {onset}");
        for _ in 0..10 {
            pf.gains(&f.frame(100.0, 1.0, &mut rng), &di);
        }
        // Ende: höchstens 0,8 je Hop zurück (dazu das Gedächtnis der Schätzung), nach 0,4 s am Boden
        let mut prev = mean(&pf.gain);
        for hop in 0..75 {
            let g = mean(pf.gains(&f.frame(0.0, 1.0, &mut rng), &di));
            assert!(g >= 0.8 * prev - 1e-6, "Hop {hop}: {prev} → {g}");
            prev = g;
        }
        assert!(prev < 1.1 * floor, "nach 0,4 s: {prev}");
    }

    #[test]
    fn coherence_correction_vanishes_for_coherent_sound() {
        let v = 0.5 / 30.0;
        // rein kohärent: unverändert
        let gx = C32::from_polar(0.999, 1.3);
        assert!((denoise_coherence(gx, 0.2, v).unwrap() - gx).norm() < 1e-4);
        // Abweichung von Γn im Bereich des Schätzfehlers: CDR 0
        assert_eq!(denoise_coherence(C32::new(0.3, 0.1), 0.3, v), None);
        // große Abweichung: Richtung bleibt, Betrag nur leicht kleiner
        let mix = C32::new(0.3, 0.6);
        let out = denoise_coherence(mix, 0.3, v).unwrap();
        let (d_in, d_out) = (mix - 0.3, out - 0.3);
        assert!((d_in.arg() - d_out.arg()).abs() < 1e-6);
        assert!(d_out.norm() < d_in.norm() && d_out.norm() > 0.97 * d_in.norm(), "{out}");
    }

    #[test]
    fn non_finite_spectrum_does_not_latch() {
        let f = Field::new();
        let di = vec![6.0; BINS];
        let (mut pf, mut fresh) = (Postfilter::new(48000.0, &f.geom), Postfilter::new(48000.0, &f.geom));
        pf.set_params(true, 0.6);
        fresh.set_params(true, 0.6);
        let mut bad = f.frame(1.0, 1.0, &mut Rng(1));
        for k in [20, 21, 60, 150] {
            bad[2][k] = C32::new(f32::INFINITY, 0.0);
            bad[5][k] = C32::new(f32::NAN, 1.0);
        }
        pf.gains(&bad, &di);
        let mut rng = Rng(2);
        for _ in 0..100 {
            let x = f.frame(0.3, 1.0, &mut rng);
            pf.gains(&x, &di);
            fresh.gains(&x, &di);
        }
        for k in 0..BINS {
            assert!((pf.gain[k] - fresh.gain[k]).abs() < 1e-4, "k={k}: {} statt {}", pf.gain[k], fresh.gain[k]);
        }
    }

    #[test]
    fn diffuse_coherence_table_matches_direct_average() {
        // gleitende Summe und Wiederverwendung gleicher Abstände gegen die direkte Rechnung
        let g = Geometry { center: 3, ring: [5, 0, 6, 2, 1, 4], ring_offset_deg: 17.0, radius_m: 0.04 };
        let pf = Postfilter::new(44100.0, &g);
        for (p, &(i, j)) in pf.pairs.iter().enumerate() {
            let d = g.distance(i, j);
            for k in SMOOTH_BINS..BINS - SMOOTH_BINS {
                let direct = (k - SMOOTH_BINS..=k + SMOOTH_BINS)
                    .map(|kk| diffuse_coherence(wavenumber(kk, 44100.0) * d))
                    .sum::<f64>()
                    / (2 * SMOOTH_BINS + 1) as f64;
                assert!((pf.gamma_n[k][p] as f64 - direct).abs() < 1e-6, "Paar {p}, k={k}");
            }
            let first = (SMOOTH_BINS..BINS - SMOOTH_BINS).find(|&k| pf.gamma_n[k][p] as f64 <= MAX_DIFFUSE_COHERENCE);
            assert_eq!(Some(pf.first_bin[p]), first, "Paar {p}");
        }
    }
}
