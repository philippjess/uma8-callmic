//! Signalweg von „Beam Out“: 7-Kanal-STFT → Beamformer → Hallunterdrückung (Kohärenz-Postfilter,
//! später Nachhall) → iSTFT. Feste Latenz `stft::LATENCY` in allen Modi und Schalterstellungen.

use crate::beam::{Beamformer, Geometry, Steering, CHANNELS};
use crate::cdr::Postfilter;
use crate::dereverb::LateReverb;
use crate::stft::{Stft, BINS};

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Params {
    pub geometry: Geometry,
    pub steering: Steering,
    /// Kohärenz-Postfilter an/aus.
    pub dereverb: bool,
    pub strength: f32,
    /// Später Nachhall (Lebart/Habets) an/aus.
    pub late_reverb: bool,
    pub t60: f32,
}

pub struct Pipeline {
    stft: Stft,
    beam: Beamformer,
    post: Postfilter,
    late: LateReverb,
    di: Vec<f32>,
}

impl Pipeline {
    pub fn new(sample_rate: f32, p: &Params) -> Self {
        let mut pl = Pipeline {
            stft: Stft::new(CHANNELS),
            beam: Beamformer::new(sample_rate, p.geometry, p.steering),
            post: Postfilter::new(sample_rate, &p.geometry),
            late: LateReverb::new(sample_rate),
            di: vec![1.0; BINS],
        };
        pl.set_params(p);
        pl
    }

    /// Ungültige Geometrien werden ignoriert; alle Wechsel sind klickfrei.
    pub fn set_params(&mut self, p: &Params) {
        self.beam.set_target(p.geometry, p.steering);
        if p.geometry.is_valid() {
            self.post.set_geometry(&p.geometry);
        }
        self.post.set_params(p.dereverb, p.strength);
        self.late.set_params(p.late_reverb, p.strength, p.t60);
    }

    /// Mittel-Mikrofon der wirksamen Geometrie (für den Roh-Weg).
    pub fn center(&self) -> usize {
        self.beam.geometry().center
    }

    /// `input[ch]` sind gleich lang wie `out`.
    pub fn process(&mut self, input: &[&[f32]; CHANNELS], out: &mut [f32]) {
        for (n, y) in out.iter_mut().enumerate() {
            if self.stft.push(|c| input[c][n]) {
                self.frame();
            }
            *y = self.stft.pop();
        }
    }

    fn frame(&mut self) {
        self.stft.analyze();
        self.beam.process(&self.stft.spectra, &mut self.stft.out, &mut self.di);
        let g_post = self.post.gains(&self.stft.spectra, &self.di);
        let g_late = self.late.gains(&self.stft.out);
        // Die Stufen ergänzen sich (Kohärenz erkennt diffusen Schall, das Abklingmodell späten
        // Nachhall in Pausen); multipliziert dämpft im Raum-Test den Schwanz um 6 dB mehr als das
        // Minimum, bei gleichem SI-SDR.
        for k in 0..BINS {
            self.stft.out[k] *= g_post[k] * g_late[k];
        }
        if !self.stft.synthesize() {
            // Überlauf: Schätzzustände verwerfen statt NaN weiterzutragen.
            self.post.reset();
            self.late.reset();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::beam::{Mode, SPEED_OF_SOUND};
    use crate::stft::LATENCY;
    use std::f32::consts::PI;
    const SR: f32 = 48000.0;

    fn params(mode: Mode, az: f32, el: f32) -> Params {
        Params {
            geometry: Geometry::UMA8,
            steering: Steering { azimuth_deg: az, elevation_deg: el, mode, min_wng_db: -3.0 },
            dereverb: false,
            strength: 0.6,
            late_reverb: false,
            t60: 0.5,
        }
    }

    /// Ebene Welle eines Sinus aus Richtung (az, el), analytisch je Kanal: Kanal m hört sie um
    /// p_m·u/c früher (wie `tests/sim.plane_wave`).
    fn plane_wave_sine(az: f32, el: f32, freq: f32, len: usize) -> Vec<Vec<f32>> {
        let pos = Geometry::UMA8.positions();
        let (a, e) = (az.to_radians(), el.to_radians());
        let u = [e.cos() * a.cos(), e.cos() * a.sin()];
        (0..CHANNELS)
            .map(|ch| {
                let lead = (pos[ch][0] as f32 * u[0] + pos[ch][1] as f32 * u[1]) / SPEED_OF_SOUND as f32;
                (0..len).map(|n| (2.0 * PI * freq * (n as f32 / SR + lead)).sin()).collect()
            })
            .collect()
    }

    fn run(pl: &mut Pipeline, x: &[Vec<f32>], from: usize, to: usize, y: &mut [f32]) {
        let refs: [&[f32]; CHANNELS] = std::array::from_fn(|i| &x[i][from..to]);
        pl.process(&refs, &mut y[from..to]);
    }

    fn rms(x: &[f32]) -> f32 {
        (x.iter().map(|v| v * v).sum::<f32>() / x.len() as f32).sqrt()
    }

    #[test]
    fn steered_beam_passes_target_and_rejects_opposite() {
        // prüft zugleich das Vorzeichen des Steuervektors gegenüber der FFT-Konvention
        for mode in [Mode::Superdirective, Mode::DelayAndSum] {
            for &f in &[1000.0f32, 2000.0, 4000.0, 6000.0] {
                let len = 9600;
                let x = plane_wave_sine(90.0, 20.0, f, len);
                let (mut on, mut off) = (vec![0.0; len], vec![0.0; len]);
                run(&mut Pipeline::new(SR, &params(mode, 90.0, 20.0)), &x, 0, len, &mut on);
                run(&mut Pipeline::new(SR, &params(mode, 270.0, 20.0)), &x, 0, len, &mut off);
                let (r_on, r_off) = (rms(&on[2 * LATENCY..]), rms(&off[2 * LATENCY..]));
                assert!((r_on - 0.7071).abs() < 0.01, "{mode:?} {f} Hz: on-axis rms {r_on}");
                let db = 20.0 * (r_on / r_off).log10();
                let need = if mode == Mode::DelayAndSum && f < 2000.0 { 1.0 } else { 6.0 };
                assert!(db >= need, "{mode:?} {f} Hz: nur {db} dB");
                // phasengleich mit dem Mittel-Mikrofon, um genau LATENCY verzögert
                let err = (2 * LATENCY..len).map(|n| (on[n] - x[0][n - LATENCY]).abs()).fold(0.0, f32::max);
                assert!(err < 0.02, "{mode:?} {f} Hz: Abweichung {err}");
            }
        }
    }

    #[test]
    fn impulse_from_zenith_has_constant_latency() {
        let mut x = vec![vec![0.0f32; 4000]; CHANNELS];
        for ch in x.iter_mut() {
            ch[500] = 1.0;
        }
        for mode in [Mode::Superdirective, Mode::Omni, Mode::DelayAndSum] {
            for (dereverb, late) in [(false, false), (true, false), (true, true)] {
                let mut p = params(mode, 123.0, 90.0);
                (p.dereverb, p.late_reverb) = (dereverb, late);
                let mut y = vec![0.0; 4000];
                run(&mut Pipeline::new(SR, &p), &x, 0, 4000, &mut y);
                let peak = (0..4000).max_by(|&a, &b| y[a].abs().total_cmp(&y[b].abs())).unwrap();
                assert_eq!(peak, 500 + LATENCY, "{mode:?} {dereverb} {late}");
                if !dereverb {
                    assert!((y[500 + LATENCY] - 1.0).abs() < 1e-3, "{mode:?}: {}", y[500 + LATENCY]);
                }
            }
        }
    }

    #[test]
    fn steering_change_is_click_free() {
        for mode in [Mode::Superdirective, Mode::DelayAndSum] {
            let len = 24000;
            let x = plane_wave_sine(0.0, 0.0, 1000.0, len);
            let mut pl = Pipeline::new(SR, &params(mode, 0.0, 0.0));
            let mut y = vec![0.0; len];
            run(&mut pl, &x, 0, len / 2, &mut y);
            pl.set_params(&params(mode, 180.0, 0.0));
            run(&mut pl, &x, len / 2, len / 2 + 300, &mut y);
            pl.set_params(&params(Mode::Omni, 180.0, 0.0));
            run(&mut pl, &x, len / 2 + 300, len, &mut y);
            let max_step = 2.0 * PI * 1000.0 / SR * 1.07;
            for n in (LATENCY + 100)..len {
                assert!((y[n] - y[n - 1]).abs() <= max_step, "{mode:?}: Sprung bei n={n}");
            }
            assert_eq!(pl.beam.steering().mode, Mode::Omni, "Überblendung nicht abgeschlossen");
        }
    }

    #[test]
    fn omni_without_dereverb_is_exact_center_delay() {
        let len = 12000;
        let x: Vec<Vec<f32>> =
            (0..CHANNELS).map(|c| (0..len).map(|n| ((n * (c + 3) * 7919) % 997) as f32 / 500.0 - 1.0).collect()).collect();
        let mut y = vec![0.0; len];
        run(&mut Pipeline::new(SR, &params(Mode::Omni, 0.0, 0.0)), &x, 0, len, &mut y);
        for n in LATENCY..len {
            assert!((y[n] - x[0][n - LATENCY]).abs() < 1e-5, "n={n}");
        }
    }

    #[test]
    fn non_finite_input_is_contained() {
        let len = 8000;
        let mut x = vec![vec![0.01f32; len]; CHANNELS];
        x[3][100] = f32::NAN;
        x[4][200] = f32::INFINITY;
        x[5][300] = 3e38;
        let mut p = params(Mode::Superdirective, 0.0, 0.0);
        (p.dereverb, p.late_reverb) = (true, true);
        let mut y = vec![0.0; len];
        run(&mut Pipeline::new(SR, &p), &x, 0, len, &mut y);
        assert!(y.iter().all(|v| v.is_finite()));
    }
}
