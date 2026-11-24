//! Immutable, bounded experimental L2 and unit-cosine profiles. No publication or authority.
use super::*;
use std::collections::{BTreeSet, VecDeque};

const MAX_PROFILE_ROWS: usize = 4096;
const MAX_PROFILE_DIM: usize = 1024;
const MAX_BUILD_SCORES: usize = 2_000_000;
const MAX_LEVEL: usize = 8;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Family { IvfMinibatch { lists: usize, iterations: usize }, IvfFlat { lists: usize, iterations: usize }, Hnsw { m: usize, ef_construction: usize } }
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct IndexSpec { pub version: u32, pub profile: Profile, pub flat_checksum: [u8;32], pub training_checksum: [u8;32], pub seed: u64, pub family: Family }
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Descriptor { pub spec: IndexSpec, pub trained_checksum: [u8;32] }
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Backend { IvfFlat, Hnsw, ExactFallback }
#[derive(Debug)]
pub(crate) struct SearchResult { pub results: Results, pub backend: Backend, pub visited: usize, pub budget_exhausted: bool, pub approximate: bool }
#[derive(Clone)]
struct Node { levels: Vec<Vec<usize>> }
enum Built { Ivf { centroids:Vec<Vec<f64>>, lists:Vec<Vec<usize>> }, Hnsw { nodes:Vec<Node>, entry:usize } }
pub(crate) struct Index { flat:FlatIndex, descriptor:Descriptor, built:Built }
fn dist(a:&[f32], b:&[f32]) -> f64 { a.iter().zip(b).map(|(x,y)|{let d=*x as f64-*y as f64;d*d}).sum() }
pub(super) fn centroid_dist(a:&[f32], b:&[f64]) -> f64 { a.iter().zip(b).map(|(x,y)|{let d=*x as f64-*y;d*d}).sum() }
pub(super) fn floats(row:&Row)->&[f32] {if let Vector::Float(ref xs)=row.vector {xs} else {unreachable!()}}
pub(super) fn step(seed:&mut u64)->u64 {*seed=seed.wrapping_add(0x9e3779b97f4a7c15);let mut z=*seed;z=(z^(z>>30)).wrapping_mul(0xbf58476d1ce4e5b9);z=(z^(z>>27)).wrapping_mul(0x94d049bb133111eb);z^(z>>31)}
fn ranked(query:&[f32], rows:&[Row], indexes:&mut [usize]) {indexes.sort_by(|a,b|dist(query,floats(&rows[*a])).total_cmp(&dist(query,floats(&rows[*b]))).then_with(||rows[*a].id.cmp(&rows[*b].id)));}
// Explore actual graph edges only. The queue is deterministically ordered by
// distance; both scored nodes and traversal are bounded by the caller's budget.
fn graph_search(query:&[f32], rows:&[Row], nodes:&[Node], entry:usize, level:usize, ef:usize, budget:&mut usize, visited_total:&mut usize)->Vec<usize> {
    let mut seen=BTreeSet::new(); let mut pending=VecDeque::from([entry]); let mut best=Vec::new();
    while !pending.is_empty() && *budget>0 {
        let mut candidates:Vec<usize>=pending.drain(..).collect();ranked(query,rows,&mut candidates);let current=candidates.remove(0);pending.extend(candidates);
        if !seen.insert(current) {continue} *budget-=1;*visited_total+=1; best.push(current);ranked(query,rows,&mut best);best.truncate(ef);
        if let Some(edges)=nodes[current].levels.get(level) {for &next in edges {if !seen.contains(&next) {pending.push_back(next)}}}
        let mut queued:Vec<usize>=pending.drain(..).collect();queued.sort_unstable();queued.dedup();ranked(query,rows,&mut queued);queued.truncate(ef);pending.extend(queued);
    }
    best
}
fn fingerprint(spec:&IndexSpec, built:&Built)->[u8;32] {
    let mut h=Sha256::new();field(&mut h,b"openclank-ann-l2-v1");field(&mut h,&spec.flat_checksum);field(&mut h,&spec.training_checksum);field(&mut h,&spec.seed.to_be_bytes());field(&mut h,&spec.version.to_be_bytes());
    match (&spec.family,built) {
        (Family::IvfFlat{lists,iterations} | Family::IvfMinibatch{lists,iterations},Built::Ivf{centroids,lists:members})=>{field(&mut h,if matches!(spec.family,Family::IvfMinibatch{..}) {b"ivf-minibatch-v1"} else {b"ivf-flat"});field(&mut h,&(*lists as u64).to_be_bytes());field(&mut h,&(*iterations as u64).to_be_bytes());for c in centroids {for x in c {field(&mut h,&x.to_bits().to_be_bytes());}}for group in members {field(&mut h,&(group.len() as u64).to_be_bytes());for i in group {field(&mut h,&(*i as u64).to_be_bytes());}}},
        (Family::Hnsw{m,ef_construction},Built::Hnsw{nodes,entry})=>{field(&mut h,b"hnsw");field(&mut h,&(*m as u64).to_be_bytes());field(&mut h,&(*ef_construction as u64).to_be_bytes());field(&mut h,&(*entry as u64).to_be_bytes());for n in nodes {field(&mut h,&(n.levels.len() as u64).to_be_bytes());for layer in &n.levels {field(&mut h,&(layer.len() as u64).to_be_bytes());for i in layer {field(&mut h,&(*i as u64).to_be_bytes());}}}},
        _=>unreachable!()
    }
    h.finalize().into()
}
impl Index {
    pub(crate) fn build(spec:IndexSpec, rows:Vec<Row>, training:Vec<Row>)->Result<Self,Error> {
        if spec.version!=1 || !matches!(spec.profile.metric,Metric::SquaredL2|Metric::UnitCosine) || spec.profile.dimension>MAX_PROFILE_DIM {return Err(Error::InvalidProfile)}
        let flat=FlatIndex::build(spec.profile.clone(),rows)?;let train=FlatIndex::build(spec.profile.clone(),training)?;
        if flat.checksum()!=spec.flat_checksum || train.checksum()!=spec.training_checksum {return Err(Error::ScopeMismatch)}
        if flat.rows.is_empty() || flat.rows.len()>MAX_PROFILE_ROWS || train.rows.is_empty() || train.rows.len()>MAX_PROFILE_ROWS {return Err(Error::Limit)}
        let mut seed=spec.seed;let mut work=0usize;
        let built=match spec.family {
            Family::IvfFlat{lists,iterations} | Family::IvfMinibatch{lists,iterations}=>{
                if lists==0 || lists>128 || lists>train.rows.len() || iterations==0 || iterations>32 {return Err(Error::Limit)}
                let cost=train.rows.len().checked_mul(lists).and_then(|v|v.checked_mul(iterations)).and_then(|v|v.checked_add(flat.rows.len()*lists)).ok_or(Error::Limit)?;
                if cost>MAX_BUILD_SCORES {return Err(Error::Limit)}
                let mut order:Vec<usize>=(0..train.rows.len()).collect();for i in (1..order.len()).rev(){let j=(step(&mut seed) as usize)%(i+1);order.swap(i,j)}
                let mut centroids:Vec<Vec<f64>>=order[..lists].iter().map(|i|floats(&train.rows[*i]).iter().map(|x|*x as f64).collect()).collect();
                if matches!(spec.family, Family::IvfMinibatch{..}) {
                    super::minibatch::train(&train.rows, &mut centroids, &mut order, iterations, &mut seed, spec.profile.metric)?;
                } else { for _ in 0..iterations {let mut sums=vec![vec![0.;spec.profile.dimension];lists];let mut counts=vec![0usize;lists];
                    for row in &train.rows {let x=floats(row);let j=(0..lists).min_by(|a,b|centroid_dist(x,&centroids[*a]).total_cmp(&centroid_dist(x,&centroids[*b])).then(a.cmp(b))).unwrap();counts[j]+=1;for (s,v) in sums[j].iter_mut().zip(x){*s+=*v as f64;}}
                    if counts.contains(&0) {return Err(Error::InvalidVector)}
                    for j in 0..lists {
                        for x in &mut sums[j] {*x/=counts[j] as f64;}
                        if spec.profile.metric==Metric::UnitCosine {
                            let norm=sums[j].iter().map(|x|x*x).sum::<f64>().sqrt();
                            if !norm.is_finite()||norm<=1e-12{return Err(Error::InvalidVector)}
                            for x in &mut sums[j] {*x/=norm;}
                        }
                    }centroids=sums;
                }}
                let mut members=vec![Vec::new();lists];for (i,row) in flat.rows.iter().enumerate(){let x=floats(row);let j=(0..lists).min_by(|a,b|centroid_dist(x,&centroids[*a]).total_cmp(&centroid_dist(x,&centroids[*b])).then(a.cmp(b))).unwrap();members[j].push(i);}
                Built::Ivf{centroids,lists:members}
            },
            Family::Hnsw{m,ef_construction}=>{
                if spec.training_checksum!=spec.flat_checksum {return Err(Error::InvalidProfile)}
                if m<2 || m>32 || ef_construction<m || ef_construction>256 {return Err(Error::Limit)}
                let mut nodes:Vec<Node>=Vec::new();let mut entry=0usize;
                for i in 0..flat.rows.len() {let mut level=0;while level<MAX_LEVEL && step(&mut seed)%m as u64==0 {level+=1}nodes.push(Node{levels:vec![Vec::new();level+1]});if i==0 {continue}
                    let mut cursor=entry;let old_top=nodes[entry].levels.len()-1;let mut remaining=MAX_BUILD_SCORES.saturating_sub(work);
                    for l in ((level+1)..=old_top).rev() {let found=graph_search(floats(&flat.rows[i]),&flat.rows,&nodes,cursor,l,1,&mut remaining,&mut work);if let Some(&x)=found.first(){cursor=x}else{return Err(Error::Limit)}}
                    for l in (0..=level.min(old_top)).rev() {let found=graph_search(floats(&flat.rows[i]),&flat.rows,&nodes,cursor,l,ef_construction,&mut remaining,&mut work);if found.is_empty(){return Err(Error::Limit)}cursor=found[0];
                        for &other in found.iter().take(m) {nodes[i].levels[l].push(other);nodes[other].levels[l].push(i);let mut edges=std::mem::take(&mut nodes[other].levels[l]);ranked(floats(&flat.rows[other]),&flat.rows,&mut edges);edges.truncate(m);nodes[other].levels[l]=edges;}
                    }
                    if level>old_top {entry=i}
                }
                Built::Hnsw{nodes,entry}
            }
        };
        let trained_checksum=fingerprint(&spec,&built);Ok(Self{flat,descriptor:Descriptor{spec,trained_checksum},built})
    }
    pub(crate) fn descriptor(&self)->&Descriptor {&self.descriptor}
    pub(crate) fn search(&self, profile:&Profile,eligible:&BTreeSet<String>,selector:Option<&BTreeSet<String>>,query:&Vector,k:usize,effort:usize,visit_budget:usize)->Result<SearchResult,Error> {
        if profile!=&self.descriptor.spec.profile {return Err(Error::ScopeMismatch)}
        validate_vector(profile,query)?;if k==0||k>MAX_RESULTS||effort==0||visit_budget==0||visit_budget>MAX_PROFILE_ROWS {return Err(Error::Limit)}
        let all=eligible.len()==self.flat.rows.len() && self.flat.rows.iter().all(|r|eligible.contains(&r.id));
        if !all || selector.is_some() {return Ok(SearchResult{results:self.flat.search(profile,eligible,selector,query,k,None)?,backend:Backend::ExactFallback,visited:0,budget_exhausted:false,approximate:false})}
        let Vector::Float(ref q)=query else{return Err(Error::InvalidVector)};let mut remaining=visit_budget;let mut visited=0usize;let mut candidates=Vec::new();
        let backend=match &self.built {
            Built::Ivf{centroids,lists}=>{if effort>centroids.len(){return Err(Error::Limit)}let mut order:Vec<usize>=(0..centroids.len()).collect();order.sort_by(|a,b|centroid_dist(q,&centroids[*a]).total_cmp(&centroid_dist(q,&centroids[*b])).then(a.cmp(b)));
                'outer:for group in order.into_iter().take(effort){for &i in &lists[group] {if remaining==0 {break 'outer}remaining-=1;visited+=1;candidates.push(i)}}Backend::IvfFlat},
            Built::Hnsw{nodes,entry}=>{if effort>256{return Err(Error::Limit)}let mut cursor=*entry;let top=nodes[cursor].levels.len()-1;
                for l in (1..=top).rev(){let found=graph_search(q,&self.flat.rows,nodes,cursor,l,1,&mut remaining,&mut visited);if let Some(&i)=found.first(){cursor=i}else{break}}
                if remaining>0 {candidates=graph_search(q,&self.flat.rows,nodes,cursor,0,effort,&mut remaining,&mut visited)}Backend::Hnsw}
        };
        let ids:BTreeSet<String>=candidates.iter().map(|i|self.flat.rows[*i].id.clone()).collect();let results=self.flat.search(profile,eligible,Some(&ids),query,k,None)?;
        Ok(SearchResult{results,backend,visited,budget_exhausted:remaining==0,approximate:true})
    }
}

/// Use the exact owner-supplied generation when a profile is unavailable; never
/// treat an incompatible or stale profile as a reason to weaken scope checks.
pub(crate) fn search_or_exact(index:Option<&Index>, flat:&FlatIndex,profile:&Profile,eligible:&BTreeSet<String>,selector:Option<&BTreeSet<String>>,query:&Vector,k:usize,effort:usize,visit_budget:usize)->Result<SearchResult,Error> {
    if let Some(index)=index {
        if index.flat.checksum()!=flat.checksum() {return Err(Error::ScopeMismatch)}
        index.search(profile,eligible,selector,query,k,effort,visit_budget)
    } else {
        Ok(SearchResult{results:flat.search(profile,eligible,selector,query,k,None)?,backend:Backend::ExactFallback,visited:0,budget_exhausted:false,approximate:false})
    }
}
const MAX_OCVP_BYTES:usize=16*1024*1024;
pub(super) struct ProfileWriter { bytes:Vec<u8> }
impl ProfileWriter {
    pub(super) fn new(tag:u8,descriptor:[u8;32])->Result<Self,Error>{
        let mut s=Self{bytes:Vec::new()};s.raw(b"OCVP")?;s.raw(&1u16.to_le_bytes())?;s.raw(&[tag,0])?;s.raw(&descriptor)?;Ok(s)
    }
    pub(super) fn raw(&mut self,value:&[u8])->Result<(),Error>{if self.bytes.len().checked_add(value.len()).is_none_or(|n|n>MAX_OCVP_BYTES){return Err(Error::Limit)}self.bytes.extend_from_slice(value);Ok(())}
    pub(super) fn byte(&mut self,value:u8)->Result<(),Error>{self.raw(&[value])}
    pub(super) fn u32(&mut self,value:usize)->Result<(),Error>{self.raw(&u32::try_from(value).map_err(|_|Error::Limit)?.to_le_bytes())}
    pub(super) fn f64(&mut self,value:f64)->Result<(),Error>{if !value.is_finite(){return Err(Error::InvalidVector)}self.raw(&value.to_bits().to_le_bytes())}
    pub(super) fn finish(self)->Vec<u8>{self.bytes}
}
pub(super) struct ProfileReader<'a>{bytes:&'a[u8],offset:usize}
impl<'a> ProfileReader<'a>{
    pub(super) fn new(bytes:&'a[u8],tag:u8,descriptor:[u8;32],expected:[u8;32])->Result<Self,Error>{
        if bytes.len()>MAX_OCVP_BYTES || <[u8;32]>::from(Sha256::digest(bytes))!=expected{return Err(Error::ScopeMismatch)}
        let mut s=Self{bytes,offset:0};if s.take(4)?!=b"OCVP" || s.take(2)?!=1u16.to_le_bytes() || s.take(2)?!=[tag,0] || s.take(32)?!=descriptor{return Err(Error::InvalidProfile)}Ok(s)
    }
    pub(super) fn take(&mut self,count:usize)->Result<&'a[u8],Error>{let end=self.offset.checked_add(count).ok_or(Error::Limit)?;let out=self.bytes.get(self.offset..end).ok_or(Error::InvalidProfile)?;self.offset=end;Ok(out)}
    pub(super) fn byte(&mut self)->Result<u8,Error>{Ok(self.take(1)?[0])}
    pub(super) fn u32(&mut self)->Result<usize,Error>{let b=self.take(4)?;Ok(u32::from_le_bytes([b[0],b[1],b[2],b[3]]) as usize)}
    pub(super) fn f64(&mut self)->Result<f64,Error>{let b=self.take(8)?;let v=f64::from_bits(u64::from_le_bytes(b.try_into().map_err(|_|Error::InvalidProfile)?));if !v.is_finite(){return Err(Error::InvalidVector)}Ok(v)}
    pub(super) fn finish(self)->Result<(),Error>{if self.offset!=self.bytes.len(){return Err(Error::InvalidProfile)}Ok(())}
}
impl Index {
    pub(crate) fn to_ocvp(&self,descriptor:[u8;32])->Result<Vec<u8>,Error>{
        let tag=match self.descriptor.spec.family {Family::IvfFlat{..}=>1,Family::IvfMinibatch{..}=>5,Family::Hnsw{..}=>2};let mut out=ProfileWriter::new(tag,descriptor)?;
        match &self.built {
            Built::Ivf{centroids,lists}=>{out.u32(self.flat.profile.dimension)?;out.u32(centroids.len())?;for centroid in centroids {for value in centroid {out.f64(*value)?;}}for list in lists {out.u32(list.len())?;for index in list {out.u32(*index)?;}}},
            Built::Hnsw{nodes,entry}=>{out.u32(nodes.len())?;out.u32(*entry)?;for node in nodes {out.u32(node.levels.len())?;for level in &node.levels {out.u32(level.len())?;for index in level {out.u32(*index)?;}}}},
        }Ok(out.finish())
    }
    pub(crate) fn from_ocvp(spec:IndexSpec,rows:Vec<Row>,descriptor:[u8;32],bytes:&[u8],expected:[u8;32])->Result<Self,Error>{
        if spec.version!=1 || !matches!(spec.profile.metric,Metric::SquaredL2|Metric::UnitCosine) || spec.profile.dimension>MAX_PROFILE_DIM || spec.training_checksum!=spec.flat_checksum {return Err(Error::InvalidProfile)}
        let flat=FlatIndex::build(spec.profile.clone(),rows)?;if flat.checksum()!=spec.flat_checksum{return Err(Error::ScopeMismatch)}
        if flat.rows.is_empty()||flat.rows.len()>MAX_PROFILE_ROWS{return Err(Error::Limit)}
        let tag=match spec.family {Family::IvfFlat{..}=>1,Family::IvfMinibatch{..}=>5,Family::Hnsw{..}=>2};let mut input=ProfileReader::new(bytes,tag,descriptor,expected)?;
        let built=match spec.family {
            Family::IvfFlat{lists,iterations} | Family::IvfMinibatch{lists,iterations}=>{
                if lists==0||lists>128||lists>flat.rows.len()||iterations==0||iterations>32||input.u32()?!=flat.profile.dimension||input.u32()?!=lists{return Err(Error::InvalidProfile)}
                let mut centroids=Vec::with_capacity(lists);for _ in 0..lists {let mut centroid=Vec::with_capacity(flat.profile.dimension);for _ in 0..flat.profile.dimension {centroid.push(input.f64()?);}
                    if flat.profile.metric==Metric::UnitCosine {let norm=centroid.iter().map(|x|x*x).sum::<f64>().sqrt();if (norm-1.).abs()>1e-6{return Err(Error::InvalidVector)}}centroids.push(centroid);}
                let mut seen=vec![false;flat.rows.len()];let mut members=Vec::with_capacity(lists);
                for _ in 0..lists {let n=input.u32()?;if n>flat.rows.len(){return Err(Error::Limit)}let mut list=Vec::with_capacity(n);for _ in 0..n {let i=input.u32()?;if i>=seen.len()||seen[i]{return Err(Error::InvalidProfile)}seen[i]=true;list.push(i);}members.push(list);}
                if seen.iter().any(|x|!*x){return Err(Error::InvalidProfile)}Built::Ivf{centroids,lists:members}
            },
            Family::Hnsw{m,ef_construction}=>{
                if m<2||m>32||ef_construction<m||ef_construction>256||input.u32()?!=flat.rows.len(){return Err(Error::InvalidProfile)}
                let entry=input.u32()?;if entry>=flat.rows.len(){return Err(Error::InvalidProfile)}let mut nodes=Vec::with_capacity(flat.rows.len());
                for row in 0..flat.rows.len(){let levels=input.u32()?;if levels==0||levels>MAX_LEVEL+1{return Err(Error::InvalidProfile)}let mut node=Node{levels:Vec::with_capacity(levels)};
                    for _ in 0..levels {let n=input.u32()?;if n>m{return Err(Error::InvalidProfile)}let mut edges=Vec::with_capacity(n);let mut seen=BTreeSet::new();for _ in 0..n {let i=input.u32()?;if i>=flat.rows.len()||i==row||!seen.insert(i){return Err(Error::InvalidProfile)}edges.push(i);}node.levels.push(edges);}nodes.push(node);}
                for node in &nodes {for (level,edges) in node.levels.iter().enumerate(){if edges.iter().any(|i|nodes[*i].levels.len()<=level){return Err(Error::InvalidProfile)}}}
                if nodes.iter().any(|n|n.levels.len()>nodes[entry].levels.len()){return Err(Error::InvalidProfile)}Built::Hnsw{nodes,entry}
            },
        };
        input.finish()?;let trained_checksum=fingerprint(&spec,&built);Ok(Self{flat,descriptor:Descriptor{spec,trained_checksum},built})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn profiles_against_scoped_exact_and_budget() {
        let p=Profile{owner:"owner".into(),workspace:"workspace".into(),project:"".into(),generation_id:"gen".into(),source_watermark:"water".into(),eligibility_epoch:"epoch".into(),model:"model".into(),model_version:"v1".into(),dimension:2,metric:Metric::SquaredL2,normalization:"none".into()};
        let rows:Vec<Row>=(0..16).map(|i|Row{id:format!("id{i:02}"),source_revision:"r1".into(),vector:Vector::Float(vec![if i<8 {i as f32*0.1} else {10.0+(i-8) as f32*0.1},0.])}).collect();
        let checksum=FlatIndex::build(p.clone(),rows.clone()).unwrap().checksum();let eligible:BTreeSet<String>=rows.iter().map(|r|r.id.clone()).collect();let q=Vector::Float(vec![0.03,0.]);
        let exact=FlatIndex::build(p.clone(),rows.clone()).unwrap();let oracle=exact.search(&p,&eligible,None,&q,1,None).unwrap();
        let unavailable=search_or_exact(None,&exact,&p,&eligible,None,&q,1,1,1).unwrap();assert_eq!(unavailable.backend,Backend::ExactFallback);assert_eq!(unavailable.results,oracle);
        for family in [Family::IvfFlat{lists:2,iterations:4},Family::Hnsw{m:4,ef_construction:16}] {
            let spec=IndexSpec{version:1,profile:p.clone(),flat_checksum:checksum,training_checksum:checksum,seed:19,family};
            let index=Index::build(spec.clone(),rows.clone(),rows.clone()).unwrap();let repeat=Index::build(spec,rows.clone(),rows.clone()).unwrap();assert_eq!(index.descriptor(),repeat.descriptor());
            let effort=if family_is_ivf(&index) {1} else {16};let found=index.search(&p,&eligible,None,&q,1,effort,16).unwrap();assert!(found.approximate);assert_eq!(found.results.hits[0].id,oracle.hits[0].id);assert!(found.visited<=16);
            let limited=index.search(&p,&eligible,None,&q,1,effort,1).unwrap();assert!(limited.budget_exhausted && limited.visited==1);
            let subset=BTreeSet::from(["id15".into()]);let scoped=index.search(&p,&subset,None,&q,1,effort,1).unwrap();assert_eq!(scoped.backend,Backend::ExactFallback);assert_eq!(scoped.results.hits[0].id,"id15");assert!(!scoped.approximate);
            let selector=BTreeSet::from(["id14".into()]);assert_eq!(index.search(&p,&eligible,Some(&selector),&q,1,effort,1).unwrap().results.hits[0].id,"id14");
        }
    }
    fn family_is_ivf(i:&Index)->bool {matches!(i.descriptor.spec.family,Family::IvfFlat{..})}
    #[test]
    fn unit_cosine_profiles_keep_metric_and_scoped_fallback() {
        let p=Profile{owner:"owner".into(),workspace:"workspace".into(),project:"".into(),generation_id:"gen".into(),source_watermark:"water".into(),eligibility_epoch:"epoch".into(),model:"model".into(),model_version:"v1".into(),dimension:2,metric:Metric::UnitCosine,normalization:"unit_l2".into()};
        let rows:Vec<Row>=(0..16).map(|i|{let angle=if i<8 {i as f32*0.04} else {std::f32::consts::PI+(i-8) as f32*0.04};Row{id:format!("id{i:02}"),source_revision:"r1".into(),vector:Vector::Float(vec![angle.cos(),angle.sin()])}}).collect();
        let exact=FlatIndex::build(p.clone(),rows.clone()).unwrap();let checksum=exact.checksum();let eligible:BTreeSet<_>=rows.iter().map(|r|r.id.clone()).collect();let query=rows[0].vector.clone();let oracle=exact.search(&p,&eligible,None,&query,1,None).unwrap();
        for family in [Family::IvfFlat{lists:2,iterations:4},Family::Hnsw{m:4,ef_construction:16}] {
            let spec=IndexSpec{version:1,profile:p.clone(),flat_checksum:checksum,training_checksum:checksum,seed:19,family};
            let index=Index::build(spec.clone(),rows.clone(),rows.clone()).unwrap();let repeat=Index::build(spec,rows.clone(),rows.clone()).unwrap();assert_eq!(index.descriptor(),repeat.descriptor());
            let found=index.search(&p,&eligible,None,&query,1,if family_is_ivf(&index){2}else{16},16).unwrap();assert!(found.approximate);assert_eq!(found.results.hits[0].id,oracle.hits[0].id);assert!((found.results.hits[0].score-1.).abs()<1e-5);
            let subset=BTreeSet::from(["id15".into()]);let safe=index.search(&p,&subset,None,&query,1,1,1).unwrap();assert_eq!(safe.backend,Backend::ExactFallback);assert_eq!(safe.results.hits[0].id,"id15");
            let mut wrong=p.clone();wrong.metric=Metric::SquaredL2;wrong.normalization="none".into();assert_eq!(index.search(&wrong,&eligible,None,&query,1,1,1).unwrap_err(),Error::ScopeMismatch);
        }
    }

    #[test]
    fn ocvp_unit_cosine_roundtrip(){
        let p=Profile{owner:"owner".into(),workspace:"workspace".into(),project:"".into(),generation_id:"gen".into(),source_watermark:"water".into(),eligibility_epoch:"epoch".into(),model:"model".into(),model_version:"v1".into(),dimension:2,metric:Metric::UnitCosine,normalization:"unit_l2".into()};
        let rows:Vec<Row>=(0..16).map(|i|{let angle=if i<8 {i as f32*0.04} else {std::f32::consts::PI+(i-8) as f32*0.04};Row{id:format!("id{i:02}"),source_revision:"r1".into(),vector:Vector::Float(vec![angle.cos(),angle.sin()])}}).collect();
        let checksum=FlatIndex::build(p.clone(),rows.clone()).unwrap().checksum();let eligible:BTreeSet<_>=rows.iter().map(|r|r.id.clone()).collect();let q=rows[0].vector.clone();
        for family in [Family::IvfFlat{lists:2,iterations:4},Family::Hnsw{m:4,ef_construction:16}] {
            let spec=IndexSpec{version:1,profile:p.clone(),flat_checksum:checksum,training_checksum:checksum,seed:19,family};let index=Index::build(spec.clone(),rows.clone(),rows.clone()).unwrap();
            let descriptor=[7u8;32];let bytes=index.to_ocvp(descriptor).unwrap();let digest:[u8;32]=Sha256::digest(&bytes).into();let restored=Index::from_ocvp(spec.clone(),rows.clone(),descriptor,&bytes,digest).unwrap();
            assert_eq!(index.descriptor(),restored.descriptor());let effort=if family_is_ivf(&index){2}else{16};assert_eq!(index.search(&p,&eligible,None,&q,1,effort,16).unwrap().results,restored.search(&p,&eligible,None,&q,1,effort,16).unwrap().results);
            let mut bad=bytes.clone();bad.push(0);let changed:[u8;32]=Sha256::digest(&bad).into();assert!(Index::from_ocvp(spec.clone(),rows.clone(),descriptor,&bad,changed).is_err());assert!(Index::from_ocvp(spec,rows.clone(),[8;32],&bytes,digest).is_err());
        }
    }

}
