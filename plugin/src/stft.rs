//! Gemeinsame STFT-Analyse/-Synthese für alle Stufen: FFT 1024, Hop 256, periodisches
//! Wurzel-Hann als Analyse- und Synthesefenster. Zusammen ergeben sie Hann, das sich bei
//! 75 % Überlappung zu 2 summiert. Die Latenz ist genau `FFT_LEN` Samples.

use realfft::{num_complex::Complex, ComplexToReal, RealFftPlanner, RealToComplex};
use std::sync::Arc;

pub const FFT_LEN: usize = 1024;
pub const HOP: usize = 256;
pub const BINS: usize = FFT_LEN / 2 + 1;
/// Latenz in Samples, unabhängig von allen Einstellungen.
pub const LATENCY: usize = FFT_LEN;
/// Eingangswerte werden auf ±MAX_INPUT begrenzt. Audio ist ±1, mit der Vorverstärkung der
/// Echounterdrückung (+18 dB) ±8; der Rest ist Datenmüll. So bleiben alle Leistungen der Stufen
/// (|X|² ≤ (FFT_LEN·MAX_INPUT)² ≈ 10¹², Produkte zweier Kanäle ≈ 10²⁶) weit unter f32::MAX, und
/// kein Schätzer kann auf ∞ hängen bleiben.
pub const MAX_INPUT: f32 = 1e3;

pub type C32 = Complex<f32>;

pub struct Stft {
    r2c: Arc<dyn RealToComplex<f32>>,
    c2r: Arc<dyn ComplexToReal<f32>>,
    window: Vec<f32>,
    input: Vec<Vec<f32>>,
    in_pos: usize,
    hop_count: usize,
    output: Vec<f32>,
    out_pos: usize,
    frame: Vec<f32>,
    scratch_fwd: Vec<C32>,
    scratch_inv: Vec<C32>,
    /// Spektren des aktuellen Frames je Kanal (nach `analyze`).
    pub spectra: Vec<Vec<C32>>,
    /// Ausgangsspektrum des aktuellen Frames (Eingabe für `synthesize`).
    pub out: Vec<C32>,
}

impl Stft {
    pub fn new(channels: usize) -> Self {
        let mut planner = RealFftPlanner::<f32>::new();
        let r2c = planner.plan_fft_forward(FFT_LEN);
        let c2r = planner.plan_fft_inverse(FFT_LEN);
        let window = (0..FFT_LEN).map(|j| (std::f32::consts::PI * j as f32 / FFT_LEN as f32).sin()).collect();
        Stft {
            scratch_fwd: r2c.make_scratch_vec(),
            scratch_inv: c2r.make_scratch_vec(),
            spectra: vec![r2c.make_output_vec(); channels],
            out: r2c.make_output_vec(),
            r2c,
            c2r,
            window,
            input: vec![vec![0.0; FFT_LEN]; channels],
            in_pos: 0,
            hop_count: 0,
            output: vec![0.0; 2 * FFT_LEN],
            out_pos: 0,
            frame: vec![0.0; FFT_LEN],
        }
    }

    /// Nimmt ein Sample je Kanal auf (nicht endliche Werte werden 0, der Rest auf ±`MAX_INPUT`
    /// begrenzt); true, wenn ein Frame fällig ist.
    pub fn push(&mut self, sample: impl Fn(usize) -> f32) -> bool {
        for (c, ring) in self.input.iter_mut().enumerate() {
            let x = sample(c);
            ring[self.in_pos] = if x.is_finite() { x.clamp(-MAX_INPUT, MAX_INPUT) } else { 0.0 };
        }
        self.in_pos = (self.in_pos + 1) % FFT_LEN;
        self.hop_count += 1;
        if self.hop_count == HOP {
            self.hop_count = 0;
            return true;
        }
        false
    }

    /// Fenstert die letzten `FFT_LEN` Samples jedes Kanals und transformiert sie nach `spectra`.
    pub fn analyze(&mut self) {
        let split = FFT_LEN - self.in_pos;
        for (ring, spec) in self.input.iter().zip(self.spectra.iter_mut()) {
            self.frame[..split].copy_from_slice(&ring[self.in_pos..]);
            self.frame[split..].copy_from_slice(&ring[..self.in_pos]);
            for (v, w) in self.frame.iter_mut().zip(&self.window) {
                *v *= w;
            }
            let _ = self.r2c.process_with_scratch(&mut self.frame, spec, &mut self.scratch_fwd);
        }
    }

    /// Rücktransformation von `out`, Synthesefenster und Overlap-Add. Ein nicht endlicher Frame
    /// (nur bei absurd großen Eingangswerten) wird verworfen; dann `false`.
    pub fn synthesize(&mut self) -> bool {
        self.out[0].im = 0.0;
        self.out[BINS - 1].im = 0.0;
        let _ = self.c2r.process_with_scratch(&mut self.out, &mut self.frame, &mut self.scratch_inv);
        let finite = self.frame.iter().all(|v| v.is_finite());
        if !finite {
            self.frame.fill(0.0);
        }
        // Frame deckt die Eingangssamples n − FFT_LEN + 1 … n ab und landet bei n + 1 … n + FFT_LEN.
        let ring = self.output.len();
        let scale = 0.5 / FFT_LEN as f32;
        for j in 0..FFT_LEN {
            self.output[(self.out_pos + 1 + j) % ring] += self.frame[j] * self.window[j] * scale;
        }
        finite
    }

    /// Ausgangssample zum zuletzt aufgenommenen Eingang (um `LATENCY` verzögert).
    pub fn pop(&mut self) -> f32 {
        let y = self.output[self.out_pos];
        self.output[self.out_pos] = 0.0;
        self.out_pos = (self.out_pos + 1) % self.output.len();
        y
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passthrough_is_exact_delay() {
        let mut s = Stft::new(2);
        let x: Vec<f32> = (0..9000).map(|n| ((n * 7919) % 1000) as f32 / 500.0 - 1.0).collect();
        let mut y = vec![0.0; x.len()];
        for n in 0..x.len() {
            if s.push(|c| if c == 1 { x[n] } else { 0.0 }) {
                s.analyze();
                s.out.copy_from_slice(&s.spectra[1]);
                s.synthesize();
            }
            y[n] = s.pop();
        }
        for n in LATENCY..x.len() {
            assert!((y[n] - x[n - LATENCY]).abs() < 1e-5, "n={n}");
        }
    }

    #[test]
    fn invalid_input_is_zeroed_or_clamped() {
        let mut s = Stft::new(4);
        let bad = [f32::NAN, f32::INFINITY, 1e20, -3e38];
        for n in 0..FFT_LEN {
            s.push(|c| if n == FFT_LEN / 2 { bad[c] } else { 0.0 });
        }
        s.analyze();
        // Fenstermitte = 1: |X| = Betrag des einzigen Samples
        for (c, expect) in [0.0, 0.0, MAX_INPUT, MAX_INPUT].into_iter().enumerate() {
            for x in &s.spectra[c] {
                assert!((x.norm() - expect).abs() <= 1e-3 * MAX_INPUT, "Kanal {c}: {x}");
            }
        }
    }
}
