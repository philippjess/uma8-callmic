//! Unterdrückung des späten Nachhalls (Lebart/Habets) in der STFT-Domäne.
//! Deaktiviert ist die Verstärkung 1: exakte Rekonstruktion mit fester Latenz.

use realfft::{num_complex::Complex, ComplexToReal, RealFftPlanner, RealToComplex};
use std::sync::Arc;

pub const FFT_LEN: usize = 1024;
pub const HOP: usize = 256;
/// Latenz in Samples, unabhängig davon, ob die Unterdrückung aktiv ist.
pub const LATENCY: usize = FFT_LEN;
const BINS: usize = FFT_LEN / 2 + 1;
const LATE_ONSET_S: f32 = 0.05;
const MAX_ONSET_FRAMES: usize = 32;
const PSD_SMOOTH: f32 = 0.8;
const GAIN_SMOOTH: f32 = 0.5;
const MAX_ATTENUATION_DB: f32 = 15.0;

pub struct Dereverb {
    sample_rate: f32,
    r2c: Arc<dyn RealToComplex<f32>>,
    c2r: Arc<dyn ComplexToReal<f32>>,
    window: Vec<f32>,
    input: Vec<f32>,
    in_pos: usize,
    hop_count: usize,
    output: Vec<f32>,
    time: usize,
    frame: Vec<f32>,
    spectrum: Vec<Complex<f32>>,
    scratch_fwd: Vec<Complex<f32>>,
    scratch_inv: Vec<Complex<f32>>,
    psd: Vec<f32>,
    psd_hist: Vec<Vec<f32>>,
    hist_pos: usize,
    onset_frames: usize,
    gain: Vec<f32>,
    enabled: bool,
    strength: f32,
    t60: f32,
}

impl Dereverb {
    pub fn new(sample_rate: f32) -> Self {
        let mut planner = RealFftPlanner::<f32>::new();
        let r2c = planner.plan_fft_forward(FFT_LEN);
        let c2r = planner.plan_fft_inverse(FFT_LEN);
        // Wurzel-Hann (periodisch): Analyse und Synthese zusammen ergeben Hann,
        // bei 75 % Überlappung summiert sich Hann zu 2.
        let window = (0..FFT_LEN).map(|j| (std::f32::consts::PI * j as f32 / FFT_LEN as f32).sin()).collect();
        let onset_frames = ((LATE_ONSET_S * sample_rate / HOP as f32).round() as usize).clamp(1, MAX_ONSET_FRAMES);
        Dereverb {
            sample_rate,
            scratch_fwd: r2c.make_scratch_vec(),
            scratch_inv: c2r.make_scratch_vec(),
            spectrum: r2c.make_output_vec(),
            r2c,
            c2r,
            window,
            input: vec![0.0; FFT_LEN],
            in_pos: 0,
            hop_count: 0,
            output: vec![0.0; 2 * FFT_LEN],
            time: FFT_LEN,
            frame: vec![0.0; FFT_LEN],
            psd: vec![0.0; BINS],
            psd_hist: vec![vec![0.0; BINS]; MAX_ONSET_FRAMES + 1],
            hist_pos: 0,
            onset_frames,
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

    pub fn process(&mut self, input: &[f32], output: &mut [f32]) {
        let ring = self.output.len();
        for (x, y) in input.iter().zip(output.iter_mut()) {
            self.input[self.in_pos] = *x;
            self.in_pos = (self.in_pos + 1) % FFT_LEN;
            self.hop_count += 1;
            if self.hop_count == HOP {
                self.hop_count = 0;
                self.process_frame();
            }
            // Alle Frames, die Zeitpunkt time − FFT_LEN abdecken, sind fertig.
            let idx = (self.time - FFT_LEN) % ring;
            *y = self.output[idx];
            self.output[idx] = 0.0;
            self.time += 1;
        }
    }

    fn process_frame(&mut self) {
        for j in 0..FFT_LEN {
            self.frame[j] = self.input[(self.in_pos + j) % FFT_LEN] * self.window[j];
        }
        let _ = self.r2c.process_with_scratch(&mut self.frame, &mut self.spectrum, &mut self.scratch_fwd);

        let delta = 3.0 * std::f32::consts::LN_10 / self.t60;
        let decay = (-2.0 * delta * (self.onset_frames * HOP) as f32 / self.sample_rate).exp();
        let slots = MAX_ONSET_FRAMES + 1;
        // hist_pos = letzter gespeicherter Frame (1 Frame alt) → D Frames alt = hist_pos + 1 − D
        let old = (self.hist_pos + 1 + slots - self.onset_frames) % slots;
        let floor = 10f32.powf(-self.strength * MAX_ATTENUATION_DB / 20.0);
        for k in 0..BINS {
            let power = self.spectrum[k].norm_sqr();
            self.psd[k] = PSD_SMOOTH * self.psd[k] + (1.0 - PSD_SMOOTH) * power;
            let late = decay * self.psd_hist[old][k];
            let target = if self.enabled {
                (1.0 - late / (self.psd[k] + 1e-12)).max(0.0).sqrt().max(floor)
            } else {
                1.0
            };
            self.gain[k] = GAIN_SMOOTH * self.gain[k] + (1.0 - GAIN_SMOOTH) * target;
            self.spectrum[k] *= self.gain[k];
        }
        self.hist_pos = (self.hist_pos + 1) % slots;
        self.psd_hist[self.hist_pos].copy_from_slice(&self.psd);

        self.spectrum[0].im = 0.0;
        self.spectrum[BINS - 1].im = 0.0;
        let _ = self.c2r.process_with_scratch(&mut self.spectrum, &mut self.frame, &mut self.scratch_inv);
        let ring = self.output.len();
        let scale = 0.5 / FFT_LEN as f32;
        let start = self.time + 1 - FFT_LEN;
        for j in 0..FFT_LEN {
            self.output[(start + j) % ring] += self.frame[j] * self.window[j] * scale;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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

    #[test]
    fn disabled_is_exact_delay() {
        let mut d = Dereverb::new(SR);
        d.set_params(false, 1.0, 0.5);
        let x = noise(20000, 1);
        let mut y = vec![0.0; x.len()];
        d.process(&x, &mut y);
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

        let mut d = Dereverb::new(SR);
        d.set_params(true, 1.0, t60);
        let mut y = vec![0.0; len];
        d.process(&wet, &mut y);
        let aligned = |a: usize, b: usize| energy(&y[a + LATENCY..b + LATENCY]);

        let (t0, t1) = (burst + (0.1 * SR) as usize, burst + (0.6 * SR) as usize);
        let tail_db = 10.0 * (energy(&wet[t0..t1]) / aligned(t0, t1)).log10();
        assert!(tail_db >= 6.0, "Nachhall nur um {tail_db} dB reduziert");

        let (b0, b1) = ((0.1 * SR) as usize, burst);
        let direct_db = 10.0 * (energy(&wet[b0..b1]) / aligned(b0, b1)).log10();
        assert!(direct_db <= 3.0, "Direktschall um {direct_db} dB gedämpft");
    }
}
