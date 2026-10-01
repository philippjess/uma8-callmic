//! Festes Beamforming im STFT-Bereich für das UMA-8 (7 Mikrofone in einer Ebene, Fernfeld).
//!
//! Superdirektiv: MVDR gegen die Kohärenzmatrix Γ des 3D-diffusen Felds mit Diagonalladung μ,
//! je Bin gerade so groß, dass der White-Noise-Gain (WNG) die Untergrenze einhält. Γ(f) ist reell,
//! symmetrisch und richtungsunabhängig; in Platz-Reihenfolge (Mitte, Ring 0–5) hängt es nur vom
//! Radius ab und wird je Radius einmal zerlegt (Γ = V·Λ·Vᵀ). Kanalzuordnung und Ringdrehung sind
//! nur Umnummerierung bzw. Drehung. Für eine Richtung sind dann WNG, dᴴ(Γ+μI)⁻¹d und der
//! Richtwirkungsfaktor O(7)-Summen über die Eigenwerte.

use crate::jacobi;
use crate::stft::{BINS, C32, FFT_LEN};
use realfft::num_complex::Complex;
use std::f64::consts::PI;

pub const CHANNELS: usize = 7;
pub const SPEED_OF_SOUND: f64 = 343.0;
/// Überblendung bei Parameterwechseln: 10 Frames ≈ 53 ms.
pub const FADE_FRAMES: usize = 10;
const MU_MIN: f64 = 1e-6;
const MU_MAX: f64 = 1e4;
const BISECT_STEPS: usize = 30;

type C64 = Complex<f64>;

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
    pub fn positions(&self) -> [[f64; 2]; CHANNELS] {
        let mut p = [[0.0; 2]; CHANNELS];
        for (k, &ch) in self.ring.iter().enumerate() {
            let a = (self.ring_offset_deg as f64 + 60.0 * k as f64).to_radians();
            p[ch] = [self.radius_m as f64 * a.cos(), self.radius_m as f64 * a.sin()];
        }
        p
    }

    pub fn distance(&self, a: usize, b: usize) -> f64 {
        let p = self.positions();
        (p[a][0] - p[b][0]).hypot(p[a][1] - p[b][1])
    }

    /// Platz je Kanal: 0 = Mitte, 1 + k = Ringposition k.
    fn slots(&self) -> [usize; CHANNELS] {
        let mut s = [0; CHANNELS];
        for (k, &ch) in self.ring.iter().enumerate() {
            s[ch] = k + 1;
        }
        s
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Superdirective,
    Omni,
    DelayAndSum,
}

impl Mode {
    /// Control „Mode“: 0 = superdirektiv, 1 = alle Richtungen (Mittel-Mikrofon), 2 = Delay-and-Sum.
    pub fn from_control(v: f32) -> Mode {
        match v.round() as i32 {
            1 => Mode::Omni,
            2 => Mode::DelayAndSum,
            _ => Mode::Superdirective,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Steering {
    pub azimuth_deg: f32,
    pub elevation_deg: f32,
    pub mode: Mode,
    /// Untergrenze des White-Noise-Gains in dB (nur superdirektiv).
    pub min_wng_db: f32,
}

/// Kohärenz des 3D-diffusen Felds zweier Mikrofone, `kd` = Wellenzahl · Abstand: sinc(kd).
pub fn diffuse_coherence(kd: f64) -> f64 {
    if kd.abs() < 1e-9 {
        1.0
    } else {
        kd.sin() / kd
    }
}

/// Wellenzahl 2πf/c von Bin `k`.
pub fn wavenumber(k: usize, sample_rate: f64) -> f64 {
    2.0 * PI * k as f64 * sample_rate / FFT_LEN as f64 / SPEED_OF_SOUND
}

/// Eigenzerlegung von Γ(f) je Bin in Platz-Reihenfolge; hängt nur vom Radius ab.
struct Eigen {
    radius_m: f32,
    lambda: Vec<[f64; CHANNELS]>,
    v: Vec<[[f64; CHANNELS]; CHANNELS]>,
}

impl Eigen {
    fn new() -> Self {
        Eigen { radius_m: f32::NAN, lambda: vec![[0.0; CHANNELS]; BINS], v: vec![[[0.0; CHANNELS]; CHANNELS]; BINS] }
    }

    /// Nur bei geändertem Radius neu (≈ 3 ms für alle Bins).
    fn update(&mut self, radius_m: f32, sample_rate: f64) {
        if self.radius_m == radius_m {
            return;
        }
        self.radius_m = radius_m;
        let slot_geom = Geometry { center: 0, ring: [1, 2, 3, 4, 5, 6], ring_offset_deg: 0.0, radius_m };
        let dist: [[f64; CHANNELS]; CHANNELS] =
            std::array::from_fn(|i| std::array::from_fn(|j| slot_geom.distance(i, j)));
        for k in 0..BINS {
            let wn = wavenumber(k, sample_rate);
            let gamma = dist.map(|row| row.map(|d| diffuse_coherence(wn * d)));
            let (l, v) = jacobi::eigh(gamma);
            self.lambda[k] = l.map(|x| x.max(0.0));
            self.v[k] = v;
        }
    }
}

/// Kleinstes μ ∈ [MU_MIN, MU_MAX] mit WNG(μ) ≥ `wng_min`; WNG wächst monoton mit μ.
/// `pw[i]` = |(Vᵀd)_i|².
fn regularization(lambda: &[f64; CHANNELS], pw: &[f64; CHANNELS], wng_min: f64) -> f64 {
    let wng = |mu: f64| {
        let (mut a, mut b) = (0.0, 0.0);
        for i in 0..CHANNELS {
            let r = pw[i] / (lambda[i] + mu);
            a += r;
            b += r / (lambda[i] + mu);
        }
        a * a / b
    };
    if wng(MU_MIN) >= wng_min {
        return MU_MIN;
    }
    if wng(MU_MAX) < wng_min {
        return MU_MAX;
    }
    let (mut lo, mut hi) = (MU_MIN.ln(), MU_MAX.ln());
    for _ in 0..BISECT_STEPS {
        let mid = 0.5 * (lo + hi);
        if wng(mid.exp()) >= wng_min {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    hi.exp()
}

/// Gewichte je Bin (Ausgang y = Σ conj(w_m)·x_m) und Richtwirkungsfaktor 1/(wᴴΓw).
pub struct Weights {
    w: Vec<[C32; CHANNELS]>,
    di: Vec<f32>,
}

impl Weights {
    fn new() -> Self {
        Weights { w: vec![[C32::new(0.0, 0.0); CHANNELS]; BINS], di: vec![1.0; BINS] }
    }

    fn design(&mut self, eig: &Eigen, geom: &Geometry, steer: &Steering, sample_rate: f64) {
        let p = geom.positions();
        let slot = geom.slots();
        let (az, el) = ((steer.azimuth_deg as f64).to_radians(), (steer.elevation_deg as f64).to_radians());
        let u = [el.cos() * az.cos(), el.cos() * az.sin()];
        let wng_min = 10f64.powf(steer.min_wng_db as f64 / 10.0);
        for k in 0..BINS {
            let wn = wavenumber(k, sample_rate);
            // Mikrofon m hört die ebene Welle um τ_m = p_m·u/c früher: d_m = exp(+j·2πf·τ_m).
            let d: [C64; CHANNELS] = std::array::from_fn(|m| C64::from_polar(1.0, wn * (p[m][0] * u[0] + p[m][1] * u[1])));
            let (lambda, v) = (&eig.lambda[k], &eig.v[k]);
            let b: [C64; CHANNELS] = std::array::from_fn(|i| (0..CHANNELS).map(|m| d[m] * v[slot[m]][i]).sum());
            let pw = b.map(|x| x.norm_sqr());
            let (w, di): ([C64; CHANNELS], f64) = match steer.mode {
                Mode::Omni => (std::array::from_fn(|m| C64::new(if m == geom.center { 1.0 } else { 0.0 }, 0.0)), 1.0),
                Mode::DelayAndSum => {
                    let n = CHANNELS as f64;
                    let wgw: f64 = (0..CHANNELS).map(|i| lambda[i] * pw[i]).sum::<f64>() / (n * n);
                    (d.map(|x| x / n), 1.0 / wgw.max(1e-12))
                }
                Mode::Superdirective => {
                    let mu = regularization(lambda, &pw, wng_min);
                    let a: f64 = (0..CHANNELS).map(|i| pw[i] / (lambda[i] + mu)).sum();
                    // w = V·c mit c = (Λ+μI)⁻¹·b / dᴴ(Γ+μI)⁻¹d; wᴴΓw = Σ λ_i·|c_i|².
                    let c: [C64; CHANNELS] = std::array::from_fn(|i| b[i] / ((lambda[i] + mu) * a));
                    let wgw: f64 = (0..CHANNELS).map(|i| lambda[i] * c[i].norm_sqr()).sum();
                    (std::array::from_fn(|m| (0..CHANNELS).map(|i| c[i] * v[slot[m]][i]).sum()), 1.0 / wgw.max(1e-12))
                }
            };
            self.w[k] = w.map(|x| C32::new(x.re as f32, x.im as f32));
            self.di[k] = di as f32;
        }
    }
}

#[inline]
fn apply(w: &[C32; CHANNELS], x: &[Vec<C32>], k: usize) -> C32 {
    let mut acc = C32::new(0.0, 0.0);
    for m in 0..CHANNELS {
        acc += w[m].conj() * x[m][k];
    }
    acc
}

pub struct Beamformer {
    sample_rate: f64,
    eigen: Eigen,
    current: (Geometry, Steering),
    target: (Geometry, Steering),
    fading_to: (Geometry, Steering),
    cur: Weights,
    next: Weights,
    fade: Option<usize>,
    started: bool,
}

impl Beamformer {
    pub fn new(sample_rate: f32, geom: Geometry, steer: Steering) -> Self {
        let sr = sample_rate as f64;
        let mut eigen = Eigen::new();
        eigen.update(geom.radius_m, sr);
        let mut cur = Weights::new();
        cur.design(&eigen, &geom, &steer, sr);
        Beamformer {
            sample_rate: sr,
            eigen,
            current: (geom, steer),
            target: (geom, steer),
            fading_to: (geom, steer),
            cur,
            next: Weights::new(),
            fade: None,
            started: false,
        }
    }

    /// Neue Geometrie/Richtung. Ungültige Geometrien werden ignoriert.
    pub fn set_target(&mut self, geom: Geometry, steer: Steering) {
        if geom.is_valid() {
            self.target = (geom, steer);
        }
    }

    /// Geometrie der gerade wirksamen Gewichte.
    pub fn geometry(&self) -> &Geometry {
        &self.current.0
    }

    /// Richtung/Modus der gerade wirksamen Gewichte.
    pub fn steering(&self) -> &Steering {
        &self.current.1
    }

    fn design(&mut self, to: (Geometry, Steering), next: bool) {
        self.eigen.update(to.0.radius_m, self.sample_rate);
        let w = if next { &mut self.next } else { &mut self.cur };
        w.design(&self.eigen, &to.0, &to.1, self.sample_rate);
    }

    /// Ein Frame: `y` = Beam-Spektrum, `di` = Richtwirkungsfaktor der wirksamen Gewichte je Bin.
    /// Vor dem ersten Frame gilt das Ziel sofort (es wurde noch nichts ausgegeben); danach wird
    /// jeder Wechsel über `FADE_FRAMES` mit Kosinus-Rampe übergeblendet.
    pub fn process(&mut self, x: &[Vec<C32>], y: &mut [C32], di: &mut [f32]) {
        if !self.started {
            self.started = true;
            if self.target != self.current {
                self.current = self.target;
                self.design(self.target, false);
            }
        }
        if self.fade.is_none() && self.target != self.current {
            self.fading_to = self.target;
            self.design(self.fading_to, true);
            self.fade = Some(0);
        }
        let Some(i) = self.fade else {
            for k in 0..BINS {
                y[k] = apply(&self.cur.w[k], x, k);
            }
            di.copy_from_slice(&self.cur.di);
            return;
        };
        let g = 0.5 - 0.5 * (std::f32::consts::PI * (i + 1) as f32 / (FADE_FRAMES + 1) as f32).cos();
        for k in 0..BINS {
            let a = apply(&self.cur.w[k], x, k);
            y[k] = a + (apply(&self.next.w[k], x, k) - a) * g;
            di[k] = self.cur.di[k] + (self.next.di[k] - self.cur.di[k]) * g;
        }
        if i + 1 >= FADE_FRAMES {
            std::mem::swap(&mut self.cur, &mut self.next);
            self.current = self.fading_to;
            self.fade = None;
        } else {
            self.fade = Some(i + 1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const SR: f64 = 48000.0;

    fn steer(mode: Mode, az: f32, el: f32) -> Steering {
        Steering { azimuth_deg: az, elevation_deg: el, mode, min_wng_db: -3.0 }
    }

    fn designed_for(g: &Geometry, s: Steering) -> Weights {
        let mut eig = Eigen::new();
        eig.update(g.radius_m, SR);
        let mut w = Weights::new();
        w.design(&eig, g, &s, SR);
        w
    }

    fn designed(s: Steering) -> Weights {
        designed_for(&Geometry::UMA8, s)
    }

    fn steering_vector(k: usize, az: f64, el: f64) -> [C64; CHANNELS] {
        let p = Geometry::UMA8.positions();
        let (az, el) = (az.to_radians(), el.to_radians());
        let wn = wavenumber(k, SR);
        std::array::from_fn(|m| C64::from_polar(1.0, wn * (p[m][0] * el.cos() * az.cos() + p[m][1] * el.cos() * az.sin())))
    }

    fn bin(f: f64) -> usize {
        (f * FFT_LEN as f64 / SR).round() as usize
    }

    fn db(x: f32) -> f32 {
        10.0 * x.log10()
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
    fn all_modes_are_distortionless_towards_target() {
        for mode in [Mode::Superdirective, Mode::DelayAndSum] {
            let w = designed(steer(mode, 30.0, 25.0));
            for k in 1..BINS {
                let d = steering_vector(k, 30.0, 25.0);
                let r: C64 = (0..CHANNELS).map(|m| C64::new(w.w[k][m].re as f64, -w.w[k][m].im as f64) * d[m]).sum();
                assert!((r - 1.0).norm() < 1e-4, "{mode:?} k={k}: {r}");
            }
        }
    }

    #[test]
    fn superdirective_meets_wng_floor_and_beats_delay_and_sum() {
        let sd = designed(steer(Mode::Superdirective, 0.0, 25.0));
        let ds = designed(steer(Mode::DelayAndSum, 0.0, 25.0));
        for k in 1..BINS {
            let wng = 1.0 / sd.w[k].iter().map(|x| x.norm_sqr()).sum::<f32>();
            assert!(db(wng) >= -3.0 - 1e-3, "k={k}: WNG {} dB", db(wng));
            assert!(sd.di[k] >= ds.di[k] * 0.999, "k={k}");
        }
        // Erwartung aus der numpy-Auslegung (Az 0°, El 25°, WNG ≥ −3 dB)
        for (f, sd_db, ds_db) in [(500.0, 5.8, 0.4), (1000.0, 7.5, 1.6), (2000.0, 8.1, 5.0)] {
            let k = bin(f);
            assert!((db(sd.di[k]) - sd_db).abs() < 0.3, "{f} Hz: SD-DI {} dB", db(sd.di[k]));
            assert!((db(ds.di[k]) - ds_db).abs() < 0.3, "{f} Hz: DS-DI {} dB", db(ds.di[k]));
        }
        // Bei tiefen Frequenzen begrenzt die WNG-Untergrenze: μ ist so klein wie möglich, WNG ≈ −3 dB.
        let k = bin(500.0);
        let wng = 1.0 / sd.w[k].iter().map(|x| x.norm_sqr()).sum::<f32>();
        assert!((db(wng) + 3.0).abs() < 0.01, "WNG {} dB", db(wng));
    }

    #[test]
    fn other_channel_order_matches_direct_computation() {
        // Kanäle vertauscht, Ring gedreht: Gewichte über Platz-Zuordnung müssen zur
        // direkt in Kanal-Reihenfolge gebildeten Γ passen.
        let g = Geometry { center: 3, ring: [5, 0, 6, 2, 1, 4], ring_offset_deg: 17.0, radius_m: 0.04 };
        let w = designed_for(&g, steer(Mode::Superdirective, 200.0, 30.0));
        let p = g.positions();
        let (az, el) = (200f64.to_radians(), 30f64.to_radians());
        for k in [5, 11, 21, 43, 85, 171, 300] {
            let wn = wavenumber(k, SR);
            let ww: [C64; CHANNELS] = std::array::from_fn(|m| C64::new(w.w[k][m].re as f64, w.w[k][m].im as f64));
            let r: C64 = (0..CHANNELS)
                .map(|m| ww[m].conj() * C64::from_polar(1.0, wn * (p[m][0] * el.cos() * az.cos() + p[m][1] * el.cos() * az.sin())))
                .sum();
            assert!((r - 1.0).norm() < 1e-4, "k={k}: wᴴd = {r}");
            let mut wgw = C64::new(0.0, 0.0);
            for i in 0..CHANNELS {
                for j in 0..CHANNELS {
                    wgw += ww[i].conj() * diffuse_coherence(wn * g.distance(i, j)) * ww[j];
                }
            }
            assert!((1.0 / wgw.re - w.di[k] as f64).abs() < 1e-3 * w.di[k] as f64, "k={k}: DI");
        }
    }

    #[test]
    fn omni_is_center_mic() {
        let w = designed(steer(Mode::Omni, 123.0, 10.0));
        for k in 0..BINS {
            assert_eq!(w.w[k][0], C32::new(1.0, 0.0));
            assert!(w.w[k][1..].iter().all(|x| x.norm() == 0.0));
            assert_eq!(w.di[k], 1.0);
        }
    }

    #[test]
    fn invalid_geometry_is_ignored() {
        let s = steer(Mode::Superdirective, 0.0, 0.0);
        let mut bf = Beamformer::new(SR as f32, Geometry::UMA8, s);
        let mut bad = Geometry::UMA8;
        bad.ring[0] = 0;
        bf.set_target(bad, steer(Mode::Superdirective, 90.0, 0.0));
        let x = vec![vec![C32::new(0.0, 0.0); BINS]; CHANNELS];
        let (mut y, mut di) = (vec![C32::new(0.0, 0.0); BINS], vec![0.0; BINS]);
        for _ in 0..2 * FADE_FRAMES {
            bf.process(&x, &mut y, &mut di);
        }
        assert_eq!(bf.current, (Geometry::UMA8, s));
    }
}
