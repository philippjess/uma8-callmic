//! Eigenzerlegung kleiner reeller symmetrischer Matrizen (zyklisches Jacobi-Verfahren, f64).

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
}
