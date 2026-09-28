//! Delay-and-Sum-Beamformer für das UMA-8 (7 Mikrofone in einer Ebene, Fernfeld).

pub const CHANNELS: usize = 7;
pub const TAPS: usize = 32;
const HALF: usize = TAPS / 2;
/// Feste Grundverzögerung in Samples: halbe FIR-Länge plus maximale Laufzeit
/// über das Array (60 mm bei 48 kHz ≈ 8,4 Samples). Das ist die Latenz.
pub const BASE_DELAY: usize = 25;
const HIST: usize = 64;
pub const FADE_SAMPLES: usize = 2400;
const SPEED_OF_SOUND: f32 = 343.0;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Geometry {
    pub center: usize,
    pub ring: [usize; 6],
    pub ring_offset_deg: f32,
    pub radius_m: f32,
}

impl Geometry {
    /// Zuordnung laut ODAS `minidsp.cfg`: Kanal 0 Mitte, 1–6 im Uhrzeigersinn ab 90°.
    pub const UMA8: Geometry = Geometry { center: 0, ring: [1, 6, 5, 4, 3, 2], ring_offset_deg: 90.0, radius_m: 0.043 };

    /// Gültig, wenn jeder der 7 Kanäle genau einmal vorkommt.
    pub fn is_valid(&self) -> bool {
        let mut seen = [false; CHANNELS];
        for &c in std::iter::once(&self.center).chain(self.ring.iter()) {
            if c >= CHANNELS || seen[c] {
                return false;
            }
            seen[c] = true;
        }
        self.radius_m > 0.0
    }

    /// Position (x, y) in Metern je Kanal.
    pub fn positions(&self) -> [[f32; 2]; CHANNELS] {
        let mut p = [[0.0; 2]; CHANNELS];
        for (k, &ch) in self.ring.iter().enumerate() {
            let a = (self.ring_offset_deg + 60.0 * k as f32).to_radians();
            p[ch] = [self.radius_m * a.cos(), self.radius_m * a.sin()];
        }
        p
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Steering {
    pub azimuth_deg: f32,
    pub elevation_deg: f32,
    pub omni: bool,
}

#[derive(Clone, Copy)]
struct Taps {
    offset: [usize; CHANNELS],
    h: [[f32; TAPS]; CHANNELS],
}

impl Taps {
    fn compute(geom: &Geometry, steer: &Steering, sample_rate: f32) -> Taps {
        let mut t = Taps { offset: [0; CHANNELS], h: [[0.0; TAPS]; CHANNELS] };
        let pos = geom.positions();
        let (az, el) = (steer.azimuth_deg.to_radians(), steer.elevation_deg.to_radians());
        let u = [el.cos() * az.cos(), el.cos() * az.sin()];
        for ch in 0..CHANNELS {
            let weight = if steer.omni {
                if ch == geom.center { 1.0 } else { 0.0 }
            } else {
                1.0 / CHANNELS as f32
            };
            // Kanal ch hört die Welle um tau Samples früher; um genau so viel mehr verzögern.
            let tau = if steer.omni {
                0.0
            } else {
                (pos[ch][0] * u[0] + pos[ch][1] * u[1]) / SPEED_OF_SOUND * sample_rate
            };
            let d = (BASE_DELAY as f32 + tau).clamp((HALF - 1) as f32, (HIST - HALF) as f32);
            let (offset, h) = fractional_delay(d);
            t.offset[ch] = offset;
            for k in 0..TAPS {
                t.h[ch][k] = h[k] * weight;
            }
        }
        t
    }
}

/// Gefensterte Sinc-Interpolation: y[n] = Σ h[k]·x[n − offset − k] ≈ x[n − d].
pub fn fractional_delay(d: f32) -> (usize, [f32; TAPS]) {
    use std::f32::consts::PI;
    let offset = d.floor() as usize - (HALF - 1);
    let mut h = [0.0f32; TAPS];
    let mut sum = 0.0;
    for (k, v) in h.iter_mut().enumerate() {
        let t = (offset + k) as f32 - d;
        let sinc = if t.abs() < 1e-6 { 1.0 } else { (PI * t).sin() / (PI * t) };
        let window = 0.5 * (1.0 + (PI * t / HALF as f32).cos());
        *v = sinc * window;
        sum += *v;
    }
    for v in h.iter_mut() {
        *v /= sum;
    }
    (offset, h)
}

pub struct Beamformer {
    sample_rate: f32,
    hist: [[f32; HIST]; CHANNELS],
    pos: usize,
    current: (Geometry, Steering),
    target: (Geometry, Steering),
    fading_to: (Geometry, Steering),
    taps: Taps,
    next_taps: Taps,
    fade: Option<usize>,
}

impl Beamformer {
    pub fn new(sample_rate: f32, geom: Geometry, steer: Steering) -> Self {
        let taps = Taps::compute(&geom, &steer, sample_rate);
        Beamformer {
            sample_rate,
            hist: [[0.0; HIST]; CHANNELS],
            pos: 0,
            current: (geom, steer),
            target: (geom, steer),
            fading_to: (geom, steer),
            taps,
            next_taps: taps,
            fade: None,
        }
    }

    /// Latenz des Beam-Ausgangs in Samples.
    pub fn latency(&self) -> usize {
        BASE_DELAY
    }

    /// Neue Geometrie/Richtung. Ungültige Geometrien werden ignoriert.
    /// Der Wechsel wird über `FADE_SAMPLES` weich übergeblendet.
    pub fn set_target(&mut self, geom: Geometry, steer: Steering) {
        if geom.is_valid() {
            self.target = (geom, steer);
        }
    }

    fn apply(&self, taps: &Taps) -> f32 {
        let mut acc = 0.0;
        for ch in 0..CHANNELS {
            let base = self.pos + HIST - taps.offset[ch];
            let h = &taps.h[ch];
            let x = &self.hist[ch];
            for k in 0..TAPS {
                acc += h[k] * x[(base - k) & (HIST - 1)];
            }
        }
        acc
    }

    /// `input[ch]` sind gleich lang wie `beam` und `raw`.
    pub fn process(&mut self, input: &[&[f32]; CHANNELS], beam: &mut [f32], raw: &mut [f32]) {
        use std::f32::consts::PI;
        for n in 0..beam.len() {
            if self.fade.is_none() && self.target != self.current {
                self.fading_to = self.target;
                self.next_taps = Taps::compute(&self.fading_to.0, &self.fading_to.1, self.sample_rate);
                self.fade = Some(0);
            }
            self.pos = (self.pos + 1) & (HIST - 1);
            for ch in 0..CHANNELS {
                self.hist[ch][self.pos] = input[ch][n];
            }
            let mut y = self.apply(&self.taps);
            if let Some(i) = self.fade {
                let g = 0.5 - 0.5 * (PI * i as f32 / FADE_SAMPLES as f32).cos();
                y = y * (1.0 - g) + self.apply(&self.next_taps) * g;
                if i + 1 >= FADE_SAMPLES {
                    self.taps = self.next_taps;
                    self.current = self.fading_to;
                    self.fade = None;
                } else {
                    self.fade = Some(i + 1);
                }
            }
            beam[n] = y;
            raw[n] = self.hist[self.current.0.center][(self.pos + HIST - BASE_DELAY) & (HIST - 1)];
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f32::consts::PI;
    const SR: f32 = 48000.0;

    /// Ebene Welle eines Sinus aus Richtung (az, el), analytisch je Kanal.
    fn plane_wave_sine(geom: &Geometry, az: f32, el: f32, freq: f32, len: usize) -> Vec<Vec<f32>> {
        let pos = geom.positions();
        let (a, e) = (az.to_radians(), el.to_radians());
        let u = [e.cos() * a.cos(), e.cos() * a.sin()];
        (0..CHANNELS)
            .map(|ch| {
                let lead = (pos[ch][0] * u[0] + pos[ch][1] * u[1]) / SPEED_OF_SOUND;
                (0..len).map(|n| (2.0 * PI * freq * (n as f32 / SR + lead)).sin()).collect()
            })
            .collect()
    }

    fn run(bf: &mut Beamformer, ch: &[Vec<f32>], from: usize, to: usize, beam: &mut [f32], raw: &mut [f32]) {
        let refs: [&[f32]; CHANNELS] = std::array::from_fn(|i| &ch[i][from..to]);
        bf.process(&refs, &mut beam[from..to], &mut raw[from..to]);
    }

    fn rms(x: &[f32]) -> f32 {
        (x.iter().map(|v| v * v).sum::<f32>() / x.len() as f32).sqrt()
    }

    fn steer(az: f32) -> Steering {
        Steering { azimuth_deg: az, elevation_deg: 0.0, omni: false }
    }

    #[test]
    fn uma8_geometry_matches_odas() {
        let p = Geometry::UMA8.positions();
        assert!(p[0][0].abs() < 1e-6 && p[0][1].abs() < 1e-6);
        assert!((p[1][0] - 0.0).abs() < 1e-3 && (p[1][1] - 0.043).abs() < 1e-3);
        assert!((p[2][0] - 0.037).abs() < 1e-3 && (p[2][1] - 0.021).abs() < 1e-3);
        assert!((p[4][0] - 0.0).abs() < 1e-3 && (p[4][1] + 0.043).abs() < 1e-3);
    }

    #[test]
    fn fractional_delay_matches_shifted_sine() {
        let (offset, h) = fractional_delay(25.3);
        let f = 1000.0;
        let x: Vec<f32> = (0..400).map(|n| (2.0 * PI * f * n as f32 / SR).sin()).collect();
        for n in 100..400 {
            let y: f32 = (0..TAPS).map(|k| h[k] * x[n - offset - k]).sum();
            let expect = (2.0 * PI * f * (n as f32 - 25.3) / SR).sin();
            assert!((y - expect).abs() < 2e-3, "n={n}: {y} vs {expect}");
        }
    }

    #[test]
    fn steered_beam_passes_target_and_rejects_opposite() {
        let g = Geometry::UMA8;
        for &f in &[2000.0f32, 3000.0, 4000.0, 6000.0] {
            let x = plane_wave_sine(&g, 90.0, 0.0, f, 9600);
            let (mut b_on, mut b_off, mut raw) = (vec![0.0; 9600], vec![0.0; 9600], vec![0.0; 9600]);
            run(&mut Beamformer::new(SR, g, steer(90.0)), &x, 0, 9600, &mut b_on, &mut raw);
            run(&mut Beamformer::new(SR, g, steer(270.0)), &x, 0, 9600, &mut b_off, &mut raw);
            let (r_on, r_off) = (rms(&b_on[200..]), rms(&b_off[200..]));
            assert!((r_on - 0.7071).abs() < 0.03, "f={f}: on-axis rms {r_on}");
            let db = 20.0 * (r_on / r_off).log10();
            assert!(db >= 6.0, "f={f}: nur {db} dB");
        }
    }

    #[test]
    fn impulse_from_zenith_has_base_latency() {
        let mut x = vec![vec![0.0f32; 200]; CHANNELS];
        for ch in x.iter_mut() {
            ch[50] = 1.0;
        }
        let cases = [
            Steering { azimuth_deg: 0.0, elevation_deg: 90.0, omni: false },
            Steering { azimuth_deg: 123.0, elevation_deg: 0.0, omni: true },
        ];
        for s in cases {
            let (mut beam, mut raw) = (vec![0.0; 200], vec![0.0; 200]);
            run(&mut Beamformer::new(SR, Geometry::UMA8, s), &x, 0, 200, &mut beam, &mut raw);
            let peak = (0..200).max_by(|&a, &b| beam[a].abs().total_cmp(&beam[b].abs())).unwrap();
            assert_eq!(peak, 50 + BASE_DELAY, "{s:?}");
            assert!((beam[50 + BASE_DELAY] - 1.0).abs() < 1e-3);
            assert_eq!(raw[50 + BASE_DELAY], 1.0);
        }
    }

    #[test]
    fn steering_change_is_click_free() {
        let g = Geometry::UMA8;
        let len = 24000;
        let x = plane_wave_sine(&g, 0.0, 0.0, 1000.0, len);
        let mut bf = Beamformer::new(SR, g, steer(0.0));
        let (mut y, mut raw) = (vec![0.0; len], vec![0.0; len]);
        run(&mut bf, &x, 0, len / 2, &mut y, &mut raw);
        bf.set_target(g, steer(180.0));
        run(&mut bf, &x, len / 2, len, &mut y, &mut raw);
        let max_step = 2.0 * PI * 1000.0 / SR * 1.07;
        // erst nach dem Einschwingen prüfen (Signal setzt bei n = 0 abrupt ein)
        for n in (2 * BASE_DELAY + TAPS)..len {
            assert!((y[n] - y[n - 1]).abs() <= max_step, "Sprung bei n={n}");
        }
        assert_eq!(bf.current.1, steer(180.0), "Überblendung nicht abgeschlossen");
    }

    #[test]
    fn invalid_geometry_is_ignored() {
        let mut bf = Beamformer::new(SR, Geometry::UMA8, steer(0.0));
        let mut bad = Geometry::UMA8;
        bad.ring[0] = 0;
        bf.set_target(bad, steer(90.0));
        let x = vec![vec![0.0f32; 4000]; CHANNELS];
        let (mut y, mut raw) = (vec![0.0; 4000], vec![0.0; 4000]);
        run(&mut bf, &x, 0, 4000, &mut y, &mut raw);
        assert_eq!(bf.current.0, Geometry::UMA8);
        assert_eq!(bf.current.1, steer(0.0));
    }
}
