//! Seeded streaming IVF training with distortion-aware empty-cluster recovery.
use super::profiles::{centroid_dist, floats, step};
use super::{Error, Metric, Row};

pub(super) fn train(
    rows: &[Row], centers: &mut [Vec<f64>], order: &mut [usize],
    epochs: usize, seed: &mut u64, metric: Metric,
) -> Result<(), Error> {
    let mut support = vec![0_u64; centers.len()];
    for _ in 0..epochs {
        for i in (1..order.len()).rev() {
            let j = step(seed) as usize % (i + 1);
            order.swap(i, j);
        }
        for batch in order.chunks(64) {
            let mut sums = vec![vec![0.; centers[0].len()]; centers.len()];
            let mut counts = vec![0_u64; centers.len()];
            let mut errors = Vec::with_capacity(batch.len());
            for &index in batch {
                let x = floats(&rows[index]);
                let (cluster, loss) = centers.iter().enumerate()
                    .map(|(j, c)| (j, centroid_dist(x, c)))
                    .min_by(|a, b| a.1.total_cmp(&b.1).then(a.0.cmp(&b.0)))
                    .ok_or(Error::InvalidVector)?;
                counts[cluster] += 1;
                errors.push((loss, index));
                for (sum, &v) in sums[cluster].iter_mut().zip(x) { *sum += f64::from(v); }
            }
            errors.sort_unstable_by(|a,b|a.0.total_cmp(&b.0).then(a.1.cmp(&b.1)));
            for (j, center) in centers.iter_mut().enumerate() {
                if counts[j] == 0 {
                    if support[j] == 0 {
                        if let Some((loss,index)) = errors.pop() {
                            if loss > f64::EPSILON {
                                for (c,&v) in center.iter_mut().zip(floats(&rows[index])) { *c = f64::from(v); }
                            }
                        }
                    }
                    continue;
                }
                let prior = support[j] as f64;
                support[j] += counts[j];
                for (c,sum) in center.iter_mut().zip(&sums[j]) {
                    *c = (*c * prior + sum) / support[j] as f64;
                }
                if metric == Metric::UnitCosine {
                    let norm = center.iter().map(|v|v*v).sum::<f64>().sqrt();
                    if !norm.is_finite() || norm <= 1e-12 { return Err(Error::InvalidVector); }
                    for c in center { *c /= norm; }
                }
            }
        }
    }
    Ok(())
}
