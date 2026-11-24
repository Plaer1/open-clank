//! Exact CPU operators only. Scope/epoch currentness and publication belong to the
//! generation owner; these values are bindings, not authorization credentials.
pub(crate) mod profiles;
mod minibatch;
pub(crate) mod quantized;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

const MAX_DIM: usize = 65_536;
const MAX_ROWS: usize = 1_000_000;
const MAX_ELEMENTS: usize = 16_000_000;
const MAX_BATCH: usize = 64;
const MAX_RESULTS: usize = 10_000;
/// Absolute tolerance on squared L2 norm for declared unit vectors.
const UNIT_NORM_TOLERANCE: f64 = 1e-4;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Metric { SquaredL2, InnerProduct, UnitCosine, Hamming }
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Profile {
    pub owner: String, pub workspace: String, pub project: String,
    pub generation_id: String, pub source_watermark: String, pub eligibility_epoch: String,
    pub model: String, pub model_version: String, pub dimension: usize,
    pub metric: Metric, pub normalization: String,
}
#[derive(Clone, Debug)]
pub(crate) enum Vector { Float(Vec<f32>), Binary(Vec<u8>) }
#[derive(Clone, Debug)]
pub(crate) struct Row { pub id: String, pub source_revision: String, pub vector: Vector }
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Hit { pub id: String, pub score: f64 }
#[derive(Clone, Debug, PartialEq)]
pub(crate) struct Results { pub hits: Vec<Hit>, pub truncated: bool }
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Error { InvalidProfile, InvalidVector, InvalidId, DuplicateId, ScopeMismatch, Limit, InvalidThreshold, ScoreOverflow }

pub(crate) struct FlatIndex { profile: Profile, rows: Vec<Row>, checksum: [u8; 32] }
fn valid_text(s: &str) -> bool { !s.is_empty() && s.len() <= 4096 && !s.chars().any(char::is_control) }
fn validate_profile(p: &Profile) -> Result<(), Error> {
    if ![&p.owner,&p.generation_id,&p.source_watermark,&p.eligibility_epoch,&p.model,&p.model_version].iter().all(|s| valid_text(s))
        || [&p.workspace,&p.project].iter().any(|s| !s.is_empty() && !valid_text(s))
        || p.dimension == 0 || p.dimension > MAX_DIM { return Err(Error::InvalidProfile); }
    match p.metric {
        Metric::UnitCosine if p.normalization != "unit_l2" => Err(Error::InvalidProfile),
        Metric::Hamming if p.normalization != "none" || p.dimension % 8 != 0 => Err(Error::InvalidProfile),
        Metric::SquaredL2 | Metric::InnerProduct if p.normalization != "none" => Err(Error::InvalidProfile),
        _ => Ok(())
    }
}
fn validate_vector(p: &Profile, v: &Vector) -> Result<(), Error> {
    match (p.metric,v) {
        (Metric::Hamming, Vector::Binary(bytes)) if bytes.len() == p.dimension / 8 => Ok(()),
        (Metric::Hamming, _) | (_, Vector::Binary(_)) => Err(Error::InvalidVector),
        (_, Vector::Float(xs)) => {
            if xs.len()!=p.dimension || xs.iter().any(|x| !x.is_finite()) { return Err(Error::InvalidVector); }
            if p.metric==Metric::UnitCosine {
                let norm: f64=xs.iter().map(|x| (*x as f64)*(*x as f64)).sum();
                if !norm.is_finite() || (norm-1.0).abs()>UNIT_NORM_TOLERANCE { return Err(Error::InvalidVector); }
            }
            Ok(())
        }
    }
}
fn field(h: &mut Sha256, bytes: &[u8]) { h.update((bytes.len() as u64).to_be_bytes()); h.update(bytes); }
impl FlatIndex {
    pub(crate) fn build(profile: Profile, mut rows: Vec<Row>) -> Result<Self, Error> {
        validate_profile(&profile)?;
        if rows.len()>MAX_ROWS || rows.len().checked_mul(profile.dimension).is_none_or(|n| n>MAX_ELEMENTS) {return Err(Error::Limit)}
        rows.sort_by(|a,b|a.id.cmp(&b.id));
        for (i,row) in rows.iter().enumerate() {
            if !valid_text(&row.id)||!valid_text(&row.source_revision) {return Err(Error::InvalidId)}
            if i>0 && rows[i-1].id==row.id {return Err(Error::DuplicateId)}
            validate_vector(&profile,&row.vector)?;
        }
        let mut h=Sha256::new(); field(&mut h,b"openclank-flat-v1");
        for s in [&profile.owner,&profile.workspace,&profile.project,&profile.generation_id,&profile.source_watermark,&profile.eligibility_epoch,&profile.model,&profile.model_version,&profile.normalization] {field(&mut h,s.as_bytes());}
        field(&mut h,&(profile.dimension as u64).to_be_bytes());
        field(&mut h,&[match profile.metric {Metric::SquaredL2=>0,Metric::InnerProduct=>1,Metric::UnitCosine=>2,Metric::Hamming=>3}]);
        field(&mut h,&(rows.len() as u64).to_be_bytes());
        for row in &rows {field(&mut h,row.id.as_bytes());field(&mut h,row.source_revision.as_bytes());match &row.vector {
            Vector::Float(xs)=>{let mut bytes=Vec::with_capacity(xs.len()*4);for x in xs {bytes.extend_from_slice(&x.to_bits().to_be_bytes());}field(&mut h,&bytes);},
            Vector::Binary(bytes)=>field(&mut h,bytes),
        }}
        Ok(Self{profile,rows,checksum:h.finalize().into()})
    }
    pub(crate) fn checksum(&self)->[u8;32] {self.checksum}
    /// `eligible` and the profile must be freshly admitted by the generation owner.
    /// A range result is truncated explicitly when more than `limit` matches exist.
    pub(crate) fn search(&self, profile:&Profile, eligible:&BTreeSet<String>, selector:Option<&BTreeSet<String>>, query:&Vector, limit:usize, threshold:Option<f64>) -> Result<Results,Error> {
        if profile!=&self.profile {return Err(Error::ScopeMismatch)}
        if limit==0 || limit>MAX_RESULTS {return Err(Error::Limit)}
        if threshold.is_some_and(|x| !x.is_finite()) {return Err(Error::InvalidThreshold)}
        validate_vector(profile,query)?;
        let mut hits=Vec::new();
        for row in &self.rows {
            if !eligible.contains(&row.id) || selector.is_some_and(|s| !s.contains(&row.id)) {continue}
            let score=match (&row.vector,query) {
                (Vector::Binary(a),Vector::Binary(b))=>a.iter().zip(b).map(|(x,y)|(x^y).count_ones() as f64).sum(),
                (Vector::Float(a),Vector::Float(b))=>a.iter().zip(b).map(|(x,y)|{let (x,y)=(*x as f64,*y as f64);if profile.metric==Metric::SquaredL2 {(x-y)*(x-y)} else {x*y}}).sum(),
                _=>return Err(Error::InvalidVector)
            };
            if !f64::is_finite(score) {return Err(Error::ScoreOverflow)}
            let lower=matches!(profile.metric,Metric::SquaredL2|Metric::Hamming);
            if threshold.is_some_and(|t| if lower {score>t} else {score<t}) {continue}
            hits.push(Hit{id:row.id.clone(),score});
        }
        let lower=matches!(profile.metric,Metric::SquaredL2|Metric::Hamming);
        hits.sort_by(|a,b|{let c=a.score.total_cmp(&b.score);(if lower {c} else {c.reverse()}).then_with(||a.id.cmp(&b.id))});
        let truncated=hits.len()>limit; hits.truncate(limit); Ok(Results{hits,truncated})
    }
    pub(crate) fn batch(&self,profile:&Profile,eligible:&BTreeSet<String>,selector:Option<&BTreeSet<String>>,queries:&[Vector],limit:usize,threshold:Option<f64>)->Result<Vec<Results>,Error> {
        if queries.is_empty()||queries.len()>MAX_BATCH {return Err(Error::Limit)}
        queries.iter().map(|q|self.search(profile,eligible,selector,q,limit,threshold)).collect()
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn profile(metric:Metric,dimension:usize)->Profile {Profile{owner:"owner".into(),workspace:"workspace".into(),project:"project".into(),generation_id:"gen".into(),source_watermark:"watermark".into(),eligibility_epoch:"epoch".into(),model:"model".into(),model_version:"v1".into(),dimension,metric,normalization:if metric==Metric::UnitCosine {"unit_l2"} else {"none"}.into()}}
    fn row(id:&str,v:Vector)->Row {Row{id:id.into(),source_revision:"r1".into(),vector:v}}
    #[test]
    fn exact_flat_scope_metric_and_bounds() {
        let p=profile(Metric::SquaredL2,2);let v=|x,y|Vector::Float(vec![x,y]);
        let rows=vec![row("hidden",v(0.,0.)),row("b",v(1.,0.)),row("a",v(-1.,0.))];
        let index=FlatIndex::build(p.clone(),rows.clone()).unwrap();let mut reversed=rows;reversed.reverse();assert_eq!(index.checksum(),FlatIndex::build(p.clone(),reversed).unwrap().checksum());
        let eligible=BTreeSet::from(["a".into(),"b".into()]);let result=index.search(&p,&eligible,None,&v(0.,0.),1,None).unwrap();assert_eq!(result.hits[0].id,"a");assert!(result.truncated);
        let selector=BTreeSet::from(["b".into(),"hidden".into()]);assert_eq!(index.batch(&p,&eligible,Some(&selector),&[v(0.,0.),v(0.,0.)],3,Some(1.)).unwrap()[0].hits[0].id,"b");
        assert_eq!(index.search(&p,&eligible,None,&v(f32::NAN,0.),1,None),Err(Error::InvalidVector));assert_eq!(index.search(&p,&eligible,None,&Vector::Float(vec![0.]),1,None),Err(Error::InvalidVector));
        let mut stale=p.clone();stale.eligibility_epoch="new".into();assert_eq!(index.search(&stale,&eligible,None,&v(0.,0.),1,None),Err(Error::ScopeMismatch));
        let c=profile(Metric::UnitCosine,2);assert!(FlatIndex::build(c.clone(),vec![row("x",v(0.,0.))]).is_err());let ci=FlatIndex::build(c.clone(),vec![row("a",v(1.,0.))]).unwrap();assert_eq!(ci.search(&c,&eligible,None,&v(1.,0.),1,None).unwrap().hits[0].score,1.);
        let ip=profile(Metric::InnerProduct,2);let ii=FlatIndex::build(ip.clone(),vec![row("a",v(1.,0.))]).unwrap();assert_eq!(ii.search(&ip,&eligible,None,&v(2.,0.),1,None).unwrap().hits[0].score,2.);
        let b=profile(Metric::Hamming,8);let bi=FlatIndex::build(b.clone(),vec![row("a",Vector::Binary(vec![3]))]).unwrap();assert_eq!(bi.search(&b,&eligible,None,&Vector::Binary(vec![0]),1,None).unwrap().hits[0].score,2.);
    }
}
