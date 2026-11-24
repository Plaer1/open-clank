//! Pure, private prerequisites; no device, stream, portal, or public audio profile.
use std::collections::VecDeque;

const MAX_BRACKET_NS: u64 = 2_000_000;
const MAX_RESIDUAL_NS: i128 = 5_000_000;
const MAX_CALIBRATION_AGE_NS: u64 = 1_000_000_000;
const MAX_DELAY_AGE_NS: u64 = 250_000_000; // Same finite window as the PCM buffer age.
const MAX_PCM_BYTES: usize = 48_000; // 250 ms of 48 kHz stereo i16
const MAX_PCM_BLOCKS: usize = 32;

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Error { Invalid, PreStart, ClockChanged, Stale, Drift, Overflow, Full }
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Calibration { offset:i128, uncertainty:u64, pw_sample:u64, base:u64, clock:u64, samples:u8 }
impl Calibration {
 pub(crate) fn sample(p1:u64, gst:u64, p2:u64, base:u64, clock:u64, previous:Option<Self>)->Result<Self,Error> {
  let width=p2.checked_sub(p1).ok_or(Error::Invalid)?;
  if width>MAX_BRACKET_NS || gst<base {return Err(Error::Invalid);}
  let midpoint=p1.checked_add(width/2).ok_or(Error::Overflow)?;
  let offset=i128::from(gst)-i128::from(midpoint);
  if let Some(old)=previous {
   if old.clock!=clock || old.base!=base {return Err(Error::ClockChanged);}
   if p1<=old.pw_sample {return Err(Error::Invalid);}
   if (offset-old.offset).abs()>MAX_RESIDUAL_NS {return Err(Error::Drift);}
  }
  Ok(Self{offset,uncertainty:(width+1)/2,pw_sample:midpoint,base,clock,samples:previous.map_or(1,|p|p.samples.saturating_add(1))})
 }
 pub(crate) fn uncertainty_ns(&self)->u64 {self.uncertainty}
 pub(crate) fn map(&self, cycle:u64, now:u64, delay_now:u64, delay_ticks:i64, num:u32, den:u32, frames:u32, rate:u32)->Result<(u64,u64),Error> {
  if self.samples<2 || num==0 || den==0 || rate==0 || frames==0 || cycle>now || delay_now>now {return Err(Error::Invalid);}
  if now.checked_sub(self.pw_sample).ok_or(Error::Invalid)?>MAX_CALIBRATION_AGE_NS || now-delay_now>MAX_DELAY_AGE_NS || cycle.abs_diff(delay_now)>MAX_DELAY_AGE_NS {return Err(Error::Stale);}
  let delay=i128::from(delay_ticks).checked_mul(i128::from(num)).and_then(|x|x.checked_mul(1_000_000_000)).ok_or(Error::Overflow)? / i128::from(den);
  let pts=i128::from(cycle).checked_sub(delay).and_then(|x|x.checked_add(self.offset)).and_then(|x|x.checked_sub(i128::from(self.base))).ok_or(Error::Overflow)?;
  if pts < -1_000_000_000 {return Err(Error::Invalid);}
  let duration=u64::from(frames).checked_mul(1_000_000_000).ok_or(Error::Overflow)?/u64::from(rate);
  if duration==0 {return Err(Error::Invalid);} if pts<0{return Err(Error::PreStart);} Ok((u64::try_from(pts).map_err(|_|Error::Overflow)?,duration))
 }
}

pub(crate) struct PcmQueue { q:VecDeque<Vec<u8>>, bytes:usize }
impl PcmQueue {
 pub(crate) fn new()->Self {Self{q:VecDeque::with_capacity(MAX_PCM_BLOCKS),bytes:0}}
 pub(crate) fn push(&mut self, data:Vec<u8>)->Result<(),Error> {
  if data.is_empty() || data.len()%2!=0 {return Err(Error::Invalid);}
  let next=self.bytes.checked_add(data.len()).ok_or(Error::Overflow)?;
  if next>MAX_PCM_BYTES || self.q.len()>=MAX_PCM_BLOCKS {return Err(Error::Full);}
  self.bytes=next;self.q.push_back(data);Ok(())
 }
 pub(crate) fn pop(&mut self)->Option<Vec<u8>> {let value=self.q.pop_front()?;self.bytes-=value.len();Some(value)}
}

#[cfg(test)]
mod tests {
 use super::*;
 #[test]
 fn synthetic_clock_and_queue() {
  let first=Calibration::sample(100,1_000_100,102,1_000_000,7,None).unwrap();
  assert_eq!(first.map(100,101,100,0,1,1,1,48_000),Err(Error::Invalid));
  let c=Calibration::sample(200,1_000_200,202,1_000_000,7,Some(first)).unwrap();
  assert_eq!(c.uncertainty_ns(),1);
  assert_eq!(c.map(300,400,390,1,1,1_000_000_000,48,48_000).unwrap(),(298,1_000_000));
  assert_eq!(c.map(300,400,390,-2,1,1_000_000_000,48,48_000).unwrap().0,301);
  assert_eq!(Calibration::sample(300,9_000_300,302,1_000_000,7,Some(c)),Err(Error::Drift));
  assert_eq!(Calibration::sample(300,1_000_300,302,1_000_000,8,Some(c)),Err(Error::ClockChanged));
  assert_eq!(c.map(300,MAX_CALIBRATION_AGE_NS+300,300,0,1,1,1,48_000),Err(Error::Stale));
  assert_eq!(c.map(1,400,390,1,1,1,1,48_000),Err(Error::PreStart));
  let mut q=PcmQueue::new();q.push(vec![0;MAX_PCM_BYTES]).unwrap();assert_eq!(q.push(vec![0;2]),Err(Error::Full));assert_eq!(q.pop().unwrap().len(),MAX_PCM_BYTES);assert!(q.pop().is_none());
 }
}
