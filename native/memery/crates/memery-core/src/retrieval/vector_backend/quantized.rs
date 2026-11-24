//! Bounded, explicit lossy CPU representations; never a source of authority.
use super::*;
const MAX_ROWS_Q:usize=4096;
const MAX_DIM_Q:usize=1024;
const MAX_CANDIDATES:usize=1024;
const MAX_BUILD_WORK:usize=2_000_000;
#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub(crate) enum TransformSpec {Identity, CenteredSignedPermutation{seed:u64}}
#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub(crate) enum QuantizerSpec {Scalar8, Product{parts:usize,centroids:usize,iterations:usize,seed:u64}}
#[derive(Clone,Debug,PartialEq,Eq)]
pub(crate) struct Spec {pub version:u32,pub profile:Profile,pub flat_checksum:[u8;32],pub training_checksum:[u8;32],pub transform:TransformSpec,pub quantizer:QuantizerSpec}
#[derive(Clone,Debug,PartialEq,Eq)]
pub(crate) struct Descriptor {pub spec:Spec,pub representation_checksum:[u8;32]}
#[derive(Debug)]
pub(crate) struct Search {pub results:Results,pub candidate_scores:Vec<Hit>,pub scored:usize,pub approximate:bool,pub exact_fallback:bool}
struct Transform {means:Vec<f64>,permutation:Vec<usize>,signs:Vec<f64>}
enum Codec {Scalar{min:Vec<f64>,step:Vec<f64>},Product{books:Vec<Vec<Vec<f64>>>}}
pub(crate) struct Index {flat:FlatIndex,descriptor:Descriptor,transform:Transform,codec:Codec,codes:Vec<Vec<u8>>}
fn next(seed:&mut u64)->u64 {*seed=seed.wrapping_add(0x9e3779b97f4a7c15);let mut x=*seed;x=(x^(x>>30)).wrapping_mul(0xbf58476d1ce4e5b9);x=(x^(x>>27)).wrapping_mul(0x94d049bb133111eb);x^(x>>31)}
fn floats(v:&Vector)->Result<&[f32],Error> {match v {Vector::Float(x)=>Ok(x),_=>Err(Error::InvalidVector)}}
fn distance(a:&[f64],b:&[f64])->Result<f64,Error>{let sum:f64=a.iter().zip(b).map(|(x,y)|{let d=x-y;d*d}).sum();if !sum.is_finite(){return Err(Error::ScoreOverflow)}Ok(sum)}
impl Transform {
    fn apply(&self,v:&[f32])->Result<Vec<f64>,Error>{let out:Vec<f64>=self.permutation.iter().enumerate().map(|(i,j)|((v[*j] as f64)-self.means[*j])*self.signs[i]).collect();if out.iter().any(|v|!v.is_finite()){return Err(Error::ScoreOverflow)}Ok(out)}
    fn inverse(&self,v:&[f64])->Result<Vec<f64>,Error>{
        if v.len()!=self.permutation.len(){return Err(Error::InvalidVector)}
        let mut out=vec![0.;v.len()];for (i,j) in self.permutation.iter().enumerate(){out[*j]=v[i]*self.signs[i]+self.means[*j];}
        if out.iter().any(|x|!x.is_finite()){return Err(Error::ScoreOverflow)}Ok(out)
    }
}
fn train_transform(spec:TransformSpec,rows:&[Row],dim:usize)->Result<Transform,Error>{
    let mut means=vec![0.;dim];let mut permutation:Vec<usize>=(0..dim).collect();let mut signs=vec![1.;dim];
    if let TransformSpec::CenteredSignedPermutation{mut seed}=spec {
        for row in rows {for (sum,v) in means.iter_mut().zip(floats(&row.vector)?){*sum+=*v as f64;}}
        for sum in &mut means {*sum/=rows.len() as f64;if !sum.is_finite(){return Err(Error::ScoreOverflow)}}
        for i in (1..dim).rev(){let j=(next(&mut seed) as usize)%(i+1);permutation.swap(i,j)}for sign in &mut signs {if next(&mut seed)&1==1 {*sign= -1.;}}
    }Ok(Transform{means,permutation,signs})
}
fn nearest(row:&[f64],book:&[Vec<f64>])->Result<usize,Error>{let mut best=None;for (i,c) in book.iter().enumerate(){let d=distance(row,c)?;if best.is_none_or(|(old,j)|d<old||(d==old&&i<j)){best=Some((d,i))}}best.map(|(_,i)|i).ok_or(Error::InvalidProfile)}
impl Codec {
    fn encode(&self,x:&[f64])->Result<Vec<u8>,Error>{match self {
        Codec::Scalar{min,step}=>Ok(x.iter().zip(min).zip(step).map(|((v,m),s)|if *s==0. {0}else{((v-m)/s).round().clamp(0.,255.) as u8}).collect()),
        Codec::Product{books}=>{let d=x.len()/books.len();books.iter().enumerate().map(|(i,b)|nearest(&x[i*d..(i+1)*d],b).map(|k|k as u8)).collect()}
    }}
    fn decode(&self,code:&[u8])->Result<Vec<f64>,Error>{match self {
        Codec::Scalar{min,step}=>{if code.len()!=min.len(){return Err(Error::InvalidVector)}Ok(code.iter().zip(min).zip(step).map(|((c,m),s)|m+*c as f64*s).collect())},
        Codec::Product{books}=>{if code.len()!=books.len(){return Err(Error::InvalidVector)}let mut out=Vec::new();for (c,b) in code.iter().zip(books){out.extend_from_slice(b.get(*c as usize).ok_or(Error::InvalidVector)?)}Ok(out)}
    }}
}
fn representation_checksum(spec:&Spec,transform:&Transform,codec:&Codec,codes:&[Vec<u8>])->[u8;32]{
    let mut h=Sha256::new();field(&mut h,b"openclank-lossy-l2-v1");field(&mut h,&spec.flat_checksum);field(&mut h,&spec.training_checksum);field(&mut h,&spec.version.to_be_bytes());
    match spec.transform {TransformSpec::Identity=>field(&mut h,b"identity"),TransformSpec::CenteredSignedPermutation{seed}=>{field(&mut h,b"centered-signed-permutation");field(&mut h,&seed.to_be_bytes())}}
    for x in &transform.means {field(&mut h,&x.to_bits().to_be_bytes())}for x in &transform.permutation {field(&mut h,&(*x as u64).to_be_bytes())}for x in &transform.signs {field(&mut h,&x.to_bits().to_be_bytes())}
    match (&spec.quantizer,codec) {
        (QuantizerSpec::Scalar8,Codec::Scalar{min,step})=>{field(&mut h,b"scalar8");for x in min.iter().chain(step){field(&mut h,&x.to_bits().to_be_bytes())}},
        (QuantizerSpec::Product{parts,centroids,iterations,seed},Codec::Product{books})=>{field(&mut h,b"product");for n in [*parts as u64,*centroids as u64,*iterations as u64,*seed] {field(&mut h,&n.to_be_bytes())}for book in books {for center in book {for x in center {field(&mut h,&x.to_bits().to_be_bytes())}}}},
        _=>unreachable!()
    }
    for code in codes {field(&mut h,code)}h.finalize().into()
}
impl Index {
    pub(crate) fn build(spec:Spec,rows:Vec<Row>,training:Vec<Row>)->Result<Self,Error>{
        if spec.version!=1||!matches!(spec.profile.metric,Metric::SquaredL2|Metric::UnitCosine)||spec.profile.dimension>MAX_DIM_Q{return Err(Error::InvalidProfile)}
        let flat=FlatIndex::build(spec.profile.clone(),rows)?;let train=FlatIndex::build(spec.profile.clone(),training)?;
        if flat.checksum()!=spec.flat_checksum||train.checksum()!=spec.training_checksum{return Err(Error::ScopeMismatch)}
        if flat.rows.is_empty()||flat.rows.len()>MAX_ROWS_Q||train.rows.is_empty()||train.rows.len()>MAX_ROWS_Q{return Err(Error::Limit)}
        let dim=spec.profile.dimension;let transform=train_transform(spec.transform,&train.rows,dim)?;let x:Vec<Vec<f64>>=train.rows.iter().map(|r|transform.apply(floats(&r.vector)?)).collect::<Result<_,_>>()?;
        let codec=match spec.quantizer {
            QuantizerSpec::Scalar8=>{let mut min=vec![f64::INFINITY;dim];let mut max=vec![f64::NEG_INFINITY;dim];for row in &x {for j in 0..dim {min[j]=min[j].min(row[j]);max[j]=max[j].max(row[j]);}}let step=min.iter().zip(&max).map(|(a,b)|(b-a)/255.).collect();Codec::Scalar{min,step}},
            QuantizerSpec::Product{parts,centroids,iterations,mut seed}=>{
                if parts==0||parts>dim||dim%parts!=0||centroids<2||centroids>256||centroids>x.len()||iterations==0||iterations>32{return Err(Error::Limit)}
                let train_work=x.len().checked_mul(parts).and_then(|n|n.checked_mul(centroids)).and_then(|n|n.checked_mul(iterations)).ok_or(Error::Limit)?;let encode_work=flat.rows.len().checked_mul(parts).and_then(|n|n.checked_mul(centroids)).ok_or(Error::Limit)?;let work=train_work.checked_add(encode_work).ok_or(Error::Limit)?;if work>MAX_BUILD_WORK{return Err(Error::Limit)}
                let d=dim/parts;let mut books=Vec::new();for part in 0..parts {let mut order:Vec<usize>=(0..x.len()).collect();for i in (1..order.len()).rev(){let j=(next(&mut seed) as usize)%(i+1);order.swap(i,j)}let mut book:Vec<Vec<f64>>=order[..centroids].iter().map(|i|x[*i][part*d..(part+1)*d].to_vec()).collect();
                    for _ in 0..iterations {let mut sums=vec![vec![0.;d];centroids];let mut counts=vec![0usize;centroids];for row in &x {let slice=&row[part*d..(part+1)*d];let k=nearest(slice,&book)?;counts[k]+=1;for (sum,v) in sums[k].iter_mut().zip(slice){*sum+=*v;}}if counts.contains(&0){return Err(Error::InvalidVector)}for k in 0..centroids {for n in &mut sums[k]{*n/=counts[k] as f64;if !n.is_finite(){return Err(Error::ScoreOverflow)}}}book=sums;}books.push(book);
                }Codec::Product{books}
            }
        };
        let codes:Vec<Vec<u8>>=flat.rows.iter().map(|r|codec.encode(&transform.apply(floats(&r.vector)?)?)).collect::<Result<_,_>>()?;
        if spec.profile.metric==Metric::UnitCosine {
            for code in &codes {let decoded=transform.inverse(&codec.decode(code)?)?;let norm=decoded.iter().map(|v|v*v).sum::<f64>().sqrt();
                if !norm.is_finite()||norm<=1e-12{return Err(Error::InvalidVector)}}
        }
        let representation_checksum=representation_checksum(&spec,&transform,&codec,&codes);Ok(Self{flat,descriptor:Descriptor{spec,representation_checksum},transform,codec,codes})
    }
    pub(crate) fn descriptor(&self)->&Descriptor {&self.descriptor}
    pub(crate) fn search(&self,profile:&Profile,eligible:&BTreeSet<String>,selector:Option<&BTreeSet<String>>,query:&Vector,k:usize,candidate_limit:usize)->Result<Search,Error>{
        if profile!=&self.descriptor.spec.profile{return Err(Error::ScopeMismatch)}validate_vector(profile,query)?;
        if k==0||candidate_limit<k||candidate_limit>MAX_CANDIDATES{return Err(Error::Limit)}
        let all=eligible.len()==self.flat.rows.len()&&self.flat.rows.iter().all(|r|eligible.contains(&r.id));
        if !all||selector.is_some(){return Ok(Search{results:self.flat.search(profile,eligible,selector,query,k,None)?,candidate_scores:vec![],scored:0,approximate:false,exact_fallback:true})}
        let q=self.transform.apply(floats(query)?)?;let mut candidates=Vec::with_capacity(self.codes.len());
        for (row,code) in self.flat.rows.iter().zip(&self.codes){
            if !eligible.contains(&row.id)||selector.is_some_and(|s|!s.contains(&row.id)){continue}
            let decoded=self.codec.decode(code)?;
            let score=if profile.metric==Metric::UnitCosine {
                let restored=self.transform.inverse(&decoded)?;let norm=restored.iter().map(|v|v*v).sum::<f64>().sqrt();
                if !norm.is_finite()||norm<=1e-12{return Err(Error::InvalidVector)}
                floats(query)?.iter().zip(&restored).map(|(a,b)|(*a as f64)*b).sum::<f64>()/norm
            } else {distance(&q,&decoded)?};
            if !score.is_finite(){return Err(Error::ScoreOverflow)}candidates.push(Hit{id:row.id.clone(),score});
        }
        let scored=candidates.len();candidates.sort_by(|a,b|if profile.metric==Metric::UnitCosine {b.score.total_cmp(&a.score).then_with(||a.id.cmp(&b.id))}else{a.score.total_cmp(&b.score).then_with(||a.id.cmp(&b.id))});candidates.truncate(candidate_limit);
        let ids:BTreeSet<String>=candidates.iter().map(|h|h.id.clone()).collect();let results=self.flat.search(profile,eligible,Some(&ids),query,k,None)?;
        Ok(Search{results,candidate_scores:candidates,scored,approximate:true,exact_fallback:false})
    }
}
pub(crate) fn search_or_exact(index:Option<&Index>,flat:&FlatIndex,profile:&Profile,eligible:&BTreeSet<String>,selector:Option<&BTreeSet<String>>,query:&Vector,k:usize,candidate_limit:usize)->Result<Search,Error>{
    if k==0||candidate_limit<k||candidate_limit>MAX_CANDIDATES{return Err(Error::Limit)}
    if let Some(index)=index {if index.flat.checksum()!=flat.checksum(){return Err(Error::ScopeMismatch)}index.search(profile,eligible,selector,query,k,candidate_limit)}
    else {Ok(Search{results:flat.search(profile,eligible,selector,query,k,None)?,candidate_scores:vec![],scored:0,approximate:false,exact_fallback:true})}
}

impl Index {
    pub(crate) fn to_ocvp(&self,descriptor:[u8;32])->Result<Vec<u8>,Error>{
        use super::profiles::ProfileWriter;
        let tag=match self.descriptor.spec.quantizer {QuantizerSpec::Scalar8=>3,QuantizerSpec::Product{..}=>4};let mut out=ProfileWriter::new(tag,descriptor)?;
        out.u32(self.flat.profile.dimension)?;out.byte(match self.descriptor.spec.transform {TransformSpec::Identity=>0,TransformSpec::CenteredSignedPermutation{..}=>1})?;
        for value in &self.transform.means {out.f64(*value)?;}for value in &self.transform.permutation {out.u32(*value)?;}for value in &self.transform.signs {out.byte(if *value==1. {1}else if *value== -1. {255}else{return Err(Error::InvalidProfile)})?;}
        match &self.codec {
            Codec::Scalar{min,step}=>{for value in min {out.f64(*value)?;}for value in step {out.f64(*value)?;}},
            Codec::Product{books}=>{out.u32(books.len())?;for book in books {out.u32(book.len())?;out.u32(book.first().ok_or(Error::InvalidProfile)?.len())?;for centroid in book {for value in centroid {out.f64(*value)?;}}}},
        }
        out.u32(self.codes.len())?;for code in &self.codes {out.u32(code.len())?;out.raw(code)?;}Ok(out.finish())
    }
    pub(crate) fn from_ocvp(spec:Spec,rows:Vec<Row>,descriptor:[u8;32],bytes:&[u8],expected:[u8;32])->Result<Self,Error>{
        use super::profiles::ProfileReader;
        if spec.version!=1||!matches!(spec.profile.metric,Metric::SquaredL2|Metric::UnitCosine)||spec.profile.dimension>MAX_DIM_Q||spec.training_checksum!=spec.flat_checksum{return Err(Error::InvalidProfile)}
        let flat=FlatIndex::build(spec.profile.clone(),rows)?;if flat.checksum()!=spec.flat_checksum{return Err(Error::ScopeMismatch)}
        if flat.rows.is_empty()||flat.rows.len()>MAX_ROWS_Q{return Err(Error::Limit)}
        let tag=match spec.quantizer {QuantizerSpec::Scalar8=>3,QuantizerSpec::Product{..}=>4};let mut input=ProfileReader::new(bytes,tag,descriptor,expected)?;let dim=flat.profile.dimension;
        if input.u32()?!=dim || input.byte()?!=match spec.transform {TransformSpec::Identity=>0,TransformSpec::CenteredSignedPermutation{..}=>1}{return Err(Error::InvalidProfile)}
        let mut means=Vec::with_capacity(dim);for _ in 0..dim {means.push(input.f64()?);}let mut permutation=Vec::with_capacity(dim);let mut seen=std::collections::BTreeSet::new();for _ in 0..dim {let i=input.u32()?;if i>=dim||!seen.insert(i){return Err(Error::InvalidProfile)}permutation.push(i);}
        let mut signs=Vec::with_capacity(dim);for _ in 0..dim {signs.push(match input.byte()? {1=>1.,255=> -1.,_=>return Err(Error::InvalidProfile)});}
        let transform=Transform{means,permutation,signs};let expected_transform=train_transform(spec.transform,&flat.rows,dim)?;
        if transform.means.iter().zip(&expected_transform.means).any(|(a,b)|a.to_bits()!=b.to_bits())||transform.permutation!=expected_transform.permutation||transform.signs!=expected_transform.signs{return Err(Error::ScopeMismatch)}
        let codec=match spec.quantizer {
            QuantizerSpec::Scalar8=>{let mut min=Vec::with_capacity(dim);let mut step=Vec::with_capacity(dim);for _ in 0..dim {min.push(input.f64()?);}for _ in 0..dim {let x=input.f64()?;if x<0. {return Err(Error::InvalidProfile)}step.push(x);}Codec::Scalar{min,step}},
            QuantizerSpec::Product{parts,centroids,iterations,..}=>{
                if parts==0||parts>dim||dim%parts!=0||centroids<2||centroids>256||centroids>flat.rows.len()||iterations==0||iterations>32||input.u32()?!=parts{return Err(Error::InvalidProfile)}
                let work=flat.rows.len().checked_mul(parts).and_then(|n|n.checked_mul(centroids)).and_then(|n|n.checked_mul(iterations+1)).ok_or(Error::Limit)?;if work>MAX_BUILD_WORK{return Err(Error::Limit)}
                let mut books=Vec::with_capacity(parts);for _ in 0..parts {if input.u32()?!=centroids||input.u32()?!=dim/parts{return Err(Error::InvalidProfile)}let mut book=Vec::with_capacity(centroids);for _ in 0..centroids {let mut center=Vec::with_capacity(dim/parts);for _ in 0..dim/parts {center.push(input.f64()?);}book.push(center);}books.push(book);}Codec::Product{books}
            },
        };
        if input.u32()?!=flat.rows.len(){return Err(Error::InvalidProfile)}let expected_len=match spec.quantizer {QuantizerSpec::Scalar8=>dim,QuantizerSpec::Product{parts,..}=>parts};let mut codes=Vec::with_capacity(flat.rows.len());
        for row in &flat.rows {let n=input.u32()?;if n!=expected_len{return Err(Error::InvalidProfile)}let code=input.take(n)?.to_vec();
            if codec.encode(&transform.apply(floats(&row.vector)?)?)?!=code{return Err(Error::ScopeMismatch)}
            if flat.profile.metric==Metric::UnitCosine {let decoded=transform.inverse(&codec.decode(&code)?)?;let norm=decoded.iter().map(|v|v*v).sum::<f64>().sqrt();if !norm.is_finite()||norm<=1e-12{return Err(Error::InvalidVector)}}codes.push(code);}
        input.finish()?;let representation_checksum=representation_checksum(&spec,&transform,&codec,&codes);
        Ok(Self{flat,descriptor:Descriptor{spec,representation_checksum},transform,codec,codes})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn scalar_pq_transform_and_scoped_rerank(){
        let profile=Profile{owner:"owner".into(),workspace:"workspace".into(),project:"".into(),generation_id:"generation".into(),source_watermark:"w".into(),eligibility_epoch:"e".into(),model:"model".into(),model_version:"1".into(),dimension:4,metric:Metric::SquaredL2,normalization:"none".into()};
        let rows:Vec<Row>=(0..12).map(|i|Row{id:format!("id{i:02}"),source_revision:"r1".into(),vector:Vector::Float(vec![i as f32,((i*7)%13) as f32,((i*5)%11) as f32,((i*3)%17) as f32])}).collect();
        let exact=FlatIndex::build(profile.clone(),rows.clone()).unwrap();let checksum=exact.checksum();let eligible:BTreeSet<String>=rows.iter().map(|r|r.id.clone()).collect();let query=Vector::Float(vec![3.,8.,4.,9.]);let oracle=exact.search(&profile,&eligible,None,&query,1,None).unwrap();let unavailable=search_or_exact(None,&exact,&profile,&eligible,None,&query,1,4).unwrap();assert!(unavailable.exact_fallback);assert_eq!(unavailable.results,oracle);assert!(search_or_exact(None,&exact,&profile,&eligible,None,&query,2,1).is_err());
        for quantizer in [QuantizerSpec::Scalar8,QuantizerSpec::Product{parts:2,centroids:2,iterations:4,seed:9}] {let spec=Spec{version:1,profile:profile.clone(),flat_checksum:checksum,training_checksum:checksum,transform:TransformSpec::CenteredSignedPermutation{seed:3},quantizer};let index=Index::build(spec.clone(),rows.clone(),rows.clone()).unwrap();let repeat=Index::build(spec,rows.clone(),rows.clone()).unwrap();assert_eq!(index.descriptor(),repeat.descriptor());assert!(!index.codes.is_empty());assert_eq!(index.codec.decode(&index.codes[0]).unwrap().len(),4);
            let found=index.search(&profile,&eligible,None,&query,1,4).unwrap();assert_eq!(found.candidate_scores.len(),4);assert!(found.approximate);assert_eq!(found.results.hits[0].id,oracle.hits[0].id);assert_eq!(found.scored,12);let subset=BTreeSet::from(["id11".into()]);let safe=index.search(&profile,&subset,None,&query,1,1).unwrap();assert!(safe.exact_fallback);assert_eq!(safe.results.hits[0].id,"id11");let mut wrong=profile.clone();wrong.eligibility_epoch="stale".into();assert_eq!(index.search(&wrong,&eligible,None,&query,1,1).unwrap_err(),Error::ScopeMismatch);assert!(index.search(&profile,&eligible,None,&query,2,1).is_err());
        }
    }
    #[test]
    fn unit_cosine_profiles_score_candidates_and_rerank_originals(){
        let p=Profile{owner:"owner".into(),workspace:"workspace".into(),project:"".into(),generation_id:"generation".into(),source_watermark:"w".into(),eligibility_epoch:"e".into(),model:"model".into(),model_version:"1".into(),dimension:4,metric:Metric::UnitCosine,normalization:"unit_l2".into()};
        let rows:Vec<Row>=(0..12).map(|i|{let mut v=vec![1.+i as f32,1.+((i*7)%13) as f32,1.+((i*5)%11) as f32,1.+((i*3)%17) as f32];let n=v.iter().map(|x|x*x).sum::<f32>().sqrt();for x in &mut v {*x/=n};Row{id:format!("id{i:02}"),source_revision:"r1".into(),vector:Vector::Float(v)}}).collect();
        let exact=FlatIndex::build(p.clone(),rows.clone()).unwrap();let checksum=exact.checksum();let eligible:BTreeSet<_>=rows.iter().map(|r|r.id.clone()).collect();let query=rows[3].vector.clone();let oracle=exact.search(&p,&eligible,None,&query,1,None).unwrap();
        for quantizer in [QuantizerSpec::Scalar8,QuantizerSpec::Product{parts:2,centroids:2,iterations:4,seed:9}] {
            let spec=Spec{version:1,profile:p.clone(),flat_checksum:checksum,training_checksum:checksum,transform:TransformSpec::CenteredSignedPermutation{seed:3},quantizer};
            let index=Index::build(spec.clone(),rows.clone(),rows.clone()).unwrap();let repeat=Index::build(spec,rows.clone(),rows.clone()).unwrap();assert_eq!(index.descriptor(),repeat.descriptor());
            let found=index.search(&p,&eligible,None,&query,1,12).unwrap();assert!(found.approximate);assert_eq!(found.results.hits[0].id,oracle.hits[0].id);assert!((found.results.hits[0].score-1.).abs()<1e-5);assert!(found.candidate_scores.windows(2).all(|w|w[0].score>=w[1].score));
            let subset=BTreeSet::from(["id11".into()]);let safe=index.search(&p,&subset,None,&query,1,1).unwrap();assert!(safe.exact_fallback);assert_eq!(safe.results.hits[0].id,"id11");
            let mut wrong=p.clone();wrong.metric=Metric::SquaredL2;wrong.normalization="none".into();assert_eq!(index.search(&wrong,&eligible,None,&query,1,1).unwrap_err(),Error::ScopeMismatch);
        }
    }

    #[test]
    fn ocvp_unit_cosine_roundtrip(){
        let p=Profile{owner:"owner".into(),workspace:"workspace".into(),project:"".into(),generation_id:"generation".into(),source_watermark:"w".into(),eligibility_epoch:"e".into(),model:"model".into(),model_version:"1".into(),dimension:4,metric:Metric::UnitCosine,normalization:"unit_l2".into()};
        let rows:Vec<Row>=(0..12).map(|i|{let mut v=vec![1.+i as f32,1.+((i*7)%13) as f32,1.+((i*5)%11) as f32,1.+((i*3)%17) as f32];let n=v.iter().map(|x|x*x).sum::<f32>().sqrt();for x in &mut v {*x/=n};Row{id:format!("id{i:02}"),source_revision:"r1".into(),vector:Vector::Float(v)}}).collect();
        let checksum=FlatIndex::build(p.clone(),rows.clone()).unwrap().checksum();let eligible:BTreeSet<_>=rows.iter().map(|r|r.id.clone()).collect();let q=rows[3].vector.clone();
        for quantizer in [QuantizerSpec::Scalar8,QuantizerSpec::Product{parts:2,centroids:2,iterations:4,seed:9}] {
            let spec=Spec{version:1,profile:p.clone(),flat_checksum:checksum,training_checksum:checksum,transform:TransformSpec::CenteredSignedPermutation{seed:3},quantizer};let index=Index::build(spec.clone(),rows.clone(),rows.clone()).unwrap();
            let descriptor=[7u8;32];let bytes=index.to_ocvp(descriptor).unwrap();let digest:[u8;32]=Sha256::digest(&bytes).into();let restored=Index::from_ocvp(spec.clone(),rows.clone(),descriptor,&bytes,digest).unwrap();
            assert_eq!(index.descriptor(),restored.descriptor());assert_eq!(index.search(&p,&eligible,None,&q,1,12).unwrap().results,restored.search(&p,&eligible,None,&q,1,12).unwrap().results);
            let mut bad=bytes.clone();bad.push(0);let changed:[u8;32]=Sha256::digest(&bad).into();assert!(Index::from_ocvp(spec.clone(),rows.clone(),descriptor,&bad,changed).is_err());assert!(Index::from_ocvp(spec,rows.clone(),[8;32],&bytes,digest).is_err());
        }
    }

}
