//! Festes Beamforming im STFT-Bereich für das UMA-8 (7 Mikrofone in einer Ebene, Fernfeld).
//!
//! Superdirektiv: MVDR gegen die Kohärenzmatrix Γ des 3D-diffusen Felds mit Diagonalladung μ,
//! je Bin gerade so groß, dass der White-Noise-Gain (WNG) die Untergrenze einhält. Γ(f) ist reell,
//! symmetrisch und richtungsunabhängig; in Platz-Reihenfolge (Mitte, Ring 0–5) hängt es nur vom
//! Radius ab und wird je Radius einmal zerlegt (Γ = V·Λ·Vᵀ). Kanalzuordnung und Ringdrehung sind
//! nur Umnummerierung bzw. Drehung. Für eine Richtung sind dann WNG, dᴴ(Γ+μI)⁻¹d und der
//! Richtwirkungsfaktor O(7)-Summen über die Eigenwerte.
//!
//! Nullstellen (optional, feste Störrichtungen wie Lautsprecher): MVDR gegen R = Γ + β·Σ d_i·d_iᴴ,
//! d_i Steuervektoren um jede Störrichtung (`NULL_PATTERN`), β nur bis 4–6 kHz (`NULL_TAPER_HZ`).
//! R ist komplex hermitesch, hängt aber nicht von der Blickrichtung ab: je Radius, Ringdrehung und
//! Nullstellen einmal zerlegt (R = U·Λ·Uᴴ), danach ist der Entwurf je Blickrichtung wieder eine
//! O(7)-Summe. Der Richtwirkungsfaktor für den Kohärenz-Postfilter bleibt 1/(wᴴΓw) gegen das
//! diffuse Feld. Ohne Nullstellen (Gewicht 0) ist alles bitgenau wie vorher.

use crate::jacobi;
use crate::stft::{BINS, C32, FFT_LEN};
use realfft::num_complex::Complex;
use std::f64::consts::PI;

pub const CHANNELS: usize = 7;
pub const SPEED_OF_SOUND: f64 = 343.0;
/// Überblendung bei Parameterwechseln: 10 Frames ≈ 53 ms.
pub const FADE_FRAMES: usize = 10;
/// Neuentwurf je Hop höchstens so viele Kosteneinheiten: Gewichte eines Bins = WEIGHTS_COST
/// (≈ 0,3 µs), Eigenzerlegung eines Bins = EIGEN_COST (≈ 5 µs). Richtung, Modus, WNG oder
/// Kanalzuordnung brauchen so 2 Hops, ein neuer Radius 17 Hops (≈ 90 ms); keiner kostet mehr als
/// ≈ 0,15 ms je Hop zusätzlich (statt bis zu 2,7 ms auf einmal).
const DESIGN_BUDGET: usize = 512;
const WEIGHTS_COST: usize = 1;
const EIGEN_COST: usize = 15;
/// Mit Nullstellen, nur Bins bis `NULL_TAPER_HZ[1]`: komplexe Zerlegung (R aufbauen + Jacobi,
/// ≈ 10 µs) bzw. Zusatz für die Gewichte (≈ 0,15 µs). Neue Nullstellen brauchen so 10 Hops
/// (≈ 50 ms, zusammen ≈ 1,3 ms), eine neue Blickrichtung weiter 2 Hops.
const NULL_EIGEN_COST: usize = 30;
const NULL_WEIGHTS_COST: usize = 1;
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

/// Zahl der Nullstellen.
pub const NULLS: usize = 2;
/// Obergrenze von `Nulls::weight_db` (β ≈ 10⁴).
pub const NULL_WEIGHT_MAX_DB: f32 = 40.0;
/// Robustheit gegen Richtungsfehler (gemessene Lautsprecherrichtung ±5–10°): jede Nullstelle ist
/// eine aufgeweitete Quelle aus 5 Richtungen, Mitte und ±NULL_SPREAD_DEG in Azimut und Elevation,
/// je mit β/5. Abgestimmt mit `tools/eval_nulls.py` (Design-Dokument).
const NULL_SPREAD_DEG: f64 = 8.0;
const NULL_PATTERN: [[f64; 2]; 5] = [[0.0, 0.0], [1.0, 0.0], [-1.0, 0.0], [0.0, 1.0], [0.0, -1.0]];
/// β voll bis NULL_TAPER_HZ[0], Kosinus-Abfall bis 0 bei NULL_TAPER_HZ[1]. Darüber (Ring jenseits
/// der räumlichen Abtastgrenze) kosten Nullstellen viel Richtwirkung, der Sprecher wird halliger
/// (8-kHz-Oktave −3…−5 dB SI-SDR), und das Echo insgesamt wird nicht leiser.
const NULL_TAPER_HZ: [f64; 2] = [4000.0, 6000.0];

/// Anteil von β bei `freq` (Hz).
fn null_taper(freq: f64) -> f64 {
    let [lo, hi] = NULL_TAPER_HZ;
    0.5 + 0.5 * (PI * ((freq - lo) / (hi - lo)).clamp(0.0, 1.0)).cos()
}

/// Feste Störrichtungen für Nullstellen des superdirektiven Beams.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Nulls {
    /// (Azimut, Elevation) in Grad je Nullstelle, im selben Koordinatensystem wie die Blickrichtung.
    /// Gleiche Richtungen zählen einmal.
    pub dirs: [[f32; 2]; NULLS],
    /// Gewicht in dB: angenommene Leistung jeder Störquelle relativ zum diffusen Feld,
    /// β = 10^(dB/10) − 1; ≤ 0 = aus (stetig: β → 0).
    pub weight_db: f32,
}

impl Nulls {
    pub const OFF: Nulls = Nulls { dirs: [[0.0; 2]; NULLS], weight_db: 0.0 };

    fn active(&self) -> bool {
        self.weight_db > 0.0
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Steering {
    pub azimuth_deg: f32,
    pub elevation_deg: f32,
    pub mode: Mode,
    /// Untergrenze des White-Noise-Gains in dB (nur superdirektiv).
    pub min_wng_db: f32,
    /// Nullstellen (nur superdirektiv).
    pub nulls: Nulls,
}

impl Steering {
    /// Nullstellen nur superdirektiv und mit endlichen Werten; sonst `Nulls::OFF`, damit
    /// gleichwertige Ziele gleich sind (kein Neuentwurf, ohne Nullstellen exakt wie bisher).
    fn canonical(mut self) -> Steering {
        let n = &mut self.nulls;
        if self.mode != Mode::Superdirective || !n.active() || !n.dirs.iter().flatten().all(|x| x.is_finite()) {
            *n = Nulls::OFF;
        } else {
            n.weight_db = n.weight_db.min(NULL_WEIGHT_MAX_DB);
        }
        self
    }
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

/// Eigenzerlegung von Γ(f) je Bin in Platz-Reihenfolge; hängt nur vom Radius ab. Gültig sind die
/// Bins `0..valid` für `radius_m`, damit ein Neuentwurf sie über mehrere Hops verteilen kann.
struct Eigen {
    radius_m: f32,
    sample_rate: f64,
    dist: [[f64; CHANNELS]; CHANNELS],
    valid: usize,
    lambda: Vec<[f64; CHANNELS]>,
    v: Vec<[[f64; CHANNELS]; CHANNELS]>,
}

impl Eigen {
    fn new(sample_rate: f64) -> Self {
        Eigen {
            radius_m: f32::NAN,
            sample_rate,
            dist: [[0.0; CHANNELS]; CHANNELS],
            valid: 0,
            lambda: vec![[0.0; CHANNELS]; BINS],
            v: vec![[[0.0; CHANNELS]; CHANNELS]; BINS],
        }
    }

    /// Neuer Radius: alle Bins ungültig. Gleicher Radius: bisherige Bins bleiben.
    fn retarget(&mut self, radius_m: f32) {
        if self.radius_m == radius_m {
            return;
        }
        self.radius_m = radius_m;
        self.valid = 0;
        let slot_geom = Geometry { center: 0, ring: [1, 2, 3, 4, 5, 6], ring_offset_deg: 0.0, radius_m };
        self.dist = std::array::from_fn(|i| std::array::from_fn(|j| slot_geom.distance(i, j)));
    }

    /// Zerlegt den nächsten ungültigen Bin (≈ 5 µs).
    fn compute_next(&mut self) {
        let k = self.valid;
        let wn = wavenumber(k, self.sample_rate);
        let gamma = self.dist.map(|row| row.map(|d| diffuse_coherence(wn * d)));
        let (l, v) = jacobi::eigh(gamma);
        self.lambda[k] = l.map(|x| x.max(0.0));
        self.v[k] = v;
        self.valid += 1;
    }

    /// Alle Bins sofort (≈ 2,5 ms; nur beim Anlegen und vor dem ersten Frame).
    fn update(&mut self, radius_m: f32) {
        self.retarget(radius_m);
        while self.valid < BINS {
            self.compute_next();
        }
    }
}

/// Was die Zerlegung von R mit Nullstellen festlegt (Γ in Platz-Reihenfolge braucht nur den
/// Radius, die Steuervektoren der Nullstellen auch die Ringdrehung).
#[derive(Clone, Copy, PartialEq)]
struct NullKey {
    radius_m: f32,
    ring_offset_deg: f32,
    nulls: Nulls,
}

impl NullKey {
    fn of(geom: &Geometry, steer: &Steering) -> Self {
        NullKey { radius_m: geom.radius_m, ring_offset_deg: geom.ring_offset_deg, nulls: steer.nulls }
    }
}

/// Eigenzerlegung von R = Γ + β/5·Σ d·dᴴ je Bin in Platz-Reihenfolge, wie `Eigen` in Etappen.
struct NullEigen {
    key: Option<NullKey>,
    sample_rate: f64,
    dist: [[f64; CHANNELS]; CHANNELS],
    /// Position je Platz (mit Ringdrehung)
    pos: [[f64; 2]; CHANNELS],
    /// Projektion jeder Musterrichtung auf die Array-Ebene
    dirs: [[f64; 2]; NULLS * NULL_PATTERN.len()],
    n_dirs: usize,
    beta: f64,
    valid: usize,
    /// Bin ohne Nullstellen (β·Abfall = 0): Entwurf wie ohne Nullstellen, keine Zerlegung
    plain: Vec<bool>,
    lambda: Vec<[f64; CHANNELS]>,
    u: Vec<[[C64; CHANNELS]; CHANNELS]>,
}

impl NullEigen {
    fn new(sample_rate: f64) -> Self {
        NullEigen {
            key: None,
            sample_rate,
            dist: [[0.0; CHANNELS]; CHANNELS],
            pos: [[0.0; 2]; CHANNELS],
            dirs: [[0.0; 2]; NULLS * NULL_PATTERN.len()],
            n_dirs: 0,
            beta: 0.0,
            valid: 0,
            plain: vec![true; BINS],
            lambda: vec![[0.0; CHANNELS]; BINS],
            u: vec![[[C64::new(0.0, 0.0); CHANNELS]; CHANNELS]; BINS],
        }
    }

    /// Neuer Schlüssel: alle Bins ungültig. Gleicher Schlüssel: bisherige Bins bleiben.
    fn retarget(&mut self, key: NullKey) {
        if self.key == Some(key) {
            return;
        }
        self.key = Some(key);
        self.valid = 0;
        let slot_geom = Geometry {
            center: 0,
            ring: [1, 2, 3, 4, 5, 6],
            ring_offset_deg: key.ring_offset_deg,
            radius_m: key.radius_m,
        };
        self.dist = std::array::from_fn(|i| std::array::from_fn(|j| slot_geom.distance(i, j)));
        self.pos = slot_geom.positions();
        self.n_dirs = 0;
        let dirs = &key.nulls.dirs;
        for (i, dir) in dirs.iter().enumerate() {
            if dirs[..i].contains(dir) {
                continue;
            }
            for [da, de] in NULL_PATTERN {
                let az = (dir[0] as f64 + da * NULL_SPREAD_DEG).to_radians();
                let el = (dir[1] as f64 + de * NULL_SPREAD_DEG).to_radians();
                self.dirs[self.n_dirs] = [el.cos() * az.cos(), el.cos() * az.sin()];
                self.n_dirs += 1;
            }
        }
        self.beta = (10f64.powf(key.nulls.weight_db as f64 / 10.0) - 1.0) / NULL_PATTERN.len() as f64;
    }

    /// Zerlegt den nächsten ungültigen Bin; false = Bin ohne Nullstellen (nichts zu rechnen).
    fn compute_next(&mut self) -> bool {
        let k = self.valid;
        self.valid += 1;
        let beta = self.beta * null_taper(k as f64 * self.sample_rate / FFT_LEN as f64);
        self.plain[k] = beta <= 0.0;
        if self.plain[k] {
            return false;
        }
        let wn = wavenumber(k, self.sample_rate);
        let mut r = self.dist.map(|row| row.map(|d| C64::new(diffuse_coherence(wn * d), 0.0)));
        for u in &self.dirs[..self.n_dirs] {
            let d: [C64; CHANNELS] =
                std::array::from_fn(|s| C64::from_polar(1.0, wn * (self.pos[s][0] * u[0] + self.pos[s][1] * u[1])));
            for i in 0..CHANNELS {
                for j in 0..CHANNELS {
                    r[i][j] += d[i] * d[j].conj() * beta;
                }
            }
        }
        let (l, u) = jacobi::eigh_hermitian(r);
        self.lambda[k] = l.map(|x| x.max(0.0));
        self.u[k] = u;
        true
    }

    fn ready(&self, key: NullKey) -> bool {
        self.key == Some(key) && self.valid == BINS
    }

    fn update(&mut self, key: NullKey) {
        self.retarget(key);
        while self.valid < BINS {
            self.compute_next();
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

/// Was der Entwurf je Bin aus Geometrie und Richtung braucht (einmal je Entwurf berechnet).
#[derive(Clone, Copy)]
struct DesignSpec {
    positions: [[f64; 2]; CHANNELS],
    slot: [usize; CHANNELS],
    center: usize,
    /// Projektion der Blickrichtung auf die Array-Ebene
    u: [f64; 2],
    wng_min: f64,
    mode: Mode,
    /// Gewichte aus der Zerlegung von R (`NullEigen`) statt von Γ
    nulls: bool,
}

impl DesignSpec {
    fn new(geom: &Geometry, steer: &Steering) -> Self {
        let (az, el) = ((steer.azimuth_deg as f64).to_radians(), (steer.elevation_deg as f64).to_radians());
        DesignSpec {
            positions: geom.positions(),
            slot: geom.slots(),
            center: geom.center,
            u: [el.cos() * az.cos(), el.cos() * az.sin()],
            wng_min: 10f64.powf(steer.min_wng_db as f64 / 10.0),
            mode: steer.mode,
            nulls: steer.mode == Mode::Superdirective && steer.nulls.active(),
        }
    }
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

    /// Entwurf für Bin `k`; die Eigenzerlegung von Bin `k` muss gültig sein (mit Nullstellen
    /// auch die von R).
    fn design_bin(&mut self, k: usize, eig: &Eigen, null_eig: &NullEigen, spec: &DesignSpec) {
        let (p, u, slot) = (&spec.positions, spec.u, &spec.slot);
        let wn = wavenumber(k, eig.sample_rate);
        // Mikrofon m hört die ebene Welle um τ_m = p_m·u/c früher: d_m = exp(+j·2πf·τ_m).
        let d: [C64; CHANNELS] = std::array::from_fn(|m| C64::from_polar(1.0, wn * (p[m][0] * u[0] + p[m][1] * u[1])));
        let (lambda, v) = (&eig.lambda[k], &eig.v[k]);
        let b: [C64; CHANNELS] = std::array::from_fn(|i| (0..CHANNELS).map(|m| d[m] * v[slot[m]][i]).sum());
        let pw = b.map(|x| x.norm_sqr());
        let (w, di): ([C64; CHANNELS], f64) = match spec.mode {
            Mode::Omni => (std::array::from_fn(|m| C64::new(if m == spec.center { 1.0 } else { 0.0 }, 0.0)), 1.0),
            Mode::DelayAndSum => {
                let n = CHANNELS as f64;
                let wgw: f64 = (0..CHANNELS).map(|i| lambda[i] * pw[i]).sum::<f64>() / (n * n);
                (d.map(|x| x / n), 1.0 / wgw.max(1e-12))
            }
            Mode::Superdirective if spec.nulls && !null_eig.plain[k] => {
                // wie unten, mit R = U·Λ·Uᴴ: b = Uᴴd, w = U·c; DI weiter gegen Γ = V·Λ_Γ·Vᵀ.
                let (lr, u) = (&null_eig.lambda[k], &null_eig.u[k]);
                let b: [C64; CHANNELS] =
                    std::array::from_fn(|i| (0..CHANNELS).map(|m| u[slot[m]][i].conj() * d[m]).sum());
                let pw = b.map(|x| x.norm_sqr());
                let mu = regularization(lr, &pw, spec.wng_min);
                let a: f64 = (0..CHANNELS).map(|i| pw[i] / (lr[i] + mu)).sum();
                let c: [C64; CHANNELS] = std::array::from_fn(|i| b[i] / ((lr[i] + mu) * a));
                let w: [C64; CHANNELS] = std::array::from_fn(|m| (0..CHANNELS).map(|i| u[slot[m]][i] * c[i]).sum());
                let wgw: f64 = (0..CHANNELS)
                    .map(|i| lambda[i] * (0..CHANNELS).map(|m| w[m] * v[slot[m]][i]).sum::<C64>().norm_sqr())
                    .sum();
                (w, 1.0 / wgw.max(1e-12))
            }
            Mode::Superdirective => {
                let mu = regularization(lambda, &pw, spec.wng_min);
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

    #[cfg(test)]
    fn design(&mut self, eig: &Eigen, null_eig: &NullEigen, geom: &Geometry, steer: &Steering) {
        let spec = DesignSpec::new(geom, steer);
        for k in 0..BINS {
            self.design_bin(k, eig, null_eig, &spec);
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

/// Laufender Neuentwurf in die freien Gewichte (`Beamformer::next`).
#[derive(Clone, Copy)]
struct Job {
    to: (Geometry, Steering),
    spec: DesignSpec,
    next_bin: usize,
}

pub struct Beamformer {
    eigen: Eigen,
    null_eigen: NullEigen,
    current: (Geometry, Steering),
    target: (Geometry, Steering),
    fading_to: (Geometry, Steering),
    cur: Weights,
    next: Weights,
    job: Option<Job>,
    fade: Option<usize>,
    started: bool,
}

impl Beamformer {
    pub fn new(sample_rate: f32, geom: Geometry, steer: Steering) -> Self {
        let steer = steer.canonical();
        let mut eigen = Eigen::new(sample_rate as f64);
        eigen.update(geom.radius_m);
        let mut null_eigen = NullEigen::new(sample_rate as f64);
        let spec = DesignSpec::new(&geom, &steer);
        if spec.nulls {
            null_eigen.update(NullKey::of(&geom, &steer));
        }
        let mut cur = Weights::new();
        for k in 0..BINS {
            cur.design_bin(k, &eigen, &null_eigen, &spec);
        }
        Beamformer {
            eigen,
            null_eigen,
            current: (geom, steer),
            target: (geom, steer),
            fading_to: (geom, steer),
            cur,
            next: Weights::new(),
            job: None,
            fade: None,
            started: false,
        }
    }

    /// Neue Geometrie/Richtung. Ungültige Geometrien werden ignoriert.
    pub fn set_target(&mut self, geom: Geometry, steer: Steering) {
        if geom.is_valid() {
            self.target = (geom, steer.canonical());
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

    /// Arbeitet am laufenden Neuentwurf, höchstens `DESIGN_BUDGET` Kosteneinheiten; true = fertig.
    fn design_step(&mut self) -> bool {
        let Some(job) = self.job.as_mut() else { return false };
        let mut budget = DESIGN_BUDGET;
        while job.next_bin < BINS && budget > 0 {
            let k = job.next_bin;
            let mut cost = WEIGHTS_COST;
            if k >= self.eigen.valid {
                self.eigen.compute_next();
                cost += EIGEN_COST;
            }
            if job.spec.nulls {
                if k >= self.null_eigen.valid && self.null_eigen.compute_next() {
                    cost += NULL_EIGEN_COST;
                }
                if !self.null_eigen.plain[k] {
                    cost += NULL_WEIGHTS_COST;
                }
            }
            self.next.design_bin(k, &self.eigen, &self.null_eigen, &job.spec);
            job.next_bin += 1;
            budget = budget.saturating_sub(cost);
        }
        job.next_bin == BINS
    }

    /// Ein Frame: `y` = Beam-Spektrum, `di` = Richtwirkungsfaktor der wirksamen Gewichte je Bin.
    /// Vor dem ersten Frame gilt das Ziel sofort (es wurde noch nichts ausgegeben); nur neue
    /// Nullstellen (Zerlegung von R ≈ 1,3 ms, mit neuem Radius dazu Γ ≈ 2,5 ms) folgen wie jeder
    /// spätere Wechsel verteilt, damit der erste `run()` nicht beides zahlt. Danach wird ein
    /// neues Ziel zuerst in Etappen entworfen (je Hop höchstens `DESIGN_BUDGET`, die alten Gewichte
    /// bleiben so lange wirksam) und dann über `FADE_FRAMES` mit Kosinus-Rampe übergeblendet.
    pub fn process(&mut self, x: &[Vec<C32>], y: &mut [C32], di: &mut [f32]) {
        if !self.started {
            self.started = true;
            if self.target != self.current {
                let (geom, mut steer) = self.target;
                if steer.nulls.active() && !self.null_eigen.ready(NullKey::of(&geom, &steer)) {
                    steer.nulls = Nulls::OFF;
                }
                self.current = (geom, steer);
                let spec = DesignSpec::new(&geom, &steer);
                self.eigen.update(geom.radius_m);
                for k in 0..BINS {
                    self.cur.design_bin(k, &self.eigen, &self.null_eigen, &spec);
                }
            }
        }
        if self.fade.is_none() {
            // Ziel geändert (auch während eines Entwurfs): Entwurf neu beginnen bzw. verwerfen.
            if self.job.map(|j| j.to) != Some(self.target) {
                self.job = (self.target != self.current).then(|| {
                    let (geom, steer) = &self.target;
                    self.eigen.retarget(geom.radius_m);
                    let spec = DesignSpec::new(geom, steer);
                    if spec.nulls {
                        self.null_eigen.retarget(NullKey::of(geom, steer));
                    }
                    Job { to: self.target, spec, next_bin: 0 }
                });
            }
            if self.design_step() {
                self.fading_to = self.job.take().map_or(self.target, |j| j.to);
                self.fade = Some(0);
            }
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
    use crate::stft::HOP;
    const SR: f64 = 48000.0;

    fn steer(mode: Mode, az: f32, el: f32) -> Steering {
        Steering { azimuth_deg: az, elevation_deg: el, mode, min_wng_db: -3.0, nulls: Nulls::OFF }
    }

    fn designed_for(g: &Geometry, s: Steering) -> Weights {
        let s = s.canonical();
        let mut eig = Eigen::new(SR);
        eig.update(g.radius_m);
        let mut null_eig = NullEigen::new(SR);
        if DesignSpec::new(g, &s).nulls {
            null_eig.update(NullKey::of(g, &s));
        }
        let mut w = Weights::new();
        w.design(&eig, &null_eig, g, &s);
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

    /// Spektren einer ebenen Welle aus (az, el), über alle Bins gleich laut.
    fn plane_wave(az: f64, el: f64) -> Vec<Vec<C32>> {
        (0..CHANNELS)
            .map(|m| (0..BINS).map(|k| steering_vector(k, az, el)[m]).map(|d| C32::new(d.re as f32, d.im as f32)).collect())
            .collect()
    }

    #[test]
    fn parameter_change_crossfades_over_at_least_40_ms() {
        // Das Overlap-Add der Synthese verschleift jeden Wechsel über einen Frame (21 ms), ein
        // Klick-Test im Zeitbereich sieht daher eine zu kurze Überblendung nicht; hier wird sie
        // direkt an den Gewichten gemessen.
        let x = plane_wave(0.0, 0.0);
        let mut bf = Beamformer::new(SR as f32, Geometry::UMA8, steer(Mode::Superdirective, 0.0, 0.0));
        let (mut y, mut di) = (vec![C32::new(0.0, 0.0); BINS], vec![0.0; BINS]);
        for f in [1000.0, 3000.0] {
            let k = bin(f);
            bf.process(&x, &mut y, &mut di);
            let before = y[k];
            bf.set_target(Geometry::UMA8, steer(Mode::Superdirective, 180.0, 0.0));
            let out: Vec<C32> = (0..40)
                .map(|_| {
                    bf.process(&x, &mut y, &mut di);
                    y[k]
                })
                .collect();
            let total = (out[39] - before).norm();
            assert!((before - 1.0).norm() < 1e-4 && total > 0.3, "{f} Hz: {before} → {}", out[39]);
            let steps: Vec<f32> =
                std::iter::once(before).chain(out.iter().copied()).collect::<Vec<_>>().windows(2).map(|w| (w[1] - w[0]).norm()).collect();
            let fade_hops = steps.iter().filter(|&&d| d > 1e-4 * total).count();
            let max_step = steps.iter().copied().fold(0.0, f32::max);
            let min_hops = (0.04 * SR / HOP as f64).ceil() as usize;
            assert!(fade_hops >= min_hops, "{f} Hz: Überblendung nur {fade_hops} Hops");
            assert!(max_step <= 0.2 * total, "{f} Hz: Sprung {max_step} von {total}");
            bf.set_target(Geometry::UMA8, steer(Mode::Superdirective, 0.0, 0.0));
            for _ in 0..40 {
                bf.process(&x, &mut y, &mut di);
            }
        }
    }

    #[test]
    fn redesign_is_spread_over_hops() {
        let s = steer(Mode::Superdirective, 40.0, 20.0);
        let x = plane_wave(40.0, 20.0);
        let mut bf = Beamformer::new(SR as f32, Geometry::UMA8, s);
        let (mut y, mut di) = (vec![C32::new(0.0, 0.0); BINS], vec![0.0; BINS]);
        bf.process(&x, &mut y, &mut di);
        // Richtung: keine neue Eigenzerlegung, Entwurf in höchstens 2 Hops, dann Überblendung
        let s2 = steer(Mode::Superdirective, 100.0, 20.0);
        bf.set_target(Geometry::UMA8, s2);
        bf.process(&x, &mut y, &mut di);
        assert!(bf.fade.is_none(), "Entwurf in einem Hop statt verteilt");
        bf.process(&x, &mut y, &mut di);
        assert!(bf.fade.is_some() && bf.eigen.valid == BINS);
        for _ in 0..FADE_FRAMES {
            bf.process(&x, &mut y, &mut di);
        }
        assert_eq!(bf.current, (Geometry::UMA8, s2));
        // Radius: Eigenzerlegung in Scheiben, alte Gewichte bleiben bis zum Ende des Entwurfs wirksam
        let mut g2 = Geometry::UMA8;
        g2.radius_m = 0.04;
        bf.set_target(g2, s2);
        let per_hop = DESIGN_BUDGET / (EIGEN_COST + WEIGHTS_COST) + 1;
        let (mut hops, mut valid) = (0, 0);
        while bf.fade.is_none() {
            bf.process(&x, &mut y, &mut di);
            hops += 1;
            assert!(bf.eigen.valid - valid <= per_hop, "Hop {hops}: {} Bins zerlegt", bf.eigen.valid - valid);
            assert!(hops < 40, "Entwurf endet nicht");
            valid = bf.eigen.valid;
        }
        assert!(hops >= 10, "Radius in {hops} Hops entworfen");
        for _ in 0..FADE_FRAMES {
            bf.process(&x, &mut y, &mut di);
        }
        assert_eq!(bf.current, (g2, s2));
        // Ergebnis gleich dem Entwurf am Stück
        let whole = designed_for(&g2, s2);
        for k in 0..BINS {
            assert_eq!(bf.cur.w[k], whole.w[k], "k={k}");
            assert_eq!(bf.cur.di[k], whole.di[k], "k={k}");
        }
        // Ziel während des Entwurfs zurückgenommen: Entwurf verworfen, nichts ändert sich
        let mut g3 = g2;
        g3.radius_m = 0.045;
        bf.set_target(g3, s2);
        bf.process(&x, &mut y, &mut di);
        bf.set_target(g2, s2);
        for _ in 0..30 {
            bf.process(&x, &mut y, &mut di);
            assert!(bf.fade.is_none() && bf.job.is_none());
        }
        assert_eq!(bf.current, (g2, s2));
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

    const SPEAKERS: [[f32; 2]; NULLS] = [[180.0, 5.0], [340.0, 5.0]];

    fn nulled(mut s: Steering, dirs: [[f32; 2]; NULLS], weight_db: f32) -> Steering {
        s.nulls = Nulls { dirs, weight_db };
        s
    }

    /// Antwort Σ conj(w_m)·d_m der Gewichte von Bin `k` auf eine ebene Welle aus `p`-Geometrie.
    fn response(w: &Weights, k: usize, p: &[[f64; 2]; CHANNELS], az: f64, el: f64) -> C64 {
        let (az, el) = (az.to_radians(), el.to_radians());
        let wn = wavenumber(k, SR);
        (0..CHANNELS)
            .map(|m| {
                let d = C64::from_polar(1.0, wn * (p[m][0] * el.cos() * az.cos() + p[m][1] * el.cos() * az.sin()));
                C64::new(w.w[k][m].re as f64, -w.w[k][m].im as f64) * d
            })
            .sum()
    }

    fn db64(x: f64) -> f64 {
        10.0 * x.log10()
    }

    #[test]
    fn nulls_attenuate_speakers_and_keep_target_distortionless() {
        let look = steer(Mode::Superdirective, 80.0, 30.0);
        let (sd, nw) = (designed(look), designed(nulled(look, SPEAKERS, 10.0)));
        let p = Geometry::UMA8.positions();
        for k in 1..BINS {
            let r = response(&nw, k, &p, 80.0, 30.0);
            assert!((r - 1.0).norm() < 1e-4, "k={k}: {r}");
            let wng = 1.0 / nw.w[k].iter().map(|x| x.norm_sqr()).sum::<f32>();
            assert!(db(wng) >= -3.0 - 1e-3, "k={k}: WNG {} dB", db(wng));
            assert!(nw.di[k] <= sd.di[k] * 1.0001, "k={k}: DI höher als ohne Nullstellen");
        }
        // je Terz um f, Gewicht 10 dB: Lautsprecher genau ≥ 20 dB, ±5° daneben ≥ 8 dB leiser als ohne
        // Nullstellen (freies Feld; im Raum mit Streuung siehe tools/eval_nulls.py), DI-Verlust
        for (f, max_di_loss) in [(1000.0, 0.4), (2000.0, 0.8), (3000.0, 1.0)] {
            let ks: Vec<usize> = (bin(f / 1.12)..=bin(f * 1.12)).collect();
            for [az, el] in SPEAKERS.map(|d| d.map(|x| x as f64)) {
                for (daz, del) in [(0.0, 0.0), (-5.0, 0.0), (5.0, 0.0), (0.0, 5.0)] {
                    let e = |w: &Weights| {
                        ks.iter().map(|&k| response(w, k, &p, az + daz, el + del).norm_sqr()).sum::<f64>()
                    };
                    let gain = db64(e(&nw) / e(&sd));
                    let need = if daz == 0.0 && del == 0.0 { 20.0 } else { 8.0 };
                    assert!(gain <= -need, "{f} Hz, {az}{daz:+}°/{el}{del:+}°: nur {gain:.1} dB");
                }
            }
            let di_loss = ks.iter().map(|&k| db(sd.di[k]) - db(nw.di[k])).sum::<f32>() / ks.len() as f32;
            assert!(di_loss <= max_di_loss, "{f} Hz: DI −{di_loss:.2} dB");
        }
        // ab NULL_TAPER_HZ[1] keine Nullstellen: genau der Entwurf ohne
        let k_hi = (NULL_TAPER_HZ[1] * FFT_LEN as f64 / SR).ceil() as usize;
        assert!((k_hi..BINS).all(|k| nw.w[k] == sd.w[k] && nw.di[k] == sd.di[k]));
        assert!(nw.w[k_hi - 1] != sd.w[k_hi - 1]);
    }

    #[test]
    fn nulls_off_is_exactly_the_plain_design() {
        let look = steer(Mode::Superdirective, 80.0, 30.0);
        let plain = designed(look);
        let mut nan_dir = SPEAKERS;
        nan_dir[1][0] = f32::NAN;
        for s in [nulled(look, SPEAKERS, 0.0), nulled(look, SPEAKERS, -5.0), nulled(look, nan_dir, 15.0)] {
            assert_eq!(s.canonical(), look);
            let w = designed(s);
            assert!((0..BINS).all(|k| w.w[k] == plain.w[k] && w.di[k] == plain.di[k]), "{s:?}");
        }
        for mode in [Mode::DelayAndSum, Mode::Omni] {
            let (a, b) = (designed(steer(mode, 80.0, 30.0)), designed(nulled(steer(mode, 80.0, 30.0), SPEAKERS, 15.0)));
            assert!((0..BINS).all(|k| a.w[k] == b.w[k] && a.di[k] == b.di[k]), "{mode:?}");
        }
    }

    /// Löst A·x = b (Gauß mit Spaltenpivotsuche).
    fn solve(mut a: [[C64; CHANNELS]; CHANNELS], mut b: [C64; CHANNELS]) -> [C64; CHANNELS] {
        for c in 0..CHANNELS {
            let p = (c..CHANNELS).max_by(|&i, &j| a[i][c].norm().total_cmp(&a[j][c].norm())).unwrap();
            a.swap(c, p);
            b.swap(c, p);
            for r in c + 1..CHANNELS {
                let f = a[r][c] / a[c][c];
                for j in c..CHANNELS {
                    let t = a[c][j];
                    a[r][j] -= f * t;
                }
                let t = b[c];
                b[r] -= f * t;
            }
        }
        let mut x = [C64::new(0.0, 0.0); CHANNELS];
        for c in (0..CHANNELS).rev() {
            let acc: C64 = (c + 1..CHANNELS).map(|j| a[c][j] * x[j]).sum();
            x[c] = (b[c] - acc) / a[c][c];
        }
        x
    }

    /// Entwurf mit Nullstellen direkt in Kanal-Reihenfolge: R aus `g.positions()`, (R+μI)⁻¹d per
    /// Gauß, μ per Bisektion wie `regularization`.
    fn direct_null_design(g: &Geometry, s: &Steering, k: usize) -> [C64; CHANNELS] {
        let (p, wn) = (g.positions(), wavenumber(k, SR));
        let sv = |az: f64, el: f64| -> [C64; CHANNELS] {
            let (az, el) = (az.to_radians(), el.to_radians());
            std::array::from_fn(|m| {
                C64::from_polar(1.0, wn * (p[m][0] * el.cos() * az.cos() + p[m][1] * el.cos() * az.sin()))
            })
        };
        let beta =
            (10f64.powf(s.nulls.weight_db as f64 / 10.0) - 1.0) / 5.0 * null_taper(k as f64 * SR / FFT_LEN as f64);
        let mut r: [[C64; CHANNELS]; CHANNELS] =
            std::array::from_fn(|i| std::array::from_fn(|j| C64::new(diffuse_coherence(wn * g.distance(i, j)), 0.0)));
        for [az, el] in s.nulls.dirs {
            for [da, de] in NULL_PATTERN {
                let v = sv(az as f64 + da * NULL_SPREAD_DEG, el as f64 + de * NULL_SPREAD_DEG);
                for i in 0..CHANNELS {
                    for j in 0..CHANNELS {
                        r[i][j] += v[i] * v[j].conj() * beta;
                    }
                }
            }
        }
        let d = sv(s.azimuth_deg as f64, s.elevation_deg as f64);
        let w_of = |mu: f64| {
            let mut a = r;
            for (i, row) in a.iter_mut().enumerate() {
                row[i] += mu;
            }
            let x = solve(a, d);
            let n: C64 = (0..CHANNELS).map(|m| d[m].conj() * x[m]).sum();
            x.map(|v| v / n)
        };
        let wng = |mu: f64| 1.0 / w_of(mu).iter().map(|v| v.norm_sqr()).sum::<f64>();
        let wng_min = 10f64.powf(s.min_wng_db as f64 / 10.0);
        if wng(MU_MIN) >= wng_min {
            return w_of(MU_MIN);
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
        w_of(hi.exp())
    }

    #[test]
    fn nulls_match_direct_design_in_channel_order() {
        // R wird in Platz-Reihenfolge mit Ringdrehung zerlegt: Gewichte und DI müssen zur direkt in
        // Kanal-Reihenfolge gerechneten Lösung passen (vertauschte Kanäle, gedrehter Ring).
        let g = Geometry { center: 3, ring: [5, 0, 6, 2, 1, 4], ring_offset_deg: 17.0, radius_m: 0.04 };
        let s = nulled(steer(Mode::Superdirective, 200.0, 30.0), [[290.0, 10.0], [95.0, 0.0]], 20.0);
        let (sd, nw) = (designed_for(&g, steer(Mode::Superdirective, 200.0, 30.0)), designed_for(&g, s));
        let p = g.positions();
        for k in [3, 11, 21, 43, 64, 85, 100, 120] {
            let direct = direct_null_design(&g, &s, k);
            let err = (0..CHANNELS)
                .map(|m| (C64::new(nw.w[k][m].re as f64, nw.w[k][m].im as f64) - direct[m]).norm())
                .fold(0.0, f64::max);
            let scale = direct.iter().map(|v| v.norm()).fold(0.0, f64::max);
            assert!(err < 1e-5 * scale.max(1.0), "k={k}: Abweichung {err} (Gewichte bis {scale})");
            if (21..=85).contains(&k) {
                let gain =
                    db64(response(&nw, k, &p, 290.0, 10.0).norm_sqr() / response(&sd, k, &p, 290.0, 10.0).norm_sqr());
                assert!(gain < -10.0, "k={k}: {gain:.1} dB");
            }
            let wn = wavenumber(k, SR);
            let mut wgw = C64::new(0.0, 0.0);
            for i in 0..CHANNELS {
                for j in 0..CHANNELS {
                    wgw += direct[i].conj() * diffuse_coherence(wn * g.distance(i, j)) * direct[j];
                }
            }
            assert!((1.0 / wgw.re - nw.di[k] as f64).abs() < 1e-4 * nw.di[k] as f64, "k={k}: DI");
        }
    }

    #[test]
    fn null_change_is_spread_over_hops_and_crossfaded() {
        let look = steer(Mode::Superdirective, 80.0, 30.0);
        let x = plane_wave(180.0, 5.0);
        let mut bf = Beamformer::new(SR as f32, Geometry::UMA8, look);
        let (mut y, mut di) = (vec![C32::new(0.0, 0.0); BINS], vec![0.0; BINS]);
        bf.process(&x, &mut y, &mut di);
        let before = y.clone();
        let s2 = nulled(look, SPEAKERS, 15.0);
        bf.set_target(Geometry::UMA8, s2);
        let per_hop = DESIGN_BUDGET / (NULL_EIGEN_COST + WEIGHTS_COST + NULL_WEIGHTS_COST) + 1;
        let (mut hops, mut valid) = (0, 0);
        while bf.fade.is_none() {
            bf.process(&x, &mut y, &mut di);
            hops += 1;
            let done = (valid..bf.null_eigen.valid).filter(|&k| !bf.null_eigen.plain[k]).count();
            assert!(done <= per_hop, "Hop {hops}: {done} Bins zerlegt");
            assert!(hops < 20, "Entwurf endet nicht");
            if bf.fade.is_none() {
                assert_eq!(y, before, "Hop {hops}: alte Gewichte nicht mehr wirksam");
            }
            valid = bf.null_eigen.valid;
        }
        assert!(hops >= 5, "Nullstellen in {hops} Hops entworfen");
        // Überblendung im 1- und 3-kHz-Bin: mindestens 40 ms, keine großen Sprünge
        let mut out = vec![(y[bin(1000.0)], y[bin(3000.0)])];
        for _ in 0..FADE_FRAMES {
            bf.process(&x, &mut y, &mut di);
            out.push((y[bin(1000.0)], y[bin(3000.0)]));
        }
        assert_eq!(bf.current, (Geometry::UMA8, s2));
        for (i, f) in [1000.0, 3000.0].into_iter().enumerate() {
            let pick = |o: &(C32, C32)| if i == 0 { o.0 } else { o.1 };
            let seq: Vec<C32> = std::iter::once(before[bin(f)]).chain(out.iter().map(pick)).collect();
            let total = (seq[seq.len() - 1] - seq[0]).norm();
            let steps: Vec<f32> = seq.windows(2).map(|w| (w[1] - w[0]).norm()).collect();
            assert!(total > 0.05, "{f} Hz: Nullstelle wirkt nicht ({total})");
            assert!(steps.iter().filter(|&&d| d > 1e-4 * total).count() >= (0.04 * SR / HOP as f64).ceil() as usize);
            assert!(steps.iter().all(|&d| d <= 0.2 * total), "{f} Hz: Sprung {steps:?}");
        }
        let whole = designed(s2);
        assert!((0..BINS).all(|k| bf.cur.w[k] == whole.w[k] && bf.cur.di[k] == whole.di[k]));
        // Blickrichtung mit Nullstellen: keine neue Zerlegung, Entwurf in 2 Hops wie ohne
        let s3 = nulled(steer(Mode::Superdirective, 60.0, 25.0), SPEAKERS, 15.0);
        bf.set_target(Geometry::UMA8, s3);
        let mut hops = 0;
        while bf.fade.is_none() {
            bf.process(&x, &mut y, &mut di);
            hops += 1;
            assert!(bf.null_eigen.valid == BINS && hops <= 2, "Hop {hops}");
        }
    }

    #[test]
    fn first_frame_defers_null_decomposition() {
        // Vor dem ersten Frame gilt das Ziel sofort – die Zerlegung von R aber nicht in einem
        // Aufruf: erst Richtung ohne Nullstellen, die Nullstellen folgen verteilt.
        let look = steer(Mode::Superdirective, 80.0, 30.0);
        let mut bf = Beamformer::new(SR as f32, Geometry::UMA8, steer(Mode::Superdirective, 0.0, 0.0));
        let s2 = nulled(look, SPEAKERS, 15.0);
        bf.set_target(Geometry::UMA8, s2);
        let x = plane_wave(80.0, 30.0);
        let (mut y, mut di) = (vec![C32::new(0.0, 0.0); BINS], vec![0.0; BINS]);
        bf.process(&x, &mut y, &mut di);
        assert_eq!(bf.current, (Geometry::UMA8, look));
        assert!(bf.null_eigen.valid < BINS / 4, "{} Bins im ersten Frame zerlegt", bf.null_eigen.valid);
        for _ in 0..60 {
            bf.process(&x, &mut y, &mut di);
            assert!(y[1..].iter().all(|v| (v - 1.0).norm() < 1e-4), "Sprecher nicht verzerrungsfrei");
        }
        assert_eq!(bf.current, (Geometry::UMA8, s2));
    }

    /// Kosten je Bin für `*_COST` (µs; `cargo test --release -- --ignored --nocapture design_costs`).
    #[test]
    #[ignore]
    fn design_costs() {
        let g = Geometry::UMA8;
        let look = steer(Mode::Superdirective, 80.0, 30.0);
        let s = nulled(look, SPEAKERS, 10.0);
        let time = |f: &mut dyn FnMut()| {
            let t = std::time::Instant::now();
            for _ in 0..5 {
                f();
            }
            t.elapsed().as_secs_f64() / 5.0 * 1e6
        };
        let mut eig = Eigen::new(SR);
        let real = time(&mut || {
            eig.retarget(0.0);
            eig.update(g.radius_m);
        }) / BINS as f64;
        let mut ne = NullEigen::new(SR);
        let complex = time(&mut || {
            ne.key = None;
            ne.update(NullKey::of(&g, &s));
        }) / ne.plain.iter().filter(|&&p| !p).count() as f64;
        let mut w = Weights::new();
        let plain = time(&mut || w.design(&eig, &ne, &g, &look)) / BINS as f64;
        let nulls = time(&mut || w.design(&eig, &ne, &g, &s)) / BINS as f64;
        println!(
            "je Bin: Γ zerlegen {real:.2} µs, R zerlegen {complex:.2} µs, Gewichte {plain:.2} µs, \
             mit Nullstellen im Mittel {nulls:.2} µs"
        );
    }
}
