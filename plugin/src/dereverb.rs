//! Unterdrückung des späten Nachhalls (Lebart/Habets) am Beam-Ausgang: PSD des späten Nachhalls
//! aus dem exponentiellen Abklingmodell (T60), Wiener-artige Verstärkung mit Untergrenze aus
//! „Strength“, zeitliche Glättung gegen Musical Noise. Deaktiviert läuft die Verstärkung auf 1.

use crate::stft::{BINS, C32, HOP};

/// Ab hier gilt Schall als später Nachhall. 50 ms ließen die ersten ≈ 60 ms jedes Ausklangs unberührt
/// und schnitten dann steil ab (im Raum hörbar als „abgehackter“ Hall); 25 ms klingen gleichmäßiger.
const LATE_ONSET_S: f32 = 0.025;
const MAX_ONSET_FRAMES: usize = 32;
const PSD_SMOOTH: f32 = 0.8;
const GAIN_SMOOTH: f32 = 0.5;
/// Größte Dämpfung bei Stärke 1 (0,6 → 15 dB). 15 dB ließen bei lauter Sprache hörbaren Hall stehen;
/// 25 dB ohne Musical Noise im Hörvergleich.
const MAX_ATTENUATION_DB: f32 = 25.0;
const EPS: f32 = 1e-30;

pub struct LateReverb {
    sample_rate: f32,
    psd: Vec<f32>,
    psd_hist: Vec<Vec<f32>>,
    hist_pos: usize,
    onset_frames: usize,
    gain: Vec<f32>,
    enabled: bool,
    strength: f32,
    t60: f32,
}

impl LateReverb {
    pub fn new(sample_rate: f32) -> Self {
        LateReverb {
            sample_rate,
            psd: vec![0.0; BINS],
            psd_hist: vec![vec![0.0; BINS]; MAX_ONSET_FRAMES + 1],
            hist_pos: 0,
            onset_frames: ((LATE_ONSET_S * sample_rate / HOP as f32).round() as usize).clamp(1, MAX_ONSET_FRAMES),
            gain: vec![1.0; BINS],
            enabled: false,
            strength: 0.6,
            t60: 0.5,
        }
    }

    pub fn set_params(&mut self, enabled: bool, strength: f32, t60: f32) {
        self.enabled = enabled;
        self.strength = strength.clamp(0.0, 1.0);
        self.t60 = t60.clamp(0.1, 1.5);
    }

    pub fn reset(&mut self) {
        self.psd.fill(0.0);
        for h in self.psd_hist.iter_mut() {
            h.fill(0.0);
        }
        self.gain.fill(1.0);
    }

    /// Aktualisiert das Modell mit dem Spektrum `y` und liefert die Verstärkung je Bin.
    pub fn gains(&mut self, y: &[C32]) -> &[f32] {
        let delta = 3.0 * std::f32::consts::LN_10 / self.t60;
        let decay = (-2.0 * delta * (self.onset_frames * HOP) as f32 / self.sample_rate).exp();
        let slots = MAX_ONSET_FRAMES + 1;
        // hist_pos = letzter gespeicherter Frame (1 Frame alt) → D Frames alt = hist_pos + 1 − D
        let old = (self.hist_pos + 1 + slots - self.onset_frames) % slots;
        let floor = 10f32.powf(-self.strength * MAX_ATTENUATION_DB / 20.0);
        for k in 0..BINS {
            let psd = PSD_SMOOTH * self.psd[k] + (1.0 - PSD_SMOOTH) * y[k].norm_sqr();
            // Denormals und (nach der Eingangsbegrenzung eigentlich unmöglich) ∞/NaN verwerfen,
            // sonst bliebe die Verstärkung für immer an der Untergrenze.
            self.psd[k] = if psd >= EPS && psd.is_finite() { psd } else { 0.0 };
            let late = decay * self.psd_hist[old][k];
            let target = if self.enabled { (1.0 - late / (self.psd[k] + EPS)).max(0.0).sqrt().max(floor) } else { 1.0 };
            self.gain[k] = GAIN_SMOOTH * self.gain[k] + (1.0 - GAIN_SMOOTH) * target;
        }
        self.hist_pos = (self.hist_pos + 1) % slots;
        self.psd_hist[self.hist_pos].copy_from_slice(&self.psd);
        &self.gain
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stft::{Stft, LATENCY};
    use realfft::RealFftPlanner;
    const SR: f32 = 48000.0;

    fn noise(len: usize, seed: u32) -> Vec<f32> {
        let mut s = seed;
        (0..len)
            .map(|_| {
                s = s.wrapping_mul(1664525).wrapping_add(1013904223);
                (s >> 8) as f32 / (1u32 << 24) as f32 * 2.0 - 1.0
            })
            .collect()
    }

    fn convolve(x: &[f32], h: &[f32]) -> Vec<f32> {
        let n = (x.len() + h.len()).next_power_of_two();
        let mut planner = RealFftPlanner::<f32>::new();
        let (fwd, inv) = (planner.plan_fft_forward(n), planner.plan_fft_inverse(n));
        let (mut a, mut b) = (x.to_vec(), h.to_vec());
        a.resize(n, 0.0);
        b.resize(n, 0.0);
        let (mut sa, mut sb) = (fwd.make_output_vec(), fwd.make_output_vec());
        fwd.process(&mut a, &mut sa).unwrap();
        fwd.process(&mut b, &mut sb).unwrap();
        for (p, q) in sa.iter_mut().zip(sb.iter()) {
            *p = *p * *q / n as f32;
        }
        let last = sa.len() - 1;
        sa[0].im = 0.0;
        sa[last].im = 0.0;
        let mut out = inv.make_output_vec();
        inv.process(&mut sa, &mut out).unwrap();
        out.truncate(x.len());
        out
    }

    fn energy(x: &[f32]) -> f32 {
        x.iter().map(|v| v * v).sum()
    }

    fn run(d: &mut LateReverb, x: &[f32]) -> Vec<f32> {
        let mut stft = Stft::new(1);
        let mut y = vec![0.0; x.len()];
        for n in 0..x.len() {
            if stft.push(|_| x[n]) {
                stft.analyze();
                let g = d.gains(&stft.spectra[0]);
                for k in 0..BINS {
                    stft.out[k] = stft.spectra[0][k] * g[k];
                }
                stft.synthesize();
            }
            y[n] = stft.pop();
        }
        y
    }

    #[test]
    fn disabled_is_exact_delay() {
        let mut d = LateReverb::new(SR);
        d.set_params(false, 1.0, 0.5);
        let x = noise(20000, 1);
        let y = run(&mut d, &x);
        for n in LATENCY..x.len() {
            assert!((y[n] - x[n - LATENCY]).abs() < 1e-4, "n={n}");
        }
    }

    #[test]
    fn enabled_reduces_reverb_tail_and_keeps_direct_sound() {
        let t60 = 0.6;
        let len = (1.5 * SR) as usize;
        let burst = (0.3 * SR) as usize;
        let dry: Vec<f32> = noise(len, 7).iter().enumerate().map(|(n, v)| if n < burst { *v } else { 0.0 }).collect();
        let ir_len = (0.8 * SR) as usize;
        let tail = noise(ir_len, 99);
        let mut ir: Vec<f32> = (0..ir_len)
            .map(|n| 0.3 * tail[n] * (-3.0 * std::f32::consts::LN_10 * n as f32 / (t60 * SR)).exp())
            .collect();
        ir[0] = 1.0;
        let wet = convolve(&dry, &ir);

        let mut d = LateReverb::new(SR);
        d.set_params(true, 1.0, t60);
        let y = run(&mut d, &wet);
        let aligned = |a: usize, b: usize| energy(&y[a + LATENCY..b + LATENCY]);

        let (t0, t1) = (burst + (0.1 * SR) as usize, burst + (0.6 * SR) as usize);
        let tail_db = 10.0 * (energy(&wet[t0..t1]) / aligned(t0, t1)).log10();
        assert!(tail_db >= 8.5, "Nachhall nur um {tail_db} dB reduziert");

        let (b0, b1) = ((0.1 * SR) as usize, burst);
        let direct_db = 10.0 * (energy(&wet[b0..b1]) / aligned(b0, b1)).log10();
        // Einsatz nach 25 ms dämpft auch Gleichbleibendes (hier 3,1 dB, mit 50 ms 1,6 dB; echte Sprache
        // gemessen 1 dB mehr als mit 50 ms). Im Hörvergleich klang der Nachhall so weniger abgehackt.
        assert!(direct_db <= 3.5, "Direktschall um {direct_db} dB gedämpft");
    }

    #[test]
    fn non_finite_spectrum_does_not_latch() {
        let y: Vec<C32> = (0..BINS).map(|k| C32::new(0.1 + (k % 7) as f32 * 0.01, 0.0)).collect();
        let mut bad = y.clone();
        bad[10] = C32::new(f32::INFINITY, 0.0);
        bad[20] = C32::new(f32::NAN, 0.0);
        bad[30] = C32::new(1e30, 0.0);
        let (mut d, mut fresh) = (LateReverb::new(SR), LateReverb::new(SR));
        d.set_params(true, 0.6, 0.5);
        fresh.set_params(true, 0.6, 0.5);
        d.gains(&bad);
        for _ in 0..100 {
            d.gains(&y);
            fresh.gains(&y);
        }
        for k in 0..BINS {
            assert!((d.gain[k] - fresh.gain[k]).abs() < 1e-4, "k={k}: {} statt {}", d.gain[k], fresh.gain[k]);
        }
    }
}
