//! Eigenzerlegung kleiner reeller symmetrischer bzw. komplexer hermitescher Matrizen (zyklisches
//! Jacobi-Verfahren, f64).

use realfft::num_complex::Complex;

type C64 = Complex<f64>;

/// Liefert (λ, V) mit A = V·diag(λ)·Vᵀ; die Spalten von V sind die Eigenvektoren.
pub fn eigh<const N: usize>(mut a: [[f64; N]; N]) -> ([f64; N], [[f64; N]; N]) {
    let mut v = [[0.0; N]; N];
    for (i, row) in v.iter_mut().enumerate() {
        row[i] = 1.0;
    }
    let norm: f64 = a.iter().flatten().map(|x| x * x).sum();
    for _sweep in 0..64 {
        let off: f64 = (0..N).flat_map(|p| (p + 1..N).map(move |q| (p, q))).map(|(p, q)| a[p][q] * a[p][q]).sum();
        if off <= 1e-30 * norm {
            break;
        }
        for p in 0..N {
            for q in p + 1..N {
                let apq = a[p][q];
                if apq == 0.0 {
                    continue;
                }
                // Rotation J mit J_pp = J_qq = c, J_pq = s, J_qp = −s; A ← JᵀAJ nullt A_pq.
                let theta = (a[q][q] - a[p][p]) / (2.0 * apq);
                let t = theta.signum() / (theta.abs() + (theta * theta + 1.0).sqrt());
                let c = 1.0 / (t * t + 1.0).sqrt();
                let s = t * c;
                for row in a.iter_mut() {
                    let (kp, kq) = (row[p], row[q]);
                    row[p] = c * kp - s * kq;
                    row[q] = s * kp + c * kq;
                }
                for k in 0..N {
                    let (pk, qk) = (a[p][k], a[q][k]);
                    a[p][k] = c * pk - s * qk;
                    a[q][k] = s * pk + c * qk;
                }
                for row in v.iter_mut() {
                    let (kp, kq) = (row[p], row[q]);
                    row[p] = c * kp - s * kq;
                    row[q] = s * kp + c * kq;
                }
            }
        }
    }
    (std::array::from_fn(|i| a[i][i]), v)
}

/// Liefert (λ, U) mit A = U·diag(λ)·Uᴴ für hermitesches A; die Spalten von U sind die Eigenvektoren.
pub fn eigh_hermitian<const N: usize>(mut a: [[C64; N]; N]) -> ([f64; N], [[C64; N]; N]) {
    let (zero, one) = (C64::new(0.0, 0.0), C64::new(1.0, 0.0));
    let mut u = [[zero; N]; N];
    for (i, row) in u.iter_mut().enumerate() {
        row[i] = one;
    }
    let norm: f64 = a.iter().flatten().map(|x| x.norm_sqr()).sum();
    for _sweep in 0..64 {
        let off: f64 = (0..N).flat_map(|p| (p + 1..N).map(move |q| (p, q))).map(|(p, q)| a[p][q].norm_sqr()).sum();
        if off <= 1e-30 * norm {
            break;
        }
        for p in 0..N {
            for q in p + 1..N {
                let r = a[p][q].norm();
                if r == 0.0 {
                    continue;
                }
                // A_pq = r·e^{jφ}. Rotation J mit J_pp = J_qq = c, J_pq = s·e^{jφ}, J_qp = −s·e^{−jφ}
                // (= diag(1, e^{−jφ})·reelle Rotation·diag(1, e^{jφ})); A ← JᴴAJ nullt A_pq.
                let ph = a[p][q] / r;
                let theta = (a[q][q].re - a[p][p].re) / (2.0 * r);
                let t = theta.signum() / (theta.abs() + (theta * theta + 1.0).sqrt());
                let c = 1.0 / (t * t + 1.0).sqrt();
                let (sp, sm) = (ph * (t * c), ph.conj() * (t * c));
                for row in a.iter_mut() {
                    let (kp, kq) = (row[p], row[q]);
                    row[p] = kp * c - kq * sm;
                    row[q] = kp * sp + kq * c;
                }
                for k in 0..N {
                    let (pk, qk) = (a[p][k], a[q][k]);
                    a[p][k] = pk * c - sp * qk;
                    a[q][k] = sm * pk + qk * c;
                }
                for row in u.iter_mut() {
                    let (kp, kq) = (row[p], row[q]);
                    row[p] = kp * c - kq * sm;
                    row[q] = kp * sp + kq * c;
                }
            }
        }
    }
    (std::array::from_fn(|i| a[i][i].re), u)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reconstructs_symmetric_matrix() {
        let mut s = 12345u32;
        let mut rnd = || {
            s = s.wrapping_mul(1664525).wrapping_add(1013904223);
            (s >> 8) as f64 / (1u32 << 24) as f64 - 0.5
        };
        let mut a = [[0.0; 7]; 7];
        for i in 0..7 {
            for j in i..7 {
                a[i][j] = rnd();
                a[j][i] = a[i][j];
            }
        }
        let (l, v) = eigh(a);
        for i in 0..7 {
            for j in 0..7 {
                let rec: f64 = (0..7).map(|k| v[i][k] * l[k] * v[j][k]).sum();
                let orth: f64 = (0..7).map(|k| v[k][i] * v[k][j]).sum();
                assert!((rec - a[i][j]).abs() < 1e-12, "A[{i}][{j}]");
                assert!((orth - if i == j { 1.0 } else { 0.0 }).abs() < 1e-12, "VᵀV[{i}][{j}]");
            }
        }
    }

    #[test]
    fn rank_one_matrix() {
        let (mut l, _) = eigh([[1.0; 7]; 7]);
        l.sort_by(f64::total_cmp);
        assert!((l[6] - 7.0).abs() < 1e-12 && l[..6].iter().all(|x| x.abs() < 1e-12), "{l:?}");
    }

    #[test]
    fn reconstructs_hermitian_matrix() {
        let mut s = 777u32;
        let mut rnd = || {
            s = s.wrapping_mul(1664525).wrapping_add(1013904223);
            (s >> 8) as f64 / (1u32 << 24) as f64 - 0.5
        };
        // gut und schlecht konditioniert (Rang-1-Anteil 10⁴ wie eine starke Nullstelle)
        for big in [0.0, 1e4] {
            let mut a = [[C64::new(0.0, 0.0); 7]; 7];
            let v: [C64; 7] = std::array::from_fn(|_| C64::new(rnd(), rnd()));
            for i in 0..7 {
                a[i][i] = C64::new(rnd(), 0.0);
                for j in i + 1..7 {
                    a[i][j] = C64::new(rnd(), rnd());
                    a[j][i] = a[i][j].conj();
                }
            }
            for i in 0..7 {
                for j in 0..7 {
                    a[i][j] += v[i] * v[j].conj() * big;
                }
            }
            let (l, u) = eigh_hermitian(a);
            let scale = 1.0 + big;
            for i in 0..7 {
                for j in 0..7 {
                    let rec: C64 = (0..7).map(|k| u[i][k] * l[k] * u[j][k].conj()).sum();
                    let orth: C64 = (0..7).map(|k| u[k][i].conj() * u[k][j]).sum();
                    assert!((rec - a[i][j]).norm() < 1e-12 * scale, "A[{i}][{j}] {big}");
                    assert!((orth - if i == j { 1.0 } else { 0.0 }).norm() < 1e-12, "UᴴU[{i}][{j}] {big}");
                }
            }
        }
    }

    #[test]
    fn hermitian_matches_real_on_real_input() {
        let a: [[f64; 7]; 7] =
            std::array::from_fn(|i| std::array::from_fn(|j| 1.0 / (1.0 + (i as f64 - j as f64).abs())));
        let (mut lr, _) = eigh(a);
        let (mut lc, _) = eigh_hermitian(a.map(|row| row.map(|x| C64::new(x, 0.0))));
        lr.sort_by(f64::total_cmp);
        lc.sort_by(f64::total_cmp);
        for i in 0..7 {
            assert!((lr[i] - lc[i]).abs() < 1e-12, "{lr:?} {lc:?}");
        }
    }
}
