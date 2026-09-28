//! Look-ahead-Begrenzer: 5 ms Vorschau, sofortiger Angriff, 100 ms Release.

pub struct Limiter {
    ring: Vec<f32>,
    pos: usize,
    gain: f32,
    release: f32,
    ceiling: f32,
}

impl Limiter {
    pub fn new(sample_rate: f32) -> Self {
        let lookahead = (0.005 * sample_rate).round() as usize;
        Limiter {
            ring: vec![0.0; lookahead + 1],
            pos: 0,
            gain: 1.0,
            release: (-1.0 / (0.1 * sample_rate)).exp(),
            ceiling: 1.0,
        }
    }

    /// Verzögerung des Ausgangs in Samples.
    pub fn latency(&self) -> usize {
        self.ring.len() - 1
    }

    pub fn set_ceiling_db(&mut self, db: f32) {
        self.ceiling = 10f32.powf(db.clamp(-12.0, 0.0) / 20.0);
    }

    pub fn process(&mut self, input: &[f32], output: &mut [f32]) {
        let len = self.ring.len();
        for (x, y) in input.iter().zip(output.iter_mut()) {
            self.ring[self.pos] = *x;
            self.pos = (self.pos + 1) % len;
            // ältester Wert im Ring = um `len - 1` Samples verzögert
            let delayed = self.ring[self.pos];
            let peak = self.ring.iter().fold(0.0f32, |m, v| m.max(v.abs()));
            let target = if peak > self.ceiling { self.ceiling / peak } else { 1.0 };
            self.gain = if target < self.gain {
                target
            } else {
                target + (self.gain - target) * self.release
            };
            *y = delayed * self.gain;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quiet_signal_passes_delayed() {
        let mut l = Limiter::new(48000.0);
        l.set_ceiling_db(-1.0);
        let x: Vec<f32> = (0..4800).map(|n| 0.1 * (n as f32 * 0.05).sin()).collect();
        let mut y = vec![0.0; x.len()];
        l.process(&x, &mut y);
        let d = l.latency();
        for n in d..x.len() {
            assert!((y[n] - x[n - d]).abs() < 1e-6, "n={n}");
        }
    }

    #[test]
    fn loud_signal_is_capped() {
        let mut l = Limiter::new(48000.0);
        l.set_ceiling_db(-1.0);
        let x: Vec<f32> = (0..48000).map(|n| 2.0 * (n as f32 * 0.0576).sin()).collect();
        let mut y = vec![0.0; x.len()];
        l.process(&x, &mut y);
        let ceiling = 10f32.powf(-1.0 / 20.0);
        assert!(y.iter().all(|v| v.abs() <= ceiling + 1e-6));
    }
}
